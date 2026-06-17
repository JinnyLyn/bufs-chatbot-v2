"""Aggregate combined88 e2e results across sparse-tokenizer variants into one table.

Applies the corrected scorer (eval_tools/_rescore88.py logic: fact-presence judging that
does NOT mis-flag a correct answer as a refusal just because it contains words like
'불가능', plus single-letter grades and 24h/12h time equivalence) uniformly to every
variant's snapshot (logs/combined88_<label>.json), so the comparison is apples-to-apples.

Reports per variant: answerable contains-rate & strict-rate, unanswerable correct-refusal
rate, and answer latency (median/mean seconds).

    python eval_tools/_aggregate_variants.py
"""
import glob
import json
import os
import re
import statistics
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

LOGDIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs")

# Order + labels for the table.
ORDER = ["V0_bm25_noidf", "V1_bm25_idf", "V2_kiwi_idf", "V3_okt_idf", "V4_bm42_idf", "V5_bm42_kiwi_idf"]

# Corrected refusal markers — only used for UNANSWERABLE items (a correct refusal must say
# it can't find the info). Answerable items are judged purely on fact presence.
REFUSAL = ["없습니다", "없음", "불가", "확인할 수 없", "찾을 수 없", "포함되어 있지 않",
           "직접 확인", "명시되어 있지 않", "알 수 없", "제공되지 않", "찾지 못"]


def extract_facts(gt):  # from _rescore88.py
    facts, s = set(), gt
    for pat in [r"\d{1,2}월\s?\d{1,2}일", r"\d{1,2}:\d{2}"]:
        for m in re.findall(pat, s):
            facts.add(m.replace(" ", ""))
        s = re.sub(pat, " ", s)
    for m in re.findall(r"\d{1,2}\.\d{1,2}", s):
        mo, da = m.split("."); facts.add(f"{int(mo)}월{int(da)}일")
    s = re.sub(r"\d{1,2}\.\d{1,2}", " ", s)
    for m in re.findall(r"(?<![A-Za-z])[A-F][+-]?(?![A-Za-z])", s):
        facts.add(m)
    for m in re.findall(r"\d[\d,]*", s):
        n = m.replace(",", "")
        if re.fullmatch(r"(19|20)\d\d", n):
            continue
        facts.add(n)
    return facts


def matched(fact, a):  # from _rescore88.py
    if re.fullmatch(r"\d+", fact):
        n = int(fact)
        if re.search(r"(?<!\d)" + fact + r"(?!\d)", a.replace(",", "")):
            return True
        if 13 <= n <= 23 and (f"오후 {n-12}시" in a or f"오후{n-12}시" in a):
            return True
        return False
    if re.fullmatch(r"\d{1,2}:\d{2}", fact):
        h, mi = fact.split(":")
        return any(v in a for v in [fact, f"{int(h):02d}:{mi}", f"{h}시 {int(mi)}분", f"{h}시{int(mi)}분"])
    if re.fullmatch(r"\d{1,2}월\d{1,2}일", fact):
        return fact in a.replace(" ", "")
    return fact in a


def is_refusal(a):
    return any(x in a for x in REFUSAL)


def score(path):
    rows = json.load(open(path, encoding="utf-8"))["results"]
    a_tot = a_contains = a_strict = r_tot = r_ok = 0
    durs = []
    for r in rows:
        d = r.get("duration_ms")
        if isinstance(d, (int, float)):
            durs.append(d / 1000.0)
        ans = r.get("answer", "") or ""
        if r.get("answerable"):
            a_tot += 1
            facts = extract_facts(r.get("ground_truth", ""))
            if facts:
                hits = [f for f in facts if matched(f, ans)]
                full, some = len(hits) == len(facts), len(hits) > 0
            else:
                gt_t = set(re.findall(r"[가-힣A-Za-z]+", r.get("ground_truth", "")))
                a_t = set(re.findall(r"[가-힣A-Za-z]+", ans))
                ov = len(gt_t & a_t) / max(1, len(gt_t)); full, some = ov >= 0.6, ov >= 0.3
            a_strict += int(full)
            a_contains += int(some)
        else:
            r_tot += 1
            r_ok += int(is_refusal(ans))
    return {
        "a_tot": a_tot, "contains": a_contains, "strict": a_strict,
        "r_tot": r_tot, "r_ok": r_ok,
        "med_s": round(statistics.median(durs), 1) if durs else None,
        "mean_s": round(statistics.mean(durs), 1) if durs else None,
    }


def main():
    found = {}
    for path in glob.glob(os.path.join(LOGDIR, "combined88_*.json")):
        label = os.path.basename(path)[len("combined88_"):-len(".json")]
        if label in ("new_result",):
            continue
        found[label] = score(path)

    labels = [l for l in ORDER if l in found] + [l for l in sorted(found) if l not in ORDER]
    if not labels:
        print("no combined88_<label>.json snapshots found in", LOGDIR)
        return

    print(f"{'variant':16} {'contains':>14} {'strict':>13} {'refusal':>12} {'med_s':>7} {'mean_s':>7}")
    print("-" * 76)
    for l in labels:
        s = found[l]
        c = f"{s['contains']}/{s['a_tot']} ({s['contains']/max(1,s['a_tot'])*100:.1f}%)"
        st = f"{s['strict']}/{s['a_tot']} ({s['strict']/max(1,s['a_tot'])*100:.1f}%)"
        rf = f"{s['r_ok']}/{s['r_tot']} ({s['r_ok']/max(1,s['r_tot'])*100:.0f}%)"
        print(f"{l:16} {c:>14} {st:>13} {rf:>12} {str(s['med_s']):>7} {str(s['mean_s']):>7}")
    print("\n(corrected scorer: answerable judged on fact-presence; refusal markers only for unanswerable)")


if __name__ == "__main__":
    main()
