"""서비스 엔드포인트 해석 — 평가 스크립트의 하드코딩 URL 제거 (stdlib만, 네트워크 X).

공유 박스에서 :11434는 남의 시스템 Ollama일 수 있고(scripts/_common.sh), 유저별
Ollama는 포트가 제각각이다. 단일 출처는 **백엔드가 실제로 다이얼하는
project/.env의 OLLAMA_BASE_URL** — scripts/_common.sh의 derive_ollama_port와
같은 원칙을 파이썬 쪽에 제공한다. 셸 환경변수가 있으면 그것이 우선.

체인 (위가 우선):
  judge_ollama_url():  $OLLAMA_JUDGE_URL → $OLLAMA_BASE_URL
                       → project/.env의 OLLAMA_BASE_URL → http://127.0.0.1:11434
                       (최후 상수는 .env조차 없는 클론 직후 환경용 — 백엔드가 돌고
                        있는 박스라면 .env가 반드시 있으므로 사실상 도달하지 않는다)
  backend_url():       $BUFS_BACKEND_URL (kpi_profiles.yaml 규약)
                       → http://localhost:$BACKEND_PORT (scripts/start-all.sh 규약)
                       → http://localhost:8000
"""
from __future__ import annotations

import os

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PROJECT_ENV = os.path.join(_REPO, "project", ".env")

_JUDGE_LAST_RESORT = "http://127.0.0.1:11434"
_BACKEND_LAST_RESORT = "http://localhost:8000"


def read_env_file_value(key: str, path: str | None = None) -> str:
    """``KEY=value`` 라인 파서 — python-dotenv 없이 project/.env 한 키만 읽는다.

    인라인 주석(``KEY=value  # 설명``, .env.example 관례)과 따옴표를 벗기고,
    같은 키가 여러 번이면 **마지막 정의**가 이긴다(_common.sh의 ``tail -1``,
    python-dotenv와 동일). 파일이 없으면 빈 문자열.
    """
    val = ""
    try:
        with open(path or _PROJECT_ENV, encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip()
                if not line or line.startswith("#") or not line.startswith(key + "="):
                    continue
                v = line.split("=", 1)[1]
                v = v.split("#", 1)[0].strip().strip('"').strip("'")
                val = v
    except OSError:
        pass
    return val


def judge_ollama_url(env_file: str | None = None) -> str:
    """LLM-judge용 Ollama 엔드포인트 (모듈 docstring의 체인)."""
    return (
        os.environ.get("OLLAMA_JUDGE_URL")
        or os.environ.get("OLLAMA_BASE_URL")
        or read_env_file_value("OLLAMA_BASE_URL", env_file)
        or _JUDGE_LAST_RESORT
    )


def backend_url() -> str:
    """챗봇 백엔드(FastAPI) 엔드포인트 (모듈 docstring의 체인)."""
    url = os.environ.get("BUFS_BACKEND_URL", "").strip()
    if url:
        return url
    port = os.environ.get("BACKEND_PORT", "").strip()
    if port:
        return f"http://localhost:{port}"
    return _BACKEND_LAST_RESORT
