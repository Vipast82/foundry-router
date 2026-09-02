"""Paid-pin priority cascade for direct dispatch: a prefer_paid persona (Cline
PLAN) lists paid models in `pinned_models` and the direct path tries them in pin
order — Opus 4.8 -> Sonnet 5 -> ... — each guardrail-gated, before degrading to
a local model on exhaustion. These cover the two policy helpers that decide which
paid pins to attempt (and in what order) and the local landing spot."""

import types

from foundry_router.facade.ollama_api import _paid_pin_order, _local_fallback


class _Pool:
    def __init__(self, types_map, avail):
        self._types = types_map          # model id -> backend type
        self._avail = avail              # list of reachable model ids

    def backend_info(self, m):
        return {"type": self._types.get(m, "ollama")}

    def available_models(self):
        return {m: ["b"] for m in self._avail}


class _Reg:
    def __init__(self, meta=None, ranking=None):
        self._meta = meta or {}
        self._ranking = ranking          # category -> ordered list of ids

    def get(self, m):
        return self._meta.get(m)

    def ranked_for_category(self, category, models, limit=1):
        if self._ranking is not None:
            order = [m for m in self._ranking.get(category, []) if m in models]
            return [{"id": order[0]}] if order else []
        return [{"id": models[0]}] if models else []


def _svc(types_map, avail, meta=None, ranking=None):
    return types.SimpleNamespace(pool=_Pool(types_map, avail),
                                 registry=_Reg(meta, ranking))


TYPES = {"qwen": "ollama",
         "claude-opus-4-8": "anthropic-compatible",
         "claude-sonnet-5": "anthropic-compatible"}

PLAN = {"local_bias_strength": "prefer_paid",
        "benchmark_category": "coding",
        "model_allowlist": '["claude-opus-4-8", "claude-sonnet-5", "qwen"]',
        "pinned_models": '["claude-opus-4-8", "claude-sonnet-5"]'}


# -- _paid_pin_order: which paid pins to try, in pin order -------------------------

def test_paid_pins_returned_in_pin_order():
    svc = _svc(TYPES, ["qwen", "claude-opus-4-8", "claude-sonnet-5"])
    assert _paid_pin_order(svc, PLAN) == ["claude-opus-4-8", "claude-sonnet-5"]


def test_reverse_pin_order_is_honored():
    svc = _svc(TYPES, ["qwen", "claude-opus-4-8", "claude-sonnet-5"])
    persona = {**PLAN, "pinned_models": '["claude-sonnet-5", "claude-opus-4-8"]'}
    assert _paid_pin_order(svc, persona) == ["claude-sonnet-5", "claude-opus-4-8"]


def test_only_prefer_paid_personas_cascade():
    # A local-first persona (Cline ACT) never starts in the paid tier: no cascade.
    svc = _svc(TYPES, ["qwen", "claude-opus-4-8", "claude-sonnet-5"])
    persona = {**PLAN, "local_bias_strength": "prefer_local"}
    assert _paid_pin_order(svc, persona) == []


def test_local_pins_excluded_from_paid_cascade():
    # A pinned LOCAL model belongs to the local fallback, not the paid cascade.
    svc = _svc(TYPES, ["qwen", "claude-opus-4-8"])
    persona = {**PLAN, "pinned_models": '["claude-opus-4-8", "qwen"]'}
    assert _paid_pin_order(svc, persona) == ["claude-opus-4-8"]


def test_unreachable_pins_skipped():
    # Sonnet pinned but not reachable -> silently skipped, Opus still leads.
    svc = _svc(TYPES, ["qwen", "claude-opus-4-8"])
    assert _paid_pin_order(svc, PLAN) == ["claude-opus-4-8"]


def test_pins_outside_allowlist_dropped():
    svc = _svc(TYPES, ["qwen", "claude-opus-4-8", "claude-sonnet-5"])
    persona = {**PLAN, "model_allowlist": '["claude-opus-4-8", "qwen"]'}
    assert _paid_pin_order(svc, persona) == ["claude-opus-4-8"]


def test_embedding_pins_dropped():
    svc = _svc(TYPES, ["qwen", "claude-opus-4-8", "claude-sonnet-5"],
               meta={"claude-opus-4-8": {"embedding": True}})
    assert _paid_pin_order(svc, PLAN) == ["claude-sonnet-5"]


def test_no_pins_is_empty():
    svc = _svc(TYPES, ["qwen", "claude-opus-4-8"])
    persona = {**PLAN, "pinned_models": "[]"}
    assert _paid_pin_order(svc, persona) == []


def test_bad_json_pins_is_empty():
    svc = _svc(TYPES, ["qwen", "claude-opus-4-8"])
    persona = {**PLAN, "pinned_models": "{not json"}
    assert _paid_pin_order(svc, persona) == []


def test_base_name_tolerant_allowlist():
    # allowlist lists the base name; a :tagged reachable pin still passes.
    tmap = {"qwen": "ollama", "claude-opus-4-8:latest": "anthropic-compatible"}
    svc = _svc(tmap, ["qwen", "claude-opus-4-8:latest"])
    persona = {**PLAN, "model_allowlist": '["claude-opus-4-8", "qwen"]',
               "pinned_models": '["claude-opus-4-8:latest"]'}
    assert _paid_pin_order(svc, persona) == ["claude-opus-4-8:latest"]


# -- _local_fallback: the exhaustion / denial landing spot -------------------------

def test_local_fallback_prefers_allowlisted_local():
    svc = _svc(TYPES, ["qwen", "claude-opus-4-8"],
               ranking={"coding": ["qwen"]})
    assert _local_fallback(svc, PLAN) == "qwen"


def test_local_fallback_degrades_to_any_local_when_none_allowlisted():
    tmap = {"other-local": "ollama", "claude-opus-4-8": "anthropic-compatible"}
    svc = _svc(tmap, ["other-local", "claude-opus-4-8"],
               ranking={"coding": ["other-local"]})
    # allowlist names no reachable local -> degrade to any local
    persona = {**PLAN, "model_allowlist": '["claude-opus-4-8"]'}
    assert _local_fallback(svc, persona) == "other-local"


def test_local_fallback_none_when_no_local_reachable():
    tmap = {"claude-opus-4-8": "anthropic-compatible"}
    svc = _svc(tmap, ["claude-opus-4-8"])
    assert _local_fallback(svc, PLAN) is None
