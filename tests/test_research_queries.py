"""Research query construction: the raw model id (with '/' and ':') was sent
verbatim into SearXNG, and its Wikipedia engine rejected the '/'-bearing string
as a malformed page-title path (400 Bad Request). Queries now use a sanitized
name, and an optional search_prefix lets an operator pin reliable SearXNG engines
via bang syntax (excluding Brave/DDG, which wall automated scraping)."""

from foundry_router.config import ResearchConfig, ResearchToolRef
from foundry_router.db import Database
from foundry_router.registry.models_db import ModelRegistry
from foundry_router.registry.research_agent import ResearchAgent, _query_name


def test_query_name_strips_slash_and_tag():
    # tokenized into clean words (no '/', no ':tag', no '-'/'_' joiners)
    assert _query_name("satgeze/qwen36-35b-uncensored-1m:latest") \
        == "satgeze qwen36 35b uncensored 1m"
    assert _query_name("ornith:35b") == "ornith"
    assert _query_name("plainname") == "plainname"


def test_query_name_normalizes_gguf_quant_repacks():
    # hosting/repackager orgs + GGUF + quant tag stripped -> the BASE model name,
    # so a GGUF repack finds the real benchmark pages (the Qwen3.8 problem)
    assert _query_name("hf.co/unsloth/Qwen3.8-27B-GGUF:UD-Q5_K_XL") == "Qwen3.8 27B"
    assert _query_name("hf.co/unsloth/Qwen3.6-35B-A3B-GGUF:UD-Q4_K_XL") == "Qwen3.6 35B A3B"
    assert _query_name("bartowski/Meta-Llama-3.1-8B-Instruct-GGUF") == "Meta Llama 3.1 8B Instruct"
    # size tokens (27B) are kept — useful in a query; only format/quant dropped
    assert "GGUF" not in _query_name("TheBloke/Foo-13B-GGUF")
    assert "13B" in _query_name("TheBloke/Foo-13B-GGUF")


class _RecordingMCP:
    def __init__(self):
        self.queries: list[str] = []

    async def call_tool(self, server, tool, args):
        if server == "searxng":
            self.queries.append(args["q"])
            return "no links"
        return "x"


def _agent(tmp_path, **research_kw):
    db = Database(tmp_path / "r.sqlite")

    async def _llm(p):
        return "{}"

    cfg = ResearchConfig(
        search=ResearchToolRef(server="searxng", tool="web_search", query_param="q"),
        fetch=ResearchToolRef(server="crawl4ai", tool="md", url_param="url"),
        **research_kw)
    mcp = _RecordingMCP()
    agent = ResearchAgent(cfg, db, ModelRegistry(db), mcp,
                          llm=_llm, available_models=lambda: [])
    return agent, mcp


async def test_no_query_contains_slash_or_tag(tmp_path):
    agent, mcp = _agent(tmp_path)
    await agent.research_model("satgeze/qwen36-35b-uncensored-1m:latest")
    assert mcp.queries, "searches should have been sent"
    for q in mcp.queries:
        assert "/" not in q and ":" not in q     # Wikipedia 400 cause is gone


async def test_search_prefix_pins_engines(tmp_path):
    agent, mcp = _agent(tmp_path, search_prefix="!google !bing")
    await agent.research_model("ornith:35b")
    assert mcp.queries and all(q.startswith("!google !bing ") for q in mcp.queries)
    assert all("ornith" in q for q in mcp.queries)


async def test_extra_queries_are_appended_with_name_substituted(tmp_path):
    # operator-added templates run on top of the 7 built-ins, {name} filled in
    agent, mcp = _agent(tmp_path, extra_queries=[
        "{name} specs hardware requirements", "  ", "{name} site:huggingface.co"])
    await agent.research_model("qwen3.8:27b")
    assert len(mcp.queries) == 7 + 2                      # blanks dropped
    assert any(q == "qwen3.8 specs hardware requirements" for q in mcp.queries)
    assert any("site:huggingface.co" in q for q in mcp.queries)


async def test_extra_query_with_stray_braces_does_not_crash(tmp_path):
    # `.replace` not `.format`, so a template with other {tokens} is harmless
    agent, mcp = _agent(tmp_path, extra_queries=["{name} {not_a_field} eval"])
    await agent.research_model("ornith:35b")
    assert any("{not_a_field}" in q for q in mcp.queries)   # left verbatim, no KeyError


# -- Part 1: pacing fields round-trip through the MCP upsert endpoint --------------

def test_mcp_pacing_fields_persist_via_endpoint(client):
    client.post("/admin/api/mcp_servers", json={
        "name": "searxng", "url": "http://x/mcp",
        "pace_seconds": 4, "rate_limit_retries": 5, "rate_limit_backoff_seconds": 45})
    s = {x["name"]: x for x in client.get("/admin/api/mcp_servers").json()["servers"]}["searxng"]
    assert s["pace_seconds"] == 4.0
    assert s["rate_limit_retries"] == 5
    assert s["rate_limit_backoff_seconds"] == 45.0
