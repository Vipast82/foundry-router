"""The Automation page (/ui/automation) and its read-side endpoints: operator
session + CSRF like the control center, the live routing base view, dry run /
check / diff over a posted document, a page that talks to the config store
(automation_api.py) the way contract v1.2 says, and a script that parses."""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

from foundry_router.config import ClientConfig

PAGE = Path(__file__).resolve().parents[1] / "foundry_router" / "ui" / "static" / "automation.html"
OP_KEY = "op-key-automation-1234"


@pytest.fixture()
def op(app, client):
    app.state.services.config_store.config.clients.append(
        ClientConfig(name="victor", key=OP_KEY, scopes=["operator"]))
    r = client.post("/control/api/login", json={"key": OP_KEY})
    assert r.status_code == 200, r.text
    return {"x-foundry-csrf": r.json()["csrf"]}


def _script() -> str:
    return re.findall(r"<script(?![^>]*src)[^>]*>(.*?)</script>", PAGE.read_text(encoding="utf-8"), re.S)[0]


def test_page_served_with_csp_allowing_only_the_pinned_cdn(client):
    r = client.get("/ui/automation")
    assert r.status_code == 200 and "Automation" in r.text
    csp = r.headers["content-security-policy"]
    assert "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net;" in csp
    assert "connect-src 'self'" in csp and "frame-ancestors 'none'" in csp
    # the one external script is pinned to an exact version with an integrity hash
    tag = re.search(r'<script src="([^"]+)"([^>]*)>', r.text)
    assert re.search(r"sortablejs@\d+\.\d+\.\d+/", tag.group(1))
    assert 'integrity="sha384-' in tag.group(2) and 'crossorigin="anonymous"' in tag.group(2)


@pytest.mark.skipif(not shutil.which("node"), reason="node not installed")
def test_page_script_parses(tmp_path):
    f = tmp_path / "auto.js"
    f.write_text(_script(), encoding="utf-8")
    r = subprocess.run(["node", "--check", str(f)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr[:2000]


def test_browser_storage_holds_preferences_and_the_dashboard_admin_key_only():
    s = _script()
    keys = set(re.findall(r'store\("([^"]+)"', s)) | set(re.findall(r"store\((ADMIN_KEY)", s))
    assert keys <= {"fr-theme", "fr.auto.ws", "fr.auto.project", "fr.auto.memproject",
                    "fr.auto.memscope", "fr.auto.qview", "ADMIN_KEY"}, keys
    # the admin key is the dashboard's own (same origin), asked for only when
    # client_auth.protect_admin is on; the operator key is never stored
    assert 'const ADMIN_KEY = "foundryAdminKey"' in s
    assert "sessionStorage" not in s
    assert '$("#login-key").value = "";' in s


def test_page_uses_the_config_store_contract():
    s = _script()
    # every store write carries the CSRF header and the signed-in operator as actor
    assert 'o.headers["x-foundry-automation"] = "1"' in s
    assert 'o.headers["x-foundry-actor"] = S.me.actor' in s
    for path in ('"automation/projects"', '"/draft"', '"/diff"', '"/deploy"', '"/draft/reset"',
                 '"/revisions?limit=30"', '"/rollback"', '"automation/schema"',
                 '"automation/role-rules/test-path"', 'AP("_router")'):
        assert path in s, path
    # route order and backend on/off: live writes after a snapshot (contract v1.2)
    assert 'adminApi("snapshots", {method: "POST"' in s
    assert 'adminApi("personas", {method: "POST"' in s and "model_allowlist" in s
    assert 'adminApi("backends/enabled", {method: "POST"' in s
    assert "STUB" not in s and "LOCAL_DRAFT" not in s


def test_endpoints_need_the_operator_session_and_csrf(client, op):
    client.cookies.clear()
    assert client.get("/control/api/automation/usage").status_code == 401
    assert client.get("/control/api/automation/seed").status_code == 401
    r = client.post("/control/api/login", json={"key": OP_KEY})
    csrf = {"x-foundry-csrf": r.json()["csrf"]}
    # read-only POSTs still need the CSRF header
    assert client.post("/control/api/automation/check", json={"doc": {}}).status_code == 403
    assert client.post("/control/api/automation/check", json={"doc": {}}, headers=csrf).status_code == 200


def test_seed_usage_dry_run_check_and_diff(client, op):
    u = client.get("/control/api/automation/usage")
    assert u.status_code == 200, u.text
    assert isinstance(u.json()["usage"], dict)
    doc = client.get("/control/api/automation/seed").json()["doc"]
    assert [r["id"] for r in doc["roles"]][:2] == ["implementer", "lead"]
    assert doc["role_rules"]["implementer"]["permissions"]["edit"] == "allow"
    assert doc["hard_denies"]

    doc["personas"]["P"] = {"routes": [{"backend": "nowhere", "model": "m", "enabled": True}]}
    doc["roles"][0]["persona"] = "P"
    r = client.post("/control/api/automation/dry-run", headers=op,
                    json={"doc": doc, "role": "implementer"}).json()["results"][0]
    assert r["chosen"] is None and "not in the pool" in r["hops"][0]["reason"]
    some = client.post("/control/api/automation/dry-run", headers=op,
                       json={"doc": doc, "roles": ["implementer", "lead"]}).json()
    assert [x["role"] for x in some["results"]] == ["implementer", "lead"]
    assert client.post("/control/api/automation/dry-run", headers=op,
                       json={"doc": "nope"}).status_code == 400

    doc["role_rules"]["implementer"]["edit_paths"]["allow"] = ["C:/Roblox/AnimalUprising/**"]
    w = client.post("/control/api/automation/check", headers=op, json={"doc": doc}).json()["warnings"]
    assert any(x["role"] == "implementer" and "shell" in x["message"] for x in w)

    after = {**doc, "backends": {**doc["backends"], "x": {"enabled": False}}}
    ch = client.post("/control/api/automation/diff", headers=op,
                     json={"before": doc, "after": after}).json()["changes"]
    assert [c["path"] for c in ch] == ["backends.x"]


def test_store_endpoints_the_page_calls_answer(client):
    """The calls the page makes, against the merged config store."""
    hdr = {"X-Foundry-Automation": "1", "X-Foundry-Actor": "victor"}
    assert "fields" in client.get("/admin/api/automation/schema").json()
    assert "projects" in client.get("/admin/api/automation/projects").json()
    d = client.get("/admin/api/automation/projects/animaluprising/draft").json()
    doc = d["doc"]
    doc.setdefault("settings", {}).setdefault("limits", {})["max_attempts"] = 5
    r = client.put("/admin/api/automation/projects/animaluprising/draft",
                   json={"doc": doc, "etag": d["etag"]}, headers=hdr)
    assert r.status_code == 200, r.text
    ch = client.get("/admin/api/automation/projects/animaluprising/diff").json()["changes"]
    assert any(c["path"] == "limits.max_attempts" for c in ch)
    t = client.post("/admin/api/automation/role-rules/test-path", headers=hdr, json={
        "project": "animaluprising", "role": "implementer", "op": "bash",
        "command": "git push --force origin main"}).json()
    assert t["decision"] == "deny"


def test_dashboard_and_control_center_link_to_automation():
    static = PAGE.parent
    assert "/ui/automation" in (static / "index.html").read_text(encoding="utf-8")
    assert "/ui/automation" in (static / "control.html").read_text(encoding="utf-8")


# ------------------------------------------------------------------ queue board (A2)

@pytest.mark.skipif(not shutil.which("node"), reason="node not installed")
def test_board_helpers_map_states_lanes_and_drags(tmp_path):
    """The board's pure logic, run in node against the real inline script:
    column mapping (folded states keep their true pill, PAUSED sits in its
    resume column, terminal states are not columns), swimlanes by track, drag
    legality (only moves with an existing typed action), and the global
    position a drop means."""
    from tests.test_control_ui_behavior import between, run_js
    s = _script()
    block = between(s, "/*qboard-start*/", "/*qboard-end*/")
    out = run_js(tmp_path, block + """
const rows = [
  {task_id: "A", state: "QUEUED", track: ""},
  {task_id: "B", state: "CHANGES_REQUIRED", track: "ui"},
  {task_id: "C", state: "PAUSED", live: {resume_state: "VERIFYING"}, track: "ui"},
  {task_id: "D", state: "CANCELLED", track: ""},
  {task_id: "E", state: "COMPLETE", track: ""},
  {task_id: "F", state: "FAILED", track: ""},
];
const out = {
  cols: rows.map(x => [x.task_id, boardColumn(x)]),
  lanes: boardLanes(rows).map(([k, xs]) => [k, xs.map(x => x.task_id)]),
  reorder: dragAction({state: "QUEUED"}, "QUEUED", "QUEUED"),
  unblock: dragAction({state: "FAILED"}, "BLOCKED", "READY"),
  illegal: [dragAction({state: "READY"}, "READY", "IMPLEMENTING"),
            dragAction({state: "IMPLEMENTING"}, "IMPLEMENTING", "QUEUED"),
            dragAction({state: "READY"}, "READY", "READY")],
  posAfter: boardDropPosition([{task_id: "A"}, {task_id: "B"}, {task_id: "C"}], "A", "B", "C"),
  posTop: boardDropPosition([{task_id: "A"}, {task_id: "B"}, {task_id: "C"}], "C", null, "A"),
  posOnly: boardDropPosition([{task_id: "A"}], "A", null, null),
};
console.log(JSON.stringify(out));
""")
    assert dict(out["cols"]) == {"A": "QUEUED", "B": "IMPLEMENTING", "C": "VERIFYING",
                                 "D": None, "E": None, "F": "BLOCKED"}
    assert out["lanes"] == [["ui", ["B", "C"]], ["", ["A", "D", "E", "F"]]]
    assert out["reorder"] == {"kind": "queue_reorder"}
    assert out["unblock"] == {"kind": "unblock"}
    assert out["illegal"] == [None, None, None]
    # moving A after B in [A, B, C]: remove A -> [B, C], insert at 2 -> [B, A, C]
    assert out["posAfter"] == 2 and out["posTop"] == 1 and out["posOnly"] == 1


def test_queue_page_has_the_board_the_toggle_and_no_complete_requeue():
    s = _script()
    # the board/table toggle persists, CANCELLED and COMPLETE collapse (V9)
    assert 'store("fr.auto.qview"' in s and 'data-act="q-view"' in s
    assert 'groupHtml("COMPLETE"' in s and 'groupHtml("CANCELLED"' in s
    # drag issues typed actions only; the queue ops hit the real endpoints
    assert '"queue_reorder"' in s and '"queue_approve"' in s and '"queue_deps"' in s
    assert 'api("queue?project="' in s and 'api("inbox")' in s
    # re-queue is offered for CANCELLED only: passed work is never rerun
    assert 'st === "CANCELLED"' in s and '["CANCELLED", "COMPLETE"].includes(st)' not in s
    # the PM inbox pieces (A3)
    assert "Resolved by the Lead this week" in s and "escalation_reason" in s
