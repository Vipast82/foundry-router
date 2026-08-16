"""Meridian auth control — refresh a stale token, or drive a full re-login,
from Foundry's admin UI instead of SSHing into the Meridian host.

Two tiers, because Meridian's re-auth has two distinct failure modes:

  1. Token EXPIRED (the common case — tokens live ~8h). Meridian exposes an
     HTTP refresh: `POST {origin}/auth/refresh` swaps the stored refresh token
     for a fresh access token with no human in the loop. Foundry calls it
     directly on the backend origin it already has (refresh_token_direct) — no
     companion, no SSH.

  2. OAuth session DEAD (`sources.oauth` goes null — the case usage.py's watcher
     alerts on). A refresh can't revive a dead credential; that needs a full
     OAuth login: `meridian profile login <profile> --headless`, which prints a
     sign-in URL, waits for you to authenticate, then asks you to paste a code
     back. That paste-back can't be a single API call, so it runs through a
     small companion service on the Meridian host (contrib/meridian-auth-
     service) — same trust model as the gateway inspect companion (localhost-
     bound, bearer token, firewalled to Foundry's host). Login is a two-step
     exchange: start -> (operator signs in) -> complete.

The companion URL/token live in kv (like the gateway inspect settings and the
MCP auth tokens) so enabling this needs no config.yaml edit or restart.
"""

from __future__ import annotations

from typing import Optional
from urllib.parse import urlsplit

AUTH_URL_KEY = "meridian_auth_url"
AUTH_TOKEN_KEY = "meridian_auth_token"


def auth_settings(db) -> dict:
    return {"url": db.kv_get(AUTH_URL_KEY) or "",
            "has_token": bool(db.kv_get(AUTH_TOKEN_KEY))}


def set_auth_settings(db, url: str, token: Optional[str] = None) -> None:
    """Persist the companion URL (and optionally its bearer token — write-only:
    None keeps the existing one, empty string clears it)."""
    url = (url or "").strip()
    if url:
        db.kv_set(AUTH_URL_KEY, url)
    else:
        db.kv_del(AUTH_URL_KEY)
    if token is not None:
        if token:
            db.kv_set(AUTH_TOKEN_KEY, token)
        else:
            db.kv_del(AUTH_TOKEN_KEY)


def _origin(url: str) -> str:
    """The scheme://host:port of a backend URL — Meridian's /auth/refresh lives
    at the root, not under the anthropic API path (a backend configured as
    .../v1 would otherwise get the wrong URL)."""
    p = urlsplit(url)
    return f"{p.scheme}://{p.netloc}" if p.scheme and p.netloc else url.rstrip("/")


async def refresh_token_direct(svc, base_url: str,
                               api_key: Optional[str] = None) -> dict:
    """Ask Meridian itself to refresh the access token (POST /auth/refresh on the
    backend origin). Covers the common 'token expired' case with no companion and
    no SSH. Returns {ok, status, detail}. A 404 means this Meridian build predates
    the endpoint — reported as guidance to use the full re-login, not a crash."""
    url = _origin(base_url) + "/auth/refresh"
    headers = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    try:
        r = await svc.http.post(url, headers=headers, timeout=30)
    except Exception as e:  # unreachable / DNS / connection reset
        return {"ok": False, "status": None, "detail": f"unreachable: {e}"}
    if r.status_code == 404:
        return {"ok": False, "status": 404,
                "detail": "this Meridian build has no /auth/refresh endpoint — "
                          "use Re-authenticate (full login) instead"}
    return {"ok": r.status_code < 400, "status": r.status_code,
            "detail": (r.text or "").strip()[:500]}


def _companion(svc) -> tuple[str, dict]:
    url = (svc.db.kv_get(AUTH_URL_KEY) or "").rstrip("/")
    headers = {}
    token = svc.db.kv_get(AUTH_TOKEN_KEY)
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return url, headers


async def companion_refresh(svc, profile: str) -> dict:
    """Non-interactive refresh via the host companion (meridian refresh-token) —
    the CLI counterpart to refresh_token_direct, for builds without the HTTP
    endpoint. {configured: False} when no companion URL is set."""
    url, headers = _companion(svc)
    if not url:
        return {"configured": False}
    r = await svc.http.post(url + "/refresh", json={"profile": profile},
                            headers=headers, timeout=60)
    r.raise_for_status()
    return {"configured": True, **r.json()}


async def login_start(svc, profile: str) -> dict:
    """Begin a full OAuth re-login on the host: the companion spawns
    `meridian profile login <profile> --headless`, captures the sign-in URL, and
    holds the process open awaiting the pasted code. Returns {configured, ok,
    session_id, url, output}."""
    url, headers = _companion(svc)
    if not url:
        return {"configured": False}
    r = await svc.http.post(url + "/login/start", json={"profile": profile},
                            headers=headers, timeout=90)
    r.raise_for_status()
    return {"configured": True, **r.json()}


async def login_complete(svc, session_id: str, code: str) -> dict:
    """Finish the re-login: hand the pasted code to the held process's stdin and
    return its result. Returns {configured, ok, output, exit_code}."""
    url, headers = _companion(svc)
    if not url:
        return {"configured": False}
    r = await svc.http.post(url + "/login/complete",
                            json={"session_id": session_id, "code": code},
                            headers=headers, timeout=120)
    r.raise_for_status()
    return {"configured": True, **r.json()}
