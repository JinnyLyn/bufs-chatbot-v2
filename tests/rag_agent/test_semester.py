"""Unit tests for semester-aware retrieval scoping (rag_agent/semester.py).

Offline and clock-free: every case pins an explicit `today`, so these never flake when
the real calendar rolls into a new semester.

The regression cases are taken verbatim from the sem2 100-Q eval set — each one is a
question that a naive "explicit 학기 marker wins, hard-filter" design would break.
"""

import datetime as dt

import pytest

from rag_agent import semester as sem

pytestmark = pytest.mark.unit

AUG = dt.date(2026, 8, 3)     # 2026학년도 2학기 (수강신청 기간)
MAY = dt.date(2026, 5, 20)    # 2026학년도 1학기
JAN = dt.date(2027, 1, 15)    # winter break → still 2026학년도 2학기


class _Doc:
    def __init__(self, source):
        self.metadata = {"source": source}

    def __repr__(self):
        return f"<{self.metadata['source']}>"


# --- academic_semester -------------------------------------------------------

@pytest.mark.parametrize("today,expected", [
    (dt.date(2026, 3, 2), (2026, 1)),    # 1학기 개강
    (MAY, (2026, 1)),
    (dt.date(2026, 7, 31), (2026, 1)),   # 여름방학은 아직 1학기 학년도
    (AUG, (2026, 2)),                    # 8월 = 2학기 수강신청 → 이미 2학기
    (dt.date(2026, 8, 31), (2026, 2)),   # 2학기 개강
    (dt.date(2026, 12, 20), (2026, 2)),
    (JAN, (2026, 2)),                    # 1~2월은 직전 학년도의 2학기
    (dt.date(2027, 2, 28), (2026, 2)),
])
def test_academic_semester(today, expected):
    assert sem.academic_semester(today) == expected


# --- target_semester ---------------------------------------------------------

def test_explicit_marker_wins_for_current_academic_year():
    assert sem.target_semester("2026학년도 2학기 개강일은 언제인가요?", AUG) == 2
    assert sem.target_semester("2026학년도 1학기 개강일은 언제인가요?", AUG) == 1


def test_no_marker_falls_back_to_today():
    assert sem.target_semester("신입생PSC세미나 분반은 어떻게 결정되나요?", AUG) == 2
    assert sem.target_semester("신입생PSC세미나 분반은 어떻게 결정되나요?", MAY) == 1


def test_regression_id9_other_academic_year_marker_is_ignored():
    """'2027학년도 1학기' 휴복학 일정은 2026학년도 **2학기** 안내에 실린다.

    리터럴 '1학기'를 믿으면 유일하게 답할 수 있는 문서를 강등시킨다.
    """
    q = "2027학년도 1학기를 위한 온라인 휴학 및 복학 신청은 언제 시작하나요?"
    assert sem.target_semester(q, AUG) == 2


def test_mixed_year_question_pairs_marker_with_its_year():
    """혼합 연도: cur_year가 질문 어딘가에 있어도, 마커에 붙은 학년도가 다르면 무시(#3).

    독립 집합 검사만 하면 '2026학년도'가 아무 데나 있다는 이유로 2025년의 '1학기'를 믿어버린다.
    """
    q = "2025학년도 1학기에 휴학했는데 2026학년도에 복학하려면 언제 신청해야 하나요?"
    assert sem.target_semester(q, AUG) == 2


def test_marker_paired_with_current_year_wins_despite_other_years():
    q = "2025학년도에 입학했는데 2026학년도 1학기 재수강 신청은 언제인가요?"
    assert sem.target_semester(q, AUG) == 1


def test_unpaired_marker_with_only_other_years_defers_to_today():
    q = "2027학년도 신입생인데, 1학기 수강신청 절차가 궁금해요"
    assert sem.target_semester(q, AUG) == 2


def test_regression_id27_전기_is_not_a_semester_marker():
    """'전기 학위수여식' = 2월 졸업식이지 1학기가 아니다."""
    q = "2026학년도 전기 학위수여식(졸업식) 날짜는?"
    assert sem.target_semester(q, AUG) == 2


def test_regression_id63_both_markers_fall_back_to_today():
    q = "2학기에 '글로벌소통역량' 교과목을 1학기 레벨 1에서 2로 바꾸어 신청해야 하나요?"
    assert sem.target_semester(q, AUG) == 2


def test_marker_with_matching_academic_year_is_trusted():
    q = "2026학년도 1학기 중간고사 기간은?"
    assert sem.target_semester(q, AUG) == 1


@pytest.mark.parametrize("q", ["", None])
def test_empty_question_is_safe(q):
    assert sem.target_semester(q, AUG) == 2


# --- source_semester ---------------------------------------------------------

@pytest.mark.parametrize("source,expected", [
    ("2026학년도2학기학사안내.pdf", 2),
    ("2026학년도1학기학사안내.pdf", 1),
    ("2026학년도 1학기 수업시간표.pdf", 1),
    ("2026년 1학기 국가근로장학금 사전교육 자료.pdf", 1),
    ("2026-1 수강신청 매뉴얼재학생.pdf", 1),
    # semester-neutral → must never be demoted
    ("[학생] 공인결석 신청 매뉴얼 24.8.19..pdf", None),
    ("부산외국어대학교 모바일 학생증 사용 안내.pdf", None),
    ("수강신청 FAQ.pdf", None),
    ("glossary.md", None),
    ("", None),
])
def test_source_semester(source, expected):
    assert sem.source_semester(source) == expected


# --- demote_other_semesters --------------------------------------------------

def test_demotion_keeps_target_and_neutral_ahead_of_other():
    docs = [
        _Doc("2026학년도1학기학사안내.pdf"),
        _Doc("2026학년도2학기학사안내.pdf"),
        _Doc("수강신청 FAQ.pdf"),
        _Doc("2026학년도 1학기 수업시간표.pdf"),
    ]
    out = sem.demote_other_semesters(docs, target=2)
    assert [d.metadata["source"] for d in out] == [
        "2026학년도2학기학사안내.pdf",
        "수강신청 FAQ.pdf",
        "2026학년도1학기학사안내.pdf",
        "2026학년도 1학기 수업시간표.pdf",
    ]


def test_demotion_is_stable_within_groups():
    docs = [_Doc("2026학년도2학기학사안내.pdf"), _Doc("glossary.md"),
            _Doc("2026학년도2학기학사안내.pdf")]
    out = sem.demote_other_semesters(docs, target=2)
    assert out == docs  # nothing to demote → original order untouched


def test_demotion_never_drops_documents():
    """강등이지 삭제가 아니다 — 다른 학기 문서만 있어도 결과가 비지 않는다."""
    docs = [_Doc("2026학년도1학기학사안내.pdf"), _Doc("2026학년도 1학기 수업시간표.pdf")]
    out = sem.demote_other_semesters(docs, target=2)
    assert len(out) == len(docs)
    assert set(map(id, out)) == set(map(id, docs))


def test_doc_without_metadata_is_treated_as_neutral():
    class Bare:
        metadata = None
    docs = [_Doc("2026학년도1학기학사안내.pdf"), Bare()]
    out = sem.demote_other_semesters(docs, target=2)
    assert isinstance(out[0], Bare)  # neutral outranks the wrong semester


# --- select_semester_scoped (#178) -------------------------------------------

SEM1 = "2026학년도1학기학사안내.pdf"
SEM2 = "2026학년도2학기학사안내.md"
NEUT = "공인결석 신청 매뉴얼.pdf"


def _scored(*pairs):
    return [(_Doc(src), score) for src, score in pairs]


def test_probe_reproduction_deep_pool_promotes_same_semester():
    """#178 재현 케이스: threshold 선컷이면 pool=[2,2,1,1]에서 승격 불가였던 상황.

    깊은 pool(0.0 fetch)에는 threshold 미달 2학기 후보가 살아 있고, 강등 1건당
    1건씩 승격되므로 top-5가 같은-학기 우선으로 재구성된다. 강등분(threshold
    통과 1학기)은 삭제가 아니라 뒤로 밀려 백필된다.
    """
    pool = _scored(
        (SEM2, 0.50), (SEM1, 0.45), (SEM2, 0.40), (SEM1, 0.35),   # ← 기존 pool 전부
        (SEM2, 0.25), (SEM2, 0.20), (NEUT, 0.15), (SEM2, 0.10),   # ← threshold 미달 잔여
    )
    out = sem.select_semester_scoped(pool, target=2, limit=5, score_threshold=0.3)
    srcs = [d.metadata["source"] for d in out]
    # 강등 2건 → 승격 2건: [S2 .5, S2 .4, S2 .25, S2 .2] + 백필 [S1 .45]
    assert srcs == [SEM2, SEM2, SEM2, SEM2, SEM1]


def test_no_demotion_means_no_subthreshold_admission():
    """#178 핵심 가드: 강등 0건이면 threshold 미달 승격도 0건.

    범위 밖 질문(아무것도 threshold를 못 넘는)은 레버 ON에서도 빈 결과 →
    NO_RELEVANT_CHUNKS → 거부 라우팅(edges.py)이 그대로 동작해야 한다.
    """
    pool = _scored((SEM2, 0.25), (NEUT, 0.15), (SEM2, 0.10))
    assert sem.select_semester_scoped(pool, target=2, limit=5, score_threshold=0.3) == []


def test_subthreshold_admissions_capped_by_demotion_count():
    """승격은 강등 1건당 1건 — 빈 슬롯 없이 저품질 청크가 밀려들지 않는다."""
    pool = _scored((SEM2, 0.50), (SEM1, 0.45),
                   (SEM2, 0.25), (SEM2, 0.20), (SEM2, 0.10))
    out = sem.select_semester_scoped(pool, target=2, limit=5, score_threshold=0.3)
    srcs = [d.metadata["source"] for d in out]
    # 강등 1건 → 승격 1건(S2 .25)만. 나머지 미달분은 미채용.
    assert srcs == [SEM2, SEM2, SEM1]


def test_wrong_semester_below_threshold_is_dropped():
    """다른-학기 + threshold 미달 = 이중 실격 — 백필로도 등장하지 않는다."""
    pool = _scored((SEM2, 0.50), (SEM1, 0.20), (SEM1, 0.10))
    out = sem.select_semester_scoped(pool, target=2, limit=5, score_threshold=0.3)
    assert len(out) == 1
    assert out[0].metadata["source"] == SEM2


def test_wrong_semester_above_threshold_backfills():
    """강등이지 삭제가 아니다 — keep 그룹이 limit 미만이면 threshold 통과분이 백필."""
    pool = _scored((SEM2, 0.50), (SEM1, 0.45), (SEM1, 0.40))
    out = sem.select_semester_scoped(pool, target=2, limit=3, score_threshold=0.3)
    assert [d.metadata["source"] for d in out] == [SEM2, SEM1, SEM1]


def test_selection_preserves_retriever_order_across_groups():
    pool = _scored((SEM2, 0.50), (NEUT, 0.35), (SEM1, 0.40), (SEM2, 0.25))
    out = sem.select_semester_scoped(pool, target=2, limit=4, score_threshold=0.3)
    # keep(threshold 통과, 랭킹 순) → 승격분(강등 1건 → 1건) → 백필
    assert [d.metadata["source"] for d in out] == [SEM2, NEUT, SEM2, SEM1]


def test_selection_empty_pool_is_safe():
    assert sem.select_semester_scoped([], target=2, limit=5, score_threshold=0.3) == []
