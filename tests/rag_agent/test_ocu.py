"""Unit tests for OCU-scoped retrieval (rag_agent/ocu.py + rag_agent/scoping.py).

Chunk fixtures are VERBATIM lines from the 2026 학사안내 guides (offline chunker
run, 2026-09-01) — one per detection pattern and one per incidental-mention class
the detector must NOT flag. If the guides' wording changes, re-run the profile in
the ocu.py module docstring before touching the expectations here.
"""

import pytest

from rag_agent import ocu, scoping

pytestmark = pytest.mark.unit


class _Doc:
    def __init__(self, content, source="2026학년도1학기학사안내.pdf"):
        self.page_content = content
        self.metadata = {"source": source}

    def __repr__(self):
        return f"<{self.page_content[:24]!r}>"


# --- is_ocu_question ---------------------------------------------------------


@pytest.mark.parametrize(
    "q",
    [
        "OCU 개강일은 언제인가요?",
        "ocu 과목 수강신청 방법 알려줘",
        "오씨유 시스템 사용료 얼마야?",
        "열린사이버대학 과목도 학점 인정되나요?",
        "온라인 공동 활용 수업이 뭔가요?",
    ],
)
def test_ocu_questions_pass_the_gate(q):
    assert ocu.is_ocu_question(q)


@pytest.mark.parametrize(
    "q",
    [
        "1학기 개강일 언제야?",
        "1학기 수강신청 기간 알려줘",
        "성적 이의신청 기간은?",
        "최대 몇 학점까지 신청 가능한가요?",
        # 일반적 의미의 공동활용 — 게이트가 열리면 레버가 무력화되므로 절대 매칭 금지 (리뷰 지적)
        "타 대학과 시설 공동활용 협약이 있나요?",
        "교내 시설 공동활용 방안이 궁금해요",
        # Latin word-boundary: the "ocu" trigram inside English words is not a marker.
        "document에서 focus 부분만 알려줘",
        "",
    ],
)
def test_general_questions_do_not_pass_the_gate(q):
    assert not ocu.is_ocu_question(q)


def test_none_question_is_safe():
    assert not ocu.is_ocu_question(None)


# --- is_ocu_chunk: OCU-topic chunks (the contaminators) ----------------------


@pytest.mark.parametrize(
    "line",
    [
        "## 3. OCU 교과목 수강 안내",
        "## 3.  OCU  교과목 수강 안내",  # 2학기 가이드의 이중 공백 표기
        "- 가. OCU 개강일 : 2026.03.02.(월) 오전 10시",
        "- 2 . OCU개강일 : 2026.03.02.(월) 오전 10시부터 수강 가능",
        "##  OCU 수강 안내",
        "- 나. 실시방법 : OCU 컨소시엄 홈페이지 상의 ‘시험’ 메뉴를 이용한 On-line시험 실시",
        "1. OCU시스템 사용료 : 과목당 24,000원",
        "- 1) OCU 홈페이지(http://cons.ocu.ac.kr/)에 반드시 납부자 본인 계정(ID/PW)으로 로그인",
        "학번과 소속 대학의 영문 이니셜을 조합하여 OCU ID 가 일괄 발급됨",
        "- 가. 수강한 교과목 성적은 해당 학기 평점평균에 반영되며, 이수구분은 OCU(자유선택)로 인정됨",
        "|최대수강신청제한<br>학점|실제 수강신청학점<br>(OCU포함)|OCU<br> 수강신청 과목수|시스템사용료|",
        "## 2. 부록 : OCU 개설 교과목 목록",
        "| OCU개설교과목목록   | 부록참고 |",
        "## ※ OCU 과목은 전공, 교양으로 인정 불가하며, OCU(자유선택)로만 학점 인정 가능함.",
        "|문의전화|(OCU) 교무, 학사, 수업운영 : 02-2197-4241|",
        "1. 한국열린사이버대학교(OCU)는 사이버공간을 통한 대학교육 및 학술교류의 대표적인 모범사례",
    ],
)
def test_ocu_topic_chunks_are_flagged(line):
    assert ocu.is_ocu_chunk(_Doc(line))


# --- is_ocu_chunk: incidental mentions in GENERAL sections (must stay put) ---


@pytest.mark.parametrize(
    "line",
    [
        # 최대 신청학점 예외 규정 — 일반 수강신청 질문의 핵심 근거
        "- 라. OCU 수강 신청자 : 최대 신청학점에서 3학점 초과 신청 가능",
        # 학년별 수강신청 대상 표
        "|2.10.(화)|2학년<br>(2~3학기 이수자)|- 교양, 글로벌소통역량<br>- 제1전공·제2전공·마이크로전공<br>- OCU<br>- 교직|",
        # 졸업 학점 인정 한도 (일반 규정)
        "- 자. OCU, 현장실습, 계절학기의 경우 졸업까지 각각 최대 24학점만 인정 가능하니 유의",
        # 성적포기 제외 목록
        "| ▫신청불가교과목 -OCU·교직(EDU)·자유선택(G/YB)·기존P/NP교과목 |",
        # 학사일정 이벤트 (일정 자체는 학기 스코프가 관리)
        "| 3 | 9(월) ~ 13(금) | 군복무 중 OCU 학점인정신청 |",
        "2026학년도 1학기 군복무 중 OCU 학점인정신청: 3월 9일(월) ~ 13일(금)",
        # 수업연한초과자 등록금 기준 (등록 안내 문서)
        "수업연한초과자 수강신청학점별 등록금 납부 기준 (OCU 수강신청 학점 포함)",
        # 용어 사전 표제어 — OCU 질문은 게이트가 열어주고, 일반 질문에서 top-k에 들 일이 없다
        "## OCU (온라인 공동활용 수업)",
        # 부록 과목 목록의 행 — 소속대학 열의 대학명만으로 강등하면 OCU 마커 없는 과목명
        # 질문의 유일한 정답 청크를 밀어낸다 (리뷰 지적사항)
        "|74|인간/사회|사건을중심으로본남북관계론|810761|구원근|한국열린사이버대학교|",
        # 영단어 속 소문자 trigram
        "see the document for details and focus on section 3",
        "",
    ],
)
def test_incidental_mentions_are_not_flagged(line):
    assert not ocu.is_ocu_chunk(_Doc(line))


def test_chunk_without_page_content_is_safe():
    class Bare:
        pass

    assert not ocu.is_ocu_chunk(Bare())


# --- scoping.select_scoped / demote_scoped with the OCU predicate ------------

OCU_개강 = "- 가. OCU 개강일 : 2026.03.02.(월) 오전 10시"
일반_개강 = "2026학년도 1학기 개강: 3월 2일(월)"
일반_수강신청 = "##  수강신청: 2026.2.9.(월) ~ 2.12.(목) 10:00 - 15:20"


def test_reported_bug_shape_ocu_chunk_demoted_below_general():
    """사용자 신고 재현: '1학기 개강일' 질문에서 OCU 개강일 청크가 일반 개강 청크를
    밀어내지 못한다 — 강등되어 뒤로 가고, 일반 청크가 앞자리를 차지한다."""
    pool = [(_Doc(OCU_개강), 0.55), (_Doc(일반_개강), 0.50), (_Doc(일반_수강신청), 0.45)]
    out = scoping.select_scoped(pool, ocu.is_ocu_chunk, limit=2, score_threshold=0.3)
    assert [d.page_content for d in out] == [일반_개강, 일반_수강신청]


def test_demoted_ocu_chunk_backfills_when_it_is_the_only_evidence():
    """강등이지 삭제가 아니다: OCU-주제 청크만 threshold를 넘는 판(예: OCU 규정이 유일한
    근거인 질문)에서는 백필로 그대로 반환된다 — 답을 잃지 않는 것이 핵심."""
    pool = [(_Doc(OCU_개강), 0.6), (_Doc("무관한 청크"), 0.1)]
    out = scoping.select_scoped(pool, ocu.is_ocu_chunk, limit=3, score_threshold=0.3)
    # 강등 1건 → 미달 승격 1건(계약대로), 강등분은 백필로 살아남는다.
    assert any(d.page_content == OCU_개강 for d in out)


def test_one_subthreshold_admission_per_demotion():
    pool = [
        (_Doc(OCU_개강), 0.55),
        (_Doc(일반_개강), 0.50),
        (_Doc(일반_수강신청), 0.25),
        (_Doc("또 다른 미달 청크"), 0.20),
    ]
    out = scoping.select_scoped(pool, ocu.is_ocu_chunk, limit=4, score_threshold=0.3)
    # 강등 1건 → 미달 승격 1건만. OCU 청크는 맨 뒤로 백필.
    assert [d.page_content for d in out] == [일반_개강, 일반_수강신청, OCU_개강]


def test_always_false_predicate_degrades_to_thresholded_topk():
    """레버 ON + OCU 질문(강등 기준 없음): un-scoped 경로와 동일한 결과여야 한다."""
    pool = [(_Doc(OCU_개강), 0.55), (_Doc(일반_개강), 0.25)]
    out = scoping.select_scoped(pool, lambda d: False, limit=5, score_threshold=0.3)
    assert [d.page_content for d in out] == [OCU_개강]


def test_demote_scoped_is_stable_within_groups():
    docs = [_Doc(OCU_개강), _Doc(일반_개강), _Doc(일반_수강신청)]
    out = scoping.demote_scoped(docs, ocu.is_ocu_chunk)
    assert [d.page_content for d in out] == [일반_개강, 일반_수강신청, OCU_개강]
    assert scoping.demote_scoped([], ocu.is_ocu_chunk) == []
