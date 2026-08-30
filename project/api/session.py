"""Session endpoints. A session is just a UUID that doubles as the LangGraph
thread_id; auxiliary CamChat fields (profile, transcript) are nulled out."""

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from api.ratelimit import check_rate_limit
from api.runtime import create_session, get_session, is_valid_session_id

router = APIRouter(prefix="/api/session", tags=["session"])


class SessionCreateRequest(BaseModel):
    # Only "ko"/"en" are meaningful (_session_info coerces anything else to "ko"), so
    # bound the field rather than letting an arbitrary-length string through.
    lang: str = Field(default="ko", max_length=8)


@router.post("")
async def create(request: Request, req: SessionCreateRequest):
    """POST /api/session — mint a new conversation."""
    check_rate_limit(request)
    return create_session(req.lang)


@router.get("/{session_id}")
async def read(session_id: str):
    """GET /api/session/{id} — current session info."""
    if not is_valid_session_id(session_id):
        raise HTTPException(status_code=422, detail="session_id must be a UUID.")
    info = get_session(session_id)
    if not info:
        raise HTTPException(status_code=404, detail="세션을 찾을 수 없습니다.")
    return info
