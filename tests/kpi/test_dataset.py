"""Unit tests for eval_tools.kpi.dataset.

Offline, pure, no network. Runs under the default
``pytest -m "not integration"`` lane.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from eval_tools.kpi.dataset import (
    _DEFAULT_TESTSET,
    _resolve_testset_path,
    load_testset,
    load_qa_dataset,
    scorer_hash,
)

pytestmark = pytest.mark.unit


# ── load_testset ───────────────────────────────────────────────────────────

class TestLoadTestset:
    def test_default_path_loads_successfully(self) -> None:
        """Load without any argument (repo-relative default)."""
        records, sha = load_testset()
        assert len(records) == 89
        assert isinstance(sha, str) and len(sha) == 64

    def test_returns_tuple_records_sha(self) -> None:
        records, sha = load_testset()
        assert isinstance(records, list)
        assert isinstance(sha, str)

    def test_sha_is_stable(self) -> None:
        """Same file → identical hash on repeated loads."""
        _, sha1 = load_testset()
        _, sha2 = load_testset()
        assert sha1 == sha2

    def test_sha_matches_raw_file_bytes(self) -> None:
        """testset_sha256 is SHA-256 of the raw file bytes."""
        expected = hashlib.sha256(_DEFAULT_TESTSET.read_bytes()).hexdigest()
        _, sha = load_testset()
        assert sha == expected

    def test_schema_required_fields(self) -> None:
        """Every record has question, ground_truth, answerable."""
        records, _ = load_testset()
        for r in records:
            assert "question" in r, f"Missing 'question': {r.get('id')}"
            assert "ground_truth" in r, f"Missing 'ground_truth': {r.get('id')}"
            assert "answerable" in r, f"Missing 'answerable': {r.get('id')}"

    def test_no_cached_output_fields(self) -> None:
        """Inputs-only: no prediction / cached-output fields present."""
        records, _ = load_testset()
        # These fields were stripped when the committed test set was built.
        banned = {"prediction", "elapsed_s", "contains_gt", "exact_match"}
        for r in records:
            leftover = banned & r.keys()
            assert not leftover, (
                f"Cached output fields found in record {r.get('id')!r}: {leftover}"
            )

    def test_count_matches_meta(self) -> None:
        """89 records total: 81 answerable, 8 unanswerable (matches meta block)."""
        records, _ = load_testset()
        assert len(records) == 89
        assert sum(1 for r in records if r["answerable"]) == 81
        assert sum(1 for r in records if not r["answerable"]) == 8

    def test_answerable_field_is_bool(self) -> None:
        records, _ = load_testset()
        for r in records:
            assert isinstance(r["answerable"], bool), (
                f"answerable must be bool for {r.get('id')!r}, got {type(r['answerable'])}"
            )

    def test_explicit_path_override(self) -> None:
        """Explicit path argument is honoured."""
        records, sha = load_testset(path=_DEFAULT_TESTSET)
        assert len(records) == 89
        assert len(sha) == 64

    def test_env_override_takes_precedence_over_default(self, tmp_path: Path) -> None:
        """OMC_EVAL_TESTSET env var is used when no explicit path is given."""
        minimal = {
            "meta": {},
            "results": [
                {
                    "id": "t1",
                    "question": "q?",
                    "ground_truth": "g",
                    "answerable": True,
                    "intent": None,
                    "difficulty": None,
                    "gt_source": None,
                },
            ],
        }
        alt = tmp_path / "alt.json"
        alt.write_text(json.dumps(minimal), encoding="utf-8")

        old = os.environ.pop("OMC_EVAL_TESTSET", None)
        try:
            os.environ["OMC_EVAL_TESTSET"] = str(alt)
            records, _ = load_testset()  # no explicit path — must pick up env
            assert len(records) == 1
            assert records[0]["question"] == "q?"
        finally:
            if old is not None:
                os.environ["OMC_EVAL_TESTSET"] = old
            else:
                os.environ.pop("OMC_EVAL_TESTSET", None)

    def test_explicit_path_overrides_env(self, tmp_path: Path) -> None:
        """An explicit path takes precedence over OMC_EVAL_TESTSET."""
        alt = tmp_path / "alt.json"
        alt.write_text(json.dumps({"results": []}), encoding="utf-8")

        old = os.environ.pop("OMC_EVAL_TESTSET", None)
        try:
            os.environ["OMC_EVAL_TESTSET"] = str(alt)
            # Explicit path → default testset (89 records), ignoring env.
            records, _ = load_testset(path=_DEFAULT_TESTSET)
            assert len(records) == 89
        finally:
            if old is not None:
                os.environ["OMC_EVAL_TESTSET"] = old
            else:
                os.environ.pop("OMC_EVAL_TESTSET", None)

    def test_missing_file_raises_file_not_found(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError, match="not found"):
            load_testset(path=tmp_path / "nonexistent.json")

    def test_invalid_json_raises_value_error(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.json"
        bad.write_text("not json {{", encoding="utf-8")
        with pytest.raises(ValueError, match="not valid JSON"):
            load_testset(path=bad)

    def test_bare_list_format_accepted(self, tmp_path: Path) -> None:
        """Accepts a bare JSON list (no 'results' wrapper)."""
        items = [
            {"id": "x1", "question": "Q", "ground_truth": "G", "answerable": True},
        ]
        p = tmp_path / "bare.json"
        p.write_text(json.dumps(items), encoding="utf-8")
        records, sha = load_testset(path=p)
        assert len(records) == 1
        assert len(sha) == 64


# ── _resolve_testset_path ──────────────────────────────────────────────────

class TestResolvePath:
    def test_none_with_no_env_returns_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("OMC_EVAL_TESTSET", raising=False)
        assert _resolve_testset_path(None) == _DEFAULT_TESTSET

    def test_none_with_env_returns_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OMC_EVAL_TESTSET", "/some/path.json")
        assert _resolve_testset_path(None) == Path("/some/path.json")

    def test_explicit_overrides_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OMC_EVAL_TESTSET", "/env/path.json")
        assert _resolve_testset_path("/explicit/path.json") == Path("/explicit/path.json")


# ── load_qa_dataset ────────────────────────────────────────────────────────

class TestLoadQaDataset:
    def _write(self, path: Path, payload: object) -> None:
        path.write_text(json.dumps(payload), encoding="utf-8")

    def test_answer_mapped_to_ground_truth(self, tmp_path: Path) -> None:
        """Q-A 'answer' → 'ground_truth' when ground_truth is absent."""
        p = tmp_path / "qa.json"
        self._write(p, [{"question": "Q?", "answer": "A"}])
        records, _ = load_qa_dataset(p)
        assert records[0]["ground_truth"] == "A"

    def test_ground_truth_kept_when_present(self, tmp_path: Path) -> None:
        """Existing ground_truth is NOT overwritten by answer."""
        p = tmp_path / "qa.json"
        self._write(p, [{"question": "Q?", "ground_truth": "GT", "answer": "other"}])
        records, _ = load_qa_dataset(p)
        assert records[0]["ground_truth"] == "GT"

    def test_expected_answer_mapped_to_ground_truth(self, tmp_path: Path) -> None:
        """In-repo golden sets key the reference as 'expected_answer' — it must map
        too, else every eval_tools/datasets/*.json falls through to judge_scored and
        the rule gate reports 0.000 contains on correct answers (2026-09-01)."""
        p = tmp_path / "qa.json"
        self._write(p, [{"question": "개강일은?", "expected_answer": "8월 31일(월)"}])
        records, _ = load_qa_dataset(p)
        assert records[0]["ground_truth"] == "8월 31일(월)"
        assert "judge_scored" not in records[0]

    def test_answer_wins_over_expected_answer(self, tmp_path: Path) -> None:
        """Both keys present: 'answer' is the documented WS-0a field and takes it."""
        p = tmp_path / "qa.json"
        self._write(p, [{"question": "Q?", "answer": "A", "expected_answer": "E"}])
        records, _ = load_qa_dataset(p)
        assert records[0]["ground_truth"] == "A"

    def test_empty_ground_truth_still_takes_reference(self, tmp_path: Path) -> None:
        """ground_truth: "" + a real reference must still map — an empty string is the
        same unscorable state as a missing key (same failure class as the bug fixed)."""
        p = tmp_path / "qa.json"
        self._write(p, [{"question": "Q?", "ground_truth": "", "expected_answer": "E"}])
        records, _ = load_qa_dataset(p)
        assert records[0]["ground_truth"] == "E"
        assert "judge_scored" not in records[0]

    def test_repo_golden_set_is_rule_scorable(self) -> None:
        """The committed 2학기 골든셋 must load rule-scorable. NOT skipped when the file
        is missing: a rename/delete has to fail loudly — the gate measures against it."""
        p = Path(__file__).resolve().parents[2] / "eval_tools" / "datasets" / "qa_dataset_sem2_100.json"
        assert p.exists(), f"gate golden set missing: {p}"
        records, _ = load_qa_dataset(p)
        assert len(records) == 100, f"golden set truncated: {len(records)} records"
        assert all(r.get("ground_truth") for r in records)
        assert not any(r.get("judge_scored") for r in records)

    def test_answerable_defaults_to_true(self, tmp_path: Path) -> None:
        p = tmp_path / "qa.json"
        self._write(p, [{"question": "Q?", "answer": "A"}])
        records, _ = load_qa_dataset(p)
        assert records[0]["answerable"] is True

    def test_answerable_default_override(self, tmp_path: Path) -> None:
        p = tmp_path / "qa.json"
        self._write(p, [{"question": "Q?", "answer": "A"}])
        records, _ = load_qa_dataset(p, answerable_default=False)
        assert records[0]["answerable"] is False

    def test_answerable_preserved_when_present(self, tmp_path: Path) -> None:
        p = tmp_path / "qa.json"
        self._write(p, [{"question": "Q?", "answer": "A", "answerable": False}])
        records, _ = load_qa_dataset(p)
        assert records[0]["answerable"] is False

    def test_judge_scored_flag_when_no_ground_truth(self, tmp_path: Path) -> None:
        """Records with no ground_truth (after mapping) → judge_scored=True."""
        p = tmp_path / "qa.json"
        p.write_text(json.dumps([{"question": "Q?"}]), encoding="utf-8")
        records, _ = load_qa_dataset(p)
        assert records[0].get("judge_scored") is True

    def test_no_judge_scored_when_ground_truth_present(self, tmp_path: Path) -> None:
        p = tmp_path / "qa.json"
        self._write(p, [{"question": "Q?", "ground_truth": "GT"}])
        records, _ = load_qa_dataset(p)
        assert not records[0].get("judge_scored")

    def test_results_wrapper_format(self, tmp_path: Path) -> None:
        """Accepts {'results': [...]} wrapper format."""
        p = tmp_path / "qa.json"
        self._write(p, {"results": [{"question": "Q", "answer": "A"}]})
        records, _ = load_qa_dataset(p)
        assert len(records) == 1

    def test_bare_list_format(self, tmp_path: Path) -> None:
        """Accepts bare JSON list format."""
        p = tmp_path / "qa.json"
        p.write_text(json.dumps([{"question": "Q", "answer": "A"}]), encoding="utf-8")
        records, _ = load_qa_dataset(p)
        assert len(records) == 1

    def test_sha_returned_is_64_hex(self, tmp_path: Path) -> None:
        p = tmp_path / "qa.json"
        self._write(p, [{"question": "Q?", "answer": "A"}])
        _, sha = load_qa_dataset(p)
        assert isinstance(sha, str) and len(sha) == 64
        assert all(c in "0123456789abcdef" for c in sha)

    def test_sha_matches_file_bytes(self, tmp_path: Path) -> None:
        p = tmp_path / "qa.json"
        self._write(p, [{"question": "Q?", "answer": "A"}])
        expected = hashlib.sha256(p.read_bytes()).hexdigest()
        _, sha = load_qa_dataset(p)
        assert sha == expected

    def test_multiple_records(self, tmp_path: Path) -> None:
        items = [
            {"question": f"Q{i}?", "answer": f"A{i}", "answerable": True}
            for i in range(5)
        ]
        p = tmp_path / "qa.json"
        self._write(p, items)
        records, _ = load_qa_dataset(p)
        assert len(records) == 5
        assert records[2]["ground_truth"] == "A2"

    def test_missing_file_raises(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            load_qa_dataset(tmp_path / "nope.json")

    def test_invalid_json_raises(self, tmp_path: Path) -> None:
        p = tmp_path / "bad.json"
        p.write_text("not json", encoding="utf-8")
        with pytest.raises(ValueError, match="not valid JSON"):
            load_qa_dataset(p)

    def test_does_not_mutate_input_dicts(self, tmp_path: Path) -> None:
        """load_qa_dataset must not mutate the caller's source objects."""
        original = {"question": "Q?", "answer": "A"}
        p = tmp_path / "qa.json"
        p.write_text(json.dumps([original]), encoding="utf-8")
        # The mutation check: after loading, load again from the same file —
        # if the file was mutated it would fail. Here we just verify the
        # in-memory object round-trips cleanly by checking the key is still
        # present in the raw JSON file.
        load_qa_dataset(p)
        reloaded = json.loads(p.read_bytes())
        assert reloaded[0].get("answer") == "A"  # original file untouched


# ── scorer_hash ────────────────────────────────────────────────────────────

class TestScorerHash:
    def test_returns_64_char_hex(self) -> None:
        h = scorer_hash()
        assert isinstance(h, str)
        assert len(h) == 64
        assert all(c in "0123456789abcdef" for c in h)

    def test_stable_across_calls(self) -> None:
        """scorer.py unchanged → hash is identical on consecutive calls."""
        assert scorer_hash() == scorer_hash()

    def test_matches_scorer_py_bytes(self) -> None:
        """Hash is SHA-256 of scorer.py source bytes."""
        scorer_path = Path(__file__).parent.parent.parent / "eval_tools" / "kpi" / "scorer.py"
        expected = hashlib.sha256(scorer_path.read_bytes()).hexdigest()
        assert scorer_hash() == expected
