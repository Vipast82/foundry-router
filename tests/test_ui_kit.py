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
