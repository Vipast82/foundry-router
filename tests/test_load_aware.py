"""Load-aware escalation: when a persona opts in and its LOCAL worker is busy,
the direct path swaps to a PAID model — which the usage guardrail then gates on
quota/cost (tested separately). These cover the swap decision itself."""

import types

from foundry_router.facade.ollama_api import _escalate_if_local_busy


class _Pool:
    def __init__(self, types_map, avail, busy):
        self._types = types_map          # model id -> backend type
        self._avail = avail              # list of reachable model ids
        self._busy = busy                # active_calls() payload

    def backend_info(self, m):
        return {"type": self._types.get(m, "ollama")}

    def available_models(self):
        return {m: ["b"] for m in self._avail}

    def active_calls(self):
        return self._busy


class _Reg:
    def get(self, m):
        return {}

    def ranked_for_category(self, category, models, limit=1):
        return [{"id": models[0]}] if models else []


def _svc(types_map, avail, busy):
    return types.SimpleNamespace(
        pool=_Pool(types_map, avail, busy),
        registry=_Reg(),
        db=types.SimpleNamespace(log_event=lambda *a, **k: None))


TYPES = {"qwen": "ollama", "claude-opus-5": "anthropic-compatible"}
ON = {"escalate_when_local_busy": 1, "benchmark_category": "coding"}


def test_no_escalation_when_flag_off():
    svc = _svc(TYPES, ["qwen", "claude-opus-5"],
               [{"model": "qwen", "count": 1}])
    assert _escalate_if_local_busy(svc, {"benchmark_category": "coding"},
                                   "qwen", "hi") == "qwen"


def test_no_escalation_when_local_idle():
    svc = _svc(TYPES, ["qwen", "claude-opus-5"], [])          # nothing in flight
    assert _escalate_if_local_busy(svc, ON, "qwen", "hi") == "qwen"


def test_escalates_to_paid_when_local_busy():
    svc = _svc(TYPES, ["qwen", "claude-opus-5"],
               [{"model": "qwen", "count": 1}])
    assert _escalate_if_local_busy(svc, ON, "qwen", "hi") == "claude-opus-5"


def test_no_escalation_when_no_paid_available():
    svc = _svc({"qwen": "ollama"}, ["qwen"],                  # only local reachable
               [{"model": "qwen", "count": 1}])
    assert _escalate_if_local_busy(svc, ON, "qwen", "hi") == "qwen"


def test_already_paid_is_untouched():
    svc = _svc(TYPES, ["qwen", "claude-opus-5"],
               [{"model": "claude-opus-5", "count": 1}])
    assert _escalate_if_local_busy(svc, ON, "claude-opus-5", "hi") == "claude-opus-5"


def test_allowlist_scopes_the_paid_target():
    # busy local, two paid reachable, but allowlist permits only one
    types_map = {"qwen": "ollama", "claude-opus-5": "anthropic-compatible",
                 "claude-sonnet-5": "anthropic-compatible"}
    svc = _svc(types_map, ["qwen", "claude-opus-5", "claude-sonnet-5"],
               [{"model": "qwen", "count": 1}])
    persona = {**ON, "model_allowlist": '["claude-sonnet-5"]'}
    assert _escalate_if_local_busy(svc, persona, "qwen", "hi") == "claude-sonnet-5"
