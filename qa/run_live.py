"""Run all 100 QA questions against the live chatbot (localhost:8000) and dump raw
answers + retrieved chunks to qa/live_runs.jsonl. Resumable: skips ids already present.

Usage: python qa/run_live.py
"""
import json
import os
import sys
import time
import urllib.parse

import requests

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

BASE = "http://localhost:8000"
HERE = os.path.dirname(os.path.abspath(__file__))
DATASET = os.path.join(HERE, "qa_dataset.json")
OUT = os.path.join(HERE, "live_runs.jsonl")


def ask(question, timeout=400):
    sid = requests.post(BASE + "/api/session", json={"lang": "ko"}, timeout=30).json()["session_id"]
    url = BASE + "/api/chat/stream?" + urllib.parse.urlencode({"session_id": sid, "question": question})
    done, event = None, None
    with requests.get(url, stream=True, timeout=timeout,
                      headers={"X-Test-Mode": "1"}) as resp:
        for line in resp.iter_lines(decode_unicode=True):
            if line is None:
                continue
            if line.startswith("event:"):
                event = line[6:].strip()
            elif line.startswith("data:") and event == "done":
                done = json.loads(line[5:].strip())
            elif line == "":
                event = None
    return done or {}


def main():
    rows = json.load(open(DATASET, encoding="utf-8"))
    done_ids = set()
    if os.path.exists(OUT):
        for line in open(OUT, encoding="utf-8"):
            line = line.strip()
            if line:
                try:
                    done_ids.add(json.loads(line)["id"])
                except Exception:
                    pass
    todo = [r for r in rows if r["id"] not in done_ids]
    print(f"{len(done_ids)} already done; running {len(todo)} of {len(rows)}", flush=True)

    jf = open(OUT, "a", encoding="utf-8")
    t0 = time.time()
    for k, r in enumerate(todo, 1):
        q = r["question"]
        try:
            d = ask(q)
            ans = d.get("answer", "")
            results = d.get("results", [])
            rec = {
                "id": r["id"], "question": q, "answer": ans,
                "results": [{"source": x.get("source", ""), "text": x.get("text", "")} for x in results],
                "tool_calls": d.get("tool_calls"), "duration_ms": d.get("duration_ms"),
                "intent": d.get("intent", ""),
            }
        except Exception as e:
            rec = {"id": r["id"], "question": q, "answer": f"(ERROR {e})", "results": [], "error": str(e)}
        jf.write(json.dumps(rec, ensure_ascii=False) + "\n")
        jf.flush()
        el = time.time() - t0
        nres = len(rec.get("results", []))
        alen = len(rec.get("answer", ""))
        print(f"[{k:3}/{len(todo)}] id={r['id']:3} ans={alen:4}c results={nres:2} ({el/60:.1f}m)", flush=True)
    jf.close()
    print("done ->", OUT, flush=True)


if __name__ == "__main__":
    main()
