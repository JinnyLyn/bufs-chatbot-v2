import json, sys, time, urllib.parse
import requests
try: sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception: pass
BASE = "http://localhost:8000"
Q = "2020학번 학점별로 필수 졸업요건 학점 알려줘"
sid = requests.post(BASE + "/api/session", json={"lang": "ko"}, timeout=30).json()["session_id"]
url = BASE + "/api/chat/stream?" + urllib.parse.urlencode({"session_id": sid, "question": Q})
done = None; event = None
with requests.get(url, stream=True, timeout=400) as resp:
    for line in resp.iter_lines(decode_unicode=True):
        if line is None: continue
        if line.startswith("event:"): event = line[6:].strip()
        elif line.startswith("data:") and event == "done": done = json.loads(line[5:].strip())
        elif line == "": event = None
if done:
    a = done.get("answer", "")
    print("=== ANSWER (first 900 chars) ===")
    print(a[:900])
    print("\n=== METRICS ===")
    print("duration_ms:", done.get("duration_ms"), "| sub_q:", done.get("sub_questions"), "| tool_calls:", done.get("tool_calls"))
    print("timing:", done.get("timing"))
    print("mentions 130학점:", "130" in a, "| mentions 2017~2020:", "2017" in a, "| 복수전공 33:", "33" in a)
    print("sources:", sorted({r.get("source") for r in done.get("results", []) if r.get("source")}))
