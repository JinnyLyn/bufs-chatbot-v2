"""Offline tests for the 7-bucket error taxonomy classifier (error_taxonomy).

No backend/LLM/network. Each of the 7 role buckets has a canonical synthetic
:class:`Signals` case, plus the graceful-degradation paths (missing context /
index / judge signals collapse to honest coarser buckets) and the offline
KB-corpus index. `error_taxonomy` is importable via pythonpath=["eval_tools"].
"""
import pytest

import error_taxonomy as et
from error_taxonomy import Signals, classify

pytestmark = pytest.mark.unit


# --------------------------------------------------------------------------- the 7 buckets


def test_correct():
    assert classify(Signals(is_correct=True)).bucket == "CORRECT"


def test_search_failure():
    # evidence not retrieved, fact IS in KB, no chunk/rank sub-signal -> ① 검색 실패
    v = classify(Signals(is_correct=False, evidence_retrieved=False, in_kb=True))
    assert v.bucket == "SEARCH_FAILURE"
    assert v.side == "retrieval"


def test_prompt_failure():
    # evidence retrieved, not hallucinated -> ② Prompt 실패
    v = classify(Signals(is_correct=False, evidence_retrieved=True, hallucinated=False))
    assert v.bucket == "PROMPT_FAILURE"
    assert v.side == "generation"


def test_no_document():
    # evidence not retrieved AND fact absent from KB -> ③ 문서 없음
    v = classify(Signals(is_correct=False, evidence_retrieved=False, in_kb=False))
    assert v.bucket == "NO_DOCUMENT"


def test_ambiguous_question():
    # ambiguous flag wins over retrieval/generation attribution -> ④ 질문 애매함
    v = classify(Signals(is_correct=False, ambiguous=True, evidence_retrieved=True))
    assert v.bucket == "AMBIGUOUS_QUESTION"


def test_hallucination():
    # evidence retrieved but a fabricated/forbidden fact asserted -> ⑤ Hallucination
    v = classify(Signals(is_correct=False, evidence_retrieved=True, hallucinated=True))
    assert v.bucket == "HALLUCINATION"


def test_chunk_problem():
    # evidence not retrieved, in KB, split across chunk boundary -> ⑥ Chunk 문제
    v = classify(Signals(is_correct=False, evidence_retrieved=False, in_kb=True, chunk_split=True))
    assert v.bucket == "CHUNK_PROBLEM"


def test_embedding_problem():
    # in a single chunk but ranked out of top-k -> ⑦ Embedding 문제
    v = classify(
        Signals(is_correct=False, evidence_retrieved=False, in_kb=True,
                chunk_split=False, ranked_out=True)
    )
    assert v.bucket == "EMBEDDING_PROBLEM"


# --------------------------------------------------------------------------- priority / ordering


def test_no_document_beats_chunk_and_embedding():
    # if the fact isn't in the KB at all, that's the root cause regardless of other flags
    v = classify(
        Signals(is_correct=False, evidence_retrieved=False, in_kb=False,
                chunk_split=True, ranked_out=True)
    )
    assert v.bucket == "NO_DOCUMENT"


def test_chunk_beats_embedding():
    v = classify(
        Signals(is_correct=False, evidence_retrieved=False, in_kb=True,
                chunk_split=True, ranked_out=True)
    )
    assert v.bucket == "CHUNK_PROBLEM"


def test_correct_beats_everything():
    v = classify(Signals(is_correct=True, ambiguous=True, evidence_retrieved=False, in_kb=False))
    assert v.bucket == "CORRECT"


# --------------------------------------------------------------------------- graceful degradation


def test_no_facts_is_skipped():
    assert classify(Signals(is_correct=False, has_facts=False)).bucket == "NO_FACTS"


def test_generation_unsplit_without_judge():
    # evidence retrieved, but no hallucination signal -> honest coarse bucket, not a guess
    v = classify(Signals(is_correct=False, evidence_retrieved=True, hallucinated=None))
    assert v.bucket == "GENERATION_UNSPLIT"
    assert v.side == "degraded"


def test_retrieval_unsplit_without_kb_signal():
    # evidence not retrieved, but KB not probed -> can't name the sub-cause
    v = classify(Signals(is_correct=False, evidence_retrieved=False, in_kb=None))
    assert v.bucket == "RETRIEVAL_UNSPLIT"


def test_unclassified_without_any_signal():
    # wrong answer, no context signal, no KB signal -> honest UNCLASSIFIED
    v = classify(Signals(is_correct=False, evidence_retrieved=None, in_kb=None))
    assert v.bucket == "UNCLASSIFIED"


def test_kb_only_attribution_when_context_unknown():
    # no context signal (None) but KB says absent -> still attribute to 문서 없음
    v = classify(Signals(is_correct=False, evidence_retrieved=None, in_kb=False))
    assert v.bucket == "NO_DOCUMENT"


# --------------------------------------------------------------------------- registry sanity


def test_every_bucket_has_label_and_side():
    for key in et.ORDER:
        label, side = et.BUCKETS[key]
        assert label and side
    assert set(et.ORDER) == set(et.BUCKETS)  # ORDER covers the registry exactly


def test_verdict_unknown_bucket_no_keyerror():
    # a Verdict rebuilt from external data (e.g. JSON) must not crash on label/side
    v = et.Verdict("NOT_A_BUCKET", "외부 유입")
    assert "NOT_A_BUCKET" in v.label
    assert v.side == "unknown"


# --------------------------------------------------------------------------- KB corpus (offline)


@pytest.fixture
def corpus():
    # "복학 신청" lives whole in a source doc AND whole in one chunk (retrievable).
    # "재입학 3월" lives in the source doc but is SPLIT across two chunks (boundary).
    sources = [
        "복학 신청은 정해진 기간에 학생포털에서 합니다. 재입학은 3월에 접수합니다.",
    ]
    chunks = [
        "복학 신청은 정해진 기간에 학생포털에서 합니다.",  # has "복학 신청" whole
        "재입학은 접수합니다.",                              # has "재입학" but not "3월"
        "일정은 3월 학사일정을 따릅니다.",                   # has "3월" but not "재입학"
    ]
    return et.KBCorpus(sources=sources, chunks=chunks)


def test_corpus_fact_in_kb(corpus):
    assert corpus.fact_in_kb("복학 신청") is True
    assert corpus.fact_in_kb("계절학기 취소") is False  # absent -> 문서 없음 signal


def test_corpus_fact_in_single_chunk(corpus):
    assert corpus.fact_in_single_chunk("복학 신청") is True


def test_corpus_fact_split_across_chunks(corpus):
    # "재입학 3월": both words in the source, but no single chunk has both -> ⑥ Chunk
    assert corpus.fact_in_kb("재입학 3월") is True
    assert corpus.fact_in_single_chunk("재입학 3월") is False
    assert corpus.fact_split("재입학 3월") is True
    # a whole-in-one-chunk fact is NOT split
    assert corpus.fact_split("복학 신청") is False


def test_corpus_fact_words_split_across_different_docs_is_not_in_kb():
    # words in DIFFERENT source docs must NOT count as "in KB" (else 문서없음 -> Chunk misattrib)
    c = et.KBCorpus(
        sources=["재입학 안내입니다.", "3월 학사일정입니다."],  # words in separate docs
        chunks=["재입학 안내입니다.", "3월 학사일정입니다."],
    )
    assert c.fact_in_kb("재입학 3월") is False   # no single doc has both words
    assert c.fact_split("재입학 3월") is False    # not in KB -> not a chunk-boundary case


def test_from_repo_empty_corpus_raises(tmp_path):
    # empty/wrong root must HARD-FAIL (silent empty corpus => everything misclassified ③)
    with pytest.raises(RuntimeError, match="비었습니다"):
        et.KBCorpus.from_repo(root=str(tmp_path))


def test_from_repo_corrupt_parent_json_warns_but_loads(tmp_path):
    (tmp_path / "markdown_docs").mkdir()
    (tmp_path / "markdown_docs" / "a.md").write_text("복학 신청 안내", encoding="utf-8")
    ps = tmp_path / "parent_store"
    ps.mkdir()
    (ps / "good.json").write_text('{"page_content": "복학 신청 안내"}', encoding="utf-8")
    (ps / "bad.json").write_text("{corrupt", encoding="utf-8")
    with pytest.warns(UserWarning, match="적재 실패"):
        c = et.KBCorpus.from_repo(root=str(tmp_path))
    assert c.n_sources == 1 and c.n_chunks == 1  # corrupt file skipped, loudly
