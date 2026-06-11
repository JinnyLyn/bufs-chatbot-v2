"""Offline unit tests for debug/status.py (bugs #4, #6, #11).

All Langfuse fetches and the /health probe are monkeypatched — NO network,
NO credentials. Orphan detection runs against a synthetic app.log under a
tmp BUFS_LOG_DIR with a deterministic newest-line reference clock.

Covered:
  #4  time-bounded recent/prior trace windows + windowed observations
        - prior window passes toTimestamp; obs fetch passes fromStartTime
        - every prior trace falls in [now-14d, now-7d)
  #6  exit code 2 is the missing-env/config-error branch only
        (health-unreachable is an exit-1 anomaly, not exit 2)
  #11 orphan grace period: a chat-IN older than the grace with no chat-OUT is
        a real ORPHAN (anomaly); a recent chat-IN with no chat-OUT is DEFERRED.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

import debug.status as status


# ── shared helpers ────────────────────────────────────────────────────────────

def _iso(dt: datetime) -> str:
    """Match _query / status _utc_since RFC 3339 format."""
    return dt.strftime("%Y-%m-%dT%H:%M:%S") + "+00:00"


def _stub_health_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make check_health return (True, ...) without touching the network."""

    class _Resp:
        status_code = 200
        headers = {"content-type": "application/json"}

        def json(self):
            return {"ok": True}

    monkeypatch.setattr(status._requests, "get", lambda *a, **k: _Resp())


def _stub_creds(monkeypatch: pytest.MonkeyPatch) -> None:
    """Provide Langfuse creds and neutralise ensure_env so main() proceeds."""
    monkeypatch.setattr(status, "ensure_env", lambda: None)
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-test")


# ── #4: time-bounded windows + windowed observations ────────────────────────────

def test_bug4_prior_window_is_time_bounded_and_obs_windowed(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    now = datetime.utcnow()
    # 250 recent traces in [now-7d, now]; 250 prior traces in [now-14d, now-7d).
    recent = [
        {"id": f"r{i}", "latency": 5.0, "timestamp": _iso(now - timedelta(days=1))}
        for i in range(250)
    ]
    prior = [
        {"id": f"p{i}", "latency": 5.0, "timestamp": _iso(now - timedelta(days=10))}
        for i in range(250)
    ]

    calls: list[dict] = []

    def fake_window(days=7, want=200, **filters):
        calls.append({"days": days, "want": want, **filters})
        # second call (days=14) is the prior window
        return prior if days == 14 else recent

    obs_calls: list[dict] = []
    # Include every _EXPECTED_NODES so node-liveness passes (keeps the run at
    # exit 0; the #4 assertions only care about the captured kwargs).
    healthy_obs = [{"name": n, "level": "DEFAULT"} for n in status._EXPECTED_NODES]

    def fake_obs(want=1200, **filters):
        obs_calls.append({"want": want, **filters})
        return list(healthy_obs)

    monkeypatch.setattr(status, "fetch_traces_window", fake_window)
    monkeypatch.setattr(status, "fetch_observations", fake_obs)
    _stub_health_ok(monkeypatch)
    _stub_creds(monkeypatch)
    # No app.log under this root → orphan check is a no-op skip.
    monkeypatch.setenv("BUFS_LOG_DIR", str(tmp_path))

    rc = status.main([])

    # prior window call carried a toTimestamp bound
    prior_call = next(c for c in calls if c["days"] == 14)
    assert "toTimestamp" in prior_call, "prior fetch must pass toTimestamp"
    recent_call = next(c for c in calls if c["days"] == 7)
    assert "toTimestamp" not in recent_call, "recent window must not be upper-bounded"

    # observations fetch carried a time window — the observations REST endpoint
    # filters on `fromStartTime` (NOT `fromTimestamp`, which is the traces param).
    assert obs_calls, "fetch_observations should have been called"
    assert "fromStartTime" in obs_calls[0], "obs fetch must pass fromStartTime"
    assert "fromTimestamp" not in obs_calls[0], "obs endpoint ignores fromTimestamp"

    # prior_only is non-empty and every prior trace lies in [now-14d, now-7d)
    lo = now - timedelta(days=14)
    hi = now - timedelta(days=7)
    assert prior, "prior fixture must be non-empty"
    for t in prior:
        ts = datetime.strptime(t["timestamp"][:19], "%Y-%m-%dT%H:%M:%S")
        assert lo <= ts < hi, f"prior trace {t['id']} ts {ts} outside [now-14d, now-7d)"

    # the run itself succeeds (health ok, no orphans, no latency degradation)
    assert rc == 0


# ── #6: exit code 2 is the config-error branch only ─────────────────────────────

def test_bug6_missing_env_returns_exit_2(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    # ensure_env is a no-op so it can't backfill creds from project/.env
    monkeypatch.setattr(status, "ensure_env", lambda: None)
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    monkeypatch.setenv("BUFS_LOG_DIR", str(tmp_path))

    assert status.main([]) == 2


def test_bug6_unreachable_health_is_exit_1_not_2(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    # Creds present (so we are past the exit-2 branch); health unreachable.
    _stub_creds(monkeypatch)

    def _boom(*a, **k):
        raise status._requests.exceptions.ConnectionError("refused")

    monkeypatch.setattr(status._requests, "get", _boom)
    # Langfuse fetches succeed and produce no anomalies.
    monkeypatch.setattr(status, "fetch_traces_window", lambda **k: [])
    monkeypatch.setattr(status, "fetch_observations", lambda **k: [])
    monkeypatch.setenv("BUFS_LOG_DIR", str(tmp_path))

    # health-unreachable is an anomaly → exit 1, never the config-error exit 2.
    assert status.main([]) == 1


def test_bug6_docstring_exit2_excludes_health_endpoint() -> None:
    doc = status.__doc__ or ""
    assert "unreachable health endpoint" not in doc
    assert "missing required env var" in doc


# ── #11: orphan grace period ────────────────────────────────────────────────────

def _write_app_log(tmp_path, lines: list[str]):
    backend = tmp_path / "backend"
    backend.mkdir(parents=True, exist_ok=True)
    (backend / "app.log").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _logline(ts: datetime, tid: str, marker: str) -> str:
    stamp = ts.strftime("%Y-%m-%d %H:%M:%S,%f")[:-3]  # millisecond precision
    return f"{stamp} [{tid}] INFO api.chat:chat_stream:77 - {marker}"


def test_bug11_old_orphan_flagged_recent_deferred(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    base = datetime(2026, 6, 11, 12, 0, 0)
    old_tid = "aaaaaaaa"
    recent_tid = "bbbbbbbb"

    # newest line = base (a chat-IN that is itself unmatched but RECENT).
    # old_tid chat-IN is 400s before newest → > 300s grace → real ORPHAN.
    # recent_tid chat-IN is 100s before newest → < 300s grace → DEFERRED.
    lines = [
        _logline(base - timedelta(seconds=400), old_tid,
                 "[chat-IN] tid=aaaaaaaa sid=00000000 q_chars=3 q='hi' model=m test=False"),
        _logline(base - timedelta(seconds=100), recent_tid,
                 "[chat-IN] tid=bbbbbbbb sid=00000000 q_chars=3 q='yo' model=m test=False"),
    ]
    _write_app_log(tmp_path, lines)
    monkeypatch.setenv("BUFS_LOG_DIR", str(tmp_path))

    ok, msgs = status.check_orphans()
    blob = "\n".join(msgs)

    assert ok is False, "an old orphan past the grace must flag an anomaly"
    assert old_tid in blob, "old orphan tid should be listed"
    assert recent_tid not in blob, "recent (deferred) orphan must NOT be flagged"
    assert "1 ORPHAN" in blob
    assert "deferred 1" in blob and "grace" in blob


def test_bug11_only_recent_orphan_is_deferred_no_anomaly(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    base = datetime(2026, 6, 11, 12, 0, 0)
    # Two lines: a chat-IN 100s before the newest line, no chat-OUT.
    lines = [
        _logline(base - timedelta(seconds=100), "cccccccc",
                 "[chat-IN] tid=cccccccc sid=00000000 q_chars=3 q='hi' model=m test=False"),
        _logline(base, "dddddddd",
                 "[chat-OUT] tid=dddddddd sid=00000000 answer_chars=5 results=1 sources=1 total_ms=900"),
    ]
    _write_app_log(tmp_path, lines)
    monkeypatch.setenv("BUFS_LOG_DIR", str(tmp_path))

    ok, msgs = status.check_orphans()
    blob = "\n".join(msgs)

    assert ok is True, "a sub-grace orphan must NOT trip an anomaly"
    assert "ORPHAN" not in blob
    assert "deferred 1" in blob


def test_bug11_grace_uses_newest_line_not_wallclock(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    # All lines are years in the past; wall-clock now() would make every
    # chat-IN ancient. The newest-line reference clock keeps the recent one
    # within grace → deterministic regardless of real time.
    base = datetime(2020, 1, 1, 0, 0, 0)
    lines = [
        _logline(base - timedelta(seconds=50), "eeeeeeee",
                 "[chat-IN] tid=eeeeeeee sid=00000000 q_chars=3 q='hi' model=m test=False"),
        _logline(base, "ffffffff",
                 "[chat-IN] tid=ffffffff sid=00000000 q_chars=3 q='yo' model=m test=False"),
    ]
    _write_app_log(tmp_path, lines)
    monkeypatch.setenv("BUFS_LOG_DIR", str(tmp_path))

    ok, msgs = status.check_orphans()
    # Both INs are within 50s of the newest line → both deferred, no anomaly.
    assert ok is True
    assert "ORPHAN" not in "\n".join(msgs)
