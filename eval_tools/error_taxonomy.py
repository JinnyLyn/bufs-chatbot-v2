"""7-버킷 오답 분류기 (error taxonomy) — 순수·오프라인·임포트 가능 모듈.

QA/평가 역할의 "오답 분석" 산출물. 기존 :mod:`_answer_analysis` 는 오답을 검색-vs-생성
**2버킷**(RETRIEVAL_ERR / GENERATION_ERR)으로만 나눴다. 이 모듈은 그 백본을 유지하되
역할 명세가 요구하는 **7버킷**으로 세분화한다::

    ① 검색 실패        SEARCH_FAILURE      (KB엔 있는데 top-k로 못 끌어옴)
    ② Prompt 실패      PROMPT_FAILURE      (컨텍스트엔 있는데 답에 못 씀 — 누락/거부/형식)
    ③ 문서 없음        NO_DOCUMENT         (골드 사실이 KB 코퍼스에 아예 없음)
    ④ 질문 애매함      AMBIGUOUS_QUESTION  (질문 자체가 과소명세 — 플래그로만 진입)
    ⑤ LLM Hallucination HALLUCINATION      (컨텍스트에 없는/반하는 사실을 지어냄)
    ⑥ Chunk 문제       CHUNK_PROBLEM       (사실이 청크 경계에서 쪼개져 단일 청크에 없음)
    ⑦ Embedding 문제   EMBEDDING_PROBLEM   (단일 청크에 있으나 임베딩 유사도가 top-k 밖으로 밀어냄)

설계 원칙 — **정직한 단계적 저하(graceful degradation).** 세밀한 잎(⑥/⑦, ②/⑤)을 가르려면
라이브 인덱스(Qdrant top-k 순위)나 LLM-judge가 필요하다. 그 신호가 없으면 **추측하지 않고**
더 거친(그러나 참인) 상위 버킷으로 떨어진다:

    · 컨텍스트 신호 없음        → RETRIEVAL_UNSPLIT / UNCLASSIFIED
    · 라이브 순위 신호 없음     → SEARCH_FAILURE (①로 흡수)
    · judge 신호 없음          → GENERATION_UNSPLIT (②/⑤ 미분리)

그래서 이 모듈 자체는 순수하다: stdlib + :mod:`qa_scorer`(파일 로더뿐) 만 쓰고 네트워크·백엔드가
없다. 신호(컨텍스트/KB/순위/judge)는 **드라이버**(``_error_analysis7.py``)가 계산해서
:class:`Signals` 로 넘긴다 — 휴리스틱은 드라이버에 두고, 분류 규칙은 여기 순수 트리에 둔다.

:func:`classify` 는 이미 집계된 불리언만 받아 트리를 걷는다. 사실집합 대조(어느 사실이 답/
컨텍스트/KB에 있나)는 드라이버가 하며(어차피 표시용으로 필요), 단위 테스트는 :class:`Signals`
를 직접 만들어 7버킷 전부와 저하 경로를 오프라인으로 검증한다(``tests/eval/``).
"""
from __future__ import annotations

import glob
import json
import os
from dataclasses import dataclass
from typing import NamedTuple, Optional

import qa_scorer  # 순수: 데이터셋 로더 + tokens_present(공백/키워드 관대 매칭)

# --------------------------------------------------------------------------- 버킷 정의

# key -> (표시 라벨, side). side 는 리포트 롤업용(retrieval / generation / question /
# correct / degraded / skip). 역할의 7버킷 + 정답 + 저하(미분리) + 스킵.
BUCKETS: dict[str, tuple[str, str]] = {
    "CORRECT":            ("정답",                 "correct"),
    "SEARCH_FAILURE":     ("① 검색 실패",          "retrieval"),
    "PROMPT_FAILURE":     ("② Prompt 실패",        "generation"),
    "NO_DOCUMENT":        ("③ 문서 없음",          "retrieval"),
    "AMBIGUOUS_QUESTION": ("④ 질문 애매함",        "question"),
    "HALLUCINATION":      ("⑤ LLM Hallucination",  "generation"),
    "CHUNK_PROBLEM":      ("⑥ Chunk 문제",         "retrieval"),
    "EMBEDDING_PROBLEM":  ("⑦ Embedding 문제",     "retrieval"),
    # 신호 부족 시 정직하게 떨어지는 상위 버킷(미분리)
    "GENERATION_UNSPLIT": ("생성오류(미분리 ②/⑤)", "degraded"),
    "RETRIEVAL_UNSPLIT":  ("검색오류(미분리)",      "degraded"),
    "UNCLASSIFIED":       ("분류불가(신호부족)",    "degraded"),
    "NO_FACTS":           ("채점불가(골드사실없음)", "skip"),
}

# 리포트 출력 순서.
ORDER: tuple[str, ...] = (
    "CORRECT",
    "SEARCH_FAILURE", "PROMPT_FAILURE", "NO_DOCUMENT", "AMBIGUOUS_QUESTION",
    "HALLUCINATION", "CHUNK_PROBLEM", "EMBEDDING_PROBLEM",
    "GENERATION_UNSPLIT", "RETRIEVAL_UNSPLIT", "UNCLASSIFIED", "NO_FACTS",
)


class Verdict(NamedTuple):
    """분류 결과: 버킷 key + 한 줄 사유."""

    bucket: str
    reason: str

    @property
    def label(self) -> str:
        return BUCKETS[self.bucket][0]

    @property
    def side(self) -> str:
        return BUCKETS[self.bucket][1]


@dataclass(frozen=True)
class Signals:
    """한 질문에 대한 **이미 집계된** 분류 신호. 드라이버가 채워 넘긴다.

    ``Optional`` 필드의 ``None`` 은 "그 신호를 측정하지 못함"을 뜻하며 트리는 이를
    추측 대신 **저하 경로**로 처리한다(모듈 docstring 참조).
    """

    is_correct: bool                       # 골드 사실이 모두 답변에 존재?
    has_facts: bool = True                 # 채점할 골드 사실이 하나라도 있나
    ambiguous: bool = False                # 질문 애매 플래그(judge/데이터셋). 기본 off
    # 검색-vs-생성 상위 분기: 답변에 빠진 사실들이 "검색된 컨텍스트"엔 있었나?
    #   True  = 전부 컨텍스트에 있었음 → 생성-side
    #   False = 하나라도 컨텍스트에 없었음 → 검색-side
    #   None  = 컨텍스트 신호 없음(로그 미확보)
    evidence_retrieved: Optional[bool] = None
    # 검색-side 세부(컨텍스트에 없던 사실 기준)
    in_kb: Optional[bool] = None           # 그 사실이 KB 코퍼스에 존재? (오프라인 코퍼스 조회)
    chunk_split: Optional[bool] = None     # KB엔 있으나 어떤 단일 청크에도 통째로는 없음
    ranked_out: Optional[bool] = None      # 단일 청크에 있으나 라이브 top-k 밖(임베딩 실패)
    # 생성-side 세부
    hallucinated: Optional[bool] = None    # 컨텍스트에 없는/반하는 사실을 단언(judge/금지어 누출)


def classify(sig: Signals) -> Verdict:
    """신호를 7버킷(+저하) 중 하나로 분류. 순수 함수 — 트리만 걷는다."""
    if not sig.has_facts:
        return Verdict("NO_FACTS", "골드 사실이 없어 오답 귀인 대상 아님")
    if sig.is_correct:
        return Verdict("CORRECT", "골드 사실이 모두 답변에 존재")

    # 오답이다. ④는 질문 자체 문제이므로 검색/생성 귀인보다 먼저 가른다(플래그 있을 때만).
    if sig.ambiguous:
        return Verdict("AMBIGUOUS_QUESTION", "질문이 과소명세/중의적이라 정답 특정 불가")

    if sig.evidence_retrieved is True:
        # 근거가 컨텍스트에 있었는데 답이 틀림 → 생성-side
        if sig.hallucinated is True:
            return Verdict("HALLUCINATION", "검색된 컨텍스트에 없는/반하는 사실을 지어냄")
        if sig.hallucinated is False:
            return Verdict("PROMPT_FAILURE", "컨텍스트에 근거가 있었으나 답에 반영 못 함(누락/거부/형식)")
        return Verdict("GENERATION_UNSPLIT", "근거는 검색됐으나 미사용 — ②/⑤ 분리엔 judge 필요")

    # evidence_retrieved 가 False(근거 미검색) 또는 None(컨텍스트 신호 없음) → 검색-side 귀인.
    # 두 경우 모두 KB/청크/순위 신호가 있으면 그대로 세분화한다.
    if sig.in_kb is False:
        return Verdict("NO_DOCUMENT", "골드 사실이 KB 코퍼스에 아예 없음")
    if sig.chunk_split is True:
        return Verdict("CHUNK_PROBLEM", "KB엔 있으나 청크 경계로 쪼개져 단일 청크에 통째로 없음")
    if sig.ranked_out is True:
        return Verdict("EMBEDDING_PROBLEM", "단일 청크에 있으나 임베딩 유사도가 top-k 밖으로 밀어냄")
    if sig.in_kb is True:
        return Verdict("SEARCH_FAILURE", "KB엔 있는데 검색이 top-k로 끌어오지 못함")

    # in_kb 미확인. 컨텍스트 신호 유무로 최소한의 정직한 버킷을 준다.
    if sig.evidence_retrieved is False:
        return Verdict("RETRIEVAL_UNSPLIT", "근거 미검색이나 KB 조회 없어 검색 세부원인 미분리")
    return Verdict("UNCLASSIFIED", "컨텍스트·KB 신호가 없어 오답 원인 귀인 불가")


# --------------------------------------------------------------------------- KB 코퍼스

# 레포 루트(이 파일: <root>/eval_tools/error_taxonomy.py) 기준 코퍼스 위치.
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class KBCorpus:
    """오프라인 KB 코퍼스 인덱스 — "문서 없음"·"Chunk 문제" 판정용.

    ``sources`` 는 원문 문서(``markdown_docs/*.md``) 전체 텍스트 리스트 = "KB에 있나".
    ``chunks`` 는 검색 단위(``parent_store/*.json`` 의 ``page_content``) = "단일 청크에 있나".
    사실이 원문엔 있는데 어떤 단일 청크에도 통째로 없으면 청크 경계 문제(⑥)로 본다.

    테스트가 디스크를 안 건드리도록 텍스트를 주입받는다. :meth:`from_repo` 가 디스크 로더.
    """

    def __init__(self, sources: list[str], chunks: list[str]) -> None:
        self._sources = sources
        self._chunks = chunks

    @classmethod
    def from_repo(cls, root: str | None = None) -> "KBCorpus":
        """레포의 ``markdown_docs/`` + ``parent_store/`` 에서 코퍼스를 적재."""
        root = root or _ROOT
        sources: list[str] = []
        for p in sorted(glob.glob(os.path.join(root, "markdown_docs", "*.md"))):
            with open(p, encoding="utf-8") as f:
                sources.append(f.read())
        chunks: list[str] = []
        for p in sorted(glob.glob(os.path.join(root, "parent_store", "*.json"))):
            try:
                with open(p, encoding="utf-8") as f:
                    d = json.load(f)
            except (OSError, json.JSONDecodeError):
                continue
            txt = d.get("page_content") if isinstance(d, dict) else None
            if txt:
                chunks.append(str(txt))
        return cls(sources=sources, chunks=chunks)

    def fact_in_kb(self, fact: str) -> bool:
        """어떤 **단일 원문 문서**가 ``fact`` 를 통째로 담고 있나(공백/키워드 관대).

        코퍼스 전체를 이어붙여 보면 다어절 사실이 서로 다른 문서에 흩어져 있어도
        참이 되어 "문서없음"을 "Chunk문제"로 오귀인한다. 문서 단위로 검사한다.
        """
        return any(qa_scorer.tokens_present(fact, doc) for doc in self._sources)

    def fact_in_single_chunk(self, fact: str) -> bool:
        """어떤 **단일** 검색 청크가 ``fact`` 를 통째로 담고 있나."""
        return any(qa_scorer.tokens_present(fact, c) for c in self._chunks)

    def fact_split(self, fact: str) -> bool:
        """원문엔 있으나 단일 청크엔 통째로 없음 = 청크 경계 문제 후보."""
        return self.fact_in_kb(fact) and not self.fact_in_single_chunk(fact)

    @property
    def n_sources(self) -> int:
        return len(self._sources)

    @property
    def n_chunks(self) -> int:
        return len(self._chunks)


def present(fact: str, text: str) -> bool:
    """``fact`` 의 모든 어절이 ``text`` 에 있나 — 드라이버의 답/컨텍스트 대조용 재노출."""
    return qa_scorer.tokens_present(fact, text)
