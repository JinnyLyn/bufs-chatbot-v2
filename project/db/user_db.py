"""SQLite user store — 계정(users) + 로그인 사용자 질문 이력(chat_messages).

CamChat 원본(`backend/database.py`)에서 인증·이력에 필요한 부분만 가져왔다. 이 레포의
다른 저장소(Qdrant 임베디드, parent_store)와 달리 여기는 순수 관계형 데이터라 stdlib
`sqlite3`로 충분하다 — 새 런타임 의존성 없음.

보안:
- PBKDF2-SHA256 (기본 600,000회 — OWASP 2024 권장치)
- 사용자별 32바이트 난수 salt
- 타이밍-세이프 비교(`hmac.compare_digest`) + 미존재 사용자에도 더미 해시 수행
- 평문 비밀번호는 저장·로깅 어디에도 남기지 않는다
"""

from __future__ import annotations

import hashlib
import hmac
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional

import config

_SALT_BYTES = 32

# sqlite3는 스레드 안전하지만, 쓰기 직렬화를 위해 프로세스 내 락을 둔다. FastAPI는
# 동기 DB 호출을 스레드풀에서 돌리므로 동시 쓰기가 실제로 발생한다.
_lock = threading.Lock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _db_path() -> Path:
    """DB 경로는 호출 시점에 읽는다 — 테스트가 config.USER_DB_PATH를 갈아끼울 수 있도록."""
    return Path(config.USER_DB_PATH)


@contextmanager
def _connect() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(str(_db_path()), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
    finally:
        conn.close()


def init_db() -> None:
    """테이블 생성 (없을 때만). 서버 기동 시 1회 호출."""
    _db_path().parent.mkdir(parents=True, exist_ok=True)
    with _lock, _connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                nickname TEXT NOT NULL,
                password_hash TEXT NOT NULL,
                salt TEXT NOT NULL,
                student_id TEXT NOT NULL,
                department TEXT NOT NULL,
                student_type TEXT NOT NULL DEFAULT '내국인',
                created_at TEXT NOT NULL,
                last_login TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS chat_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                session_id TEXT NOT NULL,
                question TEXT NOT NULL,
                answer TEXT NOT NULL,
                intent TEXT NOT NULL DEFAULT '',
                rating INTEGER,
                created_at TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_chat_messages_user "
            "ON chat_messages(user_id, created_at DESC)"
        )
        conn.commit()


# ── 비밀번호 해싱 ─────────────────────────────────────────────

def _hash_password(password: str, salt: bytes) -> str:
    # 반복 횟수는 호출 시점에 읽는다 — 테스트가 낮춰 잡을 수 있도록(_db_path와 같은 이유).
    dk = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, iterations=config.USER_PBKDF2_ITERATIONS
    )
    return dk.hex()


def _verify_password(password: str, stored_hash: str, salt_hex: str) -> bool:
    """타이밍-세이프 검증. salt가 손상됐으면 조용히 실패 처리."""
    try:
        salt = bytes.fromhex(salt_hex)
    except ValueError:
        return False
    return hmac.compare_digest(_hash_password(password, salt), stored_hash)


def _public_user(row: sqlite3.Row | dict) -> dict:
    """password_hash·salt를 제외한, API로 내보내도 되는 필드만."""
    return {
        "id": row["id"],
        "username": row["username"],
        "nickname": row["nickname"],
        "student_id": row["student_id"],
        "department": row["department"],
        "student_type": row["student_type"],
    }


# ── 계정 ──────────────────────────────────────────────────────

def create_user(
    username: str,
    nickname: str,
    password: str,
    student_id: str,
    department: str,
    student_type: str = "내국인",
) -> Optional[dict]:
    """새 계정 생성. 아이디가 이미 있으면 None."""
    salt = os.urandom(_SALT_BYTES)
    pw_hash = _hash_password(password, salt)

    with _lock, _connect() as conn:
        try:
            cur = conn.execute(
                """INSERT INTO users (username, nickname, password_hash, salt,
                                      student_id, department, student_type, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (username, nickname, pw_hash, salt.hex(),
                 student_id, department, student_type, _now()),
            )
            conn.commit()
        except sqlite3.IntegrityError:
            return None  # username UNIQUE 위반
        return {
            "id": cur.lastrowid,
            "username": username,
            "nickname": nickname,
            "student_id": student_id,
            "department": department,
            "student_type": student_type,
        }


def authenticate_user(username: str, password: str) -> Optional[dict]:
    """자격증명 검증. 실패 시 None (사유는 구분해 알려주지 않는다)."""
    with _lock, _connect() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE username = ?", (username,)
        ).fetchone()

    if row is None:
        # 존재하지 않는 아이디도 동일한 시간이 걸리도록 더미 해시 1회 수행
        # (응답 시간으로 계정 존재 여부가 새는 것을 막는다).
        _hash_password(password, os.urandom(_SALT_BYTES))
        return None

    if not _verify_password(password, row["password_hash"], row["salt"]):
        return None

    with _lock, _connect() as conn:
        conn.execute("UPDATE users SET last_login = ? WHERE id = ?", (_now(), row["id"]))
        conn.commit()

    return _public_user(row)


def get_user_by_id(user_id: int) -> Optional[dict]:
    """토큰 검증 후 최신 사용자 정보 조회."""
    with _lock, _connect() as conn:
        row = conn.execute(
            "SELECT id, username, nickname, student_id, department, student_type "
            "FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()
    return dict(row) if row else None


# ── 질문 이력 (본인 전용) ─────────────────────────────────────

def insert_chat_message(
    user_id: int,
    session_id: str,
    question: str,
    answer: str,
    intent: str = "",
) -> Optional[int]:
    """로그인 사용자의 한 턴을 저장하고 row id 반환."""
    with _lock, _connect() as conn:
        cur = conn.execute(
            """INSERT INTO chat_messages (user_id, session_id, question, answer, intent, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (user_id, session_id, question, answer, intent, _now()),
        )
        conn.commit()
        return cur.lastrowid


def list_chat_messages(user_id: int, limit: int = 50, offset: int = 0) -> list[dict]:
    """본인 이력 최신순."""
    with _lock, _connect() as conn:
        rows = conn.execute(
            """SELECT id, session_id, question, answer, intent, rating, created_at
               FROM chat_messages WHERE user_id = ?
               ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?""",
            (user_id, limit, offset),
        ).fetchall()
    return [dict(r) for r in rows]


def count_chat_messages(user_id: int) -> int:
    with _lock, _connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM chat_messages WHERE user_id = ?", (user_id,)
        ).fetchone()
    return int(row["n"]) if row else 0
