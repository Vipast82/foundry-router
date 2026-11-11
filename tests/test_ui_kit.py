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
    assert 'kit.js?v=125" data-no-sort="1" data-no-tools="1"' in (STATIC / "automation.html").read_text(encoding="utf-8")
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
    assert '<table id="pe-list" class="no-tools">' in per and 'data-p="${esc(p.virtual_name)}"' in per
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


def test_long_tables_get_a_find_box_and_paging():
    """v0.181.125: a table of 30 rows or more shows 50 at a time under a bar with a find
    box (Model Registry was 32,759 px tall, Usage Log 15,561, Events 14,007)."""
    js = (STATIC / "kit.js").read_text(encoding="utf-8")
    css = (STATIC / "kit.css").read_text(encoding="utf-8")
    assert "const LONG = 30, PAGE = 50;" in js and "function applyTools(table, head)" in js
    assert 'class="fk-find"' in js and "fk-more" in js and "fk-all" in js and "fk-less" in js
    assert "toolState[sig] = toolState[sig] ||" in js             # kept across re-renders
    assert "if (sortTable(table, head, col, dir)) { sortState[sig] = {col, dir}; tools(table, head); }" in js
    assert 'table.classList.contains("no-tools")' in js and 'table.dataset.fkFind === "off"' in js
    assert "dataset.noTools" in js
    # a detail row (one cell across the table) stays with the row above it
    assert "r.cells.length === 1 && r.cells[0].colSpan > 1" in js
    # a header re-rendered inside the same table is enhanced again (it lost its sorting)
    assert "if (head._fk) { tools(table, head); return; }" in js
    assert "tr.fk-hide { display: none; }" in css and ".fk-tools.in-box { position: sticky; left: 0; }" in css
    for page in ("index.html", "control.html"):
        html = (STATIC / page).read_text(encoding="utf-8")
        assert "/ui/kit.js?v=125" in html and "/ui/kit.css?v=125" in html and "data-no-tools" not in html


def test_long_tabs_have_sections_and_the_model_editor_is_a_side_panel():
    """v0.181.125: Model Registry, Usage Log and Guardrails show one section at a time;
    the request log comes first on Usage Log; Events filters by level and source; the
    model editor opens beside the list instead of under 600 rows."""
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    sec = html[html.index("const TAB_SECTIONS = {"):html.index("function sectionTabs()")]
    for tab in ('"Model Registry": [["Reachable now", /^Reachable now/], ["Known catalog", /^Known catalog/]]',
                '"Usage Log": [["Requests", null]', 'Guardrails: [["Limits", /^authority/]', "Pool: [["):
        assert tab in sec, tab
    fn = html[html.index("function sectionTabs()"):html.index("// Keep every control visually attached")]
    assert 'k.classList.contains("drawer")' in fn             # a side panel is never hidden with a section
    assert "let cur = defs[0][1] ? -1 : 0;" in fn             # blocks above the first section stay in view
    assert "used.has(i) ?" in fn and "if (used.size < 2)" in fn   # no button for an empty section
    models = html[html.index("async function tabModels()"):html.index("window.toggleModel")]
    assert '<aside id="model-editor" class="drawer"' in models and 'id="me-back" class="drawer-back"' in models
    assert models.index('class="dr-foot"') < models.index('id="me-save"')
    assert 'data-fk-find="off"' in models and 'type="search" id="model-search"' in models
    edit = html[html.index("window.editModel = id => {"):html.index("/* -------------------------------------------------------------- personas -- */")]
    assert 'panel.classList.add("show")' in edit and "scrollIntoView" not in edit
    assert "function closeDrawers()" in html and ".drawer.show { transform:none;" in html
    assert "#pe-drawer {" not in html and ".drawer-back.show { display:block; }" in html
    usage = html[html.index("async function tabUsage()"):html.index("async function sendFeedback")]
    main = usage[usage.index('$("#main").innerHTML'):]
    assert main.index("<th>request</th>") < main.index("${budgetCard(bg)}") < main.index("${mcpCard}")
    assert 'class="clamp2"' in usage
    events = html[html.index("async function tabEvents()"):html.index("backup & restore (backup.py)")]
    assert 'id="ev-levels"' in events and 'id="ev-source"' in events and '<table id="ev-table">' in events
    assert '["problems", "Warnings and errors"]' in events


def test_the_remaining_long_tabs_have_sections_and_the_dev_log_scrolls_in_place():
    """v0.181.127: Work (8,695 px), Training (4,590), Secrets (4,327), Clients & Keys
    (3,138) and Machines (2,896) show one section at a time; the Dev Log (13,241 px)
    scrolls inside its own pane; a client's long persona list folds away."""
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    sec = html[html.index("const TAB_SECTIONS = {"):html.index("function sectionTabs()")]
    for tab in ('Work: [["Sprint scorecard", /^Sprint scorecard/]', 'Training: [["Capture", /^Foundry capture/]',
                'Secrets: [["Store and values", null]', '"Clients & Keys": [["Clients", null]',
                'Machines: [["Machines", null]'):
        assert tab in sec, tab
    # every sectioned tab is a real tab
    import re
    tabs = html[html.index("const TABS = {"):html.index("const GROUPS = [")]
    for name in re.findall(r'^  "?([A-Za-z &]+?)"?: \[\[', sec, re.M):
        assert f'"{name}":' in tabs, name
    assert '<div id="dev-list" class="dev-pane"></div>' in html and ".dev-pane { max-height:" in html
    clients = html[html.index("async function tabClients()"):html.index("window._pickerOptions.personas = d.personas;")]
    assert "${c.personas.length} personas</summary>" in clients and "roles without a persona" in clients


def test_performance_and_data_have_sections_and_late_blocks_join_theirs():
    """v0.181.128: the sprint scorecard arrives seconds after the Work tab is drawn and
    missed its section; #main is now watched. Performance (5,582 px) and Data (5,794 px)
    show one section at a time."""
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    sec = html[html.index("const TAB_SECTIONS = {"):html.index("function sectionTabs()")]
    assert 'Performance: [["Charts", /^Speed and latency over time/]' in sec
    assert 'Data: [["Tables and retention", null]' in sec and '["Backup and restore", /^Backup & restore/]' in sec
    assert "window._sectObs = setTimeout(sectionTabs, 60);" in sec and "observe(main, {childList: true})" in sec
    assert 'class="jump"' not in html[html.index("async function tabPerformance()"):html.index('id="perf-sec-detail"')]
    data = html[html.index("async function tabData()"):html.index("function opsCards(cv, sn)")]
    assert data.index('<h3 style="margin-top:0">Tables</h3>') < data.index("${opsCards(cv, sn)}")
    backup = html[html.index("async function backupCard()"):html.index("async function providerKeysCard()")]
    assert '$("#main").appendChild(card);' in backup
    # the Work intro sentence starts with "Tasks" too: it must not claim the Tasks section
    import re
    assert '["Tasks", /^Tasks(?! a development)/]' in sec
    assert "Tasks a development" in html
    assert not re.match(r"^Tasks(?! a development)", "Tasks a development coordinator reported")
    assert re.match(r"^Tasks(?! a development)", "Tasksshowing 50 of 110")


def test_host_admin_tells_an_endpoint_that_is_not_offered_from_one_that_failed():
    """v0.181.135: brain-lane, healthy, showed "unreachable" in red three times and its
    /health reply as one line running off the page."""
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    fn = html[html.index("function probeState(p) {"):html.index("// -- full Ollama model-management panel")]
    assert '[404, 405, 501].includes(code) ? "absent" : [401, 403].includes(code) ? "denied" : "error"' in fn
    assert "if (p.state) return p.state;" in fn                       # the router's word first
    assert '(probeState(p) === "absent" && k !== "models" ? absent : failed).push([k, p]);' in fn
    assert "Not offered by this server" in fn and "Did not answer" in fn
    assert '<span class="pill bad">unreachable</span>' not in fn      # the old card for every miss
    assert "JSON.stringify(h.body, null, 1)" in fn and 'class="raw mono"' in fn      # wrapped, on request
    assert "_HEALTH_RATIOS.filter(" in fn and 'class="ov-kpis"' in fn
    assert '<div class="chips">' in fn                                # names only: no one-column table
    tab = html[html.index("async function tabHostAdmin() {"):html.index("async function hostRenderDiag(")]
    assert 'class="seg" id="host-be"' in tab and "<select" not in tab
    assert 'if (window._hostBackend !== cur.name || !$("#host-diag")) return;' in html
    css = html[:html.index("</style>")]
    assert "pre.raw { white-space:pre-wrap; overflow-wrap:anywhere;" in css


def test_the_last_five_tabs_have_numbers_on_top_and_sections():
    """v0.181.135: Active Use, Helpers & Limits, MCP Metrics and Cost."""
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    sec = html[html.index("const TAB_SECTIONS = {"):html.index("function sectionTabs()")]
    assert '"Helpers & Limits": [["Backend limits", /^Backend limits/], ["Lanes", /^Lanes for short jobs/],' in sec
    assert '"MCP Metrics": [["Over time", /^Calls over time/], ["Tools", /^Per tool/]' in sec
    assert 'Cost: [["Comparison", /^Visual comparison/], ["Head to head", /^Head to head/],' in sec
    # every section's first block exists under the name its pattern looks for
    lim = html[html.index("function queueCard(q) {"):html.index("// Alerts & digest (alerts.py)")]
    for title in ('<div class="card-h">Backend limits ', '<div class="card-h">Lanes for short jobs ',
                  '<div class="card-h">Queue classes ', '<div class="card-h">Waiting now '):
        assert title in lim, title
    assert lim.count('data-f="lane_max_prompt_tokens"') == 1 and lim.count('data-f="helpers_allowed"') == 1
    assert 'document.querySelectorAll(`input[data-b="${CSS.escape(name)}"]`)' in lim     # one Save sends both tables
    assert lim.count('class="ov-kpis"') == 1 and "slotdots" in lim
    mcp = html[html.index("async function tabMcpMetrics() {"):html.index("async function tabAgents() {")]
    order = [mcp.index(t) for t in ('ch("Calls over time"', 'ch("Per tool"', 'ch("Per server"',
                                    'ch("Context budget', 'ch("Recent calls"')]
    assert order == sorted(order)
    assert 'class="seg" id="mcpm-win"' in mcp and "kept connection" in mcp
    assert '$("#mcpm-win").value' not in mcp
    cost = html[html.index("async function tabCost() {"):html.index("async function tabLive() {")]
    assert cost.index('<div class="card-h">Visual comparison') < cost.index('<div class="card-h">Cost for this window')
    assert 'class="seg" id="cost-win"' in cost and '$("#cost-win").value' not in cost
    assert 'tile("cheapest paid"' in cost and 'class="cbar"' in cost
    au = html[html.index("async function tabActiveUse() {"):html.index("window.showRouteEdge = async")]
    assert 'class="seg" id="au-hours"' in au and 'class="ov-kpis"' in au
    assert "Share of requests" in au and 'class="clamp2"' in au
    assert "<th>IP · app</th>" not in au                               # folded under the client's name
