"""Focused re-test: the combined88 questions that failed due to (1) the calendar month
bug and (2) the graduation-credit summation bug, plus regression checks. Uses the fixed
scorer (no refusal-veto, single grades, 12h/24h time)."""
import json, re, sys, time, urllib.parse
import requests
try: sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception: pass

BASE = "http://localhost:8000"
SRC = r"C:\Users\suhwa\Desktop\bufs-chatbot\reports\retrieval_eval\combined88_results_fix_20260429.json"
# date-month fails | grad-sum fails | regression checks
SELECTED = {
 "s02":"date","q005":"date","q006":"date","q015":"date","q031":"date","q007":"date","s04":"date",
 "g02":"grad","q042":"grad",
 "q001":"reg","q004":"reg","q041":"reg","q050":"reg",
}

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
    if re.fullmatch(r"\d+", fact):
        n=int(fact)
        if re.search(r"(?<!\d)"+fact+r"(?!\d)", a.replace(",","")): return True
        if 13<=n<=23 and (f"오후 {n-12}시" in a or f"오후{n-12}시" in a): return True
        return False
    if re.fullmatch(r"\d{1,2}월\d{1,2}일", fact): return fact in a.replace(" ","")
    return fact in a

def ask(q, timeout=300):
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
npass=0; tot=0
for i,(qid,kind) in enumerate(SELECTED.items(),1):
    r=rows[qid]; gt=r["ground_truth"]
    try: done=ask(r["question"]); ans=done.get("answer","")
    except Exception as e: ans=f"(ERROR {e})"; done={}
    facts=extract_facts(gt); hits=[f for f in facts if matched(f,ans)]
    ok = bool(facts) and len(hits)==len(facts)
    tot+=1; npass+=ok
    print(f"[{i:2}/{len(SELECTED)}] {qid:5} [{kind}] {'PASS' if ok else 'FAIL'}  {len(hits)}/{len(facts)} tools={done.get('tool_calls')}")
    print(f"   GT: {gt}")
    print(f"   A : {ans[:170].replace(chr(10),' ')}")
    print("-"*70)
print(f"FOCUSED: {npass}/{tot} pass")
