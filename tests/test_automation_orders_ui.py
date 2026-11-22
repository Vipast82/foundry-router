"""The Automation page's Orders tab, run in node against the page's own inline script (the
orders block sliced out of automation.html, browser APIs stubbed): what the tab shows,
that hostile text from a heartbeat is escaped, and what each form files as an action."""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

PAGE = Path(__file__).resolve().parents[1] / "foundry_router" / "ui" / "static" / "automation.html"
pytestmark = pytest.mark.skipif(not shutil.which("node"), reason="node not installed")

PRELUDE = r"""
const ESC_MAP = {"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"};
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ESC_MAP[c]);
const when = ts => ts ? "T" + ts : "";
function fmtAge(s) { return s === null || s === undefined ? "never" : s + "s"; }
const S = {ovProjects: ["proj"]};
const el = {innerHTML: "", querySelector: () => null};
const main = {addEventListener: () => {}};
const $ = sel => sel === "#main" ? main : el;
const posted = [], toasts = [], modals = [];
let reply = {orders: {}, watch_log: [], source: null, age_s: null};
async function api(path, opts) {
  if (opts && opts.method === "POST") { posted.push(opts.body); return {action_id: "act-1", status: "accepted"}; }
  if (path.startsWith("actions/")) return {status: "applied"};
  return reply;
}
function toast(t, bad) { toasts.push([t, !!bad]); }
function idem() { return "idem-0001"; }
async function waitAction() { return {status: "applied"}; }
function formModal(title, fields, label, onSubmit, danger) { modals.push({title, fields, label, onSubmit, danger: !!danger}); }
const form = o => ({get: k => (k in o ? o[k] : null)});
"""


def _orders_js() -> str:
    script = re.findall(r"<script(?![^>]*src)[^>]*>(.*?)</script>", PAGE.read_text(encoding="utf-8"),
                        re.S)[0]
    i = script.index("S.so = {data: null")
    return script[i:script.index("/* =====", i)]


def run_js(tmp_path, body: str):
    f = tmp_path / "t.js"
    f.write_text(PRELUDE + _orders_js() + "\n(async () => {\n" + body + "\n})().catch(e => { "
                 "console.error(e); process.exit(1); });\n", encoding="utf-8")
    r = subprocess.run(["node", str(f)], capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0, r.stderr[:3000]
    return json.loads(r.stdout.strip().splitlines()[-1])


def test_the_page_script_still_parses(tmp_path):
    script = re.findall(r"<script(?![^>]*src)[^>]*>(.*?)</script>", PAGE.read_text(encoding="utf-8"),
                        re.S)[0]
    f = tmp_path / "automation.js"
    f.write_text(script, encoding="utf-8")
    r = subprocess.run(["node", "--check", str(f)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr[:2000]


def test_the_tab_shows_active_and_ended_orders_and_the_log_escaped(tmp_path):
    out = run_js(tmp_path, r"""
reply = {orders: {proj: {active: [
    {order_id: "so-0123456789", kind: "keep_going", order: "keep POL-7 going until it completes",
     by: "victor <b>", note: "need it <script>alert(1)</script>", created_ts: 100, actions: 3,
     actions_today: 2, waiting: "a decision of yours is open (dec-1)"}],
  ended: [{order_id: "so-aaaaaaaaaa", kind: "pause_all", order: "pause all sprints until 18:19",
           by: "victor", status: "done", ended_ts: 200, result: "the time is up; the project is resumed"}]}},
  watch_log: ["2026-11-18 21:59  POL-7  order 'keep POL-7 going': put it back <img src=x>"],
  source: "coordinator:ws", age_s: 7};
await renderOrders();
console.log(JSON.stringify({html: el.innerHTML, project: S.so.project}));
""")
    html = out["html"]
    assert out["project"] == "proj"
    assert "keep POL-7 going until it completes" in html and "3 times" in html and "(2 today)" in html
    assert "waiting: a decision of yours is open (dec-1)" in html
    assert html.count('data-act="so-cancel"') == 1                    # active orders only
    assert 'data-id="so-0123456789"' in html
    assert "the time is up; the project is resumed" in html and '<span class="pill ok">done</span>' in html
    assert "Reported by coordinator:ws 7s ago" in html
    assert "<script>" not in html and "<img" not in html and "victor <b>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html and "&lt;img src=x&gt;" in html
    assert 'class="wlog"' in html


def test_no_coordinator_and_no_orders_say_so(tmp_path):
    out = run_js(tmp_path, r"""
await renderOrders();
const empty = el.innerHTML;
reply = {orders: {proj: {active: [], ended: []}}, watch_log: [], source: "coordinator:ws", age_s: 1};
S.so.data = null;
await renderOrders();
console.log(JSON.stringify({empty, none: el.innerHTML}));
""")
    assert "No coordinator has reported standing orders yet" in out["empty"]
    assert "No standing order. Examples:" in out["none"] and "Nothing logged yet." in out["none"]
    assert "No coordinator has reported" not in out["none"]


def test_each_form_files_the_right_action_and_never_overwrites_its_kind(tmp_path):
    out = run_js(tmp_path, r"""
S.so.project = "proj";
const give = async f => { modals.length = 0; await orderClick("so-new", {dataset: {}}); await modals[0].onSubmit(form(f)); };
await give({kind: "keep_going", task: " POL-7 ", note: "need it to complete", minutes: "90", reset: "gpt", n: ""});
await give({kind: "pause_all", task: "x", note: "", minutes: "", reset: "gpt", n: ""});
await give({kind: "pause_all", task: "", note: "", minutes: "90", reset: "gpt", n: ""});
await give({kind: "claude_agents", task: "", note: "", minutes: "120", reset: "claude", n: "1"});
await give({kind: "claude_agents", task: "", note: "", minutes: "", reset: "", n: "2"});
modals.length = 0;
await orderClick("so-cancel", {dataset: {id: "so-0123456789"}});
await modals[0].onSubmit(form({why: "enough"}));
const cancel = {title: modals[0].title, danger: modals[0].danger};
modals.length = 0;
await orderClick("so-time", {dataset: {}});
await modals[0].onSubmit(form({task: " POL-7 ", minutes: "240", note: "four more hours"}));
console.log(JSON.stringify({posted, cancel}));
""")
    p = out["posted"]
    assert all(b["project_id"] == "proj" and b["idempotency_key"] for b in p)
    assert [b["kind"] for b in p] == ["order_add"] * 5 + ["order_cancel", "extend_time"]

    def body(b):
        return {k: v for k, v in b.items() if k not in ("kind", "project_id", "idempotency_key")}
    # keep going ends with its sprint: minutes and reset typed into the form are not sent
    assert body(p[0]) == {"order_kind": "keep_going", "task": "POL-7", "note": "need it to complete"}
    assert body(p[1]) == {"order_kind": "pause_all", "note": "", "reset": "gpt"}
    assert body(p[2]) == {"order_kind": "pause_all", "note": "", "minutes": 90}     # minutes win
    assert body(p[3]) == {"order_kind": "claude_agents", "note": "", "n": 1, "minutes": 120}
    assert body(p[4]) == {"order_kind": "claude_agents", "note": "", "n": 2}
    assert body(p[5]) == {"order_id": "so-0123456789", "why": "enough"}
    assert body(p[6]) == {"task": "POL-7", "minutes": 240, "note": "four more hours"}
    assert out["cancel"] == {"title": "Cancel this standing order?", "danger": True}
