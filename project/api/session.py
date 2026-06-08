"""Session endpoints. A session is just a UUID that doubles as the LangGraph
thread_id; auxiliary CamChat fields (profile, transcript) are nulled out."""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from api.runtime import create_session, get_session

router = APIRouter(prefix="/api/session", tags=["session"])


class SessionCreateRequest(BaseModel):
    lang: str = "ko"


@router.post("")
async def create(req: SessionCreateRequest):
    """POST /api/session — mint a new conversation."""
    return create_session(req.lang)


@router.get("/{session_id}")
async def read(session_id: str):
    """GET /api/session/{id} — current session info."""
    info = get_session(session_id)
    if not info:
        raise HTTPException(status_code=404, detail="세션을 찾을 수 없습니다.")
    return info
