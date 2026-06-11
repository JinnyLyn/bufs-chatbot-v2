"""Offline parser unit tests for debug.logs.

All tests use committed fixture files under tests/fixtures/logs/ — no live
dependencies (no Langfuse, no Ollama, no Qdrant, no network).

Fixture provenance
------------------
app_log_samples.log  — 22 real lines from 2026-06-05..10 production logs
qa_records.jsonl     — 8 records from 2026-06-05..08 production logs
                        (a687e093 answer truncated to 500ch + ellipsis)
synthetic_lines.log  — SYNTHETIC [chat-ERR] + Q&A-write-failure lines
                        (no real occurrences in production as of 2026-06-10)
README.md            — provenance and caveats

Grammar reference: .omc/research/log-study-applog.md (1,632 lines verified)
Schema reference:  .omc/research/log-study-qa.md   (496 records verified)
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

# Import the parser under test
from debug.logs import (
    CHAT_ERR_RE,
    CHAT_IN_RE,
    CHAT_OUT_RE,
    LINE_RE,
    PIPELINE_TIMING_RE,
    QA_LOG_FAIL_RE,
    LogLine,
    display_log_lines,
    grep_app_logs,
    grep_qa_logs,
    parse_chat_err,
    parse_chat_in,
    parse_chat_out,
    parse_line,
    parse_pipeline_timing,
    parse_qa_log_fail,
)

# ── fixture paths ─────────────────────────────────────────────────────────────

FIXTURES = Path(__file__).parent.parent / "fixtures" / "logs"
APP_LOG = FIXTURES / "app_log_samples.log"
QA_JSONL = FIXTURES / "qa_records.jsonl"
SYNTHETIC_LOG = FIXTURES / "synthetic_lines.log"


# ── helpers ───────────────────────────────────────────────────────────────────

def _real_lines() -> list[str]:
    return APP_LOG.read_text(encoding="utf-8").splitlines()


def _parsed_real_lines() -> list:
    return [parse_line(ln) for ln in _real_lines()]


def _qa_records() -> list[dict]:
    records = []
    for raw in QA_JSONL.read_text(encoding="utf-8").splitlines():
        raw = raw.strip()
        if raw:
            records.append(json.loads(raw))
    return records


def _synthetic_lines() -> list[str]:
    """Return non-comment, non-blank lines from synthetic_lines.log."""
    lines = []
    for ln in SYNTHETIC_LOG.read_text(encoding="utf-8").splitlines():
        if ln.startswith("#") or not ln.strip():
            continue
        lines.append(ln)
    return lines


# ═════════════════════════════════════════════════════════════════════════════
# § 1 — app.log line grammar
# ═════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
class TestLineGrammar:
    """Tests for the top-level LINE_RE and parse_line()."""

    def test_all_real_lines_match_grammar(self):
        """Every line in app_log_samples.log must match LINE_RE."""
        mismatches = []
        for ln in _real_lines():
            if not LINE_RE.match(ln):
                mismatches.append(ln[:80])
        assert not mismatches, f"Lines not matching grammar:\n" + "\n".join(mismatches)

    def test_parse_line_returns_logline(self):
        parsed = _parsed_real_lines()
        assert all(p is not None for p in parsed), "parse_line returned None for a real line"

    def test_timestamp_format(self):
        """Timestamp must be YYYY-MM-DD HH:MM:SS,mmm (comma before ms)."""
        import re
        ts_re = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3}$")
        for p in _parsed_real_lines():
            assert ts_re.match(p.timestamp), f"bad timestamp: {p.timestamp!r}"

    def test_tid_shapes(self):
        """tid is either 8 lowercase hex chars or the literal '-'."""
        import re
        hex8 = re.compile(r"^[0-9a-f]{8}$")
        for p in _parsed_real_lines():
            assert p.tid == "-" or hex8.match(p.tid), f"unexpected tid: {p.tid!r}"

    def test_level_values(self):
        """Only INFO and WARNING appear in the corpus."""
        for p in _parsed_real_lines():
            assert p.level in ("INFO", "WARNING"), f"unexpected level: {p.level!r}"

    def test_logger_name_format(self):
        """logger name must be dot-separated word chars."""
        import re
        name_re = re.compile(r"^[\w.]+$")
        for p in _parsed_real_lines():
            assert name_re.match(p.logger), f"bad logger name: {p.logger!r}"

    def test_utf8_emoji_and_korean(self):
        """Lines with emoji (🔨🚀) and Korean text must parse without error."""
        emoji_lines = [p for p in _parsed_real_lines() if "🔨" in p.message or "🚀" in p.message]
        assert emoji_lines, "No startup emoji lines found in fixture"
        for p in emoji_lines:
            assert p.logger == "__main__"

    def test_windows_path_in_message(self):
        r"""Startup log path line contains C:\Users\... — must parse."""
        path_lines = [p for p in _parsed_real_lines() if r"C:\Users" in p.message]
        assert path_lines, r"No C:\Users line found in fixture"

    def test_dash_tid_on_startup_lines(self):
        """Startup / lifespan lines have tid == '-'."""
        dash_lines = [p for p in _parsed_real_lines() if p.tid == "-"]
        assert dash_lines, "No '-' tid lines in fixture"
        loggers = {p.logger for p in dash_lines}
        # startup lines come from __main__, sentence_transformers, huggingface_hub, httpx
        assert "__main__" in loggers or "sentence_transformers.base.model" in loggers

    def test_old_httpx_info_line(self):
        """The old-format httpx INFO line (2026-06-05, before log quieting) parses."""
        httpx_lines = [p for p in _parsed_real_lines() if p.logger == "httpx"]
        assert httpx_lines, "No httpx line in fixture (was 2026-06-05 sample)"
        for p in httpx_lines:
            assert p.level == "INFO"
            assert p.tid == "-"

    def test_hf_token_warning(self):
        """The HF_TOKEN warning line parses with correct logger."""
        hf_lines = [
            p for p in _parsed_real_lines()
            if "unauthenticated requests to the HF Hub" in p.message
        ]
        assert hf_lines
        for p in hf_lines:
            assert p.level == "WARNING"
            assert "huggingface_hub" in p.logger

    def test_langfuse_observability_warning(self):
        """The core.observability Langfuse-init-failure WARNING parses."""
        obs_lines = [
            p for p in _parsed_real_lines()
            if "core.observability" in p.logger
        ]
        assert obs_lines, "No core.observability line in fixture"
        for p in obs_lines:
            assert p.level == "WARNING"
            assert "Could not initialize Langfuse" in p.message

    def test_runtime_dict_line(self):
        """The lifespan:52 startup line with runtime={...} dict parses as one line."""
        runtime_lines = [
            p for p in _parsed_real_lines()
            if "RAG system ready" in p.message and "runtime=" in p.message
        ]
        assert len(runtime_lines) == 2, (
            f"Expected 2 runtime= lines (langfuse_enabled False + True), "
            f"got {len(runtime_lines)}"
        )
        # Verify langfuse_enabled variants
        enabled_vals = {
            "True" if "langfuse_enabled': True" in p.message
            else "False" if "langfuse_enabled': False" in p.message
            else "?"
            for p in runtime_lines
        }
        assert "True" in enabled_vals and "False" in enabled_vals


# ═════════════════════════════════════════════════════════════════════════════
# § 2 — [chat-IN] field extraction
# ═════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
class TestChatIn:
    """Tests for parse_chat_in() against real fixture lines."""

    def _chat_in_lines(self):
        return [p for p in _parsed_real_lines() if "[chat-IN]" in p.message]

    def test_chat_in_count(self):
        """Fixture contains exactly 4 [chat-IN] lines (a4f2878e, e9eb99b1, a687e093, c12fa94e)."""
        assert len(self._chat_in_lines()) == 4

    def test_all_chat_in_parse(self):
        for p in self._chat_in_lines():
            result = parse_chat_in(p.message)
            assert result is not None, f"parse_chat_in returned None for: {p.message[:80]}"

    def test_tid_matches_bracket(self):
        """The tid= field in the message must equal the bracket [tid]."""
        for p in self._chat_in_lines():
            fields = parse_chat_in(p.message)
            assert fields["tid"] == p.tid

    def test_normal_single_quoted_q(self):
        """Standard single-quoted q= parses correctly."""
        # a4f2878e: q='수강신청은 어떻게 하나요?'
        target = next(p for p in self._chat_in_lines() if p.tid == "a4f2878e")
        fields = parse_chat_in(target.message)
        assert fields["q"] == "수강신청은 어떻게 하나요?"
        assert fields["q_chars"] == 14
        assert fields["test"] is False

    def test_double_quoted_q_repr_flip(self):
        """When q contains apostrophe, Python repr uses double quotes (c12fa94e)."""
        target = next(p for p in self._chat_in_lines() if p.tid == "c12fa94e")
        fields = parse_chat_in(target.message)
        # q_chars=153 but q is truncated to 80 chars
        assert fields["q_chars"] == 153
        q_val = fields["q"]
        assert len(q_val) == 80, f"q should be 80 chars (truncated), got {len(q_val)}"
        # The double-quote repr flip means q contains an apostrophe ("They're")
        assert "'" in q_val  # apostrophe inside the 80-char truncation

    def test_q_truncation_flag(self):
        """q_chars > 80 iff q is truncated."""
        for p in self._chat_in_lines():
            fields = parse_chat_in(p.message)
            q = fields["q"]
            if fields["q_chars"] > 80:
                assert len(q) == 80, f"expected truncated q to be 80 chars for {p.tid}"
            else:
                assert len(q) == fields["q_chars"], (
                    f"non-truncated q length mismatch for {p.tid}: "
                    f"q_chars={fields['q_chars']} but len(q)={len(q)}"
                )

    def test_orphan_chat_in_no_out(self):
        """e9eb99b1: orphan chat-IN has no matching chat-OUT in fixture."""
        in_tids = {p.tid for p in self._chat_in_lines()}
        out_tids = {
            p.tid for p in _parsed_real_lines() if "[chat-OUT]" in p.message
        }
        orphan_tids = in_tids - out_tids
        assert "e9eb99b1" in orphan_tids, (
            "Expected orphan tid e9eb99b1 to have IN but no OUT"
        )


# ═════════════════════════════════════════════════════════════════════════════
# § 3 — [chat-OUT] field extraction
# ═════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
class TestChatOut:
    """Tests for parse_chat_out() against real fixture lines."""

    def _chat_out_lines(self):
        return [p for p in _parsed_real_lines() if "[chat-OUT]" in p.message]

    def test_chat_out_count(self):
        """Fixture contains exactly 2 [chat-OUT] lines (a4f2878e, a687e093)."""
        assert len(self._chat_out_lines()) == 2

    def test_all_chat_out_parse(self):
        for p in self._chat_out_lines():
            assert parse_chat_out(p.message) is not None

    def test_tid_matches_bracket(self):
        for p in self._chat_out_lines():
            fields = parse_chat_out(p.message)
            assert fields["tid"] == p.tid

    def test_total_ms_is_bare_int(self):
        """chat-OUT total_ms has NO 'ms' suffix (unlike PIPELINE_TIMING)."""
        for p in self._chat_out_lines():
            fields = parse_chat_out(p.message)
            # If the regex matched, the value was parsed as int successfully
            assert isinstance(fields["total_ms"], int)
            assert fields["total_ms"] > 0

    def test_runaway_trace_a687e093(self):
        """a687e093 (290s runaway): answer_chars=21511 results=4 total_ms=290452."""
        target = next(p for p in self._chat_out_lines() if p.tid == "a687e093")
        fields = parse_chat_out(target.message)
        assert fields["answer_chars"] == 21511
        assert fields["results"] == 4
        assert fields["total_ms"] == 290452

    def test_normal_request_a4f2878e(self):
        target = next(p for p in self._chat_out_lines() if p.tid == "a4f2878e")
        fields = parse_chat_out(target.message)
        assert fields["answer_chars"] == 581
        assert fields["total_ms"] == 16937


# ═════════════════════════════════════════════════════════════════════════════
# § 4 — PIPELINE_TIMING field extraction
# ═════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
class TestPipelineTiming:
    """Tests for parse_pipeline_timing() against real fixture lines."""

    def _timing_lines(self):
        return [p for p in _parsed_real_lines() if "PIPELINE_TIMING" in p.message]

    def test_timing_count(self):
        """Fixture contains 3 PIPELINE_TIMING lines (a4f2878e, a687e093, 7f37cac8)."""
        assert len(self._timing_lines()) == 3

    def test_all_timing_parse(self):
        for p in self._timing_lines():
            assert parse_pipeline_timing(p.message) is not None

    def test_ms_suffix_stripped(self):
        """Stage values carry 'ms' in log but must be returned as ints."""
        for p in self._timing_lines():
            fields = parse_pipeline_timing(p.message)
            for key in ("total_ms", "summarize_ms", "rewrite_ms", "agent_ms", "aggregate_ms", "other_ms"):
                assert isinstance(fields[key], int), f"{key} must be int"

    def test_other_always_zero(self):
        """'other' is always 0 in all 496/496 real records."""
        for p in self._timing_lines():
            fields = parse_pipeline_timing(p.message)
            assert fields["other_ms"] == 0

    def test_runaway_aggregate_a687e093(self):
        """a687e093: aggregate=284594ms (runaway answer synthesis)."""
        target = next(p for p in self._timing_lines() if p.tid == "a687e093")
        fields = parse_pipeline_timing(target.message)
        assert fields["aggregate_ms"] == 284594
        assert fields["total_ms"] == 290452
        assert fields["sub_q"] == 1
        assert fields["tool_calls"] == 1

    def test_agent_loop_7f37cac8(self):
        """7f37cac8: tool_calls=8 agent_loop blowup."""
        target = next(p for p in self._timing_lines() if p.tid == "7f37cac8")
        fields = parse_pipeline_timing(target.message)
        assert fields["tool_calls"] == 8
        assert fields["agent_ms"] == 143719

    def test_stage_sum_lte_total(self):
        """stage sum must never exceed total (max gap 6s observed in production)."""
        for p in self._timing_lines():
            fields = parse_pipeline_timing(p.message)
            stage_sum = (
                fields["summarize_ms"] + fields["rewrite_ms"]
                + fields["agent_ms"] + fields["aggregate_ms"] + fields["other_ms"]
            )
            assert stage_sum <= fields["total_ms"], (
                f"tid={fields['tid']}: stage_sum={stage_sum} > total={fields['total_ms']}"
            )

    def test_no_ms_suffix_in_non_timing_chat_out(self):
        """chat-OUT total_ms field does NOT use ms suffix; PIPELINE_TIMING does."""
        chat_out_lines = [p for p in _parsed_real_lines() if "[chat-OUT]" in p.message]
        for p in chat_out_lines:
            # If chat-OUT message were parsed by PIPELINE_TIMING_RE it would fail
            assert parse_pipeline_timing(p.message) is None


# ═════════════════════════════════════════════════════════════════════════════
# § 5 — QA record schema
# ═════════════════════════════════════════════════════════════════════════════

EXPECTED_KEYS = frozenset(
    "timestamp trace_id session_id model intent question answer "
    "duration_ms num_results sources sub_questions tool_calls timing".split()
)
EXPECTED_TIMING_KEYS = frozenset(
    "summarize_history rewrite_query agent aggregate_answers other".split()
)


@pytest.mark.unit
class TestQASchema:
    """Tests for the 13-key QA JSONL schema against real fixture records."""

    def test_all_records_parse_as_json(self):
        records = _qa_records()
        assert len(records) == 8

    def test_exactly_13_keys(self):
        for rec in _qa_records():
            missing = EXPECTED_KEYS - rec.keys()
            extra = rec.keys() - EXPECTED_KEYS
            assert not missing, f"trace_id={rec.get('trace_id')}: missing keys {missing}"
            assert not extra,   f"trace_id={rec.get('trace_id')}: extra keys {extra}"

    def test_trace_id_is_8hex(self):
        import re
        hex8 = re.compile(r"^[0-9a-f]{8}$")
        for rec in _qa_records():
            assert hex8.match(rec["trace_id"]), f"bad trace_id: {rec['trace_id']!r}"

    def test_session_id_is_uuid4(self):
        import re
        uuid4_re = re.compile(
            r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
        )
        for rec in _qa_records():
            assert uuid4_re.match(rec["session_id"]), (
                f"bad session_id: {rec['session_id']!r}"
            )

    def test_timestamp_format(self):
        """Timestamp: YYYY-MM-DDTHH:MM:SS — no TZ, no microseconds."""
        import re
        ts_re = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}$")
        for rec in _qa_records():
            assert ts_re.match(rec["timestamp"]), f"bad timestamp: {rec['timestamp']!r}"

    def test_intent_always_empty_string(self):
        """intent is always '' in production records."""
        for rec in _qa_records():
            assert rec["intent"] == "", f"intent not empty: {rec['intent']!r}"

    def test_timing_has_5_int_keys(self):
        for rec in _qa_records():
            t = rec["timing"]
            assert isinstance(t, dict), "timing must be a dict"
            missing = EXPECTED_TIMING_KEYS - t.keys()
            extra = t.keys() - EXPECTED_TIMING_KEYS
            assert not missing, f"timing missing keys: {missing}"
            assert not extra,   f"timing extra keys: {extra}"
            for k, v in t.items():
                assert isinstance(v, int), f"timing.{k} must be int, got {type(v)}"

    def test_timing_other_always_zero(self):
        for rec in _qa_records():
            assert rec["timing"]["other"] == 0

    def test_duration_gte_timing_sum(self):
        """duration_ms >= sum(timing.values()) for all records."""
        for rec in _qa_records():
            timing_sum = sum(rec["timing"].values())
            assert rec["duration_ms"] >= timing_sum, (
                f"trace_id={rec['trace_id']}: duration={rec['duration_ms']} < "
                f"timing_sum={timing_sum}"
            )

    def test_num_results_gte_sources_len(self):
        """num_results >= len(sources) always."""
        for rec in _qa_records():
            assert rec["num_results"] >= len(rec["sources"]), (
                f"trace_id={rec['trace_id']}: num_results={rec['num_results']} < "
                f"len(sources)={len(rec['sources'])}"
            )

    def test_zero_results_implies_empty_sources(self):
        """num_results==0 ⇔ sources==[] ⇔ sub_questions==0."""
        for rec in _qa_records():
            if rec["num_results"] == 0:
                assert rec["sources"] == [], f"trace_id={rec['trace_id']}: num_results=0 but sources non-empty"
                assert rec["sub_questions"] == 0

    def test_answer_never_empty(self):
        for rec in _qa_records():
            assert rec["answer"], f"trace_id={rec['trace_id']}: empty answer"

    def test_runaway_record_a687e093(self):
        """a687e093: 290452ms, answer truncated in fixture."""
        recs = {r["trace_id"]: r for r in _qa_records()}
        assert "a687e093" in recs, "runaway fixture record missing"
        rec = recs["a687e093"]
        assert rec["duration_ms"] == 290452
        assert rec["num_results"] == 4
        # Answer was truncated in fixture (see README.md)
        assert "truncated" in rec["answer"] or len(rec["answer"]) <= 600

    def test_zero_results_record_5eda47e8(self):
        recs = {r["trace_id"]: r for r in _qa_records()}
        assert "5eda47e8" in recs
        rec = recs["5eda47e8"]
        assert rec["num_results"] == 0
        assert rec["sources"] == []
        assert rec["sub_questions"] == 0
        assert rec["tool_calls"] == 0

    def test_sources_are_strings(self):
        for rec in _qa_records():
            for s in rec["sources"]:
                assert isinstance(s, str)

    def test_model_constant(self):
        """model is always qwen3.5:9b in the committed corpus."""
        for rec in _qa_records():
            assert rec["model"] == "qwen3.5:9b"


# ═════════════════════════════════════════════════════════════════════════════
# § 6 — Synthetic: [chat-ERR] and QA-write-failure
# ═════════════════════════════════════════════════════════════════════════════
#
# IMPORTANT: These paths have ZERO real production samples (1,632 lines examined).
# The synthetic fixtures cover the grammar from chat.py:57 and chat.py:114 ONLY.
# Do not interpret passing tests as evidence that these paths are exercised by
# real traffic.

@pytest.mark.unit
class TestSyntheticPaths:
    """Tests for [chat-ERR] + QA-write-failure against SYNTHETIC fixtures only."""

    def _synth_lines(self):
        return [parse_line(ln) for ln in _synthetic_lines()]

    def test_synthetic_lines_all_parse(self):
        """Every line in synthetic_lines.log must match LINE_RE."""
        for ln in _synthetic_lines():
            assert LINE_RE.match(ln), f"synthetic line does not match grammar: {ln[:80]}"

    def test_synthetic_line_levels(self):
        """Per-line levels match the real logger calls:
        - "Q&A log failed:" wrapper (chat.py:57)  → WARNING
        - "[chat-ERR]"      (chat.py:114)          → ERROR
        - "Q&A log write failed" (qa_logger.py:78) → ERROR
        """
        for p in self._synth_lines():
            assert p is not None
            if "[chat-ERR]" in p.message:
                assert p.level == "ERROR", f"chat-ERR must be ERROR: {p.message}"
            elif "Q&A log write failed" in p.message:
                assert p.level == "ERROR", f"qa_logger write-fail must be ERROR: {p.message}"
            elif "Q&A log failed" in p.message:
                assert p.level == "WARNING", f"chat.py:57 wrapper must be WARNING: {p.message}"

    def test_chat_err_parse_deadbeef(self):
        """[chat-ERR] line for tid deadbeef parses correctly."""
        err_lines = [
            p for p in self._synth_lines()
            if p and "[chat-ERR]" in p.message
        ]
        assert err_lines, "No [chat-ERR] synthetic lines found"
        for p in err_lines:
            assert p.level == "ERROR", f"chat-ERR line must be ERROR level: {p.message}"
            fields = parse_chat_err(p.message)
            assert fields is not None, f"parse_chat_err returned None for: {p.message}"
            assert len(fields["tid"]) == 8
            assert fields["error"]  # non-empty error message

    def test_chat_err_tid_values(self):
        """Both synthetic tids (deadbeef, cafe1234) are parsed."""
        err_lines = [
            p for p in self._synth_lines()
            if p and "[chat-ERR]" in p.message
        ]
        tids = {parse_chat_err(p.message)["tid"] for p in err_lines}
        assert "deadbeef" in tids
        assert "cafe1234" in tids
        for p in err_lines:
            assert p.level == "ERROR", f"chat-ERR line must be ERROR level: {p.message}"

    def test_qa_log_fail_parse(self):
        """Q&A log failed: lines parse with parse_qa_log_fail()."""
        fail_lines = [
            p for p in self._synth_lines()
            if p and "Q&A log failed" in p.message
        ]
        assert fail_lines, "No QA-write-failure synthetic lines found"
        for p in fail_lines:
            fields = parse_qa_log_fail(p.message)
            assert fields is not None
            assert fields["error"]  # non-empty exception text

    def test_qa_log_fail_bracket_tid_is_dash_or_hex(self):
        """QA-write-failure lines carry a real tid in the bracket (not '-')."""
        fail_lines = [
            p for p in self._synth_lines()
            if p and "Q&A log failed" in p.message
        ]
        import re
        hex8 = re.compile(r"^[0-9a-f]{8}$")
        for p in fail_lines:
            assert hex8.match(p.tid) or p.tid == "-", f"unexpected tid: {p.tid!r}"

    def test_chat_err_not_in_real_lines(self):
        """Verify zero [chat-ERR] lines exist in the real production fixture."""
        real = [p for p in _parsed_real_lines() if p and "[chat-ERR]" in p.message]
        assert real == [], (
            "Unexpected [chat-ERR] lines in real fixture — update this test "
            "and remove the 'synthetic only' caveat if real samples exist."
        )

    def test_qa_log_fail_not_in_real_lines(self):
        """Verify zero Q&A-log-failed lines exist in the real production fixture."""
        real = [
            p for p in _parsed_real_lines()
            if p and "Q&A log failed" in p.message
        ]
        assert real == [], (
            "Unexpected QA-log-fail lines in real fixture — update this test "
            "and remove the 'synthetic only' caveat if real samples exist."
        )

    def test_qa_log_fail_re_matches_both_wordings(self):
        """QA_LOG_FAIL_RE matches the chat.py:57 wrapper AND the qa_logger.py:78
        ERROR ("write failed") wording, capturing the exception text either way."""
        warn = QA_LOG_FAIL_RE.search("Q&A log failed: foo")
        assert warn is not None, "must match chat.py:57 'Q&A log failed:' wrapper"
        assert warn.group(1) == "foo"
        err = QA_LOG_FAIL_RE.search("Q&A log write failed: bar")
        assert err is not None, "must match qa_logger.py:78 'Q&A log write failed:'"
        assert err.group(1) == "bar"

    def test_qa_log_write_failed_is_error_level(self):
        """The qa_logger.py:78 line is ERROR level and parses via parse_qa_log_fail."""
        write_fail = [
            p for p in self._synth_lines()
            if p and "Q&A log write failed" in p.message
        ]
        assert write_fail, "No qa_logger write-failure synthetic line found"
        for p in write_fail:
            assert p.level == "ERROR"
            fields = parse_qa_log_fail(p.message)
            assert fields is not None and fields["error"]


# ═════════════════════════════════════════════════════════════════════════════
# § 6b — display_log_lines rendering (capsys)
# ═════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
class TestDisplayPaths:
    """The widened LINE_RE means ERROR lines now reach display_log_lines().
    These assert the formerly-dead chat-ERR / QA-FAIL branches render, and that
    a generic ERROR line gets the ERROR tag (not 'info')."""

    def _synth_parsed(self):
        return [p for p in (parse_line(ln) for ln in _synthetic_lines()) if p]

    def test_chat_err_renders_tag(self, capsys):
        lines = [p for p in self._synth_parsed() if "[chat-ERR]" in p.message]
        assert lines
        display_log_lines(lines)
        out = capsys.readouterr().out
        assert "[chat-ERR]" in out

    def test_qa_write_failed_renders_qa_fail_tag(self, capsys):
        lines = [p for p in self._synth_parsed() if "Q&A log write failed" in p.message]
        assert lines, "No qa_logger write-failure synthetic line found"
        display_log_lines(lines)
        out = capsys.readouterr().out
        assert "[QA-FAIL ]" in out

    def test_generic_error_renders_error_tag_not_info(self, capsys):
        """A plain ERROR line (not chat-ERR / QA-fail) falls to the else branch
        and must render the 7-char ERROR tag, not 'info'."""
        line = LogLine(
            raw="2026-06-09 15:00:00,000 [abcd1234] ERROR core.foo:bar:9 - boom",
            timestamp="2026-06-09 15:00:00,000",
            tid="abcd1234",
            level="ERROR",
            logger="core.foo",
            func="bar",
            lineno="9",
            message="boom",
        )
        display_log_lines([line])
        out = capsys.readouterr().out
        assert "[ERROR  ]" in out
        assert "[info   ]" not in out


# ═════════════════════════════════════════════════════════════════════════════
# § 7 — grep helpers (offline, using BUFS_LOG_DIR override)
# ═════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
class TestGrepHelpers:
    """Integration of grep_app_logs / grep_qa_logs using fixture log-root."""

    # We fake a log-root by putting fixtures in the right subdirectory layout.
    # The fixture files are in tests/fixtures/logs/ which we use as BUFS_LOG_DIR,
    # but the real layout requires backend/ and qa/ subdirs.
    # We point at the fixture file directly via log_root with a tmp dir.

    @pytest.fixture
    def fake_log_root(self, tmp_path):
        """Create a fake log-root matching the expected layout."""
        backend = tmp_path / "backend"
        backend.mkdir()
        qa = tmp_path / "qa"
        qa.mkdir()
        # Copy fixtures
        import shutil
        shutil.copy(APP_LOG, backend / "app.log.2026-06-05")
        shutil.copy(QA_JSONL, qa / "qa_2026-06-08.jsonl")
        return tmp_path

    def test_grep_app_logs_known_tid(self, fake_log_root):
        """grep_app_logs returns lines for a known tid."""
        lines = grep_app_logs("a4f2878e", log_root=fake_log_root)
        assert len(lines) == 3  # chat-IN, chat-OUT, PIPELINE_TIMING
        tids = {ln.tid for ln in lines}
        assert tids == {"a4f2878e"}

    def test_grep_app_logs_orphan_tid(self, fake_log_root):
        """Orphan e9eb99b1 has exactly 1 line (chat-IN, no OUT)."""
        lines = grep_app_logs("e9eb99b1", log_root=fake_log_root)
        assert len(lines) == 1
        assert "[chat-IN]" in lines[0].message

    def test_grep_app_logs_unknown_tid(self, fake_log_root):
        """Unknown tid returns empty list."""
        lines = grep_app_logs("00000000", log_root=fake_log_root)
        assert lines == []

    def test_grep_qa_logs_known_tid(self, fake_log_root):
        """grep_qa_logs returns the QA record for a known tid."""
        recs = grep_qa_logs("a687e093", log_root=fake_log_root)
        assert len(recs) == 1
        assert recs[0]["trace_id"] == "a687e093"
        assert recs[0]["duration_ms"] == 290452

    def test_grep_qa_logs_unknown_tid(self, fake_log_root):
        recs = grep_qa_logs("00000000", log_root=fake_log_root)
        assert recs == []

    def test_grep_missing_log_dir(self, tmp_path):
        """Missing log root returns empty results without raising."""
        missing = tmp_path / "no_such_dir"
        lines = grep_app_logs("a4f2878e", log_root=missing)
        assert lines == []
        recs = grep_qa_logs("a4f2878e", log_root=missing)
        assert recs == []
