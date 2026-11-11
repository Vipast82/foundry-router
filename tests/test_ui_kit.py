"""v0.181.120: the shared UI kit (sortable / resizable tables, dropdown and scrollbar
styles, the flow map) is served to every page, and the route graph no longer runs on
the event loop."""

from pathlib import Path

STATIC = Path(__file__).resolve().parents[1] / "foundry_router" / "ui" / "static"


def test_the_kit_is_served_and_every_page_loads_it(client):
    css = client.get("/ui/kit.css")
    js = client.get("/ui/kit.js")
    assert css.status_code == 200 and css.headers["content-type"].startswith("text/css")
    assert js.status_code == 200 and "javascript" in js.headers["content-type"]
    assert "FK.tables" in js.text and "FK.flow" in js.text and "FK.onResize" in js.text
    assert ".fk-flow" in css.text and "table.fk" in css.text
    for page in ("index.html", "control.html", "automation.html"):
        html = (STATIC / page).read_text(encoding="utf-8")
        assert "/ui/kit.css" in html and "/ui/kit.js" in html, page
        # the kit must load before the page's own script uses it
        assert html.index("/ui/kit.js") < html.index("\n<script>\n"), page
    # rows on the automation page are in a deliberate order: resize only, no sorting
    assert 'kit.js?v=120" data-no-sort="1"' in (STATIC / "automation.html").read_text(encoding="utf-8")
    assert "dataset.noSort" in js.text


def test_the_admin_page_redraws_on_resize_and_draws_routes_with_the_kit():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    assert "FK.onResize(" in html and "FK.busy()" in html
    assert "FK.flow({" in html and "FK.flowWire(" in html
    assert 'width="1180"' not in html and "const W = 1180" not in html      # no fixed-size map


def test_the_route_graph_is_cached_briefly_per_router(app, client):
    from foundry_router.ui import routes as R
    R._ROUTES_CACHE.clear()
    a = client.get("/admin/api/routes?hours=1").json()
    assert "nodes" in a and "edges" in a
    assert len(R._ROUTES_CACHE) == 1
    key = next(iter(R._ROUTES_CACHE))
    assert key[0] == id(app.state.services) and key[1:] == (1.0, False, False)
    stamp = R._ROUTES_CACHE[key][0]
    client.get("/admin/api/routes?hours=1")
    assert R._ROUTES_CACHE[key][0] == stamp                 # reused, not rebuilt
    client.get("/admin/api/routes?hours=1&active=1")
    assert len(R._ROUTES_CACHE) == 2


def test_the_admin_shell_has_a_side_menu_quick_search_and_a_traffic_view():
    """v0.181.121: a side menu on wide windows (top tabs stay a choice), Ctrl+K search,
    and the overview's traffic card (backend slots, queue, live route map)."""
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    assert '<aside id="side"' in html and "function renderSide(" in html
    assert 'localStorage.getItem("fr.nav")' in html and "window.innerWidth >= 1000" in html
    assert 'id="pal-back"' in html and "function openPalette(" in html
    assert 'e.key.toLowerCase() === "k"' in html
    # the route map's parts are shared by the Routes tab and the overview
    assert html.count("routeFlowParts(g)") >= 3
    assert "async function ovTraffic()" in html and 'api("routes?hours=0.5&all=0&active=1")' in html
    assert 'id="ov-queues"' in html and 'id="ov-flow"' in html
    # a tracked client in live mode is never labelled "no key"
    assert '"has a key"' in html


def test_the_compose_file_has_a_cpu_embeddings_service():
    import yaml
    root = STATIC.parents[2]
    d = yaml.safe_load((root / "docker-compose.yml").read_text(encoding="utf-8"))
    svc = d["services"]["embeddings"]
    assert svc["image"].startswith("ollama/ollama") and svc["container_name"] == "foundry-embeddings"
    assert "deploy" not in svc and "runtime" not in svc and "devices" not in svc      # CPU only
    assert any("/root/.ollama" in v for v in svc["volumes"])
    assert "foundry-router" in d["services"]


def test_live_and_performance_are_laid_out_for_reading():
    """v0.181.123: Live leads with the requests in flight (a table, with backend slots)
    over a two-column dashboard and folds the advisor away; Performance shows its
    charts before the breakdowns and keeps settings at the bottom; charts label their
    latest values and are not flattened by a few extreme points."""
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    live = html[html.index("async function tabLive()"):html.index("async function tabCompare()")]
    assert live.index('id="live-models"') < live.index('class="lv-grid"') < live.index('id="live-advisor"')
    assert 'id="live-queues"' in live and "queueChipsHtml(q)" in live
    assert '<details class="card lv-adv">' in live and 'id="live-adv-count"' in live
    assert "<th>who</th><th>persona</th><th>model</th><th>backend</th><th>doing</th>" in live
    assert "_tps[c.model] < 2000" in live                       # no bogus stream estimates
    perf = html[html.index("async function tabPerformance()"):html.index("function renderServerHistory(")]
    order = [perf.index(x) for x in ('id="perf-summary"', 'id="perf-sec-charts"', 'id="perf-bymodel"',
                                     'id="perf-advisor"', 'id="perf-sec-servers"',
                                     'id="perf-sec-settings"', 'id="perf-label"', 'id="perf-sec-detail"')]
    assert order == sorted(order)
    chart = html[html.index("function lineChart("):html.index("function lineChart(") + 6000]
    assert "p98 * 3" in chart and "Math.min(v, hi)" in chart and "above the scale" in chart
    assert "${paths}${ends}${hits}" in chart
    assert html.count("function queueChipsHtml(q)") == 1


def test_personas_have_a_toolbar_and_a_side_panel_editor_and_pool_has_sections():
    """v0.181.124: Personas gets find and group filters and its editor moves from the
    bottom of a 4,000 px page into a side panel with sections; long tabs (Pool) show one
    section at a time."""
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    per = html[html.index("async function tabPersonas()"):html.index("function openPersonaDrawer()")]
    assert 'id="pe-find"' in per and 'id="pe-groups"' in per and 'id="pe-new"' in per
    assert '<table id="pe-list">' in per and 'data-p="${esc(p.virtual_name)}"' in per
    assert '<aside id="pe-drawer" class="drawer"' in per and 'class="dr-foot"' in per
    secs = [per.index(f'<h3 class="pe-sec">{t}</h3>') for t in ("Basics", "Routing", "Tools", "Models", "Advanced")]
    assert secs == sorted(secs)
    assert per.index('class="dr-foot"') < per.index('id="pe-save"')            # Save stays in view
    assert "Add / edit persona" not in html and 'closest(".card")\n    .scrollIntoView' not in html
    assert "function closePersonaDrawer()" in html and "openPersonaDrawer();" in html
    assert "TAB_SECTIONS" in html and '["MCP endpoint", /^Foundry-MCP aggregator/]' in html
    assert "sectionTabs();" in html and ".sect-hide { display:none !important; }" in html
    # a resize never redraws the page under an open editor
    assert ".drawer.show" in (STATIC / "kit.js").read_text(encoding="utf-8")
