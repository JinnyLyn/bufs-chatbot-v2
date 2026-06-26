"""Build the completed QA dataset: fill Gold Chunk ID, Retrieval Success,
Generation Success. Apply the 4 defect fixes. Emit CSV + Markdown + summary.

- Generation Success: manual semantic verdicts (keyword matching proved unreliable
  in both directions, so each answer was read and judged against the expected answer).
- Retrieval Success: doc-level — did the bot retrieve the KB document that holds the
  answer (gold doc resolved from the friendly name + a few corrected/vague mappings).
- Gold Chunk ID: the parent chunk in the resolved gold doc whose text best covers the
  must_include anchors (parent-store id, e.g. 2026학년도1학기학사안내_parent_31).
"""
import csv
import glob
import json
import os
import re
import sys

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
PARENT_DIR = os.path.join(REPO, "parent_store")

rows = json.load(open(os.path.join(HERE, "qa_dataset.json"), encoding="utf-8"))
live = {}
for line in open(os.path.join(HERE, "live_runs.jsonl"), encoding="utf-8"):
    if line.strip():
        d = json.loads(line)
        live[d["id"]] = d

# ---- 1) defect fixes (honest-eval: only KB-ungrounded or factually-wrong rows) ----
FIXES = {
    14: {  # original EXP had the year order backwards (lower years register first)
        "expected_answer": "졸업예정자에게 별도의 수강신청 우선권은 없습니다. 학년별 신청일이 달라 "
                            "1학년(2.9)→2학년(2.10)→3·4학년(2.11)→전 학년(2.12) 순으로 진행되며, "
                            "본인 학년의 지정일에 신청해야 합니다.",
        "must_include": ["우선권", "학년별"],
        "must_not_include": [],
        "_why": "원문 기대답변이 '3·4학년이 1·2학년보다 먼저'라 했으나 학사안내 일정표상 1학년이 먼저(2.9) 신청 → 방향 오류 수정",
    },
    56: {  # KB has no self-service '비밀번호 찾기'; grounded answer is to contact 학사지원팀
        "expected_answer": "학생포털 비밀번호를 분실했다면 교무처 학사지원팀에 문의하여 재설정·복구 절차를 "
                           "안내받아야 합니다. (초기 비밀번호는 생년월일 6자리이며 로그인 후 반드시 변경)",
        "must_include": ["학사지원팀", "재설정"],
        "must_not_include": [],
        "_why": "KB에 '비밀번호 찾기' 셀프 기능 없음 → 학사지원팀 문의로 재근거화",
    },
    74: {  # KB has no '인터넷 증명발급' service
        "expected_answer": "영문 졸업예정증명서 발급 방법은 제공된 학사 문서에 명시되어 있지 않으므로, "
                           "학사지원팀(또는 국제교류처)에 문의하여 발급 절차를 확인해야 합니다.",
        "must_include": ["졸업예정증명서", "문의"],
        "must_not_include": [],
        "_why": "KB에 '인터넷 증명발급' 미수록 → 담당부서 문의로 재근거화",
    },
    97: {  # KB has no 재학증명서 / 인터넷 증명발급
        "expected_answer": "재학증명서 발급 절차는 제공된 학사 문서에 포함되어 있지 않으므로, "
                           "학사지원팀 등 담당 부서에 문의하거나 학교 홈페이지 공지를 확인해야 합니다.",
        "must_include": ["재학증명서", "문의"],
        "must_not_include": [],
        "_why": "KB에 재학증명서/인터넷 증명발급 미수록 → 담당부서 문의로 재근거화",
    },
}
for r in rows:
    if r["id"] in FIXES:
        f = FIXES[r["id"]]
        r["expected_answer"] = f["expected_answer"]
        r["must_include"] = f["must_include"]
        r["must_not_include"] = f["must_not_include"]
# persist fixed source dataset
json.dump(rows, open(os.path.join(HERE, "qa_dataset.json"), "w", encoding="utf-8"),
          ensure_ascii=False, indent=2)

# ---- 2) resolve gold doc (KB file stem) per row ----
FRIENDLY = {
    "2026학년도 1학기 학사안내": "2026학년도1학기학사안내",
    "재학생 및 복학생 등록 안내": "부산외국어대학교 학생포털시스템-재학생 및 복학생 등록 안내",
    "공인결석 신청 매뉴얼": "[학생] 공인결석 신청 매뉴얼 24.8.19.",
    "등록금 분할납부 안내": "부산외국어대학교 학생포털시스템-등록금 분할납부 안내 (4)",
    "수업연한초과자 등록 안내": "부산외국어대학교 학생포털시스템-수업연한초과자(9학기 이상) 등록 안내",
    "학부 등록금 안내": "부산외국어대학교 학생포털시스템-학부 등록금 안내(24학번 이후)",
    "학생포털시스템": "학생포털시스템 매뉴얼학부생",
    "학생포털시스템 등록 안내": "부산외국어대학교 학생포털시스템-재학생 및 복학생 등록 안내",
    "학사공지": None,
    "장학공지": None,
}
OVERRIDES = {  # vague names / mis-assigned gold docs corrected to the doc that holds the answer
    38: "학생포털시스템 매뉴얼학부생",        # 군/일반 휴학 신청 (gold said 공인결석 매뉴얼)
    43: "학생포털시스템 매뉴얼학부생",        # 휴학 승인 확인 (학적변동)
    45: "부산외국어대학교 모바일 학생증 사용 안내",
    48: "2026학년도1학기학사안내",
    54: "학생포털시스템 매뉴얼학부생",        # 복학 승인
    56: "2026-1 수강신청 매뉴얼재학생",       # 비밀번호 안내가 있는 문서
    58: None,                                  # 복학생 장학금 신청: KB 미수록
    74: "2026학년도1학기학사안내",            # 졸업예정증명서 언급
    82: "학생포털시스템 매뉴얼학부생",        # 교육비납입증명서 출력
    95: "부산외국어대학교 모바일 학생증 사용 안내",
    97: "학생포털시스템 매뉴얼학부생",
}


def gold_stem(r):
    if r["id"] in OVERRIDES:
        return OVERRIDES[r["id"]]
    return FRIENDLY.get(r["gold_document"])


# ---- 3) manual generation verdicts (answers were read individually) ----
GEN_OK = {1, 3, 4, 11, 29, 30, 35, 37, 41, 47, 48, 51, 55, 56, 63, 65, 69, 74,
          76, 77, 81, 85, 86, 95, 97, 99, 100}

# ---- helpers for gold chunk pick ----
def norm(s):
    return re.sub(r"\s+", "", s or "")


def mi_match(phrase, ntext):
    parts = [norm(p) for p in phrase.split() if norm(p)]
    return all(p in ntext for p in parts)


PCACHE = {}
for f in glob.glob(os.path.join(PARENT_DIR, "*.json")):
    pid = os.path.basename(f)[:-5]
    doc = re.sub(r"_parent_\d+$", "", pid)
    d = json.load(open(f, encoding="utf-8"))
    PCACHE.setdefault(doc, []).append((pid, norm(d.get("page_content", ""))))


def pick_chunk(stem, must_include):
    cands = PCACHE.get(stem)
    if not cands:
        return ""
    best, bh = cands[0][0], -1
    for pid, ntext in cands:
        h = sum(1 for p in must_include if mi_match(p, ntext))
        if h > bh:
            bh, best = h, pid
    return best


def src_stem(s):
    return re.sub(r"\.(pdf|md)$", "", s or "", flags=re.I)


# ---- 4) assemble ----
out = []
ret_ok = gen_ok = 0
for r in rows:
    rid = r["id"]
    lv = live.get(rid, {})
    rsrcs = sorted({src_stem(x.get("source", "")) for x in lv.get("results", [])})
    stem = gold_stem(r)
    chunk = pick_chunk(stem, r["must_include"]) if stem else ""
    gold_chunk_id = chunk or ("N/A (KB 미수록)" if not stem else f"{stem} (전체)")
    retrieval = bool(stem and stem in rsrcs)
    generation = rid in GEN_OK
    ret_ok += retrieval
    gen_ok += generation
    out.append({
        "id": rid, "question": r["question"], "gold_intent": r["gold_intent"],
        "gold_document": r["gold_document"], "resolved_kb_doc": stem or "—",
        "gold_chunk_id": gold_chunk_id, "expected_answer": r["expected_answer"],
        "must_include": " / ".join(r["must_include"]),
        "must_not_include": " / ".join(r["must_not_include"]),
        "difficulty": r["difficulty"], "category": r["category"],
        "retrieval_success": "O" if retrieval else "X",
        "generation_success": "O" if generation else "X",
    })

# ---- 5) emit CSV ----
cols = ["id", "question", "gold_intent", "gold_document", "resolved_kb_doc",
        "gold_chunk_id", "expected_answer", "must_include", "must_not_include",
        "difficulty", "category", "retrieval_success", "generation_success"]
with open(os.path.join(HERE, "qa_completed.csv"), "w", encoding="utf-8-sig", newline="") as fh:
    w = csv.DictWriter(fh, fieldnames=cols)
    w.writeheader()
    w.writerows(out)

n = len(rows)
print(f"Retrieval Success : {ret_ok}/{n} = {ret_ok/n:.0%}")
print(f"Generation Success: {gen_ok}/{n} = {gen_ok/n:.0%}")

# per-category breakdown
from collections import defaultdict
cat = defaultdict(lambda: [0, 0, 0])
for o, r in zip(out, rows):
    c = cat[r["category"]]
    c[0] += 1
    c[1] += o["retrieval_success"] == "O"
    c[2] += o["generation_success"] == "O"
print("\ncategory            n   ret   gen")
for k, (t, rr, gg) in sorted(cat.items(), key=lambda x: -x[1][0]):
    print(f"  {k:12} {t:3}  {rr:3}/{t:<3} {gg:3}/{t}")

print("\nFixed rows:", sorted(FIXES))
json.dump(out, open(os.path.join(HERE, "qa_completed.json"), "w", encoding="utf-8"),
          ensure_ascii=False, indent=2)
print("wrote qa/qa_completed.csv, qa/qa_completed.json")
