"""Per-client rate limiting and a global concurrency cap for the expensive endpoints.

Why this exists: the service is published to the internet through the Cloudflare
tunnel with no authentication, and one `/api/chat/stream` call costs several sequential
LLM generations on the GPU. Without a limiter a single client can occupy the model
indefinitely and deny service to students at negligible cost to itself.

Client identity: the origin binds loopback (see `server.py`), so the only thing that
connects to it is cloudflared, and every remote address is 127.0.0.1. The real client
address therefore has to come from the `CF-Connecting-IP` header that Cloudflare sets.
That header is trustworthy *because* the origin is not reachable directly — anyone who
could set it themselves would have to be inside the tunnel already. If the origin is
ever exposed directly again, this degrades to limiting per spoofable header, so keep
the loopback bind.

Deliberately dependency-free (no slowapi/redis): a single uvicorn process serves this
app, so in-process state is the whole picture, and adding a shared store would buy
nothing until the deployment is multi-process.
"""

import os
import threading
import time
from collections import deque
from typing import Deque, Optional

from fastapi import HTTPException, Request

# Master switch. On by default: the safe posture for a public, unauthenticated endpoint.
RATE_LIMIT_ENABLED = os.environ.get("RATE_LIMIT_ENABLED", "true").lower() in ("1", "true", "yes", "on")
# Sustained budget per client over the window. A student asking follow-up questions runs
# well under this; a scripted abuser hits it within seconds.
RATE_LIMIT_REQUESTS = int(os.environ.get("RATE_LIMIT_REQUESTS", "20"))
RATE_LIMIT_WINDOW_S = float(os.environ.get("RATE_LIMIT_WINDOW_S", "60"))
# Exempt requests that originate on this host AND did not come through the tunnel, so
# the eval/KPI harnesses are not throttled. See _is_loopback for why this is not a
# bypass for remote callers.
RATE_LIMIT_EXEMPT_LOOPBACK = os.environ.get("RATE_LIMIT_EXEMPT_LOOPBACK", "true").lower() in (
    "1", "true", "yes", "on"
)
# Ceiling on chat streams generating at once, across all clients. The GPU serves these
# sequentially anyway, so admitting more only grows the queue and every user's latency.
MAX_CONCURRENT_STREAMS = int(os.environ.get("MAX_CONCURRENT_STREAMS", "8"))
# Retry-After (seconds) sent with every "all slots busy" refusal — as a header on the
# 503 and as `retry_after` in the in-stream busy error. The frontend locks its retry
# button and composer for exactly this long (reports/CamChat-장애대응.pdf §7.4).
BUSY_RETRY_AFTER_S = 10
# Cap on tracked clients, so the limiter's own bookkeeping cannot be turned into the
# memory-exhaustion primitive it exists to prevent.
_MAX_TRACKED_CLIENTS = int(os.environ.get("RATE_LIMIT_MAX_CLIENTS", "20000"))

_hits: dict[str, Deque[float]] = {}
_lock = threading.Lock()

_streams = 0
_streams_lock = threading.Lock()


def client_key(request: Request) -> str:
    """Identifier for the remote caller.

    `CF-Connecting-IP` only, then the socket peer. Deliberately NOT `X-Forwarded-For`
    or `True-Client-IP`: both are client-settable, so honouring them would let an
    attacker mint a fresh budget per request just by varying a header. Cloudflare
    overwrites `CF-Connecting-IP` on every proxied request, and the origin binds
    loopback, so on the tunnel path it is the caller's real address.
    """
    value = request.headers.get("CF-Connecting-IP", "").strip()
    if value:
        return value
    return request.client.host if request.client else "unknown"


def _is_loopback(request: Request) -> bool:
    """True for requests originating on this host and not proxied by Cloudflare.

    The eval and KPI harnesses mint a session per question, so a 100-question run is
    ~200 requests from one address and would trip the limit part-way through. Several
    runners do not handle 429 and would record the refusals as answers, silently
    corrupting a baseline. Tunnel traffic always carries CF-Connecting-IP, so requiring
    its absence keeps this from becoming a bypass for a *remote* caller.

    The trade-off it does accept: another account on this shared box can call the API
    over loopback unmetered. That is judged acceptable because a local account already
    has strictly more capability than the HTTP surface offers — it can read the code and
    run inference directly — and because loopback traffic is indistinguishable from a
    legitimate eval run. Set RATE_LIMIT_EXEMPT_LOOPBACK=false to meter it anyway, and
    disable the limiter for eval runs instead.
    """
    if request.headers.get("CF-Connecting-IP", "").strip():
        return False
    host = request.client.host if request.client else ""
    return host in {"127.0.0.1", "::1", "localhost"}


def _prune_locked(now: float) -> None:
    """Enforce the tracked-client cap. Caller holds `_lock`.

    Drops clients idle for a full window first. If that frees nothing — every tracked
    client is currently active — the oldest are evicted anyway, so the table is bounded
    unconditionally rather than drifting upward one entry per new client.
    """
    cutoff = now - RATE_LIMIT_WINDOW_S
    for key in [k for k, hits in _hits.items() if not hits or hits[-1] <= cutoff]:
        del _hits[key]
    if len(_hits) < _MAX_TRACKED_CLIENTS:
        return
    # Oldest-activity-first. Evicting an active client only refunds its budget; letting
    # the table grow without bound would recreate the exhaustion this module prevents.
    for key in sorted(_hits, key=lambda k: _hits[k][-1] if _hits[k] else 0.0)[
        : len(_hits) - _MAX_TRACKED_CLIENTS + 1
    ]:
        del _hits[key]


def check_rate_limit(request: Request) -> None:
    """Record one request for this client and reject it if over budget.

    Raises:
        HTTPException: 429, carrying Retry-After, when the client is over budget.
    """
    if not RATE_LIMIT_ENABLED:
        return
    if RATE_LIMIT_EXEMPT_LOOPBACK and _is_loopback(request):
        return
    key = client_key(request)
    now = time.monotonic()
    cutoff = now - RATE_LIMIT_WINDOW_S

    with _lock:
        hits = _hits.get(key)
        if hits is None:
            if len(_hits) >= _MAX_TRACKED_CLIENTS:
                _prune_locked(now)
            hits = _hits.setdefault(key, deque())
        while hits and hits[0] <= cutoff:
            hits.popleft()
        if len(hits) >= RATE_LIMIT_REQUESTS:
            retry_after = max(1, int(hits[0] + RATE_LIMIT_WINDOW_S - now) + 1)
            raise HTTPException(
                status_code=429,
                detail="요청이 너무 많습니다. 잠시 후 다시 시도해 주세요.",
                headers={"Retry-After": str(retry_after)},
            )
        hits.append(now)


class StreamSlot:
    """Holds one of the MAX_CONCURRENT_STREAMS slots.

    Acquire before starting generation and release when the stream ends, including on
    client disconnect — otherwise abandoned EventSource connections leak slots until
    the cap is exhausted and the service is wedged. `release()` is idempotent so the
    generator's `finally` is safe to run more than once.
    """

    __slots__ = ("_held",)

    def __init__(self) -> None:
        self._held = False

    def acquire(self) -> "StreamSlot":
        """Take a slot.

        Raises:
            HTTPException: 503 when every slot is busy.
        """
        global _streams
        if MAX_CONCURRENT_STREAMS <= 0:
            return self
        with _streams_lock:
            if _streams >= MAX_CONCURRENT_STREAMS:
                raise HTTPException(
                    status_code=503,
                    detail="지금 처리 중인 질문이 많습니다. 잠시 후 다시 시도해 주세요.",
                    headers={"Retry-After": str(BUSY_RETRY_AFTER_S)},
                )
            _streams += 1
            self._held = True
        return self

    def release(self) -> None:
        global _streams
        if not self._held:
            return
        with _streams_lock:
            _streams -= 1
            self._held = False

    def __enter__(self) -> "StreamSlot":
        return self.acquire()

    def __exit__(self, *exc: object) -> None:
        self.release()


def reject_if_saturated() -> None:
    """Refuse with a real 503 when every stream slot is busy, WITHOUT reserving one.

    The authoritative acquire happens inside the SSE generator, because only there is a
    release guaranteed (the producer thread's finally). Acquiring in the endpoint would
    strand a slot whenever the generator never runs — a client that disconnects before
    the response starts, or any exception raised between the acquire and the generator.
    Those stranded slots never come back, so the cap wedges the service at 503 forever.

    This check is therefore advisory and racy by design: it exists so a saturated server
    answers with a proper status code instead of opening a stream and failing inside it.
    The generator's acquire is what actually enforces the cap.

    Raises:
        HTTPException: 503 when no slot is currently free.
    """
    if MAX_CONCURRENT_STREAMS <= 0:
        return
    with _streams_lock:
        saturated = _streams >= MAX_CONCURRENT_STREAMS
    if saturated:
        raise HTTPException(
            status_code=503,
            detail="지금 처리 중인 질문이 많습니다. 잠시 후 다시 시도해 주세요.",
            headers={"Retry-After": str(BUSY_RETRY_AFTER_S)},
        )


def active_streams() -> int:
    with _streams_lock:
        return _streams


def reset_for_tests(max_concurrent: Optional[int] = None) -> None:
    """Clear limiter state between tests."""
    global _streams, MAX_CONCURRENT_STREAMS
    with _lock:
        _hits.clear()
    with _streams_lock:
        _streams = 0
    if max_concurrent is not None:
        MAX_CONCURRENT_STREAMS = max_concurrent
