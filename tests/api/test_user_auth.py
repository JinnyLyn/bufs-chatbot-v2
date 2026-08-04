"""회원가입·로그인·질문 이력 테스트 (api/user.py, api/auth_token.py, db/user_db.py).

전부 오프라인이다 — SQLite는 tmp_path에, LLM/Qdrant는 타지 않는다.

config 주의: `user_db._db_path()`와 `_hash_password()`는 config 값을 **호출 시점**에
읽는다. 덕분에 conftest의 importlib.reload 없이 monkeypatch.setattr 만으로 격리된다.
"""
from __future__ import annotations

import sqlite3
from unittest.mock import MagicMock

import pytest

pytestmark = pytest.mark.unit

pytest.importorskip("fastapi", reason="fastapi not installed")
pytest.importorskip("starlette", reason="starlette not installed")


REGISTER_BODY = {
    "username": "hongkd",
    "nickname": "홍길동",
    "password": "password123",
    "student_id": "2023",
    "department": "컴퓨터공학부",
    "student_type": "내국인",
}


@pytest.fixture()
def auth_client(tmp_path, monkeypatch):
    """계정 DB가 tmp_path에 격리된 TestClient.

    PBKDF2 반복은 1,000회로 낮춘다 — 운영 기본값 600,000회는 테스트당 ~0.5초라
    이 파일만으로도 수십 초가 된다. 해싱 *로직*은 반복 횟수와 무관하게 동일하다.
    """
    import config
    from fastapi import FastAPI
    from starlette.testclient import TestClient

    import api.auth_token as auth_token
    import api.user as user_mod
    from db import user_db

    monkeypatch.setattr(config, "USER_DB_PATH", str(tmp_path / "users.db"))
    monkeypatch.setattr(config, "USER_PBKDF2_ITERATIONS", 1000)
    monkeypatch.setattr(config, "USER_TOKEN_SECRET", "test-secret-not-for-production")
    monkeypatch.setattr(config, "USER_TOKEN_TTL_HOURS", 24)

    # 모듈 전역 상태 — 테스트 간 누수를 막는다.
    auth_token.reset_secret_cache()
    auth_token._blacklisted.clear()  # noqa: SLF001 — test-only reset
    user_mod._login_attempts.clear()  # noqa: SLF001 — test-only reset

    user_db.init_db()

    app = FastAPI()
    app.include_router(user_mod.router)
    yield TestClient(app, raise_server_exceptions=False)

    auth_token.reset_secret_cache()
    auth_token._blacklisted.clear()  # noqa: SLF001
    user_mod._login_attempts.clear()  # noqa: SLF001


def _register(client, **overrides) -> tuple[str, dict]:
    """가입 후 (token, user) 반환."""
    resp = client.post("/api/user/register", json={**REGISTER_BODY, **overrides})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    return body["token"], body["user"]


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# 회원가입
# ---------------------------------------------------------------------------

class TestRegister:
    def test_register_returns_token_and_user(self, auth_client):
        token, user = _register(auth_client)
        assert token
        assert user["username"] == "hongkd"
        assert user["nickname"] == "홍길동"
        assert user["department"] == "컴퓨터공학부"
        # 비밀번호·해시는 응답에 절대 실리지 않는다.
        assert "password" not in user and "password_hash" not in user and "salt" not in user

    def test_duplicate_username_rejected(self, auth_client):
        _register(auth_client)
        resp = auth_client.post("/api/user/register", json={**REGISTER_BODY, "nickname": "다른사람"})
        assert resp.status_code == 409

    @pytest.mark.parametrize(
        "field,value",
        [
            ("username", "ab"),            # 4자 미만
            ("username", "has space"),     # 영숫자·밑줄 외 문자
            ("username", "한글아이디"),      # 비ASCII
            ("password", "short"),         # 8자 미만
            ("nickname", "a"),             # 2자 미만
            ("student_id", "23"),          # 4자리 아님
            ("department", ""),            # 빈 값
        ],
    )
    def test_invalid_input_rejected(self, auth_client, field, value):
        resp = auth_client.post("/api/user/register", json={**REGISTER_BODY, field: value})
        assert resp.status_code == 422, f"{field}={value!r} 이 통과했다"

    def test_student_type_accepts_english_and_normalizes(self, auth_client):
        _, user = _register(auth_client, username="intl01", student_type="International")
        assert user["student_type"] == "외국인"

    def test_unknown_student_type_rejected(self, auth_client):
        resp = auth_client.post(
            "/api/user/register", json={**REGISTER_BODY, "student_type": "우주인"}
        )
        assert resp.status_code == 400

    def test_password_never_stored_in_plaintext(self, auth_client, tmp_path):
        _register(auth_client)
        conn = sqlite3.connect(str(tmp_path / "users.db"))
        row = conn.execute("SELECT password_hash, salt FROM users").fetchone()
        conn.close()
        assert REGISTER_BODY["password"] not in row[0]
        assert len(row[0]) == 64 and len(row[1]) == 64  # sha256 hex + 32바이트 salt hex


# ---------------------------------------------------------------------------
# 로그인
# ---------------------------------------------------------------------------

class TestLogin:
    def test_login_success(self, auth_client):
        _register(auth_client)
        resp = auth_client.post(
            "/api/user/login", json={"username": "hongkd", "password": "password123"}
        )
        assert resp.status_code == 200
        assert resp.json()["user"]["nickname"] == "홍길동"

    def test_wrong_password_rejected(self, auth_client):
        _register(auth_client)
        resp = auth_client.post(
            "/api/user/login", json={"username": "hongkd", "password": "wrongpassword"}
        )
        assert resp.status_code == 401

    def test_unknown_user_rejected(self, auth_client):
        resp = auth_client.post(
            "/api/user/login", json={"username": "nobody", "password": "password123"}
        )
        assert resp.status_code == 401

    def test_error_message_does_not_reveal_which_field_was_wrong(self, auth_client):
        """아이디 오류와 비밀번호 오류의 응답이 같아야 계정 존재 여부가 새지 않는다."""
        _register(auth_client)
        wrong_pw = auth_client.post(
            "/api/user/login", json={"username": "hongkd", "password": "wrongpassword"}
        )
        no_user = auth_client.post(
            "/api/user/login", json={"username": "ghost1", "password": "wrongpassword"}
        )
        assert wrong_pw.status_code == no_user.status_code
        assert wrong_pw.json()["detail"] == no_user.json()["detail"]

    def test_rate_limited_after_repeated_failures(self, auth_client):
        _register(auth_client)
        bad = {"username": "hongkd", "password": "wrongpassword"}
        for _ in range(5):
            assert auth_client.post("/api/user/login", json=bad).status_code == 401
        # 6번째부터는 잠긴다 — 올바른 비밀번호여도 마찬가지.
        assert auth_client.post("/api/user/login", json=bad).status_code == 429
        locked_out = auth_client.post(
            "/api/user/login", json={"username": "hongkd", "password": "password123"}
        )
        assert locked_out.status_code == 429

    def test_successful_login_clears_failure_counter(self, auth_client):
        _register(auth_client)
        for _ in range(4):
            auth_client.post(
                "/api/user/login", json={"username": "hongkd", "password": "wrongpassword"}
            )
        assert auth_client.post(
            "/api/user/login", json={"username": "hongkd", "password": "password123"}
        ).status_code == 200
        # 카운터가 초기화됐으므로 다시 4번 틀려도 잠기지 않는다.
        for _ in range(4):
            assert auth_client.post(
                "/api/user/login", json={"username": "hongkd", "password": "wrongpassword"}
            ).status_code == 401

    def test_error_message_language_follows_lang_param(self, auth_client):
        resp = auth_client.post(
            "/api/user/login?lang=en", json={"username": "nobody", "password": "password123"}
        )
        assert resp.status_code == 401
        assert resp.json()["detail"] == "Invalid username or password."


# ---------------------------------------------------------------------------
# 토큰 / 내 정보 / 로그아웃
# ---------------------------------------------------------------------------

class TestToken:
    def test_me_returns_current_user(self, auth_client):
        token, user = _register(auth_client)
        resp = auth_client.get("/api/user/me", headers=_auth(token))
        assert resp.status_code == 200
        assert resp.json() == user

    def test_me_without_token_rejected(self, auth_client):
        assert auth_client.get("/api/user/me").status_code == 401

    @pytest.mark.parametrize(
        "bad_token",
        [
            "not-a-token",       # 점 없음
            "a.b.c",             # 토막이 셋
            ".",                 # 빈 토막
            "aGVsbG8=.sig",      # base64url 알파벳 밖('=')
        ],
    )
    def test_malformed_token_rejected_not_500(self, auth_client, bad_token):
        """쓰레기 입력은 401이어야 한다 — 500이면 비로그인 폴백이 깨진다.

        비ASCII 토큰은 여기서 시험하지 않는다: HTTP 헤더는 ASCII 전용이라 클라이언트가
        보낼 수조차 없다. 그 경로는 쿼리스트링(SSE)뿐이라
        TestChatStreamPersistsHistory.test_invalid_token_does_not_break_chat 이 담당한다.
        """
        resp = auth_client.get("/api/user/me", headers=_auth(bad_token))
        assert resp.status_code == 401, f"{bad_token!r} → {resp.status_code}"

    def test_tampered_payload_rejected(self, auth_client):
        """서명은 그대로 두고 payload만 다른 user_id로 바꿔치기 → 거부돼야 한다."""
        import api.auth_token as auth_token

        token, _ = _register(auth_client)
        _, signature = token.split(".")
        forged_payload = auth_token._b64encode(  # noqa: SLF001
            b'{"user_id": 999, "username": "admin", "nickname": "x", "iat": 0, "exp": 99999999999}'
        )
        assert auth_client.get(
            "/api/user/me", headers=_auth(f"{forged_payload}.{signature}")
        ).status_code == 401

    def test_token_signed_with_other_secret_rejected(self, auth_client, monkeypatch):
        import config
        import api.auth_token as auth_token

        token, _ = _register(auth_client)
        monkeypatch.setattr(config, "USER_TOKEN_SECRET", "a-completely-different-secret")
        auth_token.reset_secret_cache()
        assert auth_client.get("/api/user/me", headers=_auth(token)).status_code == 401

    def test_expired_token_rejected(self, auth_client, monkeypatch):
        import config
        from api.auth_token import create_user_token

        monkeypatch.setattr(config, "USER_TOKEN_TTL_HOURS", -1)  # 발급 즉시 만료
        expired, _ = create_user_token({"id": 1, "username": "hongkd", "nickname": "홍길동"})
        assert auth_client.get("/api/user/me", headers=_auth(expired)).status_code == 401

    def test_logout_invalidates_token(self, auth_client):
        token, _ = _register(auth_client)
        assert auth_client.post("/api/user/logout", headers=_auth(token)).status_code == 200
        assert auth_client.get("/api/user/me", headers=_auth(token)).status_code == 401

    def test_logout_without_token_still_succeeds(self, auth_client):
        assert auth_client.post("/api/user/logout").status_code == 200

    def test_logout_purges_named_session(self, auth_client):
        from api.runtime import create_session, get_session

        token, _ = _register(auth_client)
        sid = create_session("ko")["session_id"]
        auth_client.post(f"/api/user/logout?session_id={sid}", headers=_auth(token))
        assert get_session(sid) is None


# ---------------------------------------------------------------------------
# 질문 이력
# ---------------------------------------------------------------------------

class TestChatHistory:
    def test_history_empty_for_new_user(self, auth_client):
        token, _ = _register(auth_client)
        body = auth_client.get("/api/user/chat-history", headers=_auth(token)).json()
        assert body == {"total": 0, "items": []}

    def test_history_requires_login(self, auth_client):
        assert auth_client.get("/api/user/chat-history").status_code == 401

    def test_history_returns_newest_first(self, auth_client):
        from db.user_db import insert_chat_message

        token, user = _register(auth_client)
        for i in range(3):
            insert_chat_message(user["id"], f"sess-{i}", f"질문{i}", f"답변{i}", intent="qa")

        body = auth_client.get("/api/user/chat-history", headers=_auth(token)).json()
        assert body["total"] == 3
        assert [it["question"] for it in body["items"]] == ["질문2", "질문1", "질문0"]

    def test_history_is_scoped_to_the_owner(self, auth_client):
        """다른 사용자의 이력이 절대 섞이면 안 된다."""
        from db.user_db import insert_chat_message

        token_a, user_a = _register(auth_client, username="usera")
        token_b, user_b = _register(auth_client, username="userb")
        insert_chat_message(user_a["id"], "s1", "A의 질문", "A의 답변")
        insert_chat_message(user_b["id"], "s2", "B의 질문", "B의 답변")

        items_a = auth_client.get("/api/user/chat-history", headers=_auth(token_a)).json()["items"]
        items_b = auth_client.get("/api/user/chat-history", headers=_auth(token_b)).json()["items"]
        assert [it["question"] for it in items_a] == ["A의 질문"]
        assert [it["question"] for it in items_b] == ["B의 질문"]

    def test_history_pagination(self, auth_client):
        from db.user_db import insert_chat_message

        token, user = _register(auth_client)
        for i in range(5):
            insert_chat_message(user["id"], "s", f"질문{i}", "답변")

        page = auth_client.get(
            "/api/user/chat-history?limit=2&offset=1", headers=_auth(token)
        ).json()
        assert page["total"] == 5  # total은 페이지가 아니라 전체 개수
        assert [it["question"] for it in page["items"]] == ["질문3", "질문2"]

    def test_limit_out_of_range_rejected(self, auth_client):
        token, _ = _register(auth_client)
        assert auth_client.get(
            "/api/user/chat-history?limit=9999", headers=_auth(token)
        ).status_code == 422


# ---------------------------------------------------------------------------
# 채팅 스트림 ↔ 이력 저장 배선 (api/chat.py)
# ---------------------------------------------------------------------------

class TestChatStreamPersistsHistory:
    """`access_token` 이 실린 채팅만 계정 이력에 남는지 확인한다."""

    @pytest.fixture()
    def chat_client(self, auth_client, monkeypatch):
        from fastapi import FastAPI
        from starlette.testclient import TestClient

        import api.chat as chat_mod

        # 그래프 전체를 흉내내는 대신 run_agent_stream을 대체한다. 여기서 검증할 것은
        # 에이전트 동작이 아니라 "done 이 나오면 계정 이력에 저장되는가"이기 때문.
        def fake_stream(session_id, question, trace_id="-"):
            yield ("token", "졸업학점은 ")
            yield ("done", {
                "answer": "졸업학점은 130학점입니다.", "source_urls": [], "results": [],
                "intent": "학사", "duration_ms": 10, "model": "fake",
                "sub_questions": 0, "tool_calls": 0, "timing": {},
            })

        monkeypatch.setattr(chat_mod, "run_agent_stream", fake_stream)
        # Q&A JSONL 로거는 실제 파일을 쓴다 — 테스트에서는 막는다.
        monkeypatch.setattr(chat_mod, "get_qa_logger", lambda: MagicMock())

        app = FastAPI()
        app.include_router(chat_mod.router)
        return TestClient(app, raise_server_exceptions=False)

    def _ask(self, chat_client, **params):
        return chat_client.get(
            "/api/chat/stream",
            params={"session_id": "sess-1", "question": "졸업학점 알려줘", **params},
        )

    def test_logged_in_turn_is_saved(self, auth_client, chat_client):
        token, _ = _register(auth_client)
        assert self._ask(chat_client, access_token=token).status_code == 200

        items = auth_client.get("/api/user/chat-history", headers=_auth(token)).json()["items"]
        assert len(items) == 1
        assert items[0]["question"] == "졸업학점 알려줘"
        assert items[0]["answer"] == "졸업학점은 130학점입니다."
        assert items[0]["intent"] == "학사"
        assert items[0]["session_id"] == "sess-1"

    def test_anonymous_turn_is_not_saved(self, auth_client, chat_client):
        """비로그인 채팅도 정상 동작하되, 아무 계정에도 기록되지 않는다."""
        token, _ = _register(auth_client)
        assert self._ask(chat_client).status_code == 200
        assert auth_client.get(
            "/api/user/chat-history", headers=_auth(token)
        ).json()["total"] == 0

    @pytest.mark.parametrize(
        "bad_token", ["garbage.token", "한글.토큰", "abc.한글서명", "a.b.c", "%20.%20"]
    )
    def test_invalid_token_does_not_break_chat(self, auth_client, chat_client, bad_token):
        """만료·위조·깨진 토큰이어도 채팅은 되어야 한다 — 로그인은 선택이다.

        비ASCII 케이스가 회귀 방지의 핵심: 예전엔 verify_user_token이 예외를 던져
        익명 폴백 대신 500이 났다.
        """
        token, _ = _register(auth_client)
        resp = self._ask(chat_client, access_token=bad_token)
        assert resp.status_code == 200 and "done" in resp.text
        assert auth_client.get(
            "/api/user/chat-history", headers=_auth(token)
        ).json()["total"] == 0

    def test_test_mode_turn_is_not_saved(self, auth_client, chat_client):
        """X-Test-Mode(평가·회귀 러너)는 실사용자 이력을 오염시키지 않아야 한다."""
        token, _ = _register(auth_client)
        resp = chat_client.get(
            "/api/chat/stream",
            params={"session_id": "sess-1", "question": "평가용 질문", "access_token": token},
            headers={"X-Test-Mode": "1"},
        )
        assert resp.status_code == 200
        assert auth_client.get(
            "/api/user/chat-history", headers=_auth(token)
        ).json()["total"] == 0


# ---------------------------------------------------------------------------
# 액세스 로그 토큰 가리기 (api/log_setup.py)
# ---------------------------------------------------------------------------

class TestAccessLogRedaction:
    """EventSource가 헤더를 못 붙여 토큰이 쿼리로 간다 — 로그에 평문으로 남으면 안 된다."""

    def _emit(self, path: str) -> str:
        import io
        import logging

        from uvicorn.logging import AccessFormatter

        from api.log_setup import _RedactQuerySecrets  # noqa: SLF001 — 테스트 대상

        buf = io.StringIO()
        handler = logging.StreamHandler(buf)
        handler.setFormatter(AccessFormatter('%(client_addr)s - "%(request_line)s" %(status_code)s'))
        logger = logging.getLogger(f"uvicorn.access.test.{id(buf)}")
        logger.propagate = False
        logger.handlers = []
        logger.addFilter(_RedactQuerySecrets())
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        # uvicorn이 실제로 넘기는 args 모양 그대로.
        logger.info('%s - "%s %s HTTP/%s" %d', "127.0.0.1:1234", "GET", path, "1.1", 200)
        return buf.getvalue()

    def test_token_value_is_redacted(self):
        out = self._emit("/api/chat/stream?session_id=abc&question=hi&access_token=eyJhbGc.SECRETSIG")
        assert "SECRETSIG" not in out
        assert "access_token=<redacted>" in out

    def test_other_params_survive(self):
        """디버깅에 필요한 나머지 쿼리는 그대로 남아야 한다."""
        out = self._emit("/api/chat/stream?session_id=abc&question=hi&access_token=tok.sig")
        assert "session_id=abc" in out and "question=hi" in out

    def test_request_without_token_is_untouched(self):
        out = self._emit("/api/chat/stream?session_id=abc&question=hi")
        assert "redacted" not in out and "session_id=abc" in out
