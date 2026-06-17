"""Korean-aware BM25 sparse embeddings for Qdrant hybrid retrieval.

The default FastEmbed BM25 (`Qdrant/bm25`) tokenizes Korean on whitespace only
(`SimpleTokenizer`: lowercase -> non-word becomes space -> split). Korean has no
morphological segmentation that way, so particles stay glued to nouns
(`졸업요건은` != `졸업요건`) and compound nouns are never split (`기말고사기간`
stays one token). That mismatch is the Korean weak link in the sparse leg of the
hybrid retriever.

This module replaces the tokenizer with a Korean morphological analyzer (Kiwi or
Okt), keeps only content-word morphemes, then reuses the *exact* FastEmbed BM25
term-frequency weighting and mmh3 token hashing. Producing the same kind of
TF-only sparse vector means the vectors are compatible with Qdrant's IDF modifier
(`SparseVectorParams(modifier="idf")`), which computes the IDF part server-side.

Strategies: "kiwi", "okt", "whitespace" (a control that mirrors FastEmbed's
SimpleTokenizer, for isolating the IDF factor without pulling in a JVM).
"""
from __future__ import annotations

import re
from collections import defaultdict

import mmh3
from langchain_qdrant.sparse_embeddings import SparseEmbeddings, SparseVector

# BM25 hyperparameters — mirror fastembed.sparse.bm25.Bm25 defaults so the TF
# weighting is directly comparable to the Qdrant/bm25 baseline.
_K = 1.2
_B = 0.75
_AVG_LEN = 256.0

# Kiwi POS tags to keep (content words): general/proper/bound nouns, numerals,
# numbers, foreign/Latin/Hanja words, verb & adjective stems, roots. Drops
# particles (J*), endings (E*), auxiliaries (VX/VCP/VCN), and punctuation (S*
# except SN/SL/SH). Matching is by tag prefix so irregular variants (VV-I,
# VV-R, ...) are covered.
_KIWI_KEEP_PREFIXES = ("NNG", "NNP", "NNB", "NR", "SN", "SL", "SH", "VV", "VA", "XR")

# Okt (konlpy) POS tags to keep, with stemming on (lemmatized verbs/adjectives).
_OKT_KEEP = {"Noun", "Verb", "Adjective", "Number", "Alpha", "Foreign"}

_WS_NONWORD = re.compile(r"[^\w]", re.UNICODE)


def term_frequency(tokens: list[str]) -> dict[int, float]:
    """BM25 TF weighting, identical to fastembed.sparse.bm25.Bm25._term_frequency.

    Returns {token_id -> tf_weight}. Qdrant applies the IDF part server-side when
    the sparse vector is configured with modifier="idf".
    """
    counter: defaultdict[str, int] = defaultdict(int)
    for tok in tokens:
        counter[tok] += 1
    doc_len = len(tokens)
    tf_map: dict[int, float] = {}
    for tok, n in counter.items():
        token_id = abs(mmh3.hash(tok))  # same id space as FastEmbed compute_token_id
        tf_map[token_id] = (n * (_K + 1)) / (n + _K * (1 - _B + _B * doc_len / _AVG_LEN))
    return tf_map


class _KiwiTokenizer:
    def __init__(self) -> None:
        from kiwipiepy import Kiwi

        self._kiwi = Kiwi()

    def __call__(self, text: str) -> list[str]:
        return [t.form for t in self._kiwi.tokenize(text) if t.tag.startswith(_KIWI_KEEP_PREFIXES)]


class _OktTokenizer:
    def __init__(self) -> None:
        from konlpy.tag import Okt

        self._okt = Okt()

    def __call__(self, text: str) -> list[str]:
        return [w for w, pos in self._okt.pos(text, stem=True) if pos in _OKT_KEEP]


class _WhitespaceTokenizer:
    """Control: mirrors FastEmbed's SimpleTokenizer (no stemmer/stopwords, which
    are English-only no-ops on Korean anyway). Lets us test 'baseline tokenizer +
    IDF' without a JVM."""

    def __call__(self, text: str) -> list[str]:
        return _WS_NONWORD.sub(" ", text.lower()).split()


_TOKENIZERS = {
    "kiwi": _KiwiTokenizer,
    "okt": _OktTokenizer,
    "whitespace": _WhitespaceTokenizer,
}


class Bm42KiwiSparse(SparseEmbeddings):
    """BM42 (Qdrant/bm42-all-minilm-l6-v2-attentions) fed Kiwi-presegmented Korean.

    BM42 is a transformer-attention sparse model, not a pluggable-tokenizer BM25; its
    weakness on Korean is that all-MiniLM's WordPiece tokenizer fragments unsegmented
    Korean. We don't replace BM42's tokenizer — we pre-segment the input with Kiwi
    (space-join morphemes) so the WordPiece tokenizer sees clean word boundaries and
    the attention weights land on real morphemes. Use with modifier="idf" (BM42's
    intended Qdrant config, same as BM25).
    """

    _MODEL = "Qdrant/bm42-all-minilm-l6-v2-attentions"

    def __init__(self) -> None:
        self._kiwi = None
        self._model = None

    def _ensure(self):
        if self._model is None:
            from fastembed import SparseTextEmbedding
            from kiwipiepy import Kiwi

            self._kiwi = Kiwi()
            self._model = SparseTextEmbedding(self._MODEL)

    def _seg(self, text: str) -> str:
        # Space-join every morpheme so all-MiniLM's WordPiece sees Korean word boundaries.
        return " ".join(t.form for t in self._kiwi.tokenize(text))

    def embed_documents(self, texts: list[str]) -> list[SparseVector]:
        self._ensure()
        segs = [self._seg(t) for t in texts]
        return [SparseVector(indices=e.indices.tolist(), values=e.values.tolist())
                for e in self._model.embed(segs)]

    def embed_query(self, text: str) -> SparseVector:
        self._ensure()
        e = next(iter(self._model.query_embed([self._seg(text)])))
        return SparseVector(indices=e.indices.tolist(), values=e.values.tolist())


class KoreanBM25Sparse(SparseEmbeddings):
    """LangChain SparseEmbeddings producing BM25 TF vectors from Korean morphemes.

    Use with a Qdrant collection whose sparse vector has modifier="idf".
    """

    def __init__(self, strategy: str) -> None:
        if strategy not in _TOKENIZERS:
            raise ValueError(
                f"unknown sparse tokenizer strategy {strategy!r}; expected one of {sorted(_TOKENIZERS)}"
            )
        self._strategy = strategy
        self._tok: object | None = None  # lazy: defer Kiwi/Okt load until first use

    def _tokenizer(self):
        if self._tok is None:
            self._tok = _TOKENIZERS[self._strategy]()
        return self._tok

    def embed_documents(self, texts: list[str]) -> list[SparseVector]:
        tok = self._tokenizer()
        out: list[SparseVector] = []
        for text in texts:
            tf = term_frequency(tok(text))
            out.append(SparseVector(indices=list(tf.keys()), values=list(tf.values())))
        return out

    def embed_query(self, text: str) -> SparseVector:
        # BM25 query side: unique tokens with weight 1.0 (IDF handled by Qdrant).
        tok = self._tokenizer()
        ids = sorted({abs(mmh3.hash(t)) for t in tok(text)})
        return SparseVector(indices=ids, values=[1.0] * len(ids))
