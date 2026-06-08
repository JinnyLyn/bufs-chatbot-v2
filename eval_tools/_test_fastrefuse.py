"""Focused fast-refuse test: the 8 out-of-scope (u01-u08, should now refuse FAST) +
6 answerable (confirm no regression). Reports per-Q latency + verdict + averages."""
import json, re, sys, time, urllib.parse, statistics
import requests
try: sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception: pass
BASE = "http://localhost:8000"
SRC = r"C:\Users\suhwa\Desktop\bufs-chatbot\reports\retrieval_eval\combined88_results_fix_20260429.json"
ANSWERABLE_SAMPLE = ["q001", "q016", "q041", "q050", "q009", "q026"]
REFUSAL = ["없습니다","없음","불가","확인할 수 없","찾을 수 없","포함되어 있지 않","직접 확인","명시되어 있지 않","알 수 없","제공되지 않","찾지 못","확인되지 않","범위를 벗어"]

def extract_facts(gt):
    facts, s = set(), gt
    for pat in [r"\d{1,2}월\s?\d{1,2}일", r"\d{1,2}:\d{2}"]:
        for m in re.findall(pat, s): facts.add(m.replace(" ", ""))
        s = re.sub(pat, " ", s)
    for m in re.findall(r"\d{1,2}\.\d{1,2}", s):
        mo, da = m.split("."); facts.add(f"{int(mo)}월{int(da)}일")
    s = re.sub(r"\d{1,2}\.\d{1,2}", " ", s)
    for m in re.findall(r"(?<![A-Za-z])[A-F][+-]?(?![A-Za-z])", s): facts.add(m)
    for m in re.findall(r"\d[\d,]*", s):
        n = m.replace(",", "")
        if re.fullmatch(r"(19|20)\d\d", n): continue
        facts.add(n)
    return facts

def matched(fact, a):
    if not a: return False
    if re.fullmatch(r"\d+", fact):
        n=int(fact)
        if re.search(r"(?<!\d)"+fact+r"(?!\d)", a.replace(",","")): return True
        return 13<=n<=23 and (f"오후 {n-12}시" in a or f"오후{n-12}시" in a)
    if re.fullmatch(r"\d{1,2}월\d{1,2}일", fact): return fact in a.replace(" ","")
    return fact in a

def is_refusal(a): return any(x in a for x in REFUSAL)

def ask(q, timeout=400):
    sid = requests.post(BASE+"/api/session", json={"lang":"ko"}, timeout=30).json()["session_id"]
    url = BASE+"/api/chat/stream?"+urllib.parse.urlencode({"session_id":sid,"question":q})
    done,event=None,None
    with requests.get(url, stream=True, timeout=timeout) as r:
        for line in r.iter_lines(decode_unicode=True):
            if line is None: continue
            if line.startswith("event:"): event=line[6:].strip()
            elif line.startswith("data:") and event=="done": done=json.loads(line[5:].strip())
            elif line=="": event=None
    return done or {}

rows={r["id"]:r for r in json.load(open(SRC,encoding="utf-8"))["results"]}
unans=[r for r in rows.values() if not r.get("answerable")]
selected=[(r,"OUT") for r in unans] + [(rows[i],"ANS") for i in ANSWERABLE_SAMPLE if i in rows]
print(f"fast-refuse test: {len(unans)} out-of-scope + {len(ANSWERABLE_SAMPLE)} answerable\n"+"="*64)

durs=[]; ref_ok=ref_tot=0; ans_ok=ans_tot=0
for r,kind in selected:
    q=r["question"]
    try: done=ask(q); a=done.get("answer","")
    except Exception as e: done={}; a=f"(ERR {e})"
    d=(done.get("duration_ms") or 0)/1000; durs.append(d); tc=done.get("tool_calls")
    if kind=="OUT":
        ref_tot+=1; ok=is_refusal(a); ref_ok+=ok
        v="REFUSE_OK" if ok else "HALLUC"
    else:
        ans_tot+=1; facts=extract_facts(r["ground_truth"]); hits=[f for f in facts if matched(f,a)]
        ok=bool(facts) and len(hits)==len(facts); ans_ok+=ok
        v="PASS" if ok else ("CONTAINS" if hits else "FAIL")
    print(f"  {r['id']:4} [{kind}] {v:10} {d:6.1f}s  tools={tc}  {q[:34]}")
    if kind=="OUT": print(f"        A: {a[:90]}")

print("="*64)
print(f"OUT-OF-SCOPE refusal: {ref_ok}/{ref_tot}   (was 5/8; u01 was 290s)")
print(f"ANSWERABLE pass:      {ans_ok}/{ans_tot}")
print(f"LATENCY: mean={statistics.mean(durs):.1f}s  median={statistics.median(durs):.1f}s  max={max(durs):.1f}s")
print(f"  out-of-scope only: mean={statistics.mean(durs[:ref_tot]):.1f}s  max={max(durs[:ref_tot]):.1f}s")
