"""Meridian auth + usage monitoring.

CORRECTED after confirming live against Meridian 1.45.0: the quota endpoint
(`/v1/usage/quota`) works and returns 200, but `sources.oauth` and
`buckets[].utilization` come back null — Anthropic's OAuth *usage* source is
dark while inference still works (sdk source). `/health` shows `loggedIn: true`
alongside it. So a null oauth source is a USAGE-DATA gap, NOT an auth failure:
auth validity now comes from `/health.loggedIn`, and re-auth is NOT the fix
(re-login doesn't repopulate it). The quota parser still handles the shape (null
utilization => no signal => window assumed available)."""

import pytest

from foundry_router.config import MeridianConfig
from foundry_router.db import Database
from foundry_router.usage import (MeridianUsage, parse_extra_usage,
                                  parse_sources)


class FakeResponse:
    def __init__(self, data):
        self._data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self._data


class FakeHTTP:
    """URL-aware: serves /health, /telemetry/summary, and the quota endpoint
    separately (auth_health now hits /health AND the quota endpoint)."""
    def __init__(self, quota=None, health=None, telemetry=None):
        self.quota, self.health, self.telemetry = quota, health, telemetry

    async def get(self, url, headers=None, timeout=None):
        if url.endswith("/health"):
            return FakeResponse(self.health if self.health is not None else {})
        if url.endswith("/telemetry/summary"):
            return FakeResponse(self.telemetry if self.telemetry is not None else {})
        return FakeResponse(self.quota if self.quota is not None else {})


class DeadHTTP:
    async def get(self, url, headers=None, timeout=None):
        raise RuntimeError("connection refused")


# Quota shapes. HEALTHY: oauth populated + both buckets + extraUsage.usedCredits
# in cents (4343 == "$43.43 spent"). NULL_USAGE: the real Meridian 1.45.0 shape —
# 200 OK, but oauth source and utilization both null.
HEALTHY = {"buckets": [{"type": "seven_day", "utilization": 0.47, "resetsAt": None},
                       {"type": "five_hour", "utilization": 0.10, "resetsAt": None}],
           "sources": {"oauth": {"account": "victor"}, "sdk": {"entryCount": 3}},
           "extraUsage": {"usedCredits": 4343}}

NULL_USAGE = {"buckets": [{"type": "five_hour", "status": "allowed",
                           "utilization": None, "resetsAt": 1786961400}],
              "sources": {"oauth": None, "sdk": {"entryCount": 1}},
              "extraUsage": None}

HEALTH_IN = {"status": "healthy", "version": "1.45.0", "mode": "passthrough",
             "auth": {"loggedIn": True, "email": "me@example.com",
                      "subscriptionType": "max"}}
HEALTH_NULLID = {"status": "healthy", "version": "1.45.0", "mode": "passthrough",
                 "auth": {"loggedIn": True, "email": None, "subscriptionType": None}}
HEALTH_OUT = {"status": "healthy", "auth": {"loggedIn": False, "email": None,
                                            "subscriptionType": None}}
TELE = {"windowMs": 3600000, "totalRequests": 2,
        "tokenUsage": {"totalInputTokens": 100, "totalOutputTokens": 50,
                       "totalCacheReadTokens": 10}}


# -- parsing (quota shape) -------------------------------------------------------

def test_parse_sources_three_states():
    assert parse_sources(HEALTHY) is True
    assert parse_sources(NULL_USAGE) is False              # sources present, oauth null
    assert parse_sources({"buckets": []}) is None          # older builds: no sources key
    assert parse_sources(None) is None


def test_parse_extra_usage_cents_to_dollars():
    assert parse_extra_usage(HEALTHY) == pytest.approx(43.43)
    assert parse_extra_usage({"extraUsage": {}}) is None
    assert parse_extra_usage({}) is None


# -- snapshot carries the signals ------------------------------------------------

async def test_snapshot_carries_oauth_and_credits(tmp_path):
    usage = MeridianUsage(MeridianConfig(), FakeHTTP(quota=HEALTHY),
                          Database(tmp_path / "o.sqlite"))
    snap = await usage.snapshot("http://m")
    assert snap["oauth_ok"] is True
    assert snap["credits_used_usd"] == pytest.approx(43.43)


async def test_null_usage_degrades_available_not_error(tmp_path):
    # the real 1.45.0 case: 200 with null utilization/oauth => no signal, window
    # assumed available (never a false "exhausted"), oauth flagged in the note
    usage = MeridianUsage(MeridianConfig(), FakeHTTP(quota=NULL_USAGE),
                          Database(tmp_path / "o1.sqlite"))
    snap = await usage.snapshot("http://m")
    assert snap["oauth_ok"] is False
    assert snap["available"] is True
    assert snap["worst_used"] is None
    assert "sources.oauth null" in snap["note"]


# -- oauth-null is INFO (usage gap), not an error, and never suggests re-auth ----

async def test_oauth_null_logs_info_not_error_and_no_reauth_prompt(tmp_path):
    db = Database(tmp_path / "o2.sqlite")
    http = FakeHTTP(quota=NULL_USAGE)
    usage = MeridianUsage(MeridianConfig(), http, db)

    snap = await usage.snapshot("http://m")
    assert snap["oauth_alert_since"]
    usage.clear_cache()
    await usage.snapshot("http://m")  # second poll while still null
    # edge-triggered: one entry, and it's INFO (not error), and does NOT tell the
    # operator to re-login (that never fixed it)
    assert db.query("SELECT * FROM event_log WHERE level='error' AND source='usage'") == []
    infos = db.query("SELECT * FROM event_log WHERE level='info' AND source='usage'"
                     " AND message LIKE '%sources.oauth is null%'")
    assert len(infos) == 1
    assert "profile login" not in infos[0]["message"]

    http.quota = HEALTHY  # usage source comes back
    usage.clear_cache()
    snap = await usage.snapshot("http://m")
    assert snap["oauth_ok"] is True and "oauth_alert_since" not in snap
    assert db.query("SELECT * FROM event_log WHERE message LIKE '%available again%'")


# -- /health is the auth signal, decoupled from usage ----------------------------

async def test_auth_valid_from_health_even_when_usage_null(tmp_path):
    # THE core fix: logged in per /health, but the quota oauth source is null.
    # valid must be True (auth is fine); usage just isn't reported.
    usage = MeridianUsage(MeridianConfig(),
                          FakeHTTP(quota=NULL_USAGE, health=HEALTH_NULLID, telemetry=TELE),
                          Database(tmp_path / "o3.sqlite"))
    h = await usage.auth_health("http://m")
    assert h["valid"] is True                    # NOT "stale/invalid" anymore
    assert h["logged_in"] is True
    assert h["usage_reported"] is False          # surfaced separately
    assert h["oauth_ok"] is False


async def test_auth_health_reports_identity_and_usage(tmp_path):
    usage = MeridianUsage(MeridianConfig(),
                          FakeHTTP(quota=HEALTHY, health=HEALTH_IN, telemetry=TELE),
                          Database(tmp_path / "o4.sqlite"))
    h = await usage.auth_health("http://m")
    assert h["valid"] is True and h["logged_in"] is True
    assert h["email"] == "me@example.com" and h["subscription_type"] == "max"
    assert h["version"] == "1.45.0" and h["mode"] == "passthrough"
    assert h["usage_reported"] is True           # oauth populated + real buckets


async def test_auth_health_not_logged_in(tmp_path):
    usage = MeridianUsage(MeridianConfig(),
                          FakeHTTP(quota=NULL_USAGE, health=HEALTH_OUT),
                          Database(tmp_path / "o5.sqlite"))
    h = await usage.auth_health("http://m")
    assert h["valid"] is False and h["logged_in"] is False


async def test_auth_health_health_unreachable(tmp_path):
    usage = MeridianUsage(MeridianConfig(), DeadHTTP(),
                          Database(tmp_path / "o6.sqlite"))
    h = await usage.auth_health("http://m")
    assert h["valid"] is False
    assert "connection refused" in (h["error"] or "")


# -- telemetry probe -------------------------------------------------------------

async def test_telemetry_parses_token_usage(tmp_path):
    usage = MeridianUsage(MeridianConfig(), FakeHTTP(telemetry=TELE),
                          Database(tmp_path / "o7.sqlite"))
    t = await usage.telemetry("http://m")
    assert t["reachable"] is True
    assert t["input_tokens"] == 100 and t["output_tokens"] == 50
    assert t["total_requests"] == 2


def test_health_endpoint_shape(client):
    r = client.get("/admin/api/meridian/health")
    assert r.status_code == 200
    body = r.json()
    assert body["backends"] == []  # test config has no anthropic backend
    assert body["checked"]
