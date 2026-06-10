"""Langfuse SDK + REST client for debug tooling.

Repo-root-anchored dotenv loading so this works regardless of cwd.
Neutralises Windows CA bundle paths on Linux/WSL (AV-injected certs).
"""

from __future__ import annotations

import os
import sys
import time
import platform
from pathlib import Path

import requests
from dotenv import load_dotenv

# ── dotenv: anchored to repo root regardless of cwd ──────────────────────────
_REPO_ROOT = Path(__file__).resolve().parents[1]
_ENV_PATH = _REPO_ROOT / "project" / ".env"
load_dotenv(_ENV_PATH, override=False)

# ── WSL/Linux: unset CA bundle env-vars if they point at Windows paths ────────
def _neutralise_windows_ca() -> None:
    """On Linux/WSL, Windows-path CA bundles break certifi. Unset them."""
    if platform.system() != "Linux":
        return
    for var in ("REQUESTS_CA_BUNDLE", "SSL_CERT_FILE"):
        val = os.environ.get(var, "")
        if val and (val[:3].endswith(":\\") or val.startswith("C:/") or
                    val.startswith("\\\\") or (len(val) > 1 and val[1] == ":")):
            del os.environ[var]


_neutralise_windows_ca()

# ── mirror LANGFUSE_BASE_URL → LANGFUSE_HOST (as config.py does) ─────────────
_BASE_URL = os.environ.get("LANGFUSE_BASE_URL", "https://cloud.langfuse.com")
if _BASE_URL and not os.environ.get("LANGFUSE_HOST"):
    os.environ["LANGFUSE_HOST"] = _BASE_URL

# ── REST helpers (proven pattern from eval_tools/_langfuse_analyze.py) ────────
_BASE = _BASE_URL.rstrip("/")
_CA = os.environ.get("REQUESTS_CA_BUNDLE")  # will be None after neutralisation on Linux


def _rest_get(path: str, **params) -> dict:
    """Authenticated REST GET with retry for transient server errors."""
    pk = os.environ.get("LANGFUSE_PUBLIC_KEY", "")
    sk = os.environ.get("LANGFUSE_SECRET_KEY", "")
    auth = (pk, sk)
    for attempt in range(5):
        try:
            r = requests.get(
                _BASE + path,
                params=params,
                auth=auth,
                timeout=60,
                verify=_CA,
            )
            if r.status_code in (429, 500, 502, 503, 504):
                time.sleep(2 * (attempt + 1))
                continue
            r.raise_for_status()
            return r.json()
        except requests.exceptions.RequestException:
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"REST GET {path} failed after 5 attempts")


# ── SDK client (langfuse 4.6.1) ───────────────────────────────────────────────
_sdk_client = None


def get_client():
    """Return the singleton Langfuse SDK client (lazily initialised)."""
    global _sdk_client
    if _sdk_client is None:
        from langfuse import get_client as _lf_get_client
        _sdk_client = _lf_get_client()
    return _sdk_client


# ── public API ────────────────────────────────────────────────────────────────

def auth_check() -> bool:
    """Verify credentials against Cloud EU via the SDK.

    Falls back to REST if the SDK surface doesn't expose auth_check().
    Returns True on success, raises on failure.
    """
    try:
        client = get_client()
        ok = client.auth_check()
        if not ok:
            raise RuntimeError(
                "auth_check() returned False — verify LANGFUSE_PUBLIC_KEY / "
                "LANGFUSE_SECRET_KEY / LANGFUSE_BASE_URL in project/.env"
            )
        return True
    except AttributeError:
        # Fallback: REST auth probe
        _rest_get("/api/public/traces", limit=1, page=1)
        return True


def fetch_one_trace(limit: int = 1) -> dict | None:
    """Fetch one (or more) traces from the REST API.

    Returns the first trace dict, or None if no traces found.
    Raises on auth / network errors.
    """
    data = _rest_get("/api/public/traces", limit=limit, page=1).get("data", [])
    return data[0] if data else None


def fetch_traces(want: int = 200, **filters) -> list[dict]:
    """Paginated fetch of up to *want* traces (mirrors _langfuse_analyze.py)."""
    out, page = [], 1
    while len(out) < want:
        data = _rest_get("/api/public/traces", limit=50, page=page, **filters).get("data", [])
        if not data:
            break
        out.extend(data)
        page += 1
    return out[:want]


def fetch_observations(want: int = 1200, **filters) -> list[dict]:
    """Paginated fetch of up to *want* observations."""
    out, page = [], 1
    while len(out) < want:
        data = _rest_get("/api/public/observations", limit=50, page=page, **filters).get("data", [])
        if not data:
            break
        out.extend(data)
        page += 1
    return out[:want]


def fetch_trace_detail(trace_id: str) -> dict:
    """Fetch full trace detail including inline observations."""
    return _rest_get(f"/api/public/traces/{trace_id}")


if __name__ == "__main__":
    # Quick smoke test: python debug/langfuse_client.py
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    print(f"dotenv loaded from: {_ENV_PATH}")
    print(f"LANGFUSE_HOST = {os.environ.get('LANGFUSE_HOST')}")
    print("Running auth_check()...")
    auth_check()
    print("auth_check() OK")
    print("Fetching one trace...")
    t = fetch_one_trace()
    if t:
        print(f"trace id={t.get('id')}  latency={t.get('latency')}s  sess={str(t.get('sessionId'))[:8]}")
    else:
        print("No traces found (empty project?)")
    print("Done.")
