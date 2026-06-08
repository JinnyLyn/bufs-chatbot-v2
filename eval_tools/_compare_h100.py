"""Compare local-after vs H100 runs (corrected scorer) + H100 latency stats."""
import json, re, sys, statistics
from collections import defaultdict
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
REFUSAL = ["없습니다","없음","불가","확인할 수 없","찾을 수 없","포함되어 있지 않","직접 확인","명시되어 있지 않","알 수 없","제공되지 않","찾지 못","확인되지 않"]

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
        n = int(fact)
        if re.search(r"(?<!\d)" + fact + r"(?!\d)", a.replace(",", "")): return True
        return 13 <= n <= 23 and (f"오후 {n-12}시" in a or f"오후{n-12}시" in a)
    if re.fullmatch(r"\d{1,2}:\d{2}", fact):
        h, mi = fact.split(":"); return any(v in a for v in [fact, f"{int(h):02d}:{mi}", f"{h}시 {int(mi)}분"])
    if re.fullmatch(r"\d{1,2}월\d{1,2}일", fact): return fact in a.replace(" ", "")
    return fact in a

def is_refusal(a): return any(x in a for x in REFUSAL)
def pct(xs, q): xs=sorted(xs); return xs[min(len(xs)-1,int(q*len(xs)))] if xs else 0

def score(path):
    res = json.load(open(path, encoding="utf-8"))["results"]
    cont=strict=atot=refok=rtot=0; by=defaultdict(lambda:[0,0]); verd={}; durs=[]
    for r in res:
        if r.get("duration_ms"): durs.append(r["duration_ms"]/1000)
        if r.get("answerable"):
            atot+=1; gt,a=r["ground_truth"],r["answer"]; facts=extract_facts(gt); hits=[f for f in facts if matched(f,a)]
            if facts: full,some=len(hits)==len(facts),len(hits)>0
            else:
                gt_t=set(re.findall(r"[가-힣A-Za-z]+",gt)); a_t=set(re.findall(r"[가-힣A-Za-z]+",a)); ov=len(gt_t&a_t)/max(1,len(gt_t)); full,some=ov>=.6,ov>=.3
            cont+=some; strict+=full; by[r["intent"]][0]+=some; by[r["intent"]][1]+=1
            verd[r["id"]]="PASS" if full else ("CONTAINS" if some else "FAIL")
        else:
            rtot+=1; ok=is_refusal(r["answer"]); refok+=ok; verd[r["id"]]="REF_OK" if ok else "HALLUC"
    return dict(cont=cont,strict=strict,atot=atot,refok=refok,rtot=rtot,by=by,verd=verd,durs=durs)

L=score("logs/combined88_local_after.json"); H=score("logs/combined88_new_result.json")
print("=== QUALITY (corrected scorer): LOCAL-after  ->  H100 ===")
print(f"  contains : {L['cont']}/{L['atot']} ({L['cont']/L['atot']*100:.1f}%)  ->  {H['cont']}/{H['atot']} ({H['cont']/H['atot']*100:.1f}%)   [bufs 80.3%]")
print(f"  strict   : {L['strict']}/{L['atot']} ({L['strict']/L['atot']*100:.1f}%)  ->  {H['strict']}/{H['atot']} ({H['strict']/H['atot']*100:.1f}%)")
print(f"  refusal  : {L['refok']}/{L['rtot']}  ->  {H['refok']}/{H['rtot']}")
print("\n=== intent contains: LOCAL -> H100 ===")
for it in sorted(set(L['by'])|set(H['by']), key=lambda x:-H['by'].get(x,[0,0])[1]):
    lo,lt=L['by'].get(it,[0,0]); ho,ht=H['by'].get(it,[0,0]); print(f"  {it:18} {lo}/{lt} -> {ho}/{ht}")
print("\n=== flips (LOCAL -> H100) ===")
up=[i for i in H['verd'] if H['verd'][i] in('PASS','CONTAINS','REF_OK') and L['verd'].get(i) in('FAIL','HALLUC')]
dn=[i for i in H['verd'] if H['verd'][i] in('FAIL','HALLUC') and L['verd'].get(i) in('PASS','CONTAINS','REF_OK')]
print(f"  improved({len(up)}): {sorted(up)}\n  regressed({len(dn)}): {sorted(dn)}")
d=H['durs']
print(f"\n=== H100 LATENCY (n={len(d)}):  mean={statistics.mean(d):.1f}s  p50={pct(d,.5):.1f}s  p90={pct(d,.9):.1f}s  p95={pct(d,.95):.1f}s  max={max(d):.1f}s")
print(f"    over-60s: {sum(x>60 for x in d)}   over-100s: {sum(x>100 for x in d)}   (LOCAL was p50~11s p90~31s max~137s)")
