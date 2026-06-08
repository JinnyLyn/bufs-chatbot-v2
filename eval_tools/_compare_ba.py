"""Fair before/after: re-score both combined88 runs with the corrected scorer."""
import json, re, sys
from collections import defaultdict
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

REFUSAL = ["없습니다", "없음", "불가", "확인할 수 없", "찾을 수 없", "포함되어 있지 않",
           "직접 확인", "명시되어 있지 않", "알 수 없", "제공되지 않", "찾지 못", "확인되지 않"]

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
        n = int(fact)
        if re.search(r"(?<!\d)" + fact + r"(?!\d)", a.replace(",", "")): return True
        if 13 <= n <= 23 and (f"오후 {n-12}시" in a or f"오후{n-12}시" in a): return True
        return False
    if re.fullmatch(r"\d{1,2}:\d{2}", fact):
        h, mi = fact.split(":")
        return any(v in a for v in [fact, f"{int(h):02d}:{mi}", f"{h}시 {int(mi)}분", f"{h}시{int(mi)}분"])
    if re.fullmatch(r"\d{1,2}월\d{1,2}일", fact): return fact in a.replace(" ", "")
    return fact in a

def is_refusal(a): return any(x in a for x in REFUSAL)

def score(path):
    res = json.load(open(path, encoding="utf-8"))["results"]
    verd = {}; cont = strict = atot = refok = rtot = 0
    by = defaultdict(lambda: [0, 0])
    for r in res:
        if r.get("answerable"):
            atot += 1; gt, a = r["ground_truth"], r["answer"]
            facts = extract_facts(gt); hits = [f for f in facts if matched(f, a)]
            if facts: full, some = len(hits) == len(facts), len(hits) > 0
            else:
                gt_t = set(re.findall(r"[가-힣A-Za-z]+", gt)); a_t = set(re.findall(r"[가-힣A-Za-z]+", a))
                ov = len(gt_t & a_t) / max(1, len(gt_t)); full, some = ov >= 0.6, ov >= 0.3
            cont += some; strict += full
            by[r["intent"]][0] += some; by[r["intent"]][1] += 1
            verd[r["id"]] = "PASS" if full else ("CONTAINS" if some else "FAIL")
        else:
            rtot += 1; ok = is_refusal(r["answer"]); refok += ok
            verd[r["id"]] = "REFUSE_OK" if ok else "HALLUC"
    return dict(contains=cont, strict=strict, atot=atot, refok=refok, rtot=rtot, by=by, verd=verd)

B = score("logs/combined88_before_fix.json")
A = score("logs/combined88_new_result.json")
print("=== BEFORE -> AFTER (corrected scorer) ===")
print(f"  answerable contains: {B['contains']}/{B['atot']} ({B['contains']/B['atot']*100:.1f}%)  ->  {A['contains']}/{A['atot']} ({A['contains']/A['atot']*100:.1f}%)   [bufs 80.3%]")
print(f"  answerable strict  : {B['strict']}/{B['atot']} ({B['strict']/B['atot']*100:.1f}%)  ->  {A['strict']}/{A['atot']} ({A['strict']/A['atot']*100:.1f}%)")
print(f"  refusal (8 unans)  : {B['refok']}/{B['rtot']} ({B['refok']/B['rtot']*100:.0f}%)  ->  {A['refok']}/{A['rtot']} ({A['refok']/A['rtot']*100:.0f}%)   [bufs 50%]")
print("\n=== intent contains: before -> after ===")
for it in sorted(set(B['by']) | set(A['by']), key=lambda x: -A['by'].get(x, [0, 0])[1]):
    bo, bt = B['by'].get(it, [0, 0]); ao, at = A['by'].get(it, [0, 0])
    print(f"  {it:16} {bo}/{bt} -> {ao}/{at}")
print("\n=== flips ===")
up = [i for i in A['verd'] if A['verd'][i] in ('PASS','CONTAINS','REFUSE_OK') and B['verd'].get(i) in ('FAIL','HALLUC')]
down = [i for i in A['verd'] if A['verd'][i] in ('FAIL','HALLUC') and B['verd'].get(i) in ('PASS','CONTAINS','REFUSE_OK')]
print(f"  improved ({len(up)}): {sorted(up)}")
print(f"  regressed ({len(down)}): {sorted(down)}")
