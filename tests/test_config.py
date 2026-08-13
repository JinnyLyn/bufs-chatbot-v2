"""Unit tests for project/config.py — env parsing, defaults, and the two
   side-effect rewrites (OLLAMA 0.0.0.0→127.0.0.1, LANGFUSE_BASE_URL→LANGFUSE_HOST).

Key constraint: config.py executes all reads at import time. Tests must:
  1. Set env vars before reloading config (monkeypatch + importlib.reload).
  2. After reload, access values via ``cfg.CONSTANT`` (not ``from config import X``).
"""
import importlib
import os
import sys

import pytest


def _reload_config(monkeypatch, **env_overrides):
    """Helper: patch env vars then reload config, returning the fresh module."""
    for k, v in env_overrides.items():
        monkeypatch.setenv(k, v)
    if "config" in sys.modules:
        mod = importlib.reload(sys.modules["config"])
    else:
        import config as mod
    return mod


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

class TestDefaults:
    def test_langfuse_enabled_defaults_to_false(self, env_isolated, monkeypatch):
        cfg = _reload_config(monkeypatch)
        assert cfg.LANGFUSE_ENABLED is False

    def test_llm_model_has_default(self, env_isolated, monkeypatch):
        cfg = _reload_config(monkeypatch)
        assert cfg.LLM_MODEL != ""

    def test_max_tool_calls_is_positive_integer(self, env_isolated, monkeypatch):
        cfg = _reload_config(monkeypatch)
        assert isinstance(cfg.MAX_TOOL_CALLS, int)
        assert cfg.MAX_TOOL_CALLS > 0

    def test_max_iterations_is_positive_integer(self, env_isolated, monkeypatch):
        cfg = _reload_config(monkeypatch)
        assert isinstance(cfg.MAX_ITERATIONS, int)
        assert cfg.MAX_ITERATIONS > 0

    def test_llm_temperature_is_float(self, env_isolated, monkeypatch):
        cfg = _reload_config(monkeypatch)
        assert isinstance(cfg.LLM_TEMPERATURE, float)

    def test_chat_log_disabled_defaults_to_false(self, env_isolated, monkeypatch):
        cfg = _reload_config(monkeypatch)
        assert cfg.CHAT_LOG_DISABLED is False

    def test_search_score_threshold_is_float(self, env_isolated, monkeypatch):
        cfg = _reload_config(monkeypatch)
        assert isinstance(cfg.SEARCH_SCORE_THRESHOLD, float)

    def test_semester_today_defaults_to_empty(self, env_isolated, monkeypatch):
        cfg = _reload_config(monkeypatch)
        assert cfg.SEMESTER_TODAY == ""


# ---------------------------------------------------------------------------
# SEMESTER_TODAY validation (fail fast — the runtime consumer swallows exceptions)
# ---------------------------------------------------------------------------

class TestSemesterToday:
    def test_valid_iso_date_accepted(self, env_isolated, monkeypatch):
        cfg = _reload_config(monkeypatch, SEMESTER_TODAY="2026-08-03")
        assert cfg.SEMESTER_TODAY == "2026-08-03"

    @pytest.mark.parametrize("bad", ["2026-13-01", "not-a-date", "08/03/2026"])
    def test_malformed_date_fails_at_import(self, env_isolated, monkeypatch, bad):
        with pytest.raises(ValueError, match="SEMESTER_TODAY"):
            _reload_config(monkeypatch, SEMESTER_TODAY=bad)
        # A failed reload leaves config partially executed — restore a fully
        # initialized module so later tests don't see torn state.
        monkeypatch.delenv("SEMESTER_TODAY")
        _reload_config(monkeypatch)


# ---------------------------------------------------------------------------
# Env overrides
# ---------------------------------------------------------------------------

class TestEnvOverrides:
    def test_max_tool_calls_read_from_env(self, env_isolated, monkeypatch):
        cfg = _reload_config(monkeypatch, MAX_TOOL_CALLS="5")
        assert cfg.MAX_TOOL_CALLS == 5

    def test_max_iterations_read_from_env(self, env_isolated, monkeypatch):
        cfg = _reload_config(monkeypatch, MAX_ITERATIONS="7")
        assert cfg.MAX_ITERATIONS == 7

    def test_llm_temperature_read_from_env(self, env_isolated, monkeypatch):
        cfg = _reload_config(monkeypatch, LLM_TEMPERATURE="0.7")
        assert cfg.LLM_TEMPERATURE == pytest.approx(0.7)

    def test_chat_log_disabled_true_for_1(self, env_isolated, monkeypatch):
        cfg = _reload_config(monkeypatch, CHAT_LOG_DISABLED="1")
        assert cfg.CHAT_LOG_DISABLED is True

    def test_chat_log_disabled_true_for_yes(self, env_isolated, monkeypatch):
        cfg = _reload_config(monkeypatch, CHAT_LOG_DISABLED="yes")
        assert cfg.CHAT_LOG_DISABLED is True

    def test_chat_log_disabled_false_for_empty(self, env_isolated, monkeypatch):
        cfg = _reload_config(monkeypatch, CHAT_LOG_DISABLED="")
        assert cfg.CHAT_LOG_DISABLED is False

    def test_llm_reasoning_true_for_true(self, env_isolated, monkeypatch):
        cfg = _reload_config(monkeypatch, LLM_REASONING="true")
        assert cfg.LLM_REASONING is True

    def test_llm_reasoning_false_by_default(self, env_isolated, monkeypatch):
        cfg = _reload_config(monkeypatch)
        assert cfg.LLM_REASONING is False

    def test_max_parent_size_read_from_env(self, env_isolated, monkeypatch):
        cfg = _reload_config(monkeypatch, MAX_PARENT_SIZE="8000")
        assert cfg.MAX_PARENT_SIZE == 8000


# ---------------------------------------------------------------------------
# LANGFUSE_BASE_URL → LANGFUSE_HOST mirror (config.py:100-108)
# ---------------------------------------------------------------------------

class TestLangfuseHostMirror:
    def test_langfuse_host_set_from_base_url_when_enabled(self, env_isolated, monkeypatch):
        """When LANGFUSE_ENABLED=true and LANGFUSE_BASE_URL is set,
        config should mirror it into os.environ['LANGFUSE_HOST']."""
        monkeypatch.setenv("LANGFUSE_ENABLED", "true")
        monkeypatch.setenv("LANGFUSE_BASE_URL", "https://cloud.langfuse.com")
        monkeypatch.delenv("LANGFUSE_HOST", raising=False)
        _reload_config(monkeypatch)
        assert os.environ.get("LANGFUSE_HOST") == "https://cloud.langfuse.com"

    def test_langfuse_host_not_overwritten_when_already_set(self, env_isolated, monkeypatch):
        """Pre-existing LANGFUSE_HOST must not be overwritten."""
        monkeypatch.setenv("LANGFUSE_ENABLED", "true")
        monkeypatch.setenv("LANGFUSE_BASE_URL", "https://cloud.langfuse.com")
        monkeypatch.setenv("LANGFUSE_HOST", "https://custom-host.example.com")
        _reload_config(monkeypatch)
        assert os.environ.get("LANGFUSE_HOST") == "https://custom-host.example.com"

    def test_langfuse_host_not_set_when_disabled(self, env_isolated, monkeypatch):
        """When LANGFUSE_ENABLED is false, LANGFUSE_HOST must not be injected."""
        monkeypatch.setenv("LANGFUSE_ENABLED", "false")
        monkeypatch.setenv("LANGFUSE_BASE_URL", "https://cloud.langfuse.com")
        monkeypatch.delenv("LANGFUSE_HOST", raising=False)
        _reload_config(monkeypatch)
        assert os.environ.get("LANGFUSE_HOST") is None

    def test_langfuse_enabled_true_for_lowercase_true(self, env_isolated, monkeypatch):
        cfg = _reload_config(monkeypatch, LANGFUSE_ENABLED="true")
        assert cfg.LANGFUSE_ENABLED is True

    def test_langfuse_enabled_true_for_title_case(self, env_isolated, monkeypatch):
        """'True'.lower() == 'true' so title-case also enables Langfuse (documented behaviour)."""
        cfg = _reload_config(monkeypatch, LANGFUSE_ENABLED="True")
        assert cfg.LANGFUSE_ENABLED is True

    def test_langfuse_enabled_false_for_non_true_values(self, env_isolated, monkeypatch):
        """Values other than 'true' (case-insensitive) must not enable Langfuse."""
        for val in ("yes", "1", "on", "enabled"):
            cfg = _reload_config(monkeypatch, LANGFUSE_ENABLED=val)
            assert cfg.LANGFUSE_ENABLED is False, f"Expected False for {val!r}"


# ---------------------------------------------------------------------------
# OLLAMA 0.0.0.0 → 127.0.0.1 rewrite (config.py:7-9)
# ---------------------------------------------------------------------------

class TestOllamaHostRewrite:
    def test_ollama_0000_rewritten_to_loopback(self, env_isolated, monkeypatch):
        """0.0.0.0 bind-all address must be rewritten to 127.0.0.1 for outgoing connections."""
        monkeypatch.setenv("OLLAMA_HOST", "0.0.0.0:11434")
        _reload_config(monkeypatch)
        assert os.environ.get("OLLAMA_HOST", "").startswith("127.0.0.1")

    def test_ollama_non_0000_not_rewritten(self, env_isolated, monkeypatch):
        """Non-bind-all addresses are passed through unchanged."""
        monkeypatch.setenv("OLLAMA_HOST", "http://192.168.1.5:11434")
        _reload_config(monkeypatch)
        assert os.environ.get("OLLAMA_HOST") == "http://192.168.1.5:11434"
