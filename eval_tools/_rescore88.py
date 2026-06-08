"""Re-score the saved combined88 answers with fixed logic (no LLM re-run):
 - answerable items judged purely on fact presence (a correct answer isn't 'refused'
   just because it contains words like '불가능'),
 - single-letter grades extracted (A, C+ ...),
 - 24h/12h time equivalence (17시 == 오후 5시).
Also categorize remaining real fails: date month-mismatch vs other.
"""
import json, re, sys
from collections import defaultdict
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

d = json.load(open("logs/combined88_new_result.json", encoding="utf-8"))["results"]
ans = [r for r in d if r.get("answerable")]


def extract_facts(gt):
    facts, s = set(), gt
    for pat in [r"\d{1,2}월\s?\d{1,2}일", r"\d{1,2}:\d{2}"]:
        for m in re.findall(pat, s): facts.add(m.replace(" ", ""))
        s = re.sub(pat, " ", s)
    for m in re.findall(r"\d{1,2}\.\d{1,2}", s):
        mo, da = m.split("."); facts.add(f"{int(mo)}월{int(da)}일")
    s = re.sub(r"\d{1,2}\.\d{1,2}", " ", s)
    for m in re.findall(r"(?<![A-Za-z])[A-F][+-]?(?![A-Za-z])", s): facts.add(m)  # grades incl single
    for m in re.findall(r"\d[\d,]*", s):
        n = m.replace(",", "")
        if re.fullmatch(r"(19|20)\d\d", n): continue
        facts.add(n)
    return facts


def matched(fact, answer):
    a = answer
    if re.fullmatch(r"\d+", fact):
        n = int(fact)
        if re.search(r"(?<!\d)" + fact + r"(?!\d)", a.replace(",", "")): return True
        if 13 <= n <= 23 and (f"오후 {n-12}시" in a or f"오후{n-12}시" in a): return True  # 24h==12h
        return False
    if re.fullmatch(r"\d{1,2}:\d{2}", fact):
        h, mi = fact.split(":")
        return any(v in a for v in [fact, f"{int(h):02d}:{mi}", f"{h}시 {int(mi)}분", f"{h}시{int(mi)}분"])
    if re.fullmatch(r"\d{1,2}월\d{1,2}일", fact):
        return fact in a.replace(" ", "")
    return fact in a


def months_days(text):
    return set(re.findall(r"(\d{1,2})월\s?(\d{1,2})일", text))


strict = contains = 0
real_fail, month_bug, fixed_artifacts = [], [], []
by = defaultdict(lambda: [0, 0])

for r in ans:
    gt, a = r["ground_truth"], r["answer"]
    facts = extract_facts(gt)
    hits = [f for f in facts if matched(f, a)]
    if facts:
        full, some = len(hits) == len(facts), len(hits) > 0
    else:
        gt_t = set(re.findall(r"[가-힣A-Za-z]+", gt)); a_t = set(re.findall(r"[가-힣A-Za-z]+", a))
        ov = len(gt_t & a_t) / max(1, len(gt_t)); full, some = ov >= 0.6, ov >= 0.3
    strict += full; contains += some
    by[r["intent"]][0] += some; by[r["intent"]][1] += 1
    if r["verdict"] == "FAIL" and some:
        fixed_artifacts.append(r["id"])           # was FAIL, now counts (scoring fix)
    if not some:                                   # still a real miss
        # month bug? same day, different month between GT and answer
        gd, ad = months_days(gt), months_days(a)
        gdays = {d for _, d in gd}; adays = {d for _, d in ad}
        if gd and ad and (gdays & adays) and not (gd & ad):
            month_bug.append((r["id"], r["intent"], gt, a[:80]))
        else:
            real_fail.append((r["id"], r["intent"], gt[:46], a[:90]))

tot = len(ans)
print(f"=== RE-SCORED (answerable={tot}) ===")
print(f"  contains: {contains}/{tot} = {contains/tot*100:.1f}%   (raw was 55/81=67.9%)")
print(f"  strict  : {strict}/{tot} = {strict/tot*100:.1f}%")
print(f"  scoring-artifact fails recovered: {len(fixed_artifacts)} -> {sorted(fixed_artifacts)}")
print(f"\n=== by intent (corrected contains) ===")
for it, (ok, t) in sorted(by.items(), key=lambda x: -x[1][1]):
    print(f"  {it:16} {ok:2}/{t:2} ({ok/t*100:3.0f}%)")
print(f"\n=== systematic DATE MONTH-BUG ({len(month_bug)}) — day right, month wrong ===")
for i, it, gt, a in month_bug:
    print(f"  {i:5} GT={gt[:42]:42} | A={a}")
print(f"\n=== other REAL fails ({len(real_fail)}) ===")
for i, it, gt, a in real_fail:
    print(f"  {i:5} [{it}] GT={gt} | A={a}")
