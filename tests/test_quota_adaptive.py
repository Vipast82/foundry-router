"""Tests for real Claude quota tracking and adaptive tier selection:
quota parsing (nullable utilization, sec-vs-ms resetsAt), authenticated
fetch, usage-aware guardrail conservation, subscription token logging, and
persona model pinning."""

import json

from foundry_router.brain.agent import _apply_pins
from foundry_router.brain import prompts
from foundry_router.config import GuardrailsConfig, MeridianConfig
from foundry_router.db import Database
from foundry_router.guardrails import GuardrailEngine, RequestGuardState
from foundry_router.usage import (MeridianUsage, claude_premium_level,
                                  log_subscription_usage,
                                  observed_subscription_usage, parse_quota)


# -- parsing --------------------------------------------------------------------

def test_parse_quota_real_shape():
    buckets = parse_quota({"buckets": [
        {"type": "five_hour", "utilization": 0.42, "resetsAt": 1780000000},      # seconds
        {"type": "seven_day", "utilization": 65, "resetsAt": 1780000000000},     # ms, percent
        {"type": "five_hour", "utilization": None, "resetsAt": None},            # no signal yet
    ]})
    assert buckets[0]["used"] == 0.42
    assert buckets[0]["label"] == "5-hour"
    assert buckets[1]["used"] == 0.65                     # percent normalized
    assert buckets[0]["resets_at"] == buckets[1]["resets_at"]  # sec vs ms, same instant
    assert buckets[2]["used"] is None                     # null = no signal, not error


def test_parse_quota_rejects_unknown_shape():
    assert parse_quota({"whatever": 1}) is None
    assert parse_quota([1, 2]) is None


def test_claude_premium_level():
    assert claude_premium_level("claude-opus-4-8") == 3
    assert claude_premium_level("claude-fable-5") == 3
    assert claude_premium_level("claude-sonnet-4-6") == 2
    assert claude_premium_level("claude-haiku-4-5") == 1
    assert claude_premium_level("glm-4.7-flash:latest") == 0


# -- authenticated fetch ------------------------------------------------------------

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
        self.calls = []

    async def get(self, url, headers=None, timeout=None):
        self.calls.append({"url": url, "headers": headers or {}})
        return FakeResponse(self.data)


async def test_snapshot_hits_quota_endpoint_with_auth(tmp_path):
    http = FakeHTTP({"buckets": [{"type": "five_hour", "utilization": 0.3,
                                  "resetsAt": 1780000000}]})
    usage = MeridianUsage(MeridianConfig(), http, Database(tmp_path / "q.sqlite"))
    snap = await usage.snapshot("http://meridian:3456", api_key="sekret")
    call = http.calls[0]
    assert call["url"].endswith("/v1/usage/quota")          # not /telemetry
    assert call["headers"]["Authorization"] == "Bearer sekret"
    assert snap["available"] is True
    assert snap["worst_used"] == 0.3
    assert "5-hour window 30% used" in snap["note"]


async def test_window_exhausted_below_min_fraction(tmp_path):
    http = FakeHTTP({"buckets": [{"type": "five_hour", "utilization": 0.97,
                                  "resetsAt": None}]})
    usage = MeridianUsage(MeridianConfig(min_window_fraction=0.05), http,
                          Database(tmp_path / "q2.sqlite"))
    ok, note = await usage.window_available("http://m")
    assert ok is False


# -- adaptive guardrail conservation ---------------------------------------------------

class FixedUsage(MeridianUsage):
    def __init__(self, cfg, db, worst_used):
        super().__init__(cfg, client=None, db=db)
        self._worst = worst_used

    async def snapshot(self, base_url, api_key=None):
        remaining = 1.0 - (self._worst or 0)
        return {"available": self._worst is None
                             or remaining >= self.cfg.min_window_fraction,
                "buckets": [], "worst_used": self._worst,
                "note": f"{self._worst:.0%} used" if self._worst is not None else "n/a"}


def _engine(tmp_path, worst_used):
    db = Database(tmp_path / "g.sqlite")
    usage = FixedUsage(MeridianConfig(), db, worst_used)
    return GuardrailEngine(GuardrailsConfig(), db, usage), db


MERIDIAN_INFO = {"name": "meridian", "type": "anthropic-compatible",
                 "url": "http://m", "api_key": "k"}


async def _verdict(engine, model):
    state = RequestGuardState()
    return await engine.check_paid_call(model, MERIDIAN_INFO, None, state,
                                        engine.effective(None))


async def test_premium_conserved_at_70pct(tmp_path):
    engine, _ = _engine(tmp_path, 0.75)
    assert not (await _verdict(engine, "claude-opus-4-8")).allowed
    assert (await _verdict(engine, "claude-sonnet-4-6")).allowed
    assert (await _verdict(engine, "claude-haiku-4-5")).allowed


async def test_only_cheapest_tier_at_85pct(tmp_path):
    engine, _ = _engine(tmp_path, 0.9)
    assert not (await _verdict(engine, "claude-opus-4-8")).allowed
    assert not (await _verdict(engine, "claude-sonnet-4-6")).allowed
    assert (await _verdict(engine, "claude-haiku-4-5")).allowed


async def test_hard_stop_when_exhausted(tmp_path):
    engine, _ = _engine(tmp_path, 0.97)
    assert not (await _verdict(engine, "claude-haiku-4-5")).allowed


async def test_no_signal_means_no_conservation(tmp_path):
    engine, _ = _engine(tmp_path, None)
    assert (await _verdict(engine, "claude-opus-4-8")).allowed


# -- subscription token logging -----------------------------------------------------

def test_subscription_usage_logged_and_summed(tmp_path):
    db = Database(tmp_path / "s.sqlite")
    log_subscription_usage(db, "claude-sonnet-4-6", "meridian", 1200, 800)
    log_subscription_usage(db, "claude-haiku-4-5", "meridian", 300, 100)
    obs = observed_subscription_usage(db)
    assert obs["last_5h"]["calls"] == 2
    assert obs["last_5h"]["prompt_tokens"] == 1500
    assert obs["last_7d"]["completion_tokens"] == 900


# -- persona model pinning ----------------------------------------------------------

def _rows(*ids):
    return [{"id": i, "relative_cost_tier": "free", "score": None} for i in ids]


def test_apply_pins_boosts_in_order_and_skips_unreachable():
    persona = {"pinned_models": json.dumps(["model-b", "ghost-model", "model-c"])}
    ranked = _apply_pins(_rows("model-a", "model-b", "model-c"), persona)
    assert [r["id"] for r in ranked] == ["model-b", "model-c", "model-a"]
    assert ranked[0]["_pinned"] and ranked[1]["_pinned"]
    assert "_pinned" not in ranked[2]


def test_pins_render_as_top_group_with_procedure():
    persona = {"pinned_models": json.dumps(["model-b"])}
    ranked = _apply_pins(_rows("model-a", "model-b"), persona)
    system = prompts.build_system_prompt(
        persona, ranked, {"model-a": "ask_model_a", "model-b": "ask_model_b"},
        "5-hour window 30% used", None)
    assert "[PINNED FOR THIS PERSONA" in system
    assert system.index("PINNED") < system.index("[FREE / LOCAL")
    assert "a0." in system  # boosted-not-mandatory procedure step


def test_pinned_models_endpoint_roundtrip(client):
    client.post("/admin/api/personas", json={
        "virtual_name": "Foundry-Coding",
        "pinned_models": ["claude-sonnet-4-6", "glm-4.7-flash:latest"]})
    d = client.get("/admin/api/personas").json()
    coding = next(p for p in d["personas"] if p["virtual_name"] == "Foundry-Coding")
    assert json.loads(coding["pinned_models"]) == ["claude-sonnet-4-6",
                                                   "glm-4.7-flash:latest"]
