"""Crawl4AI visibility + tool-preference. Research's page-fetch (Crawl4AI) was
silently swallowed, so a misconfigured/blocked fetch server looked like
"only SearXNG is ever used" with no signal. And live tool-callers leaned on
search snippets and never opened a page. Now fetches are logged either way, and
both tool-use prompts steer toward reading full pages via a crawler."""

from foundry_router.brain.prompts import build_system_prompt, build_worker_tool_prompt
from foundry_router.config import ResearchConfig, ResearchToolRef
from foundry_router.db import Database
from foundry_router.registry.models_db import ModelRegistry
from foundry_router.registry.research_agent import ResearchAgent


class _MCP:
    def __init__(self, fetch_ok):
        self.fetch_ok = fetch_ok

    async def call_tool(self, server, tool, args):
        if server == "searxng":
            return "found it at http://example.com/page and more"
        if self.fetch_ok:
            return "# Page\n\nfull markdown content"
        raise RuntimeError("Website Error (403): Access blocked (bot detection)")


def _agent(tmp_path, fetch_ok):
    db = Database(tmp_path / "r.sqlite")

    async def _llm(p):
        return "{}"   # valid JSON but empty — fetch logging already happened by then

    cfg = ResearchConfig(
        search=ResearchToolRef(server="searxng", tool="web_search"),
        fetch=ResearchToolRef(server="crawl4ai", tool="md", url_param="url"))
    return ResearchAgent(cfg, db, ModelRegistry(db), _MCP(fetch_ok),
                         llm=_llm, available_models=lambda: []), db


async def test_research_logs_fetch_failure(tmp_path):
    agent, db = _agent(tmp_path, fetch_ok=False)
    await agent.research_model("m")
    warns = db.query("SELECT * FROM event_log WHERE source='research' "
                     "AND message LIKE '%page fetch%'")
    assert warns and "crawl4ai/md" in warns[0]["message"]      # crawl4ai failure surfaced
    assert "403" in (warns[0]["detail"] or "")                 # real cause, not silent


async def test_research_logs_fetch_success(tmp_path):
    agent, db = _agent(tmp_path, fetch_ok=True)
    await agent.research_model("m")
    infos = db.query("SELECT * FROM event_log WHERE source='research' "
                     "AND level='info' AND message LIKE '%fetched%page%'")
    assert infos and "crawl4ai/md" in infos[0]["message"]      # crawl4ai USE is visible


class _MCPNoUrls:
    """SearXNG returns snippets with no URLs; records whether fetch was called."""
    def __init__(self):
        self.fetch_called = False

    async def call_tool(self, server, tool, args):
        if server == "searxng":
            return "Great model. No links here, just prose about benchmarks."
        self.fetch_called = True                       # should never happen
        return "x"


async def test_research_logs_when_no_urls_and_skips_crawl4ai(tmp_path):
    db = Database(tmp_path / "r.sqlite")

    async def _llm(p):
        return "{}"

    cfg = ResearchConfig(
        search=ResearchToolRef(server="searxng", tool="web_search"),
        fetch=ResearchToolRef(server="crawl4ai", tool="md", url_param="url"))
    mcp = _MCPNoUrls()
    agent = ResearchAgent(cfg, db, ModelRegistry(db), mcp,
                          llm=_llm, available_models=lambda: [])
    await agent.research_model("obscure:model")
    assert mcp.fetch_called is False                   # crawl4ai genuinely not called
    infos = db.query("SELECT * FROM event_log WHERE source='research' "
                     "AND message LIKE '%no fetchable URLs%'")
    assert infos and "crawl4ai/md" in infos[0]["message"]   # and it's explained, not silent


# -- prompt guidance --------------------------------------------------------------

def test_worker_prompt_steers_to_crawler():
    p = build_worker_tool_prompt().lower()
    assert "crawler" in p and "crawl4ai" in p
    assert "search" in p and "full" in p                       # search then read full page


def test_dispatcher_prompt_has_web_research_rule():
    p = build_system_prompt(None, [], {}, "n/a", None).lower()
    assert "web research" in p and "crawler" in p and "crawl4ai" in p
