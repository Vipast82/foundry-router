"""Meridian re-auth from the UI (no SSH): the two tiers are the direct HTTP
token refresh (Foundry -> Meridian /auth/refresh) and the companion-driven full
OAuth re-login (start -> paste code -> complete). Settings live in kv like the
gateway inspect companion."""

import pytest

from foundry_router.db import Database
from foundry_router.meridian_auth import (auth_settings, set_auth_settings,
                                          _origin, refresh_token_direct,
                                          login_start, login_complete)


class FakeHTTP:
    """Records posts; replays a canned response supporting both the refresh
    path (status_code/text) and the companion path (raise_for_status/json)."""
    def __init__(self, *, status=200, text="", payload=None, raises=None):
        self.status, self.text, self.payload, self.raises = status, text, payload, raises
        self.calls = []

    async def post(self, url, json=None, headers=None, timeout=None):
        self.calls.append({"url": url, "json": json, "headers": headers or {}})
        if self.raises:
            raise self.raises
        outer = self

        class R:
            status_code = outer.status
            text = outer.text
            def raise_for_status(s): pass
            def json(s): return outer.payload
        return R()


class _Svc:
    def __init__(self, db, http):
        self.db, self.http = db, http


# -- settings round-trip -----------------------------------------------------

def test_auth_settings_roundtrip(tmp_path):
    db = Database(tmp_path / "a.sqlite")
    assert auth_settings(db) == {"url": "", "has_token": False}
    set_auth_settings(db, "http://host:8898/", token="secret")
    assert auth_settings(db) == {"url": "http://host:8898/", "has_token": True}
    set_auth_settings(db, "http://host:8898/", token=None)   # None keeps token
    assert auth_settings(db)["has_token"] is True
    set_auth_settings(db, "http://host:8898/", token="")     # "" clears token
    assert auth_settings(db)["has_token"] is False
    set_auth_settings(db, "")                                 # clears url
    assert auth_settings(db)["url"] == ""


# -- direct token refresh ----------------------------------------------------

def test_origin_strips_api_path():
    assert _origin("http://h:3456/v1") == "http://h:3456"
    assert _origin("https://meridian.lan:3456/") == "https://meridian.lan:3456"


async def test_refresh_direct_success(tmp_path):
    http = FakeHTTP(status=200, text="ok")
    out = await refresh_token_direct(_Svc(Database(tmp_path / "r.sqlite"), http),
                                     "http://h:3456/v1", api_key="k")
    assert out["ok"] is True and out["status"] == 200
    assert http.calls[0]["url"] == "http://h:3456/auth/refresh"   # origin, not /v1
    assert http.calls[0]["headers"]["Authorization"] == "Bearer k"


async def test_refresh_direct_404_guides_to_full_login(tmp_path):
    http = FakeHTTP(status=404, text="nope")
    out = await refresh_token_direct(_Svc(Database(tmp_path / "r2.sqlite"), http),
                                     "http://h:3456")
    assert out["ok"] is False and out["status"] == 404
    assert "Re-authenticate" in out["detail"]


async def test_refresh_direct_unreachable(tmp_path):
    http = FakeHTTP(raises=OSError("connection refused"))
    out = await refresh_token_direct(_Svc(Database(tmp_path / "r3.sqlite"), http),
                                     "http://h:3456")
    assert out["ok"] is False and out["status"] is None
    assert "unreachable" in out["detail"]


# -- companion full re-login -------------------------------------------------

async def test_login_start_unconfigured_makes_no_call(tmp_path):
    http = FakeHTTP(payload={})
    out = await login_start(_Svc(Database(tmp_path / "l.sqlite"), http), "victor")
    assert out == {"configured": False}
    assert http.calls == []


async def test_login_start_calls_companion(tmp_path):
    db = Database(tmp_path / "l2.sqlite")
    set_auth_settings(db, "http://host:8898/", token="tok")
    http = FakeHTTP(payload={"ok": True, "session_id": "abc", "url": "https://claude.com/x"})
    out = await login_start(_Svc(db, http), "victor")
    assert out["configured"] is True and out["ok"] is True
    assert out["session_id"] == "abc" and out["url"] == "https://claude.com/x"
    assert http.calls[0]["url"] == "http://host:8898/login/start"
    assert http.calls[0]["json"] == {"profile": "victor"}
    assert http.calls[0]["headers"]["Authorization"] == "Bearer tok"


async def test_login_complete_calls_companion(tmp_path):
    db = Database(tmp_path / "l3.sqlite")
    set_auth_settings(db, "http://host:8898")
    http = FakeHTTP(payload={"ok": True, "exit_code": 0, "output": "authenticated"})
    out = await login_complete(_Svc(db, http), "abc", "CODE#state")
    assert out["configured"] is True and out["ok"] is True
    assert http.calls[0]["url"] == "http://host:8898/login/complete"
    assert http.calls[0]["json"] == {"session_id": "abc", "code": "CODE#state"}
