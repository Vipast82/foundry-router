"""The status board, router side (COORDINATOR_AUTOMATION_NEEDS item 26): the heartbeat's
`board` block behind GET /control/api/board with the models the router served in its last
100 requests, and the Automation page's Status tab run in node against its own script."""

import json
import re
import shutil
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from foundry_router.control_center import CONTRACT, ControlCenter
from tests.test_control_center import COORD, cc, login  # noqa: F401

PAGE = Path(__file__).resolve().parents[1] / "foundry_router" / "ui" / "static" / "automation.html"
BOARD = {"proj": {"ts": 1.0, "project": "proj", "paused": False, "pause_note": "",
                  "seats": [{"seat": "workstation", "kind": "workstation", "idle": False,
                             "sprint": "FIN-4", "state": "IMPLEMENTING", "phase": "implement",
                             "minutes": 41, "doing": "session working"},
                            {"seat": "slot-1", "kind": "slot", "idle": True, "could_run": ["BAL-2e"],
                             "why": "no lane is bound to it; ready to start on a slot: BAL-2e"}],
                  "blockers": [{"sprint": "FIN-4a", "state": "BLOCKED", "minutes": 180,
                                "doing": "gate: S46 c6 <b>red</b>", "holds_up": ["REL-1a", "REL-1b"],
                                "holds_up_n": 2, "waits_itself": False}],
                  "decisions": [{"decision_id": "dec-1", "task": "FIN-4", "kind": "scope_request",
                                 "whose": "yours", "question": "May it edit <x>?",
                                 "open_minutes": 12, "answers_itself_in_minutes": 18}],
                  "done": [{"sprint": "FIN-2c", "ts": 5000.0}, {"sprint": "BAL-2e1", "ts": 100.0}],
                  "orders": [{"order_id": "so-0123456789", "by": "victor", "actions": 3,
                              "order": "keep POL-7 going until it completes", "waiting": ""}],
                  "machine": {"ram_free_gb": 11.4, "ram_total_gb": 63.9}}}


def _beat(client, board=None):
    body = {"source": {"id": "coordinator:ws", "kind": "coordinator", "version": "0.181.229",
                       "capabilities": {"actions": []}},
            "contract": CONTRACT, "coordinator_version": "0.181.229", "features": ["queue"],
            "sent_ts": time.time()}
    if board is not None:
        body["board"] = board
    assert client.post("/v1/foundry/telemetry/heartbeat", headers=COORD, json=body).status_code == 200


def test_the_board_view_is_the_heartbeats_block_with_the_routers_models(client, cc):
    login(client)
    v = client.get("/control/api/board").json()
    assert v["board"] == {} and v["source"] is None and v["models"]["served"] == {}
    _beat(client, BOARD)
    v = client.get("/control/api/board?project=proj").json()
    assert v["board"] == BOARD and v["source"] == "coordinator:ws"
    assert set(v["models"]) == {"requests", "served", "fallbacks"}
    client.cookies.clear()
    assert client.get("/control/api/board").status_code == 401


def test_models_served_tallies_the_last_requests_and_counts_fallbacks():
    rows = ([{"served_model": "claude-sonnet-5-5", "attempts": 1, "models_used": "[]"}] * 81
            + [{"served_model": "qwen3.8-flash", "attempts": 3,
                "models_used": json.dumps([{"model": "claude-sonnet-5-5", "ok": False},
                                           {"model": "gpt-6.1-sol", "ok": False},
                                           {"model": "qwen3.8-flash", "ok": True}])}] * 19)
    seen = {}

    def query(sql, args=()):
        seen["sql"], seen["args"] = sql, args
        return [dict(r) for r in rows]
    me = SimpleNamespace(db=SimpleNamespace(query=query))
    got = ControlCenter.models_served(me)
    assert got == {"requests": 100, "served": {"claude-sonnet-5-5": 81, "qwen3.8-flash": 19},
                   "fallbacks": 19}
    assert "ORDER BY id DESC LIMIT ?" in seen["sql"] and seen["args"] == (100,)
    # a router with no request log yet
    me = SimpleNamespace(db=SimpleNamespace(query=lambda *a: 1 / 0))
    assert ControlCenter.models_served(me) == {"requests": 0, "served": {}, "fallbacks": 0}


PRELUDE = r"""
const ESC_MAP = {"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"};
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ESC_MAP[c]);
const when = ts => ts ? "T" + ts : "";
function fmtAge(s) { return s === null || s === undefined ? "never" : s + "s"; }
function sprintPill(st) { return `<span class="pill">${esc(st || "?")}</span>`; }
const S = {ovProjects: ["proj"]};
const el = {innerHTML: ""};
const main = {addEventListener: () => {}};
const $ = sel => sel === "#main" ? main : el;
const store = {};
const localStorage = {getItem: k => (k in store ? store[k] : null), setItem: (k, v) => { store[k] = v; }};
let reply = {board: {}, models: {requests: 0, served: {}, fallbacks: 0}, source: null, age_s: null};
async function api() { return reply; }
"""


def _board_js() -> str:
    script = re.findall(r"<script(?![^>]*src)[^>]*>(.*?)</script>", PAGE.read_text(encoding="utf-8"),
                        re.S)[0]
    i = script.index("S.bd = {data: null")
    return script[i:script.index("/* =====", i)]


@pytest.mark.skipif(not shutil.which("node"), reason="node not installed")
def test_the_status_tab_shows_the_board_escaped_and_counts_done_from_the_last_look(tmp_path):
    body = ("reply = " + json.dumps({"board": BOARD, "source": "coordinator:ws", "age_s": 4,
                                     "models": {"requests": 100, "fallbacks": 19, "served": {
                                         "claude-sonnet-5-5": 81, "qwen3.8-flash": 19}}}) + r""";
await renderBoard();
const first = el.innerHTML;
store["foundry.board.seen"] = JSON.stringify({proj: 1000});
S.bd.data = null;
await renderBoard();
const second = el.innerHTML;
boardClick("bd-seen", {});
const marked = JSON.parse(store["foundry.board.seen"]).proj > 1000;
reply = {board: {}, models: {}, source: null, age_s: null};
S.bd.data = null;
await renderBoard();
console.log(JSON.stringify({first, second, marked, none: el.innerHTML}));
""")
    f = tmp_path / "t.js"
    f.write_text(PRELUDE + _board_js() + "\n(async () => {\n" + body + "\n})().catch(e => { "
                 "console.error(e); process.exit(1); });\n", encoding="utf-8")
    r = subprocess.run(["node", str(f)], capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0, r.stderr[:3000]
    out = json.loads(r.stdout.strip().splitlines()[-1])
    h = out["first"]
    assert "<b>workstation</b>" in h and "FIN-4" in h and "41 min" in h and "session working" in h
    assert '<span class="pill warn">idle</span>' in h
    assert "no lane is bound to it; ready to start on a slot: BAL-2e" in h
    assert "<b>2</b>" in h and "REL-1a, REL-1b" in h and "3 h" in h           # held up, 180 min
    assert "gate: S46 c6 &lt;b&gt;red&lt;/b&gt;" in h and "<b>red</b>" not in h
    assert "May it edit &lt;x&gt;?" in h and "answers itself in 18 min" in h
    assert '<span class="pill warn">yours</span>' in h
    assert "In the last 24 h (2)" in h and "Since you marked it seen" not in h
    assert "keep POL-7 going until it completes" in h and "acted 3" in h
    assert "ram free <b>11.4</b> GB" in h and "claude-sonnet-5-5 81" in h
    assert "19 fell to a later route" in h and "last 100 requests" in h
    # after "mark as seen" at t=1000: only what completed later counts as new
    s = out["second"]
    seen_part = s.split("Since you marked it seen")[1].split("In the last 24 h")[0]
    assert "FIN-2c" in seen_part and "BAL-2e1" not in seen_part
    assert out["marked"] is True
    assert "No coordinator has reported a status board yet" in out["none"]


def test_the_automation_page_has_the_status_tab_wired():
    html = PAGE.read_text(encoding="utf-8")
    assert 'id="tab-status"' in html and 'id="v-status"' in html
    assert '"tracks", "status", "orders", "services"' in html
    assert 'if (S.view === "status") return renderBoard();' in html
    assert 'else if (a.startsWith("bd-")) boardClick(a, b);' in html
    assert 'S.view === "status" && !busy("v-status")' in html      # refreshes itself
