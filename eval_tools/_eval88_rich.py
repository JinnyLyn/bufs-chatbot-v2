"""Rich combined88 eval: same scoring as _eval_combined88.py, but also captures the
backend's full `done` payload per question — crucially `results` (the retrieved context
chunks) and `timing` (per-node latency) — so a downstream stage-attribution pass can tell
WHERE each wrong answer broke (rewrite / retrieval / generation / KB-coverage) without
re-querying. Output: logs/combined88_rich.jsonl + logs/combined88_rich_result.json.
"""
import json, re, sys, time, urllib.parse
import requests
try: sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception: pass

BASE = "http://localhost:8000"
SRC = r"C:\Users\suhwa\Desktop\bufs-chatbot\reports\retrieval_eval\combined88_results_fix_20260429.json"
OUT = r"C:\Users\suhwa\Desktop\agentic-rag-for-dummies-main\logs\combined88_rich_result.json"
JSONL = r"C:\Users\suhwa\Desktop\agentic-rag-for-dummies-main\logs\combined88_rich.jsonl"

REFUSAL = ["없습니다", "없음", "불가", "확인할 수 없", "찾을 수 없", "포함되어 있지 않",
           "직접 확인", "명시되어 있지 않", "알 수 없", "제공되지 않", "찾지 못"]


def extract_facts(gt: str):
    facts, s = set(), gt
    for pat in [r"\d{1,2}월\s?\d{1,2}일", r"\d{1,2}:\d{2}"]:
        for m in re.findall(pat, s):
            facts.add(m.replace(" ", ""))
        s = re.sub(pat, " ", s)
    for m in re.findall(r"\d{1,2}\.\d{1,2}", s):
        mo, da = m.split("."); facts.add(f"{int(mo)}월{int(da)}일")
    s = re.sub(r"\d{1,2}\.\d{1,2}", " ", s)
    for m in re.findall(r"[A-F]\+", s):
        facts.add(m)
    for m in re.findall(r"\d[\d,]*", s):
        n = m.replace(",", "")
        if re.fullmatch(r"(19|20)\d\d", n):
            continue
        facts.add(n)
    return facts


def matched(fact: str, answer: str) -> bool:
    a = answer or ""
    if re.fullmatch(r"\d+", fact):
        return re.search(r"(?<!\d)" + fact + r"(?!\d)", a.replace(",", "")) is not None
    if re.fullmatch(r"\d{1,2}:\d{2}", fact):
        h, mi = fact.split(":")
        return any(v in a for v in [fact, f"{int(h):02d}:{mi}", f"{h}시 {int(mi)}분", f"{h}시{int(mi)}분"])
    if re.fullmatch(r"\d{1,2}월\d{1,2}일", fact):
        return fact in a.replace(" ", "")
    return fact in a


def is_refusal(ans: str) -> bool:
    return any(x in ans for x in REFUSAL)


def results_text(results) -> str:
    """Flatten the retrieved-context chunks into one searchable string."""
    if not isinstance(results, list):
        return ""
    return "\n".join(str(r.get("text", "")) for r in results if isinstance(r, dict))


def ask(question, timeout=300):
    sid = requests.post(BASE + "/api/session", json={"lang": "ko"}, timeout=30).json()["session_id"]
    url = BASE + "/api/chat/stream?" + urllib.parse.urlencode({"session_id": sid, "question": question})
    done, event = None, None
    with requests.get(url, stream=True, timeout=timeout) as resp:
        for line in resp.iter_lines(decode_unicode=True):
            if line is None: continue
            if line.startswith("event:"): event = line[6:].strip()
            elif line.startswith("data:") and event == "done": done = json.loads(line[5:].strip())
            elif line == "": event = None
    return done or {}


data = json.load(open(SRC, encoding="utf-8"))["results"]
print(f"running {len(data)} questions vs {BASE} (rich capture)", flush=True)
jf = open(JSONL, "w", encoding="utf-8")

ans_pass = ans_contains = ans_total = 0
ref_correct = ref_total = 0
results_all = []
durations = []
t_start = time.time()

for k, r in enumerate(data, 1):
    q = r["question"]; gt = r.get("ground_truth", ""); answerable = str(r.get("answerable", True)).lower() == "true"
    try:
        done = ask(q); ans = done.get("answer", "")
    except Exception as e:
        done = {}; ans = f"(ERROR {e})"

    ctx = results_text(done.get("results"))
    if done.get("duration_ms"): durations.append(done["duration_ms"])

    if answerable:
        ans_total += 1
        facts = extract_facts(gt)
        hits = [f for f in facts if matched(f, ans)]
        if facts:
            full = len(hits) == len(facts); some = len(hits) > 0
        else:
            gtoks = set(re.findall(r"[가-힣A-Za-z]+", gt)); atoks = set(re.findall(r"[가-힣A-Za-z]+", ans))
            ov = len(gtoks & atoks) / max(1, len(gtoks)); full = ov >= 0.6; some = ov >= 0.3
        refused = is_refusal(ans)
        if full and not refused: ans_pass += 1
        if some and not refused: ans_contains += 1
        verdict = "PASS" if (full and not refused) else ("CONTAINS" if (some and not refused) else "FAIL")
        rec = {"id": r["id"], "intent": r.get("intent"), "gt_source": r.get("gt_source"),
               "difficulty": r.get("difficulty"), "answerable": True, "question": q, "ground_truth": gt,
               "facts": sorted(facts), "matched": hits, "verdict": verdict, "answer": ans,
               "refused": refused, "context_text": ctx,
               "sub_questions": done.get("sub_questions"), "tool_calls": done.get("tool_calls"),
               "timing": done.get("timing"), "duration_ms": done.get("duration_ms"),
               "source_urls": done.get("source_urls")}
    else:
        ref_total += 1
        ok = is_refusal(ans); ref_correct += int(ok)
        verdict = "REFUSE_OK" if ok else "HALLUCINATED"
        rec = {"id": r["id"], "intent": r.get("intent"), "gt_source": r.get("gt_source"),
               "answerable": False, "question": q, "ground_truth": gt, "verdict": verdict, "answer": ans,
               "context_text": ctx, "timing": done.get("timing"), "duration_ms": done.get("duration_ms")}

    results_all.append(rec); jf.write(json.dumps(rec, ensure_ascii=False) + "\n"); jf.flush()
    el = time.time() - t_start
    print(f"[{k:2}/{len(data)}] {r['id']:5} {verdict:12} pass={ans_pass} contains={ans_contains}/{ans_total} ref={ref_correct}/{ref_total} {done.get('duration_ms','?')}ms ({el/60:.1f}m)", flush=True)

avg_ms = sum(durations) / max(1, len(durations))
summary = {
    "answerable_total": ans_total, "strict_pass": ans_pass,
    "strict_pass_rate": round(ans_pass / max(1, ans_total), 4),
    "contains_correct": ans_contains, "contains_rate": round(ans_contains / max(1, ans_total), 4),
    "unanswerable_total": ref_total, "correct_refusals": ref_correct,
    "refusal_rate": round(ref_correct / max(1, ref_total), 4),
    "avg_duration_ms": round(avg_ms), "avg_duration_s": round(avg_ms / 1000, 1),
    "median_duration_s": round(sorted(durations)[len(durations) // 2] / 1000, 1) if durations else None,
    "max_duration_s": round(max(durations) / 1000, 1) if durations else None,
    "total_wall_min": round((time.time() - t_start) / 60, 1),
}
json.dump({"summary": summary, "results": results_all}, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
jf.close()
print("\n=== SUMMARY ===", flush=True)
print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
print("report ->", OUT, flush=True)
