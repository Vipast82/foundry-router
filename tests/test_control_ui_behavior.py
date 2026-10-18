"""Behavior of the control-center page's own logic, run in node against the
real inline script (functions sliced out of control.html, browser APIs
stubbed): layout/preference persistence and migration, following the active
coordinator session without moving a pinned history view, notification
permission states (no claimed delivery without permission), and the proactive
lanes panel (hostile text escaped, unavailable metrics say N/A, not-inspected
results flagged, quota defer reasons shown)."""

import json
import re
import shutil
import subprocess

import pytest

from tests.test_control_ui import _script

pytestmark = pytest.mark.skipif(not shutil.which("node"), reason="node not installed")

PRELUDE = """
const ESC_MAP = {"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"};
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ESC_MAP[c]);
const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
"""


def run_js(tmp_path, body: str):
    f = tmp_path / "t.js"
    f.write_text(PRELUDE + body, encoding="utf-8")
    r = subprocess.run(["node", str(f)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr[:3000]
    return json.loads(r.stdout.strip().splitlines()[-1])


def between(src: str, start: str, end: str) -> str:
    i = src.index(start)
    return src[i:src.index(end, i)]


def test_layout_and_preferences_persist_and_migrate(tmp_path):
    s = _script()
    block = between(s, "const PREF_KEY", "function loadPrefs")
    out = run_js(tmp_path, block + """
const stored = {v: 2, fontSize: 15, sidebar: {order: ["helpers", "identity", "gone"], hidden: ["mcp"], collapsed: []},
                layout: {presets: {landscape: {inspectorW: 512, inspectorPos: "bottom", graphH: 99, helperW: 600, helpers: 2}}},
                showLanes: false};
const m = migratePrefs(JSON.parse(JSON.stringify(stored)));
const bad = migratePrefs("garbage");
console.log(JSON.stringify({font: m.fontSize, order: m.sidebar.order, hidden: m.sidebar.hidden,
  land: m.layout.presets.landscape, portrait: m.layout.presets.portrait.inspectorW,
  lanes: m.showLanes, v: m.v, badOrder: bad.sidebar.order.length, badLanes: bad.showLanes}));
""")
    assert out["font"] == 15 and out["land"]["inspectorW"] == 512 and out["land"]["graphH"] == 99
    assert out["order"][:2] == ["helpers", "identity"] and "gone" not in out["order"]
    assert "lanes" in out["order"] and out["hidden"] == ["mcp"]          # new section appended
    assert out["lanes"] is False and out["badLanes"] is True              # user choice kept
    assert out["portrait"] == 330 and out["v"] == 2 and out["badOrder"] >= 15


def test_following_the_coordinator_never_moves_a_pinned_view(tmp_path):
    s = _script()
    fn = between(s, "function applyFollow()", "/* ======")
    out = run_js(tmp_path, """
let POPOUT = ""; const P = {followCoordinator: true}; const opened = [];
const main = {id: "main", sid: "ses_old", pinned: false};
const paneById = () => main; const tdata = t => (t && t.data) || {};
let task = {data: {session_id: "ses_new"}};
const curTask = () => task; const renderPaneHead = () => {};
const openInPane = (p, sid, o) => { opened.push([sid, o.follow]); p.sid = sid; };
""" + fn + """
applyFollow();                                   // session changed: follow it
main.pinned = true; task = {data: {session_id: "ses_third"}};
applyFollow();                                   // a pinned history view is never moved
main.pinned = false; P.followCoordinator = false; applyFollow();
P.followCoordinator = true; POPOUT = "pane"; applyFollow();
console.log(JSON.stringify({opened, sid: main.sid}));
""")
    assert out["opened"] == [["ses_new", True]] and out["sid"] == "ses_new"


@pytest.mark.parametrize("perm,expect", [("denied", []), ("default", []),
                                         ("granted", [["a1", "notification", "shown"]]),
                                         ("throws", [["a1", "notification", "failed"]])])
def test_notifications_need_permission_and_report_honestly(tmp_path, perm, expect):
    s = _script()
    fn = between(s, "function notifyOS(a)", "function renderSoundBtn")
    out = run_js(tmp_path, f"""
const reports = []; const report = (id, ch, res) => reports.push([id, ch, res]);
const P = {{notify: {{enabled: true, critical: true, high: true, completion: false, stale: true}}}};
class Notification {{ constructor() {{ if ("{perm}" === "throws") throw new Error("blocked"); }} }}
Notification.permission = "{perm}" === "throws" ? "granted" : "{perm}";
const window = {{Notification}};
""" + fn + """
notifyOS({alert_id: "a1", title: "Question", detail: "x", kind: "question", severity: "critical"});
notifyOS({alert_id: "a2", title: "Done", kind: "completion", severity: "info"});   // class muted
console.log(JSON.stringify(reports));
""")
    assert out == expect


def test_lanes_panel_escapes_and_is_honest_about_missing_data(tmp_path):
    s = _script()
    method = between(s, "  lanes(s) {\n    if (!P.showLanes)", "  sessions(s) {")
    out = run_js(tmp_path, """
const P = {showLanes: true}; const nowS = () => 1000; let loads = 0;
const loadLanes = () => { loads++; };
const NA = why => `<span class="dim" title="${esc(why || "")}">N/A</span>`;
const kv = rows => rows.filter(Boolean).map(([k, v]) => `<div>${esc(k)}</div><div>${v}</div>`).join("");
const relAge = s => Math.round(s) + "s"; const tsText = t => t ? "T" + t : "";
const lanesKey = (p, t) => String(p || "") + "|" + String(t || "");
const S = {project: "proj", task: "T2", lanesKey: "proj|T2", openDiffs: new Set(["hu-2"]), lanesAt: 999, lanes: {state: {enabled: true, paused: false, lanes: {visual: true}},
  artifacts: [{artifact_id: "art 1", rel_path: "docs/evidence/T2/m.png", generation: 2, width: 6, height: 4,
               sha256: "abcdef0123456789abcdef", revision: "1234567890abcdef", status: "current", valid: "ok", has_thumb: 1}],
  units: [
   {unit_id: "hu-1", lane: "visual", status: "done", feedback: "rejected", persona: "V", backend: "vision",
    artifact_id: "art 1", reason: "served by qwen-text, which is not one of the vision-validated routes",
    envelope: {not_inspected: true, summary: "<script>alert(1)</script>", findings: []}, created_ts: 1},
   {unit_id: "hu-2", lane: "failure_triage", status: "deferred", reason: "meridian: DEFER — Weekly 72.0% used",
    envelope: {findings: [{severity: "high", confidence: 0.9, observed: "<img src=x onerror=alert(1)>",
                           hypothesis: "h", paths: ["src/a.luau"]}],
               patch: "--- a/src/a.luau\\n+++ b/src/a.luau\\n@@ -1 +1 @@\\n-x\\n+<b>y</b>\\n",
               checks: {drift: {"src/a.luau": "changed"}}}, created_ts: 2}]}};
const R = {
""" + method + """
};
const html = R.lanes(null);
console.log(JSON.stringify({html, loads}));
""")
    h = out["html"]
    assert "<script>" not in h and "&lt;script&gt;" in h
    assert "<img src=x" not in h and "&lt;img" in h and "&lt;b&gt;y&lt;/b&gt;" in h
    assert "Not inspected" in h and "vision-validated" in h
    assert "Weekly 72.0% used" in h and "rebase needed" in h
    assert ">N/A<" in h                                       # turnaround/tokens unavailable
    assert "/control/api/artifacts/art%201/thumbnail" in h   # id URL-encoded
    assert "Pause proactive lanes" in h and out["loads"] == 0
    assert '<details data-diff="hu-2" open>' in h                 # an opened diff stays open


# ------------------------------------------------------------------ graph fit, placement, floats

def _graph_js(tmp_path, body):
    s = _script()
    return run_js(tmp_path, between(s, "/*glayout-start*/", "/*glayout-end*/") + body)


def _nodes(n_sessions=10):
    nodes = [{"id": "coordinator", "kind": "coordinator"}]
    nodes += [{"id": f"s{i}", "kind": "session"} for i in range(n_sessions)]
    nodes += [{"id": f"p{i}", "kind": "persona"} for i in range(4)]
    nodes += [{"id": f"b{i}", "kind": "backend"} for i in range(3)]
    nodes += [{"id": f"m{i}", "kind": "model"} for i in range(3)]
    return nodes


def test_graph_fits_a_wide_short_band_without_shrinking_labels(tmp_path):
    """Victor's case: a ~2000x170 strip. The old layout shrank a 970-wide,
    ~400-tall drawing to fit 170 px (text ~5 px). Now long columns split into
    sub-columns across the width and the scale never drops below readable."""
    out = _graph_js(tmp_path, f"""
const r = graphLayout({json.dumps(_nodes(10))}, 2000, 170, false, null);
const ys = Object.values(r.pos).map(p => p.y), xs = Object.values(r.pos).map(p => p.x);
console.log(JSON.stringify({{scale: r.scale, fits: r.fits, W: r.W, H: r.H, maxY: Math.max(...ys), maxX: Math.max(...xs)}}));
""")
    assert out["scale"] >= 0.95 and out["fits"]                    # readable AND inside the box
    assert out["H"] * out["scale"] <= 170 and out["W"] * out["scale"] <= 2000
    assert out["maxX"] > 1000                                       # spread across the width


def test_graph_never_shrinks_below_readable_and_scrolls_instead(tmp_path):
    out = _graph_js(tmp_path, f"""
const r = graphLayout({json.dumps(_nodes(80))}, 400, 120, false, null);
const z = graphLayout({json.dumps(_nodes(3))}, 900, 400, false, 0.1);
const big = graphLayout({json.dumps(_nodes(3))}, 900, 400, false, 9);
console.log(JSON.stringify({{scale: r.scale, fits: r.fits, zoomMin: z.scale, zoomMax: big.scale}}));
""")
    assert out["scale"] == 0.95 and out["fits"] is False            # floor holds; the box scrolls
    assert out["zoomMin"] == 0.95 and out["zoomMax"] == 3           # zoom is clamped both ways


def test_tall_box_uses_the_vertical_arrangement(tmp_path):
    out = _graph_js(tmp_path, f"""
const r = graphLayout({json.dumps(_nodes(6))}, 420, 1200, true, null);
console.log(JSON.stringify({{fits: r.fits, scale: r.scale, W: r.W}}));
""")
    assert out["fits"] and out["scale"] >= 0.95 and out["W"] <= 420 / 0.95


def test_floating_cards_stay_on_screen_and_usable(tmp_path):
    s = _script()
    out = run_js(tmp_path, between(s, "/*floats-start*/", "/*floats-end*/") + """
console.log(JSON.stringify({
  off: clampFloat({x: 5000, y: -300, w: 500, h: 300}, 1280, 720),
  tiny: clampFloat({x: 10, y: 10, w: 20, h: 10}, 1280, 720),
  huge: clampFloat({x: 0, y: 0, w: 9999, h: 9999}, 800, 600),
  dflt: clampFloat(undefined, 1280, 720),
  clean: sanitizeFloats({"graph": {x: 1.4, y: "bad", w: 300, h: 200}, "sec:lanes": {x: 1}, "evil<x>": {x: 1}, "sec:": {}}),
}));
""")
    assert out["off"] == {"x": 772, "y": 8, "w": 500, "h": 300}
    assert out["tiny"]["w"] == 240 and out["tiny"]["h"] == 140
    assert out["huge"] == {"x": 8, "y": 8, "w": 784, "h": 584}
    assert out["dflt"]["x"] + out["dflt"]["w"] <= 1280 - 8
    assert set(out["clean"]) == {"graph", "sec:lanes"} and out["clean"]["graph"]["x"] == 1
    assert "y" not in out["clean"]["graph"] or out["clean"]["graph"].get("y") is None


def test_graph_placement_and_floats_persist_per_preset(tmp_path):
    s = _script()
    block = between(s, "const PREF_KEY", "function loadPrefs")     # includes the floats helpers
    out = run_js(tmp_path, block + """
const stored = {v: 2, layout: {preset: "landscape", presets: {
  landscape: {inspectorW: 400, graphPos: "float", floats: {graph: {x: 100, y: 90, w: 700, h: 300},
                                                           "sec:lanes": {x: 20, y: 30, w: 400, h: 500}}},
  portrait: {graphPos: "nonsense"}}}};
const m = migratePrefs(JSON.parse(JSON.stringify(stored)));
const fresh = migratePrefs(null);
const old = migratePrefs({v: 2, layout: {presets: {landscape: {inspectorW: 390, graphH: 150}}}});
console.log(JSON.stringify({land: m.layout.presets.landscape, port: m.layout.presets.portrait,
  freshLand: fresh.layout.presets.landscape.graphPos, freshPort: fresh.layout.presets.portrait.graphPos,
  oldLand: old.layout.presets.landscape}));
""")
    land = out["land"]
    assert land["graphPos"] == "float" and land["inspectorW"] == 400
    assert land["floats"]["graph"] == {"x": 100, "y": 90, "w": 700, "h": 300}
    assert land["floats"]["sec:lanes"]["h"] == 500
    assert out["port"]["graphPos"] == "bottom" and out["port"]["floats"] == {}   # bad value -> default
    assert out["freshLand"] == "side" and out["freshPort"] == "bottom"
    assert out["oldLand"]["graphH"] == 150 and out["oldLand"]["floats"] == {}    # older prefs migrate


def test_graph_popout_and_float_controls_exist():
    s = _script()
    assert 'popout({popout: "graph"' in s and 'POPOUT === "graph"' in s
    assert 'id="g-pos"' in s and 'data-float="1"' in s and "function dockFloat" in s
    assert 'head.onkeydown' in s and 'e.altKey' in s                     # keyboard move/resize
    assert "body.narrow-ui #floatLayer { display:none; }" in PAGE_TEXT()


def PAGE_TEXT():
    from tests.test_control_ui import PAGE
    return PAGE.read_text(encoding="utf-8")


def test_popped_out_cards_leave_the_main_view_and_come_back(tmp_path):
    """A section/graph open in its own window is left out of the main page;
    when that window says it closed (or stops announcing itself for ~75 s) it returns."""
    s = _script()
    out = run_js(tmp_path, between(s, "/*popped-start*/", "/*popped-end*/") + """
const m = new Map([["w1", {id: "sec:lanes", project: "A", seen: 1000}], ["w2", {id: "graph", project: "A", seen: 70000}]]);
const changed = prunePopped(m, 77000);
console.log(JSON.stringify({ids: [popId("section:lanes"), popId("graph"), popId("pane"), popId(""),
  popId("pane", "ses_1")],
  changed, left: [...m.keys()], again: prunePopped(m, 77100), ttl: POP_TTL_MS}));
""")
    assert out["ids"] == ["sec:lanes", "graph", "", "", "pane:ses_1"]    # panes too (by session)
    assert out["changed"] is True and out["left"] == ["w2"] and out["again"] is False
    assert out["ttl"] >= 60000      # a minimized window's throttled (~1/min) timer can't flap it
    assert 'isPopped("graph")' in s and "data-showhere" in s and '"fr.cc.popouts"' in s
    assert 'addEventListener("pagehide"' in s
    assert 'isPopped("pane:" + p.sid)' in s and "function poppedPlaceholder" in s


def test_active_only_keeps_running_agents_and_their_whole_path(tmp_path):
    g = {"nodes": [{"id": i, "kind": k} for i, k in (
            ("coordinator", "coordinator"), ("s:busy", "session"), ("s:old", "session"),
            ("s:child", "session"), ("p:Local", "persona"), ("b:strata", "backend"),
            ("m:qwen", "model"), ("p:Old", "persona"), ("mcp:Roblox", "mcp"))],
         "edges": [
            {"from": "coordinator", "to": "s:busy", "kind": "lifecycle", "state": "active"},
            {"from": "coordinator", "to": "s:old", "kind": "lifecycle", "state": "completed"},
            {"from": "s:busy", "to": "s:child", "kind": "delegation", "state": "completed"},
            {"from": "s:child", "to": "p:Local", "kind": "inference", "state": "active"},
            {"from": "p:Local", "to": "b:strata", "kind": "inference", "state": "active"},
            {"from": "b:strata", "to": "m:qwen", "kind": "inference", "state": "active"},
            {"from": "s:old", "to": "p:Old", "kind": "inference", "state": "completed"},
            {"from": "s:busy", "to": "mcp:Roblox", "kind": "tool", "state": "completed"}]}
    s = _script()
    out = run_js(tmp_path, between(s, "/*glayout-start*/", "/*glayout-end*/") + f"""
const a = activeSubgraph({json.dumps(g)});
const none = activeSubgraph({{nodes: [{{id: "x", kind: "session"}}], edges: [{{from: "coordinator", to: "x", kind: "lifecycle", state: "completed"}}]}});
console.log(JSON.stringify({{nodes: a.nodes.map(n => n.id).sort(), edges: a.edges.length, none: none.nodes.length}}));
""")
    # the running child's chain reads end to end; finished work and idle tools are gone
    assert out["nodes"] == sorted(["coordinator", "s:busy", "s:child", "p:Local", "b:strata",
                                   "m:qwen"])
    assert out["edges"] == 5 and out["none"] == 0
    assert 'id="g-active"' in s and "graphActiveOnly" in s


def test_popouts_are_scoped_by_project_and_counted_per_window(tmp_path):
    """Popping section X in project A leaves a tab showing project B alone; with
    two windows of the same card, closing one keeps it out of the main page."""
    s = _script()
    out = run_js(tmp_path, between(s, "/*popped-start*/", "/*popped-end*/") + """
const m = new Map([["w1", {id: "sec:lanes", project: "A", seen: 1}], ["w2", {id: "sec:lanes", project: "A", seen: 1}]]);
const inA = poppedHas(m, "sec:lanes", "A"), inB = poppedHas(m, "sec:lanes", "B");
m.delete("w1");                                    // one of the two windows closed
const stillA = poppedHas(m, "sec:lanes", "A");
m.delete("w2");
console.log(JSON.stringify({inA, inB, stillA, gone: poppedHas(m, "sec:lanes", "A")}));
""")
    assert out == {"inA": True, "inB": False, "stillA": True, "gone": False}
    # the messages carry project + a per-window token; main page keys by token
    assert "token, project:" in s and "S.popped.set(tok," in s and "poptoken" in s


def test_lanes_follow_the_selection_and_route_the_project_wide_action(tmp_path):
    s = _script()
    block = between(s, "/*lanes-load-start*/", "/*lanes-load-end*/")
    out = run_js(tmp_path, """
const S = {project: "A", task: "T1"}; const nowS = () => 5; let rendered = 0;
const scheduleInspector = () => { rendered++; };
let resolveApi; const api = url => new Promise(r => { resolveApi = () => r({url}); });
""" + block + """
(async () => {
  const p = loadLanes();
  S.project = "B"; S.task = "T9";                  // selection changed while the request was in flight
  resolveApi(); await p;
  const stale = S.lanes === undefined && rendered === 0;
  const q = loadLanes(); resolveApi(); await q;
  const tasks = new Map([["A/T1", {project_id: "A", task_id: "T1", received_ts: 1}],
                         ["A/T2", {project_id: "A", task_id: "T2", received_ts: 5}],
                         ["B/T9", {project_id: "B", task_id: "T9", received_ts: 9}]]);
  console.log(JSON.stringify({stale, key: S.lanesKey, url: S.lanes.url,
    pick: lanesActionTask("A", "", tasks), keep: lanesActionTask("A", "T1", tasks), none: lanesActionTask("Z", "", tasks)}));
})();
""")
    assert out["stale"] is True                     # the old project's reply was dropped
    assert out["key"] == "B|T9" and "project=B" in out["url"] and "task=T9" in out["url"]
    assert out["pick"] == "T2" and out["keep"] == "T1" and out["none"] == ""
    assert "S.lanes = null; S.lanesAt = 0; S.lanesKey = \"\"" in s      # reset on selection change
    assert "task_id: tid, expected: {}" in s


def test_open_links_do_not_jump_and_narrow_layout_respects_popped_panes():
    s = _script()
    fn = between(s, "function wireOpenButtons(root)", "function tokRows")
    assert "ev.preventDefault(); openBeside(b.dataset.open)" in fn
    assert "tpPopped ? poppedPlaceholder() : tp.el" in s
    assert 'details[data-diff]' in s and '"toggle"' in s


def test_graph_redraw_keeps_focus_and_never_interrupts_a_drag(tmp_path):
    s = _script()
    out = run_js(tmp_path, between(s, "/*glayout-start*/", "/*glayout-end*/") + """
const e = {from: "s:a", to: "p:b", kind: "inference", label: ""};
console.log(JSON.stringify({n: graphFocusKey({node: "s:a"}), e: graphFocusKey({edge: "3", ekey: edgeKey(e)}),
  none: graphFocusKey({}), ek: edgeKey(e)}));
""")
    assert out["n"] == "n:s:a" and out["e"] == "e:" + out["ek"] and out["none"] == ""
    fn = between(s, "function renderGraph()", "function graphZoom")
    assert "if (S.graphDragging) { S.graphRenderPending = true; return; }" in fn
    assert "restoreFocus();" in fn and 'data-ekey="${esc(edgeKey(e))}"' in fn


def test_active_only_keeps_queued_and_deferred_lane_work(tmp_path):
    g = {"nodes": [{"id": i, "kind": k} for i, k in (
            ("coordinator", "coordinator"), ("s:main", "session"), ("u:1", "lane_unit"),
            ("u:2", "lane_unit"), ("u:3", "lane_unit"), ("u:old", "lane_unit"), ("p:x", "persona"))],
         "edges": [
            {"from": "coordinator", "to": "s:main", "kind": "lifecycle", "state": "completed"},
            {"from": "s:main", "to": "u:1", "kind": "lineage", "state": "queued"},
            {"from": "s:main", "to": "u:2", "kind": "lineage", "state": "deferred"},
            {"from": "s:main", "to": "u:3", "kind": "lineage", "state": "waiting"},
            {"from": "s:main", "to": "u:old", "kind": "lineage", "state": "completed"},
            {"from": "s:main", "to": "p:x", "kind": "inference", "state": "waiting"}]}
    s = _script()
    out = run_js(tmp_path, between(s, "/*glayout-start*/", "/*glayout-end*/") + f"""
const a = activeSubgraph({json.dumps(g)});
console.log(JSON.stringify({{nodes: a.nodes.map(n => n.id).sort()}}));
""")
    assert out["nodes"] == sorted(["coordinator", "s:main", "u:1", "u:2", "u:3"])
    assert "queued, waiting or deferred lane work" in s
