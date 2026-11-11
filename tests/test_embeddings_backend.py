"""v0.181.122: an embeddings-only backend (the CPU embeddings service). Its health
probe is an embedding, not a chat, and an embedding model asked for without its tag
resolves to the served one."""

import asyncio
from types import SimpleNamespace

from foundry_router.config import HealthConfig
from foundry_router.facade.ollama_api import _resolve_embed_model
from foundry_router.health import HealthMonitor


class _Proto:
    def __init__(self, fail=False, empty=False):
        self.fail, self.empty, self.calls = fail, empty, []

    async def embed(self, model, inputs, **kw):
        self.calls.append((model, inputs))
        if self.fail:
            raise RuntimeError("connection refused")
        return {"embeddings": [] if self.empty else [[0.1, 0.2, 0.3]], "prompt_eval_count": 1}


def _svc(models, proto):
    state = SimpleNamespace(models=models, protocol=proto)
    cfg = SimpleNamespace(health=HealthConfig())
    return SimpleNamespace(
        pool=SimpleNamespace(backends={"embeddings": state}),
        registry=SimpleNamespace(get=lambda m: {"embedding": 1} if "embed" in m else {}),
        config_store=SimpleNamespace(config=cfg))


def test_an_embeddings_only_backend_is_probed_with_an_embedding():
    proto = _Proto()
    h = HealthMonitor(_svc(["nomic-embed-text:latest"], proto))
    res = asyncio.run(h.probe("embeddings"))
    assert res["ok"] is True and res["model"] == "nomic-embed-text:latest"
    assert res["reply"] == "embedding, 3 dimensions" and res["detail"] == ""
    assert proto.calls == [("nomic-embed-text:latest", ["ping"])]
    assert h.probes["embeddings"]["ok"] is True
    # a failing or empty answer is still reported as a failure, with the reason
    bad = asyncio.run(HealthMonitor(_svc(["nomic-embed-text:latest"], _Proto(fail=True))).probe("embeddings"))
    assert bad["ok"] is False and "connection refused" in bad["detail"]
    empty = asyncio.run(HealthMonitor(_svc(["nomic-embed-text:latest"], _Proto(empty=True))).probe("embeddings"))
    assert empty["ok"] is False and empty["detail"] == "empty embedding"
    # a backend with nothing at all keeps the old message
    none = asyncio.run(HealthMonitor(_svc([], _Proto())).probe("embeddings"))
    assert none["ok"] is False and "no chat model" in none["detail"]


def test_an_untagged_embedding_model_resolves_to_the_served_one():
    served = {"nomic-embed-text:latest": ["embeddings"], "mxbai-embed-large:335m": ["embeddings"],
              "bge:small": ["embeddings"], "bge:large": ["embeddings"]}
    pool = SimpleNamespace(available_models=lambda: served,
                           backend_info=lambda m: {"name": "embeddings"} if m in served else None)
    svc = SimpleNamespace(pool=pool)
    assert _resolve_embed_model(svc, "nomic-embed-text") == "nomic-embed-text:latest"
    assert _resolve_embed_model(svc, "nomic-embed-text:latest") == "nomic-embed-text:latest"
    assert _resolve_embed_model(svc, "mxbai-embed-large") == "mxbai-embed-large:335m"   # its only tag
    assert _resolve_embed_model(svc, "bge") == "bge"                    # two tags: not guessed
    assert _resolve_embed_model(svc, "nomic-embed-text:v1.5") == "nomic-embed-text:v1.5"   # a tag is exact
    assert _resolve_embed_model(svc, "unknown") == "unknown" and _resolve_embed_model(svc, "") == ""
