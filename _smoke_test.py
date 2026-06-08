import json, sys, time, urllib.parse
import requests
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

BASE = "http://localhost:8000"
QUESTION = sys.argv[1] if len(sys.argv) > 1 else "공인결석은 어떻게 신청하나요?"
sid = requests.post(BASE + "/api/session", json={"lang": "ko"}, timeout=30).json()["session_id"]
print("Q:", QUESTION)
url = BASE + "/api/chat/stream?" + urllib.parse.urlencode({"session_id": sid, "question": QUESTION})
t0 = time.monotonic()
tokens, done, err, event = [], None, None, None
with requests.get(url, stream=True, timeout=600) as resp:
    for line in resp.iter_lines(decode_unicode=True):
        if line is None: continue
        if line.startswith("event:"): event = line[6:].strip()
        elif line.startswith("data:"):
            data = line[5:].strip()
            if event == "token":
                try: tokens.append(json.loads(data)["token"])
                except Exception: pass
            elif event == "done": done = json.loads(data)
            elif event == "error": err = data
        elif line == "": event = None
print("wall:", round(time.monotonic() - t0, 1), "s | streamed:", len("".join(tokens)), "chars")
if err: print("ERROR:", err)
if done:
    print("\nANSWER:\n" + done["answer"][:500])
    print("\nsources:", sorted({it.get("source") for it in done.get("results", []) if it.get("source")}))
    print("result items:", len(done.get("results", [])), "| duration_ms:", done.get("duration_ms"), "| SMOKE_OK")
