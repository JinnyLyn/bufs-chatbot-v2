"""Per-question, per-STAGE failure attribution for the combined88 eval — "unit-test each
stage". Reads logs/combined88_rich.jsonl (which carries the retrieved `context_text` and the
final `answer`), the 8-doc KB corpus, and (best-effort) Langfuse rewrite_query outputs, then
for every wrong answer decides WHICH stage broke:

  REWRITE_CLARIFY   rewrite_query wrongly judged the question unclear → asked to clarify,
                    never searched (pure rewrite-stage bug).
  KB_COVERAGE       the answer's key fact is in NONE of the 8 KB docs → not a pipeline bug,
                    the source document was dropped from the KB.
  RETRIEVAL_ERR     fact IS in the KB corpus but was NOT in the retrieved context. Split into
                      REWRITE_LOSSY  the rewritten query dropped the question's key term, or
                      SEARCH_ERR     query was fine but search/ranking missed it.
  GENERATION_ERR    fact WAS in the retrieved context but not in the answer. If the answer
                    also refused → FALSE_REFUSAL.

Run from the repo root that hosts the live KB (needs project/.env for Langfuse +
markdown_docs/ corpus):
  cd <repo_root>
  python eval_tools/_stage_attribution.py
"""
import os, re, sys, json, glob, time
from collections import Counter, defaultdict
import requests
from dotenv import load_dotenv
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
load_dotenv("project/.env")

RICH = r"C:\Users\suhwa\Desktop\agentic-rag-for-dummies-main\logs\combined88_rich.jsonl"
CORPUS_DIR = r"C:\Users\suhwa\Desktop\agentic-rag-for-dummies-main\markdown_docs"
OUT = r"C:\Users\suhwa\Desktop\agentic-rag-for-dummies-main\logs\stage_attribution.json"

BASE = os.environ.get("LANGFUSE_BASE_URL", "").rstrip("/")
PK, SK = os.environ.get("LANGFUSE_PUBLIC_KEY"), os.environ.get("LANGFUSE_SECRET_KEY")
CA = os.environ.get("REQUESTS_CA_BUNDLE")

CLARIFY_HINTS = ["추가 정보가 필요", "구체적으로", "어떤 것을 말씀", "질문을 정확히", "다시 말씀",
                 "무엇을 알고 싶", "명확히 해", "어느 것", "정확히 이해"]


def extract_facts(gt):
    facts, s = set(), gt
    for pat in [r"\d{1,2}월\s?\d{1,2}일", r"\d{1,2}:\d{2}"]:
        for m in re.findall(pat, s): facts.add(m.replace(" ", ""))
        s = re.sub(pat, " ", s)
    for m in re.findall(r"\d{1,2}\.\d{1,2}", s):
        mo, da = m.split("."); facts.add(f"{int(mo)}월{int(da)}일")
    s = re.sub(r"\d{1,2}\.\d{1,2}", " ", s)
    for m in re.findall(r"[A-F]\+", s): facts.add(m)
    for m in re.findall(r"\d[\d,]*", s):
        n = m.replace(",", "")
        if re.fullmatch(r"(19|20)\d\d", n): continue
        facts.add(n)
    return facts


def matched(fact, text):
    if not text: return False
    if re.fullmatch(r"\d{1,2}월\d{1,2}일", fact):
        # 학사일정 tables store a date as split 월|일 cells, e.g. "| 6 | 8(월) ~ 12 |"
        # or as "6/8" / "6.8" — recognize those forms too, not just contiguous "6월8일".
        mo, da = re.match(r"(\d{1,2})월(\d{1,2})일", fact).groups()
        mo_i, da_i = int(mo), int(da)
        nospace = text.replace(" ", "")
        if f"{mo}월{da}일" in nospace: return True
        if re.search(rf"\b{mo_i}\s*[/.]\s*{da_i}\b", text): return True            # 6/8 or 6.8
        # table cells: month value, then the day value (optionally "(요일)") in the next cell
        if re.search(rf"\|\s*{mo_i}\s*\|\s*{da_i}\b", text): return True            # | 6 | 8...
        if re.search(rf"\b{mo_i}\s*월[^|]*?\|\s*{da_i}\b", text): return True
        return False
    if re.fullmatch(r"\d+", fact):
        return re.search(r"(?<!\d)" + fact + r"(?!\d)", text.replace(",", "")) is not None
    if re.fullmatch(r"\d{1,2}:\d{2}", fact):
        h, mi = fact.split(":")
        return any(v in text for v in [fact, f"{int(h):02d}:{mi}", f"{h}시 {int(mi)}분", f"{h}시{int(mi)}분"])
    return fact in text


def key_terms(q):
    """Distinctive Korean content tokens (>=2 chars), minus generic question words."""
    stop = {"언제", "어디", "무엇", "얼마", "어떻게", "어떤", "인가", "인가요", "무슨", "있나요",
            "알려줘", "되나요", "하나요", "什么", "기간", "경우"}
    toks = re.findall(r"[가-힣A-Za-z]{2,}", q)
    return {t for t in toks if t not in stop}


# ── Langfuse rewrite_query map: originalQuery -> {rewritten, is_clear} (best-effort) ──
def fetch_rewrites():
    out = {}
    if not (BASE and PK and SK):
        return out
    try:
        for page in range(1, 9):
            r = requests.get(BASE + "/api/public/observations",
                             params={"limit": 50, "page": page, "name": "rewrite_query"},
                             auth=(PK, SK), timeout=60, verify=CA)
            if r.status_code != 200: break
            data = r.json().get("data", [])
            if not data: break
            for o in data:
                outp = o.get("output")
                if isinstance(outp, dict) and outp.get("originalQuery"):
                    oq = outp["originalQuery"].strip()
                    # keep the most recent (observations come newest-first per page)
                    out.setdefault(oq, {"rewritten": outp.get("rewrittenQuestions") or [],
                                        "is_clear": outp.get("questionIsClear", True)})
        time.sleep(0)
    except Exception as e:
        print(f"(Langfuse rewrite fetch skipped: {e})", file=sys.stderr)
    return out


def main():
    rows = [json.loads(l) for l in open(RICH, encoding="utf-8")]
    corpus = ""
    for f in glob.glob(os.path.join(CORPUS_DIR, "*.md")):
        corpus += "\n" + open(f, encoding="utf-8").read()
    rewrites = fetch_rewrites()
    print(f"loaded {len(rows)} eval rows, corpus {len(corpus)} chars, {len(rewrites)} rewrite traces\n" + "=" * 78)

    cat = Counter(); by_intent = defaultdict(Counter); detail = []
    ans_rows = [r for r in rows if r.get("answerable")]
    for r in ans_rows:
        q, ans, ctx = r["question"], r.get("answer", ""), r.get("context_text", "")
        facts = set(r.get("facts") or extract_facts(r.get("ground_truth", "")))
        if not facts:
            stage = "CORRECT" if r["verdict"] in ("PASS", "CONTAINS") else "NO_FACT"
            cat[stage] += 1; by_intent[r.get("intent")][stage] += 1
            continue
        in_ans = {f for f in facts if matched(f, ans)}
        in_ctx = {f for f in facts if matched(f, ctx)}
        in_corp = {f for f in facts if matched(f, corpus)}
        missing = facts - in_ans

        rw = rewrites.get(q.strip())
        is_clarify = (rw and not rw.get("is_clear", True)) or \
                     (len(ans) < 120 and any(h in ans for h in CLARIFY_HINTS))

        if not missing:
            stage, sub = "CORRECT", ""
        elif is_clarify:
            stage, sub = "REWRITE_CLARIFY", ""
        else:
            miss_in_corp = missing & in_corp
            miss_not_corp = missing - in_corp
            miss_in_ctx = missing & in_ctx
            if miss_not_corp and not miss_in_corp:
                stage, sub = "KB_COVERAGE", ""          # all missing facts absent from KB
            elif miss_in_corp - in_ctx:                  # corpus has it, retrieval didn't surface it
                stage = "RETRIEVAL_ERR"
                # rewrite lossy? key term of question dropped from rewritten query
                sub = "SEARCH_ERR"
                if rw and rw.get("rewritten"):
                    rwtext = " ".join(rw["rewritten"])
                    kt = key_terms(q)
                    dropped = {t for t in kt if t not in rwtext}
                    if kt and len(dropped) / len(kt) >= 0.5:
                        sub = "REWRITE_LOSSY"
            elif missing and missing <= in_ctx:          # context had every missing fact
                stage = "GENERATION_ERR"
                sub = "FALSE_REFUSAL" if r.get("refused") else ""
            else:                                        # mixed
                stage = "KB_COVERAGE" if miss_not_corp else "RETRIEVAL_ERR"; sub = "MIXED"

        cat[stage] += 1; by_intent[r.get("intent")][stage] += 1
        if stage != "CORRECT":
            detail.append({"id": r["id"], "intent": r.get("intent"), "gt_source": r.get("gt_source"),
                           "verdict": r["verdict"], "stage": stage, "sub": sub,
                           "question": q, "ground_truth": r.get("ground_truth"),
                           "missing_facts": sorted(missing),
                           "missing_in_corpus": sorted(missing & in_corp),
                           "missing_in_context": sorted(missing & in_ctx),
                           "rewritten": (rw or {}).get("rewritten"),
                           "answer_head": ans[:160]})

    tot = sum(cat.values())
    print(f"STAGE ATTRIBUTION  (answerable={tot})")
    order = ["CORRECT", "REWRITE_CLARIFY", "KB_COVERAGE", "RETRIEVAL_ERR", "GENERATION_ERR", "NO_FACT"]
    for c in order:
        if cat.get(c): print(f"  {c:16} {cat[c]:3}  ({cat[c]/tot*100:4.0f}%)")
    print("\nBY INTENT:")
    for it, cc in sorted(by_intent.items(), key=lambda x: -sum(x[1].values())):
        parts = " ".join(f"{k}={v}" for k, v in cc.items())
        print(f"  {str(it):16} {parts}")
    print("\n" + "=" * 78 + "\nFAILED QUESTIONS (stage | id | why):")
    for d in sorted(detail, key=lambda d: d["stage"]):
        sub = f"/{d['sub']}" if d['sub'] else ""
        print(f"\n  [{d['stage']}{sub}] {d['id']} ({d['intent']}, src={d['gt_source']}) verdict={d['verdict']}")
        print(f"     Q: {d['question'][:64]}")
        print(f"     GT: {str(d['ground_truth'])[:64]}   missing={d['missing_facts']}")
        print(f"     in_corpus={d['missing_in_corpus']}  in_context={d['missing_in_context']}")
        if d["rewritten"]: print(f"     rewritten={d['rewritten']}")
        print(f"     answer: {d['answer_head']}")

    json.dump({"counts": dict(cat), "by_intent": {k: dict(v) for k, v in by_intent.items()}, "detail": detail},
              open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print("\nreport ->", OUT)


if __name__ == "__main__":
    main()
