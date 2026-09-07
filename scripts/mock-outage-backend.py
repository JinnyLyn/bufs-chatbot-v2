#!/usr/bin/env python3
"""Fake chat backend for rehearsing the frontend's outage handling.

The real backend cannot be told to stall, drop a connection or report saturation on
demand, and the production stack must not be restarted for a UI check. This server
speaks just enough of the `/api/session` + `/api/chat/stream` contract for the
frontend, and picks a failure mode from the QUESTION TEXT, so every screen in
reports/CamChat-장애대응.pdf §7 / §16 can be reproduced on a laptop:

    question        behaviour
    --------        ---------
    ok              status events, a few tokens, then `done` (the normal path)
    busy            HTTP 503 + Retry-After: 10 before the stream opens (§7.4)
    busy-stream     stream opens, then an `error` event with code=busy (§7.4, race path)
    ratelimit       HTTP 429 + Retry-After: 30
    stall           one status event, then only keepalive pings — never a token (§7.1/7.2)
    slow            tokens, a 50 s silence, then the rest and `done` (§7.1 recovers)
    cut             a few tokens, then the connection is closed without `done` (§7.3)
    cut-once        like `cut` the first time a session asks; the retry completes normally
    error           stream opens, then a generic `error` event
    http500         HTTP 500 with a JSON body
    html503         HTTP 503 with an HTML body and NO Retry-After (what a tunnel/CDN emits)
    down            drop the TCP connection without a response

Run:
    python3 scripts/mock-outage-backend.py --port 8099
    cd frontend && BACKEND_ORIGIN=http://localhost:8099 npx next dev -p 3100

Then open http://localhost:3100/ko/chat and send one of the words above as the question.
Anything else behaves like `ok`. Tokens are ASCII so the script has no encoding concerns.

What this mock does NOT reproduce:
  - proxy/CDN buffering of the stream (it answers with `Connection: close`, not the
    keep-alive + `X-Accel-Buffering: no` the real backend sends) — a stall caused by an
    intermediary cannot be rehearsed here, only one caused by the origin;
  - cross-origin CORS behaviour (no `OPTIONS` handler, no CORS headers) — it is meant to
    sit behind the Next.js `/api` rewrite, same-origin, like production behind cloudflared.
"""

import argparse
import json
import sys
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

PING_INTERVAL_S = 15  # sse-starlette's default keepalive cadence
_CUT_ONCE_SEEN: set[str] = set()  # session ids whose `cut-once` request was already cut


def _sse(event: str, data) -> bytes:
    payload = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
    return f"event: {event}\r\ndata: {payload}\r\n\r\n".encode("utf-8")


def _ping() -> bytes:
    return f": ping - {time.time():.0f}\r\n\r\n".encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # noqa: D401 — quieter than the default
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    # -- helpers -------------------------------------------------------------------
    def _json(self, status: int, body: dict, headers: dict | None = None) -> None:
        raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(raw)

    def _open_stream(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.flush()

    def _emit(self, chunk: bytes) -> None:
        self.wfile.write(chunk)
        self.wfile.flush()

    def _silence(self, seconds: float) -> None:
        """Send only keepalive pings for `seconds` — progress-free time for the UI."""
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            time.sleep(min(PING_INTERVAL_S, max(0.0, end - time.monotonic())))
            self._emit(_ping())

    def _tokens(self, words) -> None:
        for w in words:
            self._emit(_sse("token", {"token": w + " "}))
            time.sleep(0.15)

    # -- routes --------------------------------------------------------------------
    def do_POST(self) -> None:  # noqa: N802
        if urlparse(self.path).path != "/api/session":
            self._json(404, {"detail": "not found"})
            return
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        self._json(200, {
            "session_id": str(uuid.uuid4()), "lang": "ko", "user_profile": None,
            "has_transcript": False, "messages_count": 0,
        })

    def do_GET(self) -> None:  # noqa: N802
        url = urlparse(self.path)
        if url.path == "/health":
            self._json(200, {"status": "ok"})
            return
        if url.path != "/api/chat/stream":
            self._json(404, {"detail": "not found"})
            return
        qs = parse_qs(url.query)
        mode = (qs.get("question") or ["ok"])[0].strip().lower()
        if mode == "cut-once":
            sid = (qs.get("session_id") or [""])[0]
            mode = "ok" if sid in _CUT_ONCE_SEEN else "cut"
            if len(_CUT_ONCE_SEEN) > 1000:  # every page load mints a new session id
                _CUT_ONCE_SEEN.clear()
            _CUT_ONCE_SEEN.add(sid)
        try:
            self._serve(mode)
        except (BrokenPipeError, ConnectionResetError):
            sys.stderr.write(f"[{mode}] client went away\n")

    def _serve(self, mode: str) -> None:
        if mode == "busy":
            self._json(503, {"detail": "지금 처리 중인 질문이 많습니다. 잠시 후 다시 시도해 주세요."},
                       {"Retry-After": "10"})
            return
        if mode == "ratelimit":
            self._json(429, {"detail": "요청이 너무 많습니다. 잠시 후 다시 시도해 주세요."},
                       {"Retry-After": "30"})
            return
        if mode == "http500":
            self._json(500, {"detail": "Internal Server Error"})
            return
        if mode == "html503":
            raw = b"<html><body><h1>503 Service Unavailable</h1></body></html>"
            self.send_response(503)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return
        if mode == "down":
            # Send nothing and let the framework close the socket after we return: the
            # browser sees a connection closed with no response (a network error), which
            # is what a dead origin looks like. Closing the socket here by hand makes the
            # framework's post-request flush blow up instead.
            self.close_connection = True
            return

        self._open_stream()
        self._emit(_sse("status", {"stage": "searching", "searches": 0}))

        if mode == "busy-stream":
            self._emit(_sse("error", {
                "message": "지금 처리 중인 질문이 많습니다. 잠시 후 다시 시도해 주세요.",
                "code": "busy", "retry_after": 10,
            }))
            return
        if mode == "error":
            self._emit(_sse("error", {"message": "처리 중 오류가 발생했습니다. 다시 시도해 주세요."}))
            return
        if mode == "stall":
            self._silence(3 * 60)  # past the 120 s hard limit; the frontend hangs up first
            return

        time.sleep(0.5)
        self._emit(_sse("status", {"stage": "writing", "searches": 1}))
        head = ["This", "is", "a", "mock", "answer", "for", "mode", f"`{mode}`."]
        tail = ["It", "finished", "normally."]
        self._tokens(head)
        if mode == "cut":
            return  # close without `done`
        if mode == "slow":
            self._silence(50)  # > 45 s soft notice, < 120 s hard limit
        self._tokens(tail)
        answer = " ".join(head + tail)
        self._emit(_sse("done", {
            "answer": answer, "source_urls": [], "results": [], "intent": "mock",
            "duration_ms": 1234,
        }))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=8099)
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args()
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    srv.daemon_threads = True
    print(f"mock outage backend on http://{args.host}:{args.port} (Ctrl-C to stop)", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
