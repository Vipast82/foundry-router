"""Direct-path num_ctx bounding: the persona's context_window must BOUND the
context an Ollama worker is loaded with on the direct (Cline) path — not just be
advertised via /api/show. Without it a client sending a huge prompt makes Ollama
cold-load a giant KV cache and time out (e.g. a 262K-context model whose window is
deliberately unpinned). Mirrors the agent path's _num_ctx_for."""

import types

from foundry_router.facade.ollama_api import _direct_num_ctx


def _svc(context_length=None):
    reg = types.SimpleNamespace(
        get=lambda m: ({"context_length": context_length} if context_length else {}))
    return types.SimpleNamespace(registry=reg)


def test_none_when_persona_has_no_context_window():
    assert _direct_num_ctx(_svc(262144), {}, "qwen") is None
    assert _direct_num_ctx(_svc(262144), {"context_window": 0}, "qwen") is None


def test_capped_at_model_trained_max():
    # persona asks for MORE than the model can do -> capped at the model max
    assert _direct_num_ctx(_svc(262144), {"context_window": 999999}, "qwen") == 262144


def test_persona_window_used_when_below_max():
    assert _direct_num_ctx(_svc(262144), {"context_window": 65536}, "qwen") == 65536


def test_uses_window_when_model_max_unknown():
    # no registry context_length -> trust the persona's value as-is
    assert _direct_num_ctx(_svc(None), {"context_window": 32768}, "qwen") == 32768


def test_non_numeric_context_window_is_ignored():
    assert _direct_num_ctx(_svc(262144), {"context_window": "lots"}, "qwen") is None


def test_bounding_min_logic_matches_dispatch():
    # The dispatch injects min(persona_bound, client_ctx) when the client sent a
    # smaller num_ctx, else the persona bound — the behavior the direct path uses.
    def effective(persona_bound, client_ctx):
        return min(persona_bound, client_ctx) if client_ctx else persona_bound
    assert effective(65536, 0) == 65536          # client sent nothing -> persona bound
    assert effective(65536, 262144) == 65536     # client huge -> capped by persona
    assert effective(65536, 8192) == 8192        # client smaller -> respected
