"""Research robustness: benchmark-source targeting. The fetch budget is small,
so candidate URLs are ranked (the model's OWN site and known benchmark hosts
first) and a HuggingFace card is seeded even if search missed it — so a model
whose benchmarks live on its own page (e.g. ornith.online) actually gets pulled."""

from foundry_router.config import ResearchConfig, ResearchToolRef
from foundry_router.db import Database
from foundry_router.registry.models_db import ModelRegistry
from foundry_router.registry.research_agent import (
    ResearchAgent, _model_tokens, _rank_urls, _seed_urls)


def test_model_tokens_drops_size_and_variant_noise():
    assert _model_tokens("ornith:35b") == ["ornith"]
    toks = _model_tokens("satgeze/qwen36-35b-uncensored-1m:latest")
    assert "satgeze" in toks and "qwen36" in toks
    assert "35b" not in toks and "1m" not in toks and "uncensored" not in toks
    assert "latest" not in toks


def test_seed_urls_builds_hf_card_for_namespaced_ids():
    assert _seed_urls("org/model:latest") == ["https://huggingface.co/org/model"]
    assert _seed_urls("ornith:35b") == []          # nothing to guess for a bare name


def test_rank_puts_own_site_and_benchmark_hosts_first():
    model = "ornith:35b"
    urls = [
        "https://randomblog.example.com/opinion",
        "https://huggingface.co/some/model",
        "https://ornith.online/ornith-1-0-model-35b",   # the model's OWN site
    ]
    ranked = _rank_urls(urls, model)
    assert ranked[0] == "https://ornith.online/ornith-1-0-model-35b"  # own site wins
    assert "huggingface.co" in ranked[1]                             # benchmark host next
    assert ranked[-1].startswith("https://randomblog")               # generic last


def test_rank_dedupes_on_fragment():
    ranked = _rank_urls(
        ["https://x.com/a#top", "https://x.com/a#bottom", "https://x.com/a"], "m:1")
    assert ranked == ["https://x.com/a"]


# -- integration: the model's own benchmark page is fetched first -----------------

class _OrderMCP:
    def __init__(self):
        self.fetched: list[str] = []

    async def call_tool(self, server, tool, args):
        if server == "searxng":
            return ("See https://randomblog.example.com/x and "
                    "https://ornith.online/ornith-1-0-model-35b for details.")
        self.fetched.append(args["url"])
        return "# Ornith 1.0\n\nMMLU 78.2, GPQA Diamond 51.0"


async def test_own_site_fetched_before_generic(tmp_path):
    db = Database(tmp_path / "r.sqlite")

    async def _llm(p):
        return "{}"

    cfg = ResearchConfig(
        search=ResearchToolRef(server="searxng", tool="web_search"),
        fetch=ResearchToolRef(server="crawl4ai", tool="md", url_param="url"))
    mcp = _OrderMCP()
    agent = ResearchAgent(cfg, db, ModelRegistry(db), mcp,
                          llm=_llm, available_models=lambda: [])
    await agent.research_model("ornith:35b")
    assert mcp.fetched, "crawl4ai should have been called"
    assert mcp.fetched[0] == "https://ornith.online/ornith-1-0-model-35b"
