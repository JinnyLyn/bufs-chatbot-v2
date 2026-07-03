"""Consolidated QA/eval report (Markdown) from a RAGAS+KPI run.

Consumes ``_ragas_kpi.py`` output, reuses ``_error_buckets.classify`` for the 7-bucket
analysis, and emits one Markdown report: KPI headline, KPI × difficulty, KPI × category,
7-bucket table, guard-false-positive list, wrong-answer detail, caveats.

Run::  python eval_tools/_qa_report.py --in logs/ragas_kpi_factual100_latest.json --out logs/QA_REPORT.md
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _error_buckets as eb  # noqa: E402

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def _m(vals):
    v = [x for x in vals if x is not None and x >= 0]
    return round(sum(v) / len(v), 3) if v else None


def _hmean(a, b):
    return round(2 * a * b / (a + b), 3) if (a and b and a + b > 0) else 0.0


def _slice(rows, keyf):
    g = defaultdict(list)
    for r in rows:
        g[keyf(r)].append(r)
    out = {}
    for k, rs in g.items():
        prec, rec = _m([r.get("context_precision") for r in rs]), _m([r.get("context_recall") for r in rs])
        out[k] = {"n": len(rs), "Accuracy": _m([r.get("answer_correctness") for r in rs]),
                  "Precision": prec, "Recall": rec, "F1": _hmean(prec or 0, rec or 0),
                  "Faithfulness": _m([r.get("faithfulness") for r in rs])}
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="inp", required=True)
    ap.add_argument("--out", default=os.path.join(_REPO, "logs", "QA_REPORT.md"))
    ap.add_argument("--parent-store", default=os.path.join(_REPO, "parent_store"))
    args = ap.parse_args()

    with open(args.inp, encoding="utf-8") as fh:
        d = json.load(fh)
    rows, kpi = d.get("results"), d.get("kpi")
    if not rows or not kpi:
        raise SystemExit(f"[error] {args.inp}: results/kpi 키가 없습니다 — _ragas_kpi 출력 파일인지 확인.")
    kb = eb.kb_docs(args.parent_store)
    for r in rows:
        r["_bucket"], r["_reason"], r["_conf"] = eb.classify(r, kb)
    cnt = Counter(r["_bucket"] for r in rows)
    n = len(rows)
    n_correct = sum(cnt[b] for b in eb.CORRECT_BUCKETS)
    n_wrong = n - n_correct - cnt["판정불가"]

    L = [f"# QA/평가 리포트\n",
         f"- 생성: **{d.get('generator')}** · 평가(judge): **{d.get('judge')}** (교차계열, 자기편향 회피)",
         f"- 문항수: **{n}** · 생성일: {time.strftime('%Y-%m-%d %H:%M')}\n",
         "## 1. KPI 헤드라인\n",
         "| 지표 | 값 | RAGAS 매핑 |", "|---|---|---|",
         f"| Accuracy | **{kpi['Accuracy']}** | answer_correctness |",
         f"| Precision | **{kpi['Precision']}** | context_precision |",
         f"| Recall | **{kpi['Recall']}** | context_recall |",
         f"| F1 | **{kpi['F1']}** | P·R 조화평균 |",
         f"| Faithfulness | **{kpi['Faithfulness']}** | 답변 근거성 |",
         f"| (참고) AnswerRelevancy | {kpi['AnswerRelevancy']} | 질문-답변 적합성 |",
         f"\n- doc_recall(문자열매칭)={d.get('doc_recall_rate')} · guard 위반={d.get('guard_violations')}/{n}\n",
         "## 2. KPI × 난이도\n",
         "| 난이도 | n | Accuracy | Precision | Recall | F1 | Faithfulness |", "|---|--:|--:|--:|--:|--:|--:|"]
    diff = _slice(rows, lambda r: r.get("difficulty"))
    for k in ["Easy", "Medium", "Hard"]:
        if k in diff:
            s = diff[k]
            L.append(f"| {k} | {s['n']} | {s['Accuracy']} | {s['Precision']} | {s['Recall']} | {s['F1']} | {s['Faithfulness']} |")

    L += ["\n## 3. KPI × 카테고리\n",
          "| 카테고리 | n | Accuracy | Precision | Recall | F1 | Faithfulness |", "|---|--:|--:|--:|--:|--:|--:|"]
    for k, s in sorted(_slice(rows, lambda r: r.get("category")).items(), key=lambda x: -x[1]["n"]):
        L.append(f"| {k} | {s['n']} | {s['Accuracy']} | {s['Precision']} | {s['Recall']} | {s['F1']} | {s['Faithfulness']} |")

    L += [f"\n## 4. 오답 분석 (7-bucket) — 정답 {n_correct}/{n}, 오답 {n_wrong}\n",
          "| 버킷 | 건수 | 전체% | 오답중% |", "|---|--:|--:|--:|"]
    for b in eb.BUCKET_ORDER:
        if cnt[b] == 0:
            continue
        ofw = f"{cnt[b]/n_wrong*100:.0f}%" if (n_wrong and b not in eb.CORRECT_BUCKETS and b != "판정불가") else "-"
        L.append(f"| {b} | {cnt[b]} | {cnt[b]/n*100:.0f}% | {ofw} |")

    L += ["\n## 5. 오답 상세 (검토용 · ●high ◐med ○low)\n",
          "| id | 버킷 | 신뢰도 | corr | faith | c_recall | 질문 | 판단근거 |", "|--:|---|:-:|--:|--:|--:|---|---|"]
    cf = {"high": "●", "medium": "◐", "low": "○"}
    for r in sorted([r for r in rows if r["_bucket"] not in eb.CORRECT_BUCKETS], key=lambda r: (r["_bucket"], r["id"])):
        L.append(f"| {r['id']} | {r['_bucket']} | {cf.get(r['_conf'],'?')} | {r.get('answer_correctness')} | "
                 f"{r.get('faithfulness')} | {r.get('context_recall')} | {r['question'][:28].replace('|','/')} | "
                 f"{r['_reason'][:60].replace('|','/')} |")

    gtrip = [r for r in rows if r["_bucket"] == "정답(guard 오탐)"]
    if gtrip:
        L += ["\n## 5-1. guard 오탐 — 정답이나 금지어 substring 검출 (distractor 재검토)\n",
              "| id | correctness | must_not_include | 질문 |", "|--:|--:|---|---|"]
        for r in sorted(gtrip, key=lambda r: r["id"]):
            L.append(f"| {r['id']} | {r.get('answer_correctness')} | {r.get('must_not_include')} | {r['question'][:34].replace('|','/')} |")

    L += ["\n## 6. 주의사항\n",
          "- **생성/평가 모델 분리**로 자기편향 회피(요구사항 충족).",
          "- **gold_document 라벨 한계**: 실제 KB 단일문서가 아닌 공지 카테고리(예: 학사공지/장학공지)는 KB 매핑 불가 → '문서 없음/Embedding' 판정 저신뢰(○), 수동 확인 필요.",
          "- Chunk/Embedding 구분은 doc_hit(정규화 매칭) 기반 1차 추정 — 심층 확인은 Langfuse 트레이스(`_answer_analysis.py`) 병행 권장.",
          "- 판정불가 = judge 점수 파싱 실패(재실행 대상)."]

    with open(args.out, "w", encoding="utf-8") as fh:
        fh.write("\n".join(L) + "\n")
    print("report ->", os.path.relpath(args.out, _REPO))
    print("KPI:", kpi)
    print("buckets:", dict(cnt))


if __name__ == "__main__":
    main()
