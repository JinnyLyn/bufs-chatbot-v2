"""사용자 인증 라우터 — 회원가입 / 로그인 / 내 정보 / 로그아웃 / 질문 이력.

로그인은 **선택**이다. 비로그인 사용자도 채팅은 그대로 되고, 로그인하면 질문 이력이
계정에 쌓인다(`api/chat.py`의 access_token 경로).

보안:
- 비밀번호는 PBKDF2-SHA256으로만 저장 (`db/user_db.py`)
- 로그인 실패는 IP 기준 5회/15분으로 제한
- 아이디 오류와 비밀번호 오류를 구분해 알려주지 않는다 (계정 존재 여부 노출 방지)
- 비밀번호는 로그에 남기지 않는다
"""

from __future__ import annotations

import logging
import time
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

from api.auth_token import blacklist_token, create_user_token, verify_user_token
from api.runtime import delete_session
from db.user_db import (
    authenticate_user,
    count_chat_messages,
    create_user,
    get_user_by_id,
    list_chat_messages,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/user", tags=["user"])
security = HTTPBearer(auto_error=False)

# ── 브루트포스 제한 ──
_MAX_ATTEMPTS = 5
_LOCKOUT_SECONDS = 900  # 15분
_MAX_TRACKED_IPS = 10_000
_login_attempts: dict[str, list[float]] = {}

# ── 응답 메시지 (프론트의 lang 토글을 그대로 따른다) ──
_MSG = {
    "auth_required": {"ko": "로그인이 필요합니다.", "en": "Sign-in required."},
    "token_invalid": {"ko": "로그인이 만료되었습니다. 다시 로그인해 주세요.",
                      "en": "Your session expired. Please sign in again."},
    "login_failed": {"ko": "아이디 또는 비밀번호가 잘못되었습니다.",
                     "en": "Invalid username or password."},
    "username_taken": {"ko": "이미 사용 중인 아이디입니다.", "en": "That username is already taken."},
    "invalid_student_type": {"ko": "학생 유형이 올바르지 않습니다.", "en": "Invalid student type."},
    "user_not_found": {"ko": "사용자를 찾을 수 없습니다.", "en": "User not found."},
    "rate_limited": {"ko": "로그인 시도가 너무 많습니다. 15분 후 다시 시도해 주세요.",
                     "en": "Too many login attempts. Please try again in 15 minutes."},
}

_STUDENT_TYPES = {
    "내국인": "내국인", "domestic": "내국인",
    "외국인": "외국인", "international": "외국인", "intl": "외국인",
    "편입생": "편입생", "transfer": "편입생",
}


def _msg(key: str, lang: str) -> str:
    entry = _MSG[key]
    return entry.get(lang, entry["ko"])


def _normalize_student_type(value: str) -> Optional[str]:
    """KO/EN 양쪽을 받아 KO로 정규화. 알 수 없는 값이면 None."""
    return _STUDENT_TYPES.get((value or "").strip().lower())


# ── 스키마 ────────────────────────────────────────────────────

class UserRegister(BaseModel):
    username: str = Field(..., min_length=4, max_length=20, pattern=r"^[a-zA-Z0-9_]+$",
                          description="영숫자·밑줄 4-20자")
    nickname: str = Field(..., min_length=2, max_length=20, description="표시 닉네임")
    password: str = Field(..., min_length=8, max_length=64)
    student_id: str = Field(..., min_length=4, max_length=4, description="입학연도 (예: 2023)")
    department: str = Field(..., min_length=1, max_length=50, description="학과/전공")
    student_type: str = Field(default="내국인", description="내국인/외국인/편입생")


class UserLogin(BaseModel):
    username: str = Field(..., min_length=1, max_length=20)
    password: str = Field(..., min_length=1, max_length=64)


class UserInfo(BaseModel):
    id: int
    username: str
    nickname: str
    student_id: str
    department: str
    student_type: str


class AuthToken(BaseModel):
    token: str
    expires_at: str
    user: UserInfo


class ChatHistoryItem(BaseModel):
    id: int
    session_id: str
    question: str
    answer: str
    intent: str = ""
    rating: Optional[int] = None
    created_at: str


class ChatHistoryResponse(BaseModel):
    total: int
    items: list[ChatHistoryItem] = Field(default_factory=list)


# ── 브루트포스 제한 ───────────────────────────────────────────

def _check_rate_limit(ip: str, lang: str) -> None:
    now = time.time()
    attempts = [t for t in _login_attempts.get(ip, []) if now - t < _LOCKOUT_SECONDS]
    if attempts:
        _login_attempts[ip] = attempts
    else:
        _login_attempts.pop(ip, None)
    if len(attempts) >= _MAX_ATTEMPTS:
        raise HTTPException(status_code=429, detail=_msg("rate_limited", lang))


def _record_failed_attempt(ip: str) -> None:
    # 만료된 IP 엔트리를 정리 — 없으면 서로 다른 IP가 쌓이며 무한정 커진다.
    if len(_login_attempts) >= _MAX_TRACKED_IPS:
        now = time.time()
        for stale_ip in [
            k for k, v in _login_attempts.items()
            if all(now - t >= _LOCKOUT_SECONDS for t in v)
        ]:
            _login_attempts.pop(stale_ip, None)
    _login_attempts.setdefault(ip, []).append(time.time())


# ── 인증 의존성 ───────────────────────────────────────────────

async def require_user(
    lang: str = Query("ko"),
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
) -> dict:
    """유효한 토큰을 요구한다. payload 반환."""
    if credentials is None:
        raise HTTPException(status_code=401, detail=_msg("auth_required", lang))
    payload = verify_user_token(credentials.credentials)
    if payload is None:
        raise HTTPException(status_code=401, detail=_msg("token_invalid", lang))
    return payload


# ── 엔드포인트 ────────────────────────────────────────────────

@router.post("/register", response_model=AuthToken)
async def register(body: UserRegister, lang: str = Query("ko")):
    """POST /api/user/register — 가입 후 바로 로그인 상태가 된다."""
    student_type = _normalize_student_type(body.student_type)
    if student_type is None:
        raise HTTPException(status_code=400, detail=_msg("invalid_student_type", lang))

    user = create_user(
        username=body.username,
        nickname=body.nickname,
        password=body.password,  # create_user 내부에서 해싱
        student_id=body.student_id,
        department=body.department,
        student_type=student_type,
    )
    if user is None:
        raise HTTPException(status_code=409, detail=_msg("username_taken", lang))

    logger.info("user registered: %s", body.username)
    token, expires_at = create_user_token(user)
    return AuthToken(token=token, expires_at=expires_at, user=UserInfo(**user))


@router.post("/login", response_model=AuthToken)
async def login(body: UserLogin, request: Request, lang: str = Query("ko")):
    """POST /api/user/login — 자격증명 검증 후 토큰 발급."""
    client_ip = request.client.host if request.client else "unknown"
    _check_rate_limit(client_ip, lang)

    user = authenticate_user(body.username, body.password)
    if user is None:
        _record_failed_attempt(client_ip)
        logger.info("login failed: username=%s ip=%s", body.username, client_ip)
        raise HTTPException(status_code=401, detail=_msg("login_failed", lang))

    _login_attempts.pop(client_ip, None)
    logger.info("user logged in: %s", body.username)
    token, expires_at = create_user_token(user)
    return AuthToken(token=token, expires_at=expires_at, user=UserInfo(**user))


@router.get("/me", response_model=UserInfo)
async def get_me(lang: str = Query("ko"), payload: dict = Depends(require_user)):
    """GET /api/user/me — 토큰으로 최신 사용자 정보 조회 (프론트 부팅 시 토큰 검증용)."""
    user = get_user_by_id(payload["user_id"])
    if user is None:
        raise HTTPException(status_code=404, detail=_msg("user_not_found", lang))
    return UserInfo(**user)


@router.post("/logout")
async def logout(
    session_id: Optional[str] = Query(None),
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
):
    """POST /api/user/logout — 토큰 폐기 + 해당 대화 세션 정리.

    토큰이 없어도 200을 반환한다. 로그아웃은 실패시켜서 얻을 게 없다.
    """
    if credentials and credentials.credentials:
        blacklist_token(credentials.credentials)
    if session_id:
        delete_session(session_id)
    return {"ok": True}


@router.get("/chat-history", response_model=ChatHistoryResponse)
async def get_chat_history(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    payload: dict = Depends(require_user),
):
    """GET /api/user/chat-history — 본인이 로그인 상태에서 한 질문·답변 (최신순)."""
    uid = int(payload["user_id"])
    return ChatHistoryResponse(
        total=count_chat_messages(uid),
        items=[ChatHistoryItem(**it) for it in list_chat_messages(uid, limit=limit, offset=offset)],
    )
