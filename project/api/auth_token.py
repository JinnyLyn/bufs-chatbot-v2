"""로그인 토큰 발급·검증 (HMAC-SHA256 서명).

`<payload_b64url>.<signature_b64url>` 형식의 자체 서명 토큰이다. 표준 JWT 라이브러리를
새로 들이지 않으려고 stdlib(hmac/hashlib/base64)만 사용한다 — 서명·만료·블랙리스트라는
필요한 성질은 모두 갖췄다.

라우터(`api/user.py`)와 분리해 둔 이유: SSE 채팅 엔드포인트(`api/chat.py`)도 토큰을
검증해야 하는데, 라우터 모듈을 임포트하면 순환 의존이 생긴다.

시크릿 우선순위: `USER_TOKEN_SECRET` env > 키 파일 > 1회 자동 생성 후 파일 저장.
소스에 기본 시크릿을 박아두지 않는다 — 박아두면 배포본에서 누구나 토큰을 위조할 수 있다.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import config

logger = logging.getLogger(__name__)

_secret: Optional[bytes] = None

# 발급되는 토큰은 base64url 두 토막뿐이다 (`_b64encode` 결과 + '.' + 서명).
_TOKEN_RE = re.compile(r"[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+")

# 로그아웃한 토큰. 프로세스 메모리에만 있으므로 서버 재시작 시 비워진다 — 재시작 후에도
# 살아있는 토큰이 마음에 걸린다면 USER_TOKEN_TTL_HOURS를 줄이는 쪽이 현실적이다.
_blacklisted: set[str] = set()


def _load_or_create_secret() -> bytes:
    env_secret = config.USER_TOKEN_SECRET.strip()
    if env_secret:
        return env_secret.encode("utf-8")

    key_file = Path(config.USER_TOKEN_SECRET_FILE)
    if key_file.exists():
        stored = key_file.read_text(encoding="utf-8").strip()
        if stored:
            return stored.encode("utf-8")

    key_file.parent.mkdir(parents=True, exist_ok=True)
    generated = secrets.token_urlsafe(48)
    key_file.write_text(generated, encoding="utf-8")
    try:
        os.chmod(key_file, 0o600)  # POSIX만 적용, Windows에선 무시된다
    except OSError:
        pass
    logger.warning(
        "USER_TOKEN_SECRET 이 없어 %s 에 새 서명키를 생성했습니다. "
        "운영에서는 env로 주입하세요 (키가 바뀌면 발급된 토큰이 모두 무효화됩니다).",
        key_file,
    )
    return generated.encode("utf-8")


def _get_secret() -> bytes:
    global _secret
    if _secret is None:
        _secret = _load_or_create_secret()
    return _secret


def reset_secret_cache() -> None:
    """테스트에서 시크릿을 갈아끼울 때 사용."""
    global _secret
    _secret = None


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _sign(payload_b64: str) -> str:
    return _b64encode(
        hmac.new(_get_secret(), payload_b64.encode("ascii"), hashlib.sha256).digest()
    )


def create_user_token(user: dict) -> tuple[str, str]:
    """(token, 만료시각 ISO8601) 반환."""
    now = datetime.now(timezone.utc)
    exp = now + timedelta(hours=config.USER_TOKEN_TTL_HOURS)
    payload = {
        "user_id": user["id"],
        "username": user["username"],
        "nickname": user["nickname"],
        "iat": int(now.timestamp()),
        "exp": int(exp.timestamp()),
    }
    payload_b64 = _b64encode(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
    return f"{payload_b64}.{_sign(payload_b64)}", exp.isoformat()


def verify_user_token(token: str) -> Optional[dict]:
    """서명·만료·블랙리스트를 검사하고 payload를 반환. 무효면 None.

    어떤 입력이 와도 예외를 던지지 않는다 — 이 함수는 로그인 선택 경로(채팅)에서도
    불리므로, 망가진 토큰 하나가 500으로 번지면 비로그인 폴백이 동작하지 않는다.
    """
    if not token or token in _blacklisted:
        return None
    # 서명 계산 전에 모양부터 본다. `_sign()`의 encode("ascii")와 compare_digest는
    # 비ASCII 문자에 예외를 던지는데, 토큰은 전적으로 외부 입력이다.
    if not _TOKEN_RE.fullmatch(token):
        return None
    payload_b64, signature = token.split(".")
    if not hmac.compare_digest(signature, _sign(payload_b64)):
        return None
    try:
        payload = json.loads(_b64decode(payload_b64))
        if datetime.now(timezone.utc).timestamp() > float(payload["exp"]):
            return None
    except (ValueError, KeyError, TypeError):
        return None
    return payload


def blacklist_token(token: str) -> None:
    if token:
        _blacklisted.add(token)


def resolve_user_id(token: Optional[str]) -> Optional[int]:
    """토큰이 유효하면 user_id, 아니면 None — 로그인 선택(optional) 경로용."""
    if not token:
        return None
    payload = verify_user_token(token)
    if payload is None:
        return None
    try:
        return int(payload["user_id"])
    except (KeyError, TypeError, ValueError):
        return None
