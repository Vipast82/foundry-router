"""Meridian oauth-staleness monitoring + auth-health probe (session spec
items 1 & 2). Found live TWICE: sources.oauth silently goes null, the
five_hour bucket vanishes, seven_day serves a stale cached read — usage
figures drift (30% shown vs 43% real) with no error anywhere. The fix is
manual (meridian profile login --headless) but detection must be automatic:
edge-triggered Events alert, UI banner via oauth_ok, on-demand health check.
"""

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
    def __init__(self, data):
        self.data = data

    async def get(self, url, headers=None, timeout=None):
        return FakeResponse(self.data)


class DeadHTTP:
    async def get(self, url, headers=None, timeout=None):
        raise RuntimeError("connection refused")


# Shapes confirmed live: healthy has oauth populated + both buckets +
# extraUsage.usedCredits in cents (4343 == the Claude app's "$43.43 spent");
# stale has oauth null and ONLY seven_day (served from a cached read).
HEALTHY = {"buckets": [{"type": "seven_day", "utilization": 0.47, "resetsAt": None},
                       {"type": "five_hour", "utilization": 0.10, "resetsAt": None}],
           "sources": {"oauth": {"account": "victor"}, "sdk": {"entryCount": 3}},
           "extraUsage": {"usedCredits": 4343}}

STALE = {"buckets": [{"type": "seven_day", "utilization": 0.30, "resetsAt": None}],
         "sources": {"oauth": None, "sdk": {"entryCount": 0}}}


# -- parsing ---------------------------------------------------------------------

def test_parse_sources_three_states():
    assert parse_sources(HEALTHY) is True
    assert parse_sources(STALE) is False
    assert parse_sources({"buckets": []}) is None     # older builds: no sources key
    assert parse_sources(None) is None


def test_parse_extra_usage_cents_to_dollars():
    assert parse_extra_usage(HEALTHY) == pytest.approx(43.43)
    assert parse_extra_usage({"extraUsage": {}}) is None
    assert parse_extra_usage({}) is None


# -- snapshot carries the new signals ----------------------------------------------

async def test_snapshot_carries_oauth_and_credits(tmp_path):
    db = Database(tmp_path / "o.sqlite")
    usage = MeridianUsage(MeridianConfig(), FakeHTTP(HEALTHY), db)
    snap = await usage.snapshot("http://m")
    assert snap["oauth_ok"] is True
    assert snap["credits_used_usd"] == pytest.approx(43.43)
    assert "stale" not in snap["note"]


async def test_stale_oauth_flagged_in_note(tmp_path):
    usage = MeridianUsage(MeridianConfig(), FakeHTTP(STALE),
                          Database(tmp_path / "o1.sqlite"))
    snap = await usage.snapshot("http://m")
    assert snap["oauth_ok"] is False
    assert "stale" in snap["note"]  # the brain's prompt sees the caveat too


# -- edge-triggered alerting ---------------------------------------------------------

async def test_oauth_null_alert_fires_once_and_clears_on_recovery(tmp_path):
    db = Database(tmp_path / "o2.sqlite")
    http = FakeHTTP(STALE)
    usage = MeridianUsage(MeridianConfig(), http, db)

    snap = await usage.snapshot("http://m")
    assert snap["oauth_alert_since"]
    usage.clear_cache()
    await usage.snapshot("http://m")  # second poll while still stale
    errors = db.query("SELECT * FROM event_log WHERE level='error' AND source='usage'")
    assert len(errors) == 1, "alert must be edge-triggered, not one per poll"
    assert "meridian profile login" in errors[0]["message"]  # names the fix

    http.data = HEALTHY  # re-login happened on the host
    usage.clear_cache()
    snap = await usage.snapshot("http://m")
    assert snap["oauth_ok"] is True
    assert "oauth_alert_since" not in snap
    infos = db.query("SELECT * FROM event_log WHERE level='info' AND source='usage'")
    assert any("restored" in r["message"] for r in infos)


async def test_unreachable_endpoint_neither_raises_nor_clears_alert(tmp_path):
    db = Database(tmp_path / "o3.sqlite")
    http = FakeHTTP(STALE)
    usage = MeridianUsage(MeridianConfig(), http, db)
    await usage.snapshot("http://m")                       # alert raised
    usage.client = DeadHTTP()
    usage.clear_cache()
    snap = await usage.snapshot("http://m")                # no signal at all
    assert snap["oauth_ok"] is None
    assert snap["oauth_alert_since"], "absence of signal is not evidence of recovery"


# -- on-demand auth health (item 2) ---------------------------------------------------

async def test_auth_health_valid_then_stale(tmp_path):
    db = Database(tmp_path / "o4.sqlite")
    http = FakeHTTP(HEALTHY)
    usage = MeridianUsage(MeridianConfig(), http, db)
    h = await usage.auth_health("http://m")
    assert h["valid"] is True and h["oauth_ok"] is True and h["last_checked"]

    http.data = STALE
    h = await usage.auth_health("http://m")  # bypasses the 30s cache
    assert h["valid"] is False and h["oauth_ok"] is False


async def test_auth_health_reports_fetch_error(tmp_path):
    usage = MeridianUsage(MeridianConfig(), DeadHTTP(),
                          Database(tmp_path / "o5.sqlite"))
    h = await usage.auth_health("http://m")
    assert h["valid"] is False
    assert "connection refused" in (h["error"] or "")


def test_health_endpoint_shape(client):
    r = client.get("/admin/api/meridian/health")
    assert r.status_code == 200
    body = r.json()
    assert body["backends"] == []  # test config has no anthropic backend
    assert body["checked"]
