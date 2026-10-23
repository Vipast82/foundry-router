"""The Client tab's backing module (client_guide.py, v0.139.0): the install
guide extracted and rendered from docs/CLIENT_MANAGER.md (one source of truth),
the GitHub release-availability states (available / no_assets / untagged /
unreachable) with caching, the version-match badge, and the dashboard's
/admin/api/client/* endpoints."""

import asyncio

import pytest

from foundry_router import client_guide as CG


def run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


# --------------------------------------------------------------------------- #
# Guide: docs/CLIENT_MANAGER.md is the one source of truth                     #
# --------------------------------------------------------------------------- #

def test_guide_extracts_the_install_sections_from_the_real_doc_in_order():
    g = CG.load_guide()
    assert not g["missing"] and g["source"] == "docs/CLIENT_MANAGER.md"
    ids = [s["id"] for s in g["sections"]]
    assert ids == ["install", "first-run", "router-key-for-the-relay", "tray",
                   "commands", "troubleshooting", "migrating-this-workstation"]
    by_id = {s["id"]: s["text"] for s in g["sections"]}
    assert "FoundryClientSetup" in by_id["install"]          # Windows installer named
    assert "FOUNDRY_CLIENT_KEY" in by_id["router-key-for-the-relay"]
    assert "client-logs" in by_id["troubleshooting"]         # where logs live
    # the hash is a stable content fingerprint
    assert g["hash"] == CG.load_guide()["hash"] and len(g["hash"]) == 12


def test_guide_handles_a_missing_doc_without_failing(monkeypatch, tmp_path):
    monkeypatch.setattr(CG, "DOC", tmp_path / "nope.md")
    g = CG.load_guide()
    assert g["missing"] and g["sections"] == []
    h = CG.guide_html()
    assert h["missing"] and "cannot be shown" in h["html"]


def test_markdown_renderer_escapes_and_covers_the_doc_subset():
    html = CG._md_html(
        "A **bold** line with `code` and <script>alert(1)</script>.\n\n"
        "- item one\n- item **two**\n\n"
        "1. first\n2. second\n\n"
        "| a | b |\n|---|---|\n| 1 | <x> |\n\n"
        "```\nplain <code> block\n```\n\n"
        "See [the docs](https://example.com/d).")
    assert "<script>" not in html and "&lt;script&gt;" in html
    assert "<b>bold</b>" in html and "<code>code</code>" in html
    assert "<ul><li>item one</li>" in html and "<ol><li>first</li>" in html
    assert "<th>a</th>" in html and "<td>&lt;x&gt;</td>" in html
    assert "<pre><code>plain &lt;code&gt; block</code></pre>" in html
    assert '<a href="https://example.com/d" target="_blank" rel="noopener">the docs</a>' in html


def test_guide_html_has_a_toc_whose_anchors_match_the_sections():
    h = CG.guide_html()
    assert not h["missing"] and h["hash"]
    assert '<a href="#cg-install">' in h["html"] and '<h3 id="cg-install">' in h["html"]
    assert '<h3 id="cg-troubleshooting">' in h["html"]


# --------------------------------------------------------------------------- #
# Release availability: honest states, cached                                  #
# --------------------------------------------------------------------------- #

REPO = "owner/repo"
TAGS = f"https://api.github.com/repos/{REPO}/releases/tags/v1.2.3"
LATEST = f"https://api.github.com/repos/{REPO}/releases/latest"
RELEASE = {"tag_name": "v1.2.3", "html_url": f"https://github.com/{REPO}/releases/tag/v1.2.3",
           "assets": [
               {"name": "FoundryClient-1.2.3-x86_64.AppImage", "size": 200,
                "browser_download_url": "https://github.com/dl/linux"},
               {"name": "FoundryClientSetup-1.2.3.exe", "size": 100,
                "browser_download_url": "https://github.com/dl/win"},
               {"name": "notes.txt", "size": 1, "browser_download_url": "x"}]}


class FakeFetch:
    def __init__(self, routes):
        self.routes, self.calls = routes, []

    async def __call__(self, url):
        self.calls.append(url)
        r = self.routes[url]
        if isinstance(r, Exception):
            raise r
        return r


def source(routes, clock=lambda: 1000.0):
    return CG.ReleaseSource(REPO, FakeFetch(routes), clock)


def test_matching_release_with_installers_is_available_windows_first():
    s = source({TAGS: (200, RELEASE)})
    r = run(s.release_info("1.2.3"))
    assert r["state"] == "available" and r["tag"] == "v1.2.3" and not r["cached"]
    assert [a["os"] for a in r["assets"]] == ["windows", "linux"]      # txt dropped
    assert r["assets"][0]["url"] == "https://github.com/dl/win" and r["assets"][0]["size"] == 100
    assert s.fetch.calls == [TAGS]                                     # no latest lookup needed


def test_release_without_assets_says_so_and_offers_the_latest():
    s = source({TAGS: (200, {"tag_name": "v1.2.3", "assets": []}),
                LATEST: (200, {**RELEASE, "tag_name": "v1.2.0"})})
    r = run(s.release_info("1.2.3"))
    assert r["state"] == "no_assets" and "no installer is attached" in r["note"]
    assert r["latest"]["tag"] == "v1.2.0" and len(r["latest"]["assets"]) == 2


def test_untagged_dev_build_is_flagged_with_the_latest_as_fallback():
    s = source({TAGS: (404, {}), LATEST: (200, RELEASE)})
    r = run(s.release_info("1.2.3"))
    assert r["state"] == "untagged" and "dev build" in r["note"]
    assert r["latest"]["tag"] == "v1.2.3"


def test_unreachable_github_degrades_to_unverified_guessed_links():
    s = source({TAGS: OSError("boom")})
    r = run(s.release_info("1.2.3"))
    assert r["state"] == "unreachable" and "unverified" in r["note"]
    assert all(a["unverified"] for a in r["assets"])
    assert r["assets"][0]["url"].endswith("/v1.2.3/FoundryClientSetup-1.2.3.exe")


def test_answers_are_cached_an_hour_errors_shorter_and_refresh_bypasses():
    t = {"now": 1000.0}
    s = source({TAGS: (200, RELEASE)}, clock=lambda: t["now"])
    assert not run(s.release_info("1.2.3"))["cached"]
    assert run(s.release_info("1.2.3"))["cached"] and len(s.fetch.calls) == 1
    t["now"] += CG.CACHE_TTL_S + 1
    assert not run(s.release_info("1.2.3"))["cached"] and len(s.fetch.calls) == 2
    assert not run(s.release_info("1.2.3", refresh=True))["cached"] and len(s.fetch.calls) == 3
    # an unreachable answer is retried sooner
    s2 = source({TAGS: OSError("x")}, clock=lambda: t["now"])
    run(s2.release_info("1.2.3"))
    t["now"] += CG.ERROR_TTL_S + 1
    assert not run(s2.release_info("1.2.3"))["cached"] and len(s2.fetch.calls) == 2


@pytest.mark.parametrize("client_v,router_v,want", [
    ("0.139.0", "0.139.0", "match"), ("v0.139.0", "0.139.0", "match"),
    ("0.134.0", "0.139.0", "older"), ("0.140.0", "0.139.0", "newer"),
    (None, "0.139.0", "unknown"), ("", "0.139.0", "unknown")])
def test_version_match_badge(client_v, router_v, want):
    assert CG.version_match(client_v, router_v) == want


# --------------------------------------------------------------------------- #
# The dashboard endpoints                                                      #
# --------------------------------------------------------------------------- #

def test_guide_endpoint_serves_the_rendered_doc(client):
    r = client.get("/admin/api/client/guide")
    assert r.status_code == 200
    d = r.json()
    assert d["source"] == "docs/CLIENT_MANAGER.md" and not d["missing"] and d["hash"]
    assert '<h3 id="cg-install">' in d["html"] and "<script>" not in d["html"]


def test_install_endpoint_reports_version_release_and_clients(client, app, monkeypatch):
    from foundry_router import __version__
    tags = f"https://api.github.com/repos/{REPO}/releases/tags/v{__version__}"
    monkeypatch.setattr(CG, "_SOURCE", source({tags: (200, {
        "tag_name": f"v{__version__}", "assets": RELEASE["assets"]})}))
    d = client.get("/admin/api/client/install").json()
    assert d["router_version"] == __version__ and d["repo"] == REPO
    assert d["release"]["state"] == "available" and d["clients"] == []
    # a heart-beating client shows up with its version compared to the router's
    app.state.services.control.heartbeat(
        {"source": {"id": "client:ws", "kind": "client", "version": "0.1.0"},
         "contract": "foundry.cc.v1", "features": ["stack_control"], "overall": "ok",
         "components": []}, {"client_id": "victor"})
    d = client.get("/admin/api/client/install").json()
    assert [c["source_id"] for c in d["clients"]] == ["client:ws"]
    c = d["clients"][0]
    assert c["version"] == "0.1.0" and c["match"] == "older" and c["fresh"] is True
    assert c["overall"] == "ok"
