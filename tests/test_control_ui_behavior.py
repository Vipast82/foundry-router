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
const S = {lanesAt: 999, lanes: {state: {enabled: true, paused: false, lanes: {visual: true}},
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
