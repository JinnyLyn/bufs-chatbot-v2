"""Deterministic, SEEDED perturbations of combined88's answerable questions.

Real users type ambiguous / typo'd / mis-spaced queries; combined88 is a clean,
curated set whose accuracy *overstates* real-world behaviour. This module
generates a **messy** sibling of the benchmark by applying realistic, seeded
transforms to each answerable question while **preserving its ground_truth** —
so ``contains``/``strict`` stay scorable **offline, deterministically, MCP-free**
(the model still has to answer the *perturbed* question; the GT it is scored
against is unchanged).

Determinism contract (WS-R1)
----------------------------
Every transform draws from a :class:`random.Random` seeded by
``sha256(base_seed | parent_id | perturbation_type)`` — a *content* hash, NOT
Python's ``hash()`` (which is salted per-process via ``PYTHONHASHSEED``). The
same ``base_seed`` therefore yields byte-identical output across runs, machines,
and processes. This is the property ``tests/kpi/test_perturb.py`` pins.

Perturbation taxonomy (6 types)
-------------------------------
``typo``       jamo / keyboard-adjacency typo (single 두벌식 edit).
``spacing``    붙여쓰기 — drop all internal spaces.
``honorific``  register shift toward casual / banmal command form.
``truncation`` truncate + ellipsis (user trails off).
``paraphrase`` conversational filler / hedge wrapper.
``codeswitch`` KO->EN code-switch of one academic term.

A transform returns ``None`` when it does not apply to a given question (e.g.
``spacing`` on a space-less question, ``codeswitch`` with no known term) — that
child is simply omitted, keeping output deterministic and free of no-op clones.
"""
from __future__ import annotations

import hashlib
import json
import random
from pathlib import Path
from typing import Callable, Iterable, Optional

# Default seed: the project date. Stable, documented, overridable per call.
DEFAULT_SEED = 20260624

# Repo-relative committed test set (inputs-only). Kept as a convenience loader so
# this module is self-contained; the canonical loader is WS-B's ``dataset.py``.
_DEFAULT_TESTSET = Path(__file__).resolve().parents[2] / "data" / "combined88.json"

# Ordered taxonomy — also the deterministic emission order per parent.
PERTURBATIONS: tuple[str, ...] = (
    "typo",
    "spacing",
    "honorific",
    "truncation",
    "paraphrase",
    "codeswitch",
)

# --------------------------------------------------------------------------- #
# Seeded RNG helper
# --------------------------------------------------------------------------- #


def _rng(base_seed: int, parent_id: object, ptype: str) -> random.Random:
    """A :class:`random.Random` seeded by a *content* hash (process-stable)."""
    digest = hashlib.sha256(f"{base_seed}|{parent_id}|{ptype}".encode("utf-8")).digest()
    return random.Random(int.from_bytes(digest[:8], "big"))


# --------------------------------------------------------------------------- #
# Hangul jamo (de)composition + 두벌식 keyboard adjacency
# --------------------------------------------------------------------------- #

_S_BASE = 0xAC00
_S_LAST = 0xD7A3

# 19 초성 / 21 중성 / 28 종성 (index 0 == no final).
_CHO = "ㄱㄲㄴㄷㄸㄹㅁㅂㅃㅅㅆㅇㅈㅉㅊㅋㅌㅍㅎ"
_JUNG = "ㅏㅐㅑㅒㅓㅔㅕㅖㅗㅘㅙㅚㅛㅜㅝㅞㅟㅠㅡㅢㅣ"
_JONG = ["", "ㄱ", "ㄲ", "ㄳ", "ㄴ", "ㄵ", "ㄶ", "ㄷ", "ㄹ", "ㄺ", "ㄻ", "ㄼ", "ㄽ",
         "ㄾ", "ㄿ", "ㅀ", "ㅁ", "ㅂ", "ㅄ", "ㅅ", "ㅆ", "ㅇ", "ㅈ", "ㅊ", "ㅋ",
         "ㅌ", "ㅍ", "ㅎ"]

# 두벌식 layout rows (QWERTY physical key order). Horizontal neighbours model the
# most common slip: hitting the key next to the intended one.
_KB_ROWS = (
    "ㅂㅈㄷㄱㅅㅛㅕㅑㅐㅔ",   # q w e r t y u i o p
    "ㅁㄴㅇㄹㅎㅗㅓㅏㅣ",     # a s d f g h j k l
    "ㅋㅌㅊㅍㅠㅜㅡ",         # z x c v b n m
)
# Double consonants (shift keys) -> their unshifted base: "forgot to hold shift".
_SHIFT_SLIP = {"ㄲ": "ㄱ", "ㄸ": "ㄷ", "ㅃ": "ㅂ", "ㅆ": "ㅅ", "ㅉ": "ㅈ"}

# jamo -> (row index, position) for O(1) neighbour lookup.
_KB_POS: dict[str, tuple[int, int]] = {
    j: (r, c) for r, row in enumerate(_KB_ROWS) for c, j in enumerate(row)
}


def _decompose(ch: str) -> Optional[tuple[int, int, int]]:
    """Hangul syllable -> (cho, jung, jong) indices, or ``None`` if not a syllable."""
    code = ord(ch)
    if not (_S_BASE <= code <= _S_LAST):
        return None
    s = code - _S_BASE
    return s // 588, (s % 588) // 28, s % 28


def _compose(cho: int, jung: int, jong: int) -> str:
    """(cho, jung, jong) indices -> a single Hangul syllable."""
    return chr(_S_BASE + (cho * 21 + jung) * 28 + jong)


_CHO_SET = frozenset(_CHO)
_JUNG_SET = frozenset(_JUNG)


def _keyboard_candidates(jamo: str, valid: frozenset[str]) -> list[str]:
    """Keyboard-adjacent jamo of the **same class** (RNG-free, deterministic).

    ``valid`` constrains the result to legal 초성 (``_CHO_SET``) or 중성
    (``_JUNG_SET``) — a consonant key's horizontal neighbour can be a vowel
    (e.g. ㅅ↔ㅛ), which is not a valid replacement for an initial consonant, so
    such cross-class neighbours are filtered out here.
    """
    if jamo in _SHIFT_SLIP:  # double consonant -> unshifted base ("forgot shift")
        base = _SHIFT_SLIP[jamo]
        return [base] if base in valid else []
    pos = _KB_POS.get(jamo)
    if pos is None:
        return []
    row, col = pos
    candidates = []
    if col > 0:
        candidates.append(_KB_ROWS[row][col - 1])
    if col < len(_KB_ROWS[row]) - 1:
        candidates.append(_KB_ROWS[row][col + 1])
    return [c for c in candidates if c in valid]


# --------------------------------------------------------------------------- #
# Individual perturbation transforms — each (str, Random) -> Optional[str]
# --------------------------------------------------------------------------- #


def _perturb_typo(question: str, rng: random.Random) -> Optional[str]:
    """Apply a single jamo keyboard-adjacency typo to one syllable.

    The scan is RNG-free and only marks syllables that have a *valid same-class*
    neighbour, so the subsequent edit always succeeds (no silent ``None`` after a
    syllable is selected). Exactly one syllable is changed.
    """
    # (char_index, which) for syllables with a valid 초성/중성 replacement.
    perturbable: list[tuple[int, str]] = []
    for i, ch in enumerate(question):
        decomp = _decompose(ch)
        if decomp is None:
            continue
        cho, jung, _ = decomp
        if _keyboard_candidates(_CHO[cho], _CHO_SET):
            perturbable.append((i, "cho"))
        if _keyboard_candidates(_JUNG[jung], _JUNG_SET):
            perturbable.append((i, "jung"))
    if not perturbable:
        return None
    idx, which = rng.choice(perturbable)
    cho, jung, jong = _decompose(question[idx])  # type: ignore[misc]
    if which == "cho":
        cho = _CHO.index(rng.choice(_keyboard_candidates(_CHO[cho], _CHO_SET)))
    else:
        jung = _JUNG.index(rng.choice(_keyboard_candidates(_JUNG[jung], _JUNG_SET)))
    out = question[:idx] + _compose(cho, jung, jong) + question[idx + 1:]
    return out if out != question else None


def _perturb_spacing(question: str, rng: random.Random) -> Optional[str]:
    """붙여쓰기: drop every internal space (the most common real mis-spacing)."""
    collapsed = "".join(question.split())
    return collapsed if collapsed != question else None


# Register shift: formal interrogatives -> casual / banmal command forms. Applied
# longest-pattern-first so "무엇입니까?" is caught before "입니까?".
_HONORIFIC_MAP: tuple[tuple[str, str], ...] = (
    ("무엇입니까?", "뭐임?"),
    ("무엇인가요?", "뭐임?"),
    ("무엇인가?", "뭐임?"),
    ("입니까?", "임?"),
    ("인가요?", "임?"),
    ("나요?", "냐?"),
    ("가요?", "냐?"),
    ("인가?", "임?"),
    ("는가?", "냐?"),
)


def _perturb_honorific(question: str, rng: random.Random) -> Optional[str]:
    """Shift politeness toward casual. Guaranteed change for ``?``-ending Qs."""
    for formal, casual in _HONORIFIC_MAP:
        if question.endswith(formal):
            return question[: -len(formal)] + casual
    # Generic fallback: drop the question mark, append a casual command tail.
    stripped = question.rstrip().rstrip("?？").rstrip()
    if stripped and stripped != question.rstrip():
        return stripped + " 알려줘"
    return None


def _perturb_truncation(question: str, rng: random.Random) -> Optional[str]:
    """User trails off: keep ~70% of the question + an ellipsis."""
    core = question.rstrip().rstrip("?？").rstrip()
    cut = int(len(core) * 0.7)
    if cut < 2 or cut >= len(core):
        return None
    return core[:cut].rstrip() + "…"


# Conversational hedges / fillers a real user prepends.
_FILLERS: tuple[str, ...] = ("혹시 ", "그 ", "저기 ", "음 ", "아 ")


def _perturb_paraphrase(question: str, rng: random.Random) -> Optional[str]:
    """Wrap with a deterministic conversational filler (light paraphrase)."""
    filler = rng.choice(_FILLERS)
    out = filler + question
    return out if out != question else None


# KO -> EN academic-term code-switch table (first match wins, longest first).
_CODESWITCH_MAP: tuple[tuple[str, str], ...] = (
    ("졸업요건", "graduation requirement"),
    ("장학금", "scholarship"),
    ("수강신청", "course registration"),
    ("복수전공", "double major"),
    ("부전공", "minor"),
    ("전과", "major change"),
    ("휴학", "leave of absence"),
    ("복학", "reinstatement"),
    ("졸업", "graduation"),
    ("학기", "semester"),
    ("학점", "credit"),
    ("수강", "course"),
    ("신청", "registration"),
    ("등록금", "tuition"),
    ("성적", "grade"),
    ("개강", "semester start"),
    ("종강", "semester end"),
)


def _perturb_codeswitch(question: str, rng: random.Random) -> Optional[str]:
    """Replace one Korean academic term with its English equivalent."""
    hits = [(ko, en) for ko, en in _CODESWITCH_MAP if ko in question]
    if not hits:
        return None
    ko, en = hits[0]  # longest-first table -> deterministic, most specific term
    return question.replace(ko, en, 1)


_TRANSFORMS: dict[str, Callable[[str, random.Random], Optional[str]]] = {
    "typo": _perturb_typo,
    "spacing": _perturb_spacing,
    "honorific": _perturb_honorific,
    "truncation": _perturb_truncation,
    "paraphrase": _perturb_paraphrase,
    "codeswitch": _perturb_codeswitch,
}


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #


def perturb_question(question: str, ptype: str, *, base_seed: int = DEFAULT_SEED,
                     parent_id: object = "") -> Optional[str]:
    """Perturb a single question by ``ptype``. Returns ``None`` if not applicable."""
    if ptype not in _TRANSFORMS:
        raise ValueError(f"unknown perturbation {ptype!r}; expected one of {list(PERTURBATIONS)}")
    return _TRANSFORMS[ptype](question, _rng(base_seed, parent_id, ptype))


def perturb_record(parent: dict, *, types: Iterable[str] = PERTURBATIONS,
                   base_seed: int = DEFAULT_SEED) -> list[dict]:
    """Generate perturbed child records for one answerable parent record.

    Each child **preserves the parent's ``ground_truth``** (and ``answerable``,
    ``intent``, ``gt_source``) so it stays offline-scorable; it adds ``parent_id``
    and ``perturbation`` provenance and a derived ``id`` (``<parent>__<ptype>``).
    Non-applicable transforms are skipped.
    """
    if not parent.get("answerable"):
        return []
    question = str(parent.get("question") or "")
    parent_id = parent.get("id")
    children: list[dict] = []
    for ptype in types:
        perturbed = perturb_question(
            question, ptype, base_seed=base_seed, parent_id=parent_id
        )
        if perturbed is None:
            continue
        children.append({
            "id": f"{parent_id}__{ptype}",
            "parent_id": parent_id,
            "perturbation": ptype,
            "question": perturbed,
            "ground_truth": parent.get("ground_truth"),  # PRESERVED verbatim
            "answerable": True,
            "intent": parent.get("intent"),
            "difficulty": parent.get("difficulty"),
            "gt_source": parent.get("gt_source"),
        })
    return children


def perturb_dataset(records: Iterable[dict], *, types: Iterable[str] = PERTURBATIONS,
                    base_seed: int = DEFAULT_SEED) -> list[dict]:
    """Perturb every answerable record in ``records`` -> flat list of children.

    Deterministic order: parents in input order, perturbations in ``types`` order.
    Unanswerable records are ignored (we only stress the answerable set).
    """
    out: list[dict] = []
    for parent in records:
        out.extend(perturb_record(parent, types=types, base_seed=base_seed))
    return out


def load_answerable_parents(path: Path | str = _DEFAULT_TESTSET) -> list[dict]:
    """Convenience loader: the committed test set's answerable records.

    Reads the ``{"meta", "results": [...]}`` inputs-only shape directly (no
    dependency on WS-B's ``dataset.py``) and returns only ``answerable`` records.
    """
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return [r for r in data.get("results", []) if r.get("answerable")]
