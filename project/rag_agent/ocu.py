"""OCU-scoped retrieval (issue: OCU 교차 오염).

Each 학사안내 guide embeds a full OCU(한국열린사이버대학교 컨소시엄) block — its own
개강일(2026-1: 3.2.(월) 10시), 수강신청 방법, 시스템 사용료(과목당 24,000원), On-line
시험·성적 규정, 부록 개설 과목 목록 — describing the same *kinds* of facts as the
normal curriculum with different values. A general "1학기 개강일/수강신청/성적"
question lexically matches those chunks, and the answer quotes OCU rules the asker
never asked about (user report, 2026-09-01).

Chunk-level profile (offline chunker run over the two 2026 guides, 2026-09-01):
52 of 1,193 child chunks carry the "OCU" token, and they split cleanly in two:

1. **OCU-topic chunks** (the OCU 안내 sections + 부록 headers): OCU is what the
   chunk is *about*, recognizable by topical collocations — "OCU 교과목 수강 안내",
   "OCU 개강일", "OCU 컨소시엄", "OCU시스템 사용료", "OCU(자유선택)", "OCU 홈페이지",
   "cons.ocu.ac.kr" … These are the contaminators.
2. **Incidental mentions inside general sections** — "- 라. OCU 수강 신청자 : 최대
   신청학점에서 3학점 초과 신청 가능", 학년별 수강신청 대상 표의 "- OCU" 항목,
   성적포기 제외 목록의 "OCU·교직(EDU)", 학사일정의 "군복무 중 OCU 학점인정신청" …
   General evidence that must NOT be demoted: a 최대 신청학점 question needs exactly
   these chunks.

So ``is_ocu_chunk`` keys on the topical collocations, never on the bare token.

Child-level detection alone is NOT enough (prod A/B, 2026-09-01): the guides'
OCU chapter uses flat ``##`` subsections ("## 5. 성적평가(상대평가)",
"## 7. 최종성적 확인 및 이의신청" …) whose body text never repeats an OCU
collocation. Those children look fully general, rank for general 성적/수강
questions, and parent expansion then hauls the whole OCU block into the answer
context. ``is_ocu_parent`` is the backstop: tools demotes a child when the child
OR its parent is OCU-topic (``tools._parent_is_ocu``), and nodes skips OCU
parents at expansion time (``nodes._expand_parent_context``) — child evidence
stays in the prompt either way.

Parent verdict is DENSITY-based, not mere presence — MIN_PARENT_SIZE merging
makes parents straddle chapter borders, so one stray collocation must not flag a
parent. Live-store measurement (2026-09-01, 170 parents, 2학기 guide):

    hits  hits/KB  parent                      truth
      15     6.6   parent_13 (OCU 본문)          OCU
       8     3.7   parent_15 (OCU 끝+일반 시작)   border
       5     2.1   parent_14 (OCU 본문)          OCU
       3     0.61  parent_86 (전화번호부, "(OCU) 교무" 류)  general
       1     0.13  parent_0  (표지/차례)          general

≥2 hits AND ≥1.0 hits/KB keeps all real OCU parents (margin 2.1 vs 0.61 = 3.4x)
and clears the phone-directory / TOC collateral. Border parents like parent_15
are still demoted — accepted collateral: demote-never-delete readmits their
general children (학사경고자 …) when they are the only evidence, but ranking
shifts, so the 정답률 eval must confirm (flagged in the PR). Same acceptance for
the old deliberate 부록 course-list gap.

Question side: demotion applies only when the question does NOT mention OCU
(OCU / 오씨유 / 열린사이버 / 온라인 공동활용 …). Both detectors fail open: a missed OCU
marker in the question, or a missed OCU chunk, just keeps today's behavior for
that doc — demote-never-delete (mechanics shared via rag_agent.scoping) means
even a demoted chunk stays reachable when it is the only evidence.

Applied by tools.ToolFactory._search_child_chunks behind
``config.OCU_FILTER_ENABLED`` (default OFF).
"""

from __future__ import annotations

import re

# Question-side gate. Latin "ocu" is word-bounded so English words containing the
# trigram ("document", "focus" …) never make a question look OCU-scoped. 공동활용 is
# anchored to the OCU senses (온라인 공동활용 / 공동활용 수업, the glossary's expansion) —
# bare 공동활용 also means ordinary resource sharing ("시설 공동활용 협약") and would
# silently stand the lever down on exactly the general questions it protects.
_OCU_QUESTION_RE = re.compile(
    r"(?i)(?<![a-z])ocu(?![a-z])"
    r"|오\s*씨\s*유"
    r"|열린\s*사이버"
    r"|온라인\s*공동\s*활용"
    r"|공동\s*활용\s*수업",
)

# Chunk-side detection: OCU as the chunk's TOPIC. Each alternative is a collocation
# observed in the guides' OCU 안내/부록 sections and absent from every incidental
# mention (see module docstring). "OCU" is matched case-sensitively on purpose —
# the corpus always writes the acronym in caps, and a case-insensitive match would
# hit the "ocu" inside English words.
_OCU_TOPIC_RE = re.compile(
    "|".join(
        [
            r"OCU\s*교과목\s*수강\s*안내",  # 섹션 제목
            r"OCU\s*개강일",
            r"OCU\s*수강\s*안내",  # ID 발급/비밀번호 섹션 ("OCU 수강 신청자"와 다름)
            r"OCU\s*컨소시엄",  # 수업·시험·성적 규정 문단
            r"OCU\s*시스템",  # 시스템 사용료 (붙여쓴 "OCU시스템" 포함)
            r"OCU\s*홈페이지",
            r"OCU\s*ID",
            r"OCU\s*개설\s*교과목",  # 부록 (붙여쓴 "OCU개설교과목목록" 포함)
            r"OCU\s*과목은",  # 부록 주석 "OCU 과목은 전공, 교양으로 인정 불가"
            r"OCU\s*수강신청\s*과목",  # 사용료 표 머리행
            r"OCU\s*\(\s*자유선택\s*\)",  # 학점인정 "이수구분은 OCU(자유선택)"
            r"\(\s*OCU\s*포함\s*\)",  # 사용료 표 "실제 수강신청학점(OCU포함)"
            r"\(\s*OCU\s*\)",  # "한국열린사이버대학교(OCU)", 문의전화 "(OCU) 교무"
            # NB: bare "한국열린사이버대학교" is deliberately NOT a pattern — it appears as
            # the 소속대학 column value in 부록 course-list rows, and flagging it would
            # demote the only chunk answering a course-name question asked without an
            # OCU marker. The intro sentence that names the university is caught by
            # "(OCU)" above.
            r"cons\.ocu\.ac\.kr",
        ]
    )
)


def is_ocu_question(question) -> bool:
    """True when the question itself asks about OCU — scoping must then stand down."""
    return bool(_OCU_QUESTION_RE.search(question or ""))


def is_ocu_chunk(doc) -> bool:
    """True when OCU is the chunk's topic (not a mere incidental mention)."""
    return bool(_OCU_TOPIC_RE.search(getattr(doc, "page_content", "") or ""))


# Parent-verdict thresholds — measured over the live store, table in the module
# docstring. Presence alone is NOT enough: merged parents straddle chapter borders.
_PARENT_MIN_HITS = 2
_PARENT_MIN_HITS_PER_KB = 1.0


def is_ocu_parent(content) -> bool:
    """True when a PARENT chunk's text is OCU-topic — backstop for children inside
    the OCU chapter that carry no collocation themselves (module docstring).
    Density-gated so a lone incidental collocation (전화번호부의 "(OCU) 교무", 차례의
    섹션 제목) never flags a general parent."""
    text = content or ""
    if not text:
        return False
    hits = len(_OCU_TOPIC_RE.findall(text))
    return hits >= _PARENT_MIN_HITS and hits * 1000 / len(text) >= _PARENT_MIN_HITS_PER_KB
