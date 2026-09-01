"""endpoints.py — 엔드포인트 해석 체인 단위 테스트 (순수 오프라인).

체인 규약이 회귀하면 평가 스크립트가 남의 시스템 Ollama(:11434)를 다이얼하거나
(공유 박스 — scripts/_common.sh 참고) 죽은 포트로 나가므로, 우선순위 전 단계와
project/.env 파서(인라인 주석·따옴표·마지막 정의 우선)를 고정한다.
"""
from __future__ import annotations

import pytest

import endpoints

pytestmark = pytest.mark.unit

_ENV_KEYS = ("OLLAMA_JUDGE_URL", "OLLAMA_BASE_URL", "BUFS_BACKEND_URL", "BACKEND_PORT")


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in _ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


# ── read_env_file_value ─────────────────────────────────────────────────────
class TestReadEnvFileValue:
    def test_plain_value(self, tmp_path) -> None:
        f = tmp_path / ".env"
        f.write_text("OLLAMA_BASE_URL=http://127.0.0.1:11500\n", encoding="utf-8")
        assert endpoints.read_env_file_value("OLLAMA_BASE_URL", str(f)) == "http://127.0.0.1:11500"

    def test_inline_comment_and_quotes_stripped(self, tmp_path) -> None:
        f = tmp_path / ".env"
        f.write_text('OLLAMA_BASE_URL="http://127.0.0.1:11500"   # 유저별 Ollama\n', encoding="utf-8")
        assert endpoints.read_env_file_value("OLLAMA_BASE_URL", str(f)) == "http://127.0.0.1:11500"

    def test_last_definition_wins(self, tmp_path) -> None:
        # _common.sh의 `tail -1` / python-dotenv와 동일 의미론
        f = tmp_path / ".env"
        f.write_text(
            "OLLAMA_BASE_URL=http://old:1\nOLLAMA_BASE_URL=http://new:2\n", encoding="utf-8"
        )
        assert endpoints.read_env_file_value("OLLAMA_BASE_URL", str(f)) == "http://new:2"

    def test_commented_line_ignored(self, tmp_path) -> None:
        f = tmp_path / ".env"
        f.write_text("# OLLAMA_BASE_URL=http://ghost:9\n", encoding="utf-8")
        assert endpoints.read_env_file_value("OLLAMA_BASE_URL", str(f)) == ""

    def test_prefix_key_not_matched(self, tmp_path) -> None:
        # OLLAMA_BASE_URL_EXTRA 같은 상위집합 키에 오매칭되면 안 된다
        f = tmp_path / ".env"
        f.write_text("OLLAMA_BASE_URL_EXTRA=http://ghost:9\n", encoding="utf-8")
        assert endpoints.read_env_file_value("OLLAMA_BASE_URL", str(f)) == ""

    def test_missing_file_returns_empty(self, tmp_path) -> None:
        assert endpoints.read_env_file_value("OLLAMA_BASE_URL", str(tmp_path / "nope")) == ""


# ── judge_ollama_url 체인 ───────────────────────────────────────────────────
class TestJudgeOllamaUrl:
    def test_judge_env_wins(self, monkeypatch, tmp_path) -> None:
        monkeypatch.setenv("OLLAMA_JUDGE_URL", "http://judge:1")
        monkeypatch.setenv("OLLAMA_BASE_URL", "http://base:2")
        assert endpoints.judge_ollama_url(str(tmp_path / "nope")) == "http://judge:1"

    def test_base_env_second(self, monkeypatch, tmp_path) -> None:
        monkeypatch.setenv("OLLAMA_BASE_URL", "http://base:2")
        assert endpoints.judge_ollama_url(str(tmp_path / "nope")) == "http://base:2"

    def test_dotenv_third(self, tmp_path) -> None:
        f = tmp_path / ".env"
        f.write_text("OLLAMA_BASE_URL=http://dotenv:3\n", encoding="utf-8")
        assert endpoints.judge_ollama_url(str(f)) == "http://dotenv:3"

    def test_last_resort_constant(self, tmp_path) -> None:
        assert endpoints.judge_ollama_url(str(tmp_path / "nope")) == "http://127.0.0.1:11434"


# ── backend_url 체인 ────────────────────────────────────────────────────────
class TestBackendUrl:
    def test_bufs_backend_url_wins(self, monkeypatch) -> None:
        monkeypatch.setenv("BUFS_BACKEND_URL", "http://backend:9999")
        monkeypatch.setenv("BACKEND_PORT", "8010")
        assert endpoints.backend_url() == "http://backend:9999"

    def test_backend_port_second(self, monkeypatch) -> None:
        monkeypatch.setenv("BACKEND_PORT", "8010")
        assert endpoints.backend_url() == "http://localhost:8010"

    def test_default_8000(self) -> None:
        assert endpoints.backend_url() == "http://localhost:8000"
