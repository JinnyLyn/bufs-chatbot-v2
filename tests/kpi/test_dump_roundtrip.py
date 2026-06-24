"""Regression: a live ``run``'s own ``predictions.json`` must round-trip through
``--from-predictions``.

The live CLI persists ``predictions.json`` as a list of ``{"source", "records"}``
wrappers (cli.py). Before this test, ``_load_dump_records`` only understood
``{"results": [...]}`` / bare lists, so re-scoring a real run's output silently
yielded zero scorable records → all-zero metrics → false NO-GO on the
gate-of-record (suhwan-captures-dump → from-predictions) path. The integration
fixture used the flat shape and missed it.
"""

import json

import pytest

from eval_tools.kpi.cli import _load_dump_records

pytestmark = pytest.mark.unit

_REC_A = {"id": "s01", "question": "q", "ground_truth": "130", "answerable": True, "answer": "130 학점"}
_REC_B = {"id": "u01", "question": "q2", "ground_truth": "", "answerable": False, "answer": "문서에 없습니다"}


def _write(tmp_path, name, obj):
    p = tmp_path / name
    p.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
    return p


def test_live_run_wrapper_shape_roundtrips(tmp_path):
    # exactly what cli persists: [{"source": ..., "records": [...]}]
    dump = _write(tmp_path, "run1.json", [{"source": "live", "records": [_REC_A, _REC_B]}])
    recs = _load_dump_records(dump)
    assert recs == [_REC_A, _REC_B]
    assert all("answer" in r and "ground_truth" in r for r in recs)


def test_multi_wrapper_flattens(tmp_path):
    dump = _write(tmp_path, "d.json", [
        {"source": "a", "records": [_REC_A]},
        {"source": "b", "records": [_REC_B]},
    ])
    assert _load_dump_records(dump) == [_REC_A, _REC_B]


def test_legacy_results_dict_still_works(tmp_path):
    dump = _write(tmp_path, "d.json", {"results": [_REC_A]})
    assert _load_dump_records(dump) == [_REC_A]


def test_records_dict_works(tmp_path):
    dump = _write(tmp_path, "d.json", {"records": [_REC_A]})
    assert _load_dump_records(dump) == [_REC_A]


def test_bare_record_list_still_works(tmp_path):
    # a flat list of scorable records (each has 'answer') is NOT a wrapper list
    dump = _write(tmp_path, "d.json", [_REC_A, _REC_B])
    assert _load_dump_records(dump) == [_REC_A, _REC_B]
