"""Run a subset of bufs-chatbot's eval_ko.jsonl against the NEW agentic-rag chatbot
(localhost:8000 SSE) and score each answer against its key_facts.

key_facts use a compact notation: "130"=130학점, "3.2"=3월 2일, "9:45"=9시 45분, "C+"=grade.
The matcher expands each fact to plausible Korean surface forms before substring-checking.
"""
import json, re, sys, time, urllib.parse
import requests

try: sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception: pass

BASE = "http://localhost:8000"
EVAL = r"C:\Users\suhwa\Desktop\bufs-chatbot\data\eval_multilingual\eval_ko.jsonl"
SELECTED = ["q001","q004","q009","q016","q026","q028","q035","q041","q042","q043","q044","q050"]
OUT = r"C:\Users\suhwa\Desktop\agentic-rag-for-dummies-main\logs\eval_ko_subset_result.json"


def fact_variants(fact: str):
    """Return plausible surface forms of a compact key_fact."""
    f = fact.strip()
    out = {f, f.replace(" ", "")}
    m = re.fullmatch(r"(\d{1,2})\.(\d{1,2})", f)          # date M.D -> M월 D일
    if m:
        mo, d = int(m.group(1)), int(m.group(2))
        out |= {f"{mo}월 {d}일", f"{mo}월{d}일", f"{mo:02d}.{d:02d}", f"{mo}.{d}", f"{mo}/{d}"}
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", f)             # time H:MM -> H시 M분
    if m:
        h, mi = int(m.group(1)), int(m.group(2))
        out |= {f"{h}:{mi:02d}", f"{h:02d}:{mi:02d}", f"{h}시 {mi}분", f"{h}시{mi}분"}
        if mi == 0: out |= {f"{h}시"}
    if re.fullmatch(r"\d+", f):                            # bare number -> also "N학점"/"N시"
        out |= {f"{f}학점", f"{f}시", f"{f},000"}
    return {x for x in out if x}


def matched(fact, answer):
    a = answer.replace(" ", "")
    return any(v.replace(" ", "") in a for v in fact_variants(fact))


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


rows = {json.loads(l)["id"]: json.loads(l) for l in open(EVAL, encoding="utf-8") if l.strip()}
items = [rows[i] for i in SELECTED if i in rows]
print(f"running {len(items)} questions against {BASE}\n" + "=" * 72)

results, n_pass, n_partial = [], 0, 0
for k, r in enumerate(items, 1):
    q, kf = r["question"], r.get("key_facts", [])
    t0 = time.time()
    try:
        done = ask(q)
    except Exception as e:
        done = {"answer": f"(ERROR {e})"}
    ans = done.get("answer", "")
    hits = [f for f in kf if matched(f, ans)]
    ok = len(hits) == len(kf) and len(kf) > 0
    no_kf = len(kf) == 0
    verdict = "PASS" if ok else ("(no kf)" if no_kf else ("PARTIAL" if hits else "FAIL"))
    if ok: n_pass += 1
    elif hits: n_partial += 1
    dt = time.time() - t0
    print(f"[{k:2}/{len(items)}] {r['id']} [{r['category']}] {verdict}  facts {len(hits)}/{len(kf)}  {dt:.0f}s  tools={done.get('tool_calls')}")
    print(f"   Q: {q}")
    print(f"   key_facts={kf}  matched={hits}")
    print(f"   GT: {r.get('ground_truth','')}")
    print(f"   A : {ans[:280].replace(chr(10),' ')}")
    print("-" * 72)
    results.append({"id": r["id"], "category": r["category"], "question": q, "key_facts": kf,
                    "matched": hits, "verdict": verdict, "ground_truth": r.get("ground_truth",""),
                    "answer": ans, "tool_calls": done.get("tool_calls"), "duration_ms": done.get("duration_ms")})

total = len(items)
print("=" * 72)
print(f"SUMMARY: PASS {n_pass}/{total}  | PARTIAL {n_partial}  | (full key_fact match = PASS)")
json.dump({"summary": {"pass": n_pass, "partial": n_partial, "total": total}, "results": results},
          open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
print("report ->", OUT)
