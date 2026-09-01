"""Concurrency load harness for the chat backend — capacity + quality-capacity measurement.

Drives N closed-loop virtual users against ``GET /api/chat/stream`` (SSE) and measures,
per question:

* TTFT   — time until the first ``token`` event (what the user perceives as "the answer
           started"); the pipeline's earlier nodes hide behind the frontend thinking UI.
* total  — time until the ``done`` event (full answer).
* result — ok | rejected (HTTP 503 admission control) | error | timeout.

Modes
-----
ramp     step through increasing user counts (--steps "1,2,4,6,8"), Q questions per user
         per step, cool-down between steps. The report's two headline numbers fall out:
         capacity (last step with 0 errors) and quality capacity (last step meeting SLO).
overload single step far above MAX_CONCURRENT_STREAMS to verify graceful 503 behaviour.
soak     hold one user count for --duration minutes to surface leaks/latency drift.

Run from the repo root on the serving host (loopback is rate-limit exempt):

    .venv/bin/python eval_tools/_load_test.py ramp --steps 1,2,4,6,8 --questions 8
    .venv/bin/python eval_tools/_load_test.py overload --users 14 --questions 2
    .venv/bin/python eval_tools/_load_test.py soak --users 4 --duration 60

Results land in logs/load/ as JSON (one file per run, gitignored) and a summary table
prints to stdout. Questions are sampled from the qa100 golden set so retrieval cost is
realistic. This harness only reads the service — it never restarts anything.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import sys
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path

import httpx

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DATASET = REPO_ROOT / "eval_tools" / "datasets" / "qa_dataset.json"
DEFAULT_OUT_DIR = REPO_ROOT / "logs" / "load"

# Generous per-question ceiling: beyond this the user has certainly given up.
QUESTION_TIMEOUT_S = 180.0


@dataclass
class Sample:
    """One question's outcome under load."""

    step_users: int
    user_idx: int
    question_id: int
    result: str  # ok | rejected | error | timeout
    # Recorded so load-test records can later be stripped from logs/qa/ by exact
    # session_id — the only criterion that never misclassifies organic traffic.
    session_id: str = ""
    ttft_ms: float | None = None
    total_ms: float | None = None
    server_duration_ms: float | None = None
    http_status: int | None = None
    error: str | None = None
    started_at: float = 0.0


@dataclass
class StepStats:
    users: int
    samples: list[Sample] = field(default_factory=list)

    def _values(self, attr: str) -> list[float]:
        return sorted(getattr(s, attr) for s in self.samples if s.result == "ok" and getattr(s, attr) is not None)

    @staticmethod
    def _pct(values: list[float], p: float) -> float | None:
        if not values:
            return None
        idx = min(len(values) - 1, max(0, round(p / 100 * (len(values) + 1)) - 1))
        return values[idx]

    def summary(self) -> dict:
        ttft = self._values("ttft_ms")
        total = self._values("total_ms")
        n = len(self.samples)
        ok = sum(1 for s in self.samples if s.result == "ok")
        rejected = sum(1 for s in self.samples if s.result == "rejected")
        errors = n - ok - rejected
        return {
            "users": self.users,
            "requests": n,
            "ok": ok,
            "rejected_503": rejected,
            "errors": errors,
            "ttft_p50_s": round(self._pct(ttft, 50) / 1000, 1) if ttft else None,
            "ttft_p95_s": round(self._pct(ttft, 95) / 1000, 1) if ttft else None,
            "ttft_max_s": round(ttft[-1] / 1000, 1) if ttft else None,
            "total_p50_s": round(self._pct(total, 50) / 1000, 1) if total else None,
            "total_p95_s": round(self._pct(total, 95) / 1000, 1) if total else None,
            "total_max_s": round(total[-1] / 1000, 1) if total else None,
        }


async def create_session(client: httpx.AsyncClient, base_url: str) -> str:
    resp = await client.post(f"{base_url}/api/session", json={"lang": "ko"}, timeout=30.0)
    resp.raise_for_status()
    return resp.json()["session_id"]


async def ask_question(
    client: httpx.AsyncClient, base_url: str, session_id: str, question: dict, step_users: int, user_idx: int
) -> Sample:
    sample = Sample(step_users=step_users, user_idx=user_idx, question_id=question["id"], result="error")
    params = {"session_id": session_id, "question": question["question"]}
    t0 = time.monotonic()
    sample.started_at = time.time()
    current_event: str | None = None
    data_lines: list[str] = []
    try:
        # sse-starlette sends a keepalive ping every 15s regardless of generator
        # progress, which resets httpx's per-read timeout — so the per-question
        # ceiling must be a wall-clock bound around the whole stream.
        async with asyncio.timeout(QUESTION_TIMEOUT_S), client.stream(
            "GET", f"{base_url}/api/chat/stream", params=params, timeout=httpx.Timeout(None, connect=15.0)
        ) as resp:
            sample.http_status = resp.status_code
            if resp.status_code == 503:
                sample.result = "rejected"
                return sample
            if resp.status_code != 200:
                sample.error = f"HTTP {resp.status_code}"
                return sample
            async for raw_line in resp.aiter_lines():
                line = raw_line.rstrip("\n")
                if line.startswith("event:"):
                    current_event = line[6:].strip()
                    continue
                if line.startswith("data:"):
                    data_lines.append(line[5:].lstrip())
                    continue
                if line:
                    continue
                # blank line = event delimiter
                if current_event == "token" and sample.ttft_ms is None:
                    sample.ttft_ms = (time.monotonic() - t0) * 1000
                elif current_event == "done":
                    payload = json.loads("\n".join(data_lines) or "{}")
                    sample.total_ms = (time.monotonic() - t0) * 1000
                    sample.server_duration_ms = payload.get("duration_ms")
                    # A refusal can legitimately finish with no token before done.
                    if sample.ttft_ms is None:
                        sample.ttft_ms = sample.total_ms
                    sample.result = "ok"
                    return sample
                elif current_event == "error":
                    msg = "\n".join(data_lines)[:200]
                    # The slot race inside the SSE generator delivers the same busy
                    # rejection as the 503 path, but over an already-open 200 stream
                    # (advisory reject_if_saturated() vs authoritative acquire).
                    if "지금 처리 중인 질문이 많습니다" in msg:
                        sample.result = "rejected"
                    else:
                        sample.error = msg
                    return sample
                current_event = None
                data_lines = []
            sample.error = "stream closed without done event"
    except (httpx.TimeoutException, asyncio.TimeoutError):
        sample.result = "timeout"
        sample.error = f"no done event within {QUESTION_TIMEOUT_S:.0f}s"
    except Exception as exc:  # noqa: BLE001 — record, don't crash the step
        sample.error = f"{type(exc).__name__}: {exc}"
    return sample


async def virtual_user(
    base_url: str,
    questions: list[dict],
    step_users: int,
    user_idx: int,
    think_time_s: float,
    results: list[Sample],
    stagger_s: float,
    deadline: float | None = None,
) -> None:
    """Closed loop: ask, wait for the full answer, think, ask again."""
    await asyncio.sleep(stagger_s)
    async with httpx.AsyncClient() as client:
        try:
            session_id = await create_session(client, base_url)
        except Exception as exc:  # noqa: BLE001
            results.append(
                Sample(step_users=step_users, user_idx=user_idx, question_id=-1, result="error",
                       error=f"session: {type(exc).__name__}: {exc}")
            )
            return
        i = 0
        while True:
            if deadline is not None and time.monotonic() >= deadline:
                return
            if deadline is None and i >= len(questions):
                return
            q = questions[i % len(questions)]
            sample = await ask_question(client, base_url, session_id, q, step_users, user_idx)
            sample.session_id = session_id
            results.append(sample)
            i += 1
            await asyncio.sleep(think_time_s)


async def run_step(
    base_url: str, users: int, dataset: list[dict], questions_per_user: int, think_time_s: float,
    duration_min: float | None = None, seed: int = 42,
) -> StepStats:
    rng = random.Random(seed + users)
    shuffled = dataset[:]
    rng.shuffle(shuffled)
    results: list[Sample] = []
    deadline = time.monotonic() + duration_min * 60 if duration_min else None
    tasks = []
    for u in range(users):
        qs = [shuffled[(u * questions_per_user + k) % len(shuffled)] for k in range(questions_per_user)]
        # Stagger arrivals over a few seconds so a step is "N people using it",
        # not a synchronized burst (overload mode uses stagger 0 for the burst).
        stagger = rng.uniform(0, min(5.0, users)) if think_time_s > 0 else 0.0
        tasks.append(virtual_user(base_url, qs, users, u, think_time_s, results, stagger, deadline))
    await asyncio.gather(*tasks)
    step = StepStats(users=users)
    step.samples = results
    return step


def print_table(steps: list[StepStats]) -> None:
    cols = ["users", "requests", "ok", "rejected_503", "errors",
            "ttft_p50_s", "ttft_p95_s", "ttft_max_s", "total_p50_s", "total_p95_s", "total_max_s"]
    rows = [s.summary() for s in steps]
    print("| " + " | ".join(cols) + " |")
    print("|" + "|".join("---" for _ in cols) + "|")
    for r in rows:
        print("| " + " | ".join(str(r[c]) if r[c] is not None else "-" for c in cols) + " |")


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("mode", choices=["ramp", "overload", "soak"])
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--steps", default="1,2,4,6,8", help="ramp: comma-separated user counts")
    parser.add_argument("--users", type=int, default=4, help="overload/soak: user count")
    parser.add_argument("--questions", type=int, default=8, help="questions per user per step")
    parser.add_argument("--duration", type=float, default=60.0, help="soak: minutes")
    parser.add_argument("--think-time", type=float, default=5.0, help="seconds between a user's questions")
    parser.add_argument("--cooldown", type=float, default=20.0, help="seconds between ramp steps")
    parser.add_argument("--dataset", default=str(DEFAULT_DATASET))
    parser.add_argument("--tag", default="")
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    dataset = json.loads(Path(args.dataset).read_text(encoding="utf-8"))
    out_dir = DEFAULT_OUT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)

    steps: list[StepStats] = []
    if args.mode == "ramp":
        user_counts = [int(x) for x in args.steps.split(",") if x.strip()]
        for users in user_counts:
            print(f"[step] {users} users x {args.questions} questions ...", flush=True)
            steps.append(await run_step(args.base_url, users, dataset, args.questions, args.think_time, seed=args.seed))
            print_table(steps[-1:])
            await asyncio.sleep(args.cooldown)
    elif args.mode == "overload":
        print(f"[overload] {args.users} users burst (no think-time stagger) ...", flush=True)
        steps.append(await run_step(args.base_url, args.users, dataset, args.questions, 0.0, seed=args.seed))
    else:  # soak
        print(f"[soak] {args.users} users for {args.duration:.0f} min ...", flush=True)
        steps.append(
            await run_step(args.base_url, args.users, dataset, args.questions, args.think_time,
                           duration_min=args.duration, seed=args.seed)
        )

    print("\n=== summary ===")
    print_table(steps)

    tag = f"_{args.tag}" if args.tag else ""
    out_path = out_dir / f"load_{args.mode}{tag}_{time.strftime('%Y%m%d_%H%M%S')}.json"
    out_path.write_text(
        json.dumps(
            {
                "mode": args.mode,
                "args": vars(args),
                "summary": [s.summary() for s in steps],
                "samples": [asdict(x) for s in steps for x in s.samples],
            },
            ensure_ascii=False,
            indent=1,
        ),
        encoding="utf-8",
    )
    print(f"\n[saved] {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
