"""The Distributed Foundry GUI pieces, at the pages and at the routes their buttons call:
Automation → Tracks (host / tunnel pills, the Lane tunnels card, Slot pickers, Bring home and
Move slot) and Status (a seat's host and tunnel); the Studio slots form's os / studio selects;
the router's Fleet and Fleet → Setup tabs and the Lanes checklist (Access → Machines); the OS
and ssh user of Add a Studio slot / Add a workstation and the Windows install command; the
setup wizard's Kilo / OpenCode choice; the Foundry Client's Lane agent settings and Platform
check (GET /api/doctor/platform)."""

import copy
import json
import pathlib
import re
import shutil
import subprocess
import sys

import pytest
from fastapi.testclient import TestClient

from foundry_automation import document as D
from foundry_router.control_center import ACTION_KINDS
from tests.test_control_center import cc  # noqa: F401

ROOT = pathlib.Path(__file__).resolve().parents[1]
STATIC = ROOT / "foundry_router" / "ui" / "static"
CLIENT_HTML = ROOT / "foundry_client" / "static" / "client.html"


def _read(p):
    return p.read_text(encoding="utf-8")


def _between(s, a, b):
    i = s.index(a)
    return s[i:s.index(b, i)]


# ----------------------------------------------------------------------- Automation page

def test_tracks_tab_draws_host_tunnel_and_the_lane_buttons():
    s = _read(STATIC / "automation.html")
    card = _between(s, "function trackCard(t, st)", "function studioTimeline")
    assert "hostPill(t, st)" in card and '"tr-home"' in card and '"tr-slot"' in card
    assert "lane_tunnels" in _between(s, "function hostPill", "function slotOptions")
    assert "function laneTunnelsCard(st)" in s and "${st ? laneTunnelsCard(st) : \"\"}" in s
    click = _between(s, "async function trackClick(a, b)", "$(\"#main\").addEventListener(\"change\"")
    # the buttons file the action kinds the router takes
    for kind in ("track_bring_home", "track_set_slot", "track_start"):
        assert f'"{kind}"' in click and kind in ACTION_KINDS
    assert "force_local: true" in click and 'slot: String(f.get("slot") || "") || "none"' in click
    assert '...(slot ? {slot} : {})' in click and '...(used.length ? {slots} : {})' in click
    assert 'name="slot"' in click and ".trp-slot" in click
    # the proposal box gets a Slot select per track
    assert 'class="trp-slot"' in _between(s, "function planHtml(plan, st)", "async function renderTracks")


def test_status_seats_show_host_and_lane_tunnel():
    s = _read(STATIC / "automation.html")
    seat = _between(s, "function seatWhere(s)", "async function renderBoard")
    assert "s.host" in seat and "s.lane_tunnel" in seat and "seatWhere(s)" in seat


def test_slots_form_draws_enum_fields_as_selects(client):
    s = _read(STATIC / "automation.html")
    form = _between(s, "function slotsHtml(f)", "function fieldHtml(f)")
    assert "else if (sf.enum) c = `<select ${common}>" in form
    assert "else if (sf.enum) { v = t.value;" in form
    schema = client.get("/admin/api/automation/schema").json()
    fields = {f["key"]: f for f in schema["fields"]}
    assert fields["slots.*.os"]["type"] == "enum" and fields["slots.*.os"]["enum"] == ["linux", "windows"]
    assert fields["slots.*.studio"]["enum"] == ["container", "native"]


def test_a_slot_os_outside_the_choice_is_refused_by_the_settings_store(app):
    from foundry_automation.schema import FIELDS, check_value
    f = next(x for x in FIELDS if x.key == "slots.*.os")
    assert check_value(f, "windows") is None and check_value(f, "macos")
    st = app.state.services.automation
    d = D.empty("game")
    d["settings"]["roles"] = {"implementer": "Foundry-Kilo-Local"}
    d["settings"]["slots"] = {"slot-w": {"os": "windows", "studio": "native", "lanes": True}}
    assert st.validate("game", d)["ok"] is True
    d["settings"]["slots"]["slot-w"]["os"] = "macos"
    v = st.validate("game", d)
    assert v["ok"] is False and "slots.slot-w.os" in json.dumps(v["errors"])


# ----------------------------------------------------------------------- the router UI

def test_fleet_tabs_are_in_the_navigation_and_call_real_routes(app):
    s = _read(STATIC / "index.html")
    tabs = _between(s, "const TABS = {", "const GROUPS = [")
    assert '"Fleet":          tabFleet,' in tabs and '"Fleet setup":    tabFleetSetup,' in tabs
    groups = _between(s, "const GROUPS = [", "];")
    order = re.findall(r'^  \["([^"]+)"', groups, re.M)
    assert order.index("Fleet") == order.index("Automation") + 1 and order.index("Fleet") < order.index("Access")
    assert '["Fleet",       ["Fleet", "Fleet setup"]]' in groups
    assert '"Fleet setup": "Setup"' in s                        # shown as "Setup"
    fleet = _between(s, "async function tabFleet()", "function bindFleet(root)")
    assert 'api("fleet")' in fleet and "data-fl-lanes" in s and "data-fl-machines" in s
    paths = {getattr(r, "path", "") for r in app.routes}
    assert "/admin/api/fleet" in paths
    assert "/admin/api/automation/projects/{project}/draft" in paths
    assert "/admin/api/machines/{name}/lanes" in paths and "/admin/api/machines/{name}/code" in paths
    setup = _between(s, "async function tabFleetSetup()", "// Access → Secrets")
    for bit in ("fleetDiagram()", "foundry-client doctor", "foundry-client doctor --platform",
                "data-fs-kind", "kind: sel.value", "/ui/setup#game"):
        assert bit in setup, bit
    assert "<svg class=\"fl-diagram\"" in s and ".fl-diagram .bx { fill:var(--panel-2)" in s


def test_lanes_checklist_has_the_four_steps_and_the_way_back():
    s = _read(STATIC / "index.html")
    ck = _between(s, "async function openLanesChecklist(name)", "// Fleet → Setup")
    for bit in ('"ln-keys-on"', '"ln-set-on"', '"ln-code"', '"ln-remote-on"', '"ln-recheck"',
                '"ln-remote-off"', '"ln-set-off"', '"ln-keys-off"', "Bring its running lanes home first",
                "secret store must be <b>unlocked</b>", "slots[sid].lanes = v",
                "remote_lanes: v", "installHtml(r, os)"):
        assert bit in ck, bit
    # the Machines tab's Lanes button opens it
    mach = _between(s, "async function tabMachines()", "/* ------------------------------------------------------------ fleet -- */")
    assert "openLanesChecklist(b.dataset.mcLanes)" in mach
    # the draft edit sends the Automation page's CSRF header
    assert '"X-Foundry-Automation": "1"' in _between(s, "function autoApi(", "function copyBtn(")


def test_the_draft_edit_the_checklist_makes_is_accepted(app, client):
    """What automationDraftEdit does for step 2 and step 4: GET the draft, set the value,
    PUT it back with the etag and the X-Foundry-Automation header."""
    d = D.empty("proj")
    d["settings"]["roles"] = {"implementer": "Foundry-Kilo-Local"}
    d["settings"]["slots"] = {"slot-2": {"container": "studio-slot", "host": "studio@10.0.0.2"}}
    app.state.services.automation._append("proj", d, "test", "", "test")
    cur = client.get("/admin/api/automation/projects/proj/draft").json()
    doc = copy.deepcopy(cur["doc"])
    doc["settings"]["slots"]["slot-2"]["lanes"] = True
    doc["settings"]["automation"] = {**(doc["settings"].get("automation") or {}), "remote_lanes": True}
    doc["settings"]["client"] = {**(doc["settings"].get("client") or {}), "kind": "opencode"}
    assert client.put("/admin/api/automation/projects/proj/draft",
                      json={"doc": doc, "etag": cur["etag"]}).status_code == 403   # no header
    r = client.put("/admin/api/automation/projects/proj/draft", headers={"X-Foundry-Automation": "1"},
                   json={"doc": doc, "etag": cur["etag"]})
    assert r.status_code == 200, r.text
    assert r.json()["validation"]["ok"] is True, r.json()["validation"]
    got = client.get("/admin/api/automation/projects/proj/draft").json()["doc"]["settings"]
    assert got["slots"]["slot-2"]["lanes"] is True and got["automation"]["remote_lanes"] is True
    assert got["client"]["kind"] == "opencode"


def test_add_slot_and_workstation_send_os_and_ssh_user_and_the_code_shows_windows():
    s = _read(STATIC / "index.html")
    mach = _between(s, "async function tabMachines()", "/* ------------------------------------------------------------ fleet -- */")
    assert 'id="mc-os"' in mach and 'id="mc-user"' in mach and 'id="mw-os"' in mach and 'id="mw-user"' in mach
    assert "body.ssh_user = user" in mach and ", os};" in mach
    assert 'A Linux machine (a PC, or a Proxmox LXC) that runs' not in mach
    assert "install\\\\workstation.ps1" in mach
    inst = _between(s, "function installHtml(r, os)", "function showInstallCode(")
    assert 'os === "windows" && i.windows' in inst and "typeof i.windows === \"string\"" in inst


@pytest.mark.skipif(not shutil.which("node"), reason="node is not installed")
def test_install_commands_pick_windows_or_linux_in_the_page(tmp_path):
    """Run the page's own installHtml in node: a Windows machine gets the "windows" command
    (a string or a table of them), and a router without one says what to run instead."""
    s = _read(STATIC / "index.html")
    fn = _between(s, "function installHtml(r, os)", "function showInstallCode(")
    js = ("const esc = s => String(s ?? '').replace(/[&<>\"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;'}[c]));\n"
          "const _tsText = v => String(v);\nconst copyBtn = t => '[copy]';\n" + fn +
          "\nconst base = {code: 'FNDR-AAAA-BBBB-CCCC', code_expires: 1, machine: {kind: 'slot'}, install: {router_url: 'http://r:8000', proxmox_host: 'bash create-slot-lxc.sh', inside_lxc: 'bash setup-host.sh'}};\n"
          "const out = {linux: installHtml(base, 'linux'),"
          " win: installHtml({...base, install: {...base.install, windows: 'pwsh slot.ps1 -Code X'}}, 'windows'),"
          " wintab: installHtml({...base, install: {...base.install, windows: {slot_ps1: 'pwsh a.ps1'}}}, 'windows'),"
          " winnone: installHtml(base, 'windows')};\nconsole.log(JSON.stringify(out));\n")
    p = tmp_path / "inst.js"
    p.write_text(js, encoding="utf-8")
    r = subprocess.run(["node", str(p)], capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert "bash setup-host.sh" in out["linux"] and "pwsh" not in out["linux"]
    assert "pwsh slot.ps1 -Code X" in out["win"] and "setup-host.sh" not in out["win"]
    assert "pwsh a.ps1" in out["wintab"] and "slot ps1" in out["wintab"]
    assert "install\\slot.ps1" in out["winnone"] and "setup-host.sh" not in out["winnone"]


NODE_STUBS = r"""
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
const _tsText = v => String(v); const when = v => String(v); const toast = () => {};
const CSS = {escape: s => s}; let current = ""; const render = () => {}; const closeModal = () => {};
const _adminKey = () => ""; const STATE_CLS = {};
function el() { return {innerHTML: "", value: "kilo", dataset: {}, onclick: null,
  querySelectorAll: () => [], querySelector: () => el()}; }
const MAIN = el(); let MODAL = "";
const $ = (sel, root) => sel === "#main" ? MAIN : el();
const $$ = () => [];
const openModal = (title, html) => { MODAL = title + "\n" + html; return el(); };
const CALLS = [];
"""


def _node(tmp_path, js):
    p = tmp_path / "page.js"
    p.write_text(js, encoding="utf-8")
    r = subprocess.run(["node", str(p)], capture_output=True, text=True, encoding="utf-8", timeout=120)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout.strip().splitlines()[-1])


@pytest.mark.skipif(not shutil.which("node"), reason="node is not installed")
def test_fleet_tabs_and_lanes_checklist_render_the_real_payload(app, client, cc, tmp_path):  # noqa: F811
    """The Fleet tab, Fleet → Setup and the Lanes checklist, run in node on the payload
    GET /admin/api/fleet really returns (tests/test_fleet.py's machines and heartbeats)."""
    from tests.test_fleet import _fleet_env
    _fleet_env(app, client)
    fleet = client.get("/admin/api/fleet").json()
    machines = client.get("/admin/api/machines").json()
    draft = client.get("/admin/api/automation/projects/proj/draft").json()
    s = _read(STATIC / "index.html")
    block = _between(s, "/* ------------------------------------------------------------ fleet -- */",
                     "// Access → Secrets (secret_store.py")
    js = NODE_STUBS + f"""
const DATA = {{fleet: {json.dumps(fleet)}, machines: {json.dumps(machines)}, draft: {json.dumps(draft)}}};
async function api(path, opts = {{}}) {{
  CALLS.push([path, (opts || {{}}).method || "GET"]);
  if (path === "fleet") return DATA.fleet;
  if (path === "machines") return DATA.machines;
  if (path.startsWith("automation/projects/")) return DATA.draft;
  throw new Error("unexpected " + path);
}}
{block}
(async () => {{
  const out = {{}};
  await tabFleet(); out.fleet = MAIN.innerHTML;
  await tabFleetSetup(); out.setup = MAIN.innerHTML;
  await openLanesChecklist("slot-2"); out.lanes2 = MODAL;
  await openLanesChecklist("slot-3"); out.lanes3 = MODAL;
  out.calls = CALLS;
  console.log(JSON.stringify(out));
}})().catch(e => {{ console.error(e.stack || e); process.exit(1); }});
"""
    out = _node(tmp_path, js)
    f = out["fleet"]
    assert "ws-pc" in f and "slot-2" in f and "slot-3" in f and ">junction<" in f
    assert "OpenCode server is off" in f and "remote_lanes on" in f        # the warnings
    assert "✗ lane keys" in f and "✓ agent answers" in f and "tunnel down" in f
    assert 'data-fl-lanes="slot-2"' in f and "/ui/automation#tracks" in f
    assert "lane-a" in f and "S-1" in f
    st = out["setup"]
    assert 'class="fl-diagram"' in st and "foundry-client doctor --platform" in st
    assert 'data-fs-kind="proj"' in st and '<option value="opencode" selected>' in st
    l2 = out["lanes2"]
    assert l2.startswith("Lanes on slot-2") and 'id="ln-keys-on"' in l2   # step 1 to do
    assert "on (deployed)" in l2                                          # steps 2 and 4 done
    assert "Running on slot-2 now" in l2 and "lane-a" in l2               # bring it home first
    l3 = out["lanes3"]
    assert "keys made" in l3 and 'id="ln-set-on"' in l3 and 'id="ln-keys-off"' in l3
    assert ["fleet", "GET"] in out["calls"] and ["machines", "GET"] in out["calls"]


@pytest.mark.skipif(not shutil.which("node"), reason="node is not installed")
def test_tracks_and_status_cards_render_lane_fields(tmp_path):
    from tests.test_fleet import TRACKS
    s = _read(STATIC / "automation.html")
    tracks = _between(s, "const TRACK_CLS = ", "async function trackAction(")
    seats = _between(s, "function bdMins(m)", "async function renderBoard")
    st = json.loads(json.dumps(TRACKS["proj"]))
    for t in st["tracks"]:
        t.setdefault("worktree", "C:/w/.lanes/" + t["name"])
        t.setdefault("branch", "track/" + t["name"])
    plan = {"plan_id": "trp-0123456789ab", "agents": 2, "max_agents": 3, "main": None,
            "tracks": [{"name": "b1", "sprints": ["B1"], "needs": ["blender"]}], "notes": []}
    js = NODE_STUBS + "const S = {tr: {}};\n" + tracks + "\n" + seats + f"""
const st = {json.dumps(st)};
const out = {{cards: st.tracks.map(t => trackCard(t, st)).join("\\n"), tunnels: laneTunnelsCard(st),
             plan: planHtml({json.dumps(plan)}, st), old: trackCard(st.tracks[0], {{tracks: st.tracks}}),
             seat: seatRow({{seat: "slot-2", kind: "slot", idle: false, lane: "lane-a", host: "slot-2",
                             lane_tunnel: "down: connection refused", sprint: "S-1", state: "IMPLEMENTING"}})}};
console.log(JSON.stringify(out));
"""
    out = _node(tmp_path, js)
    c = out["cards"]
    assert "on slot-2" in c and "tunnel up" in c and 'data-act="tr-home"' in c and 'data-act="tr-slot"' in c
    assert ">here<" in c and "Studio: slot-3" in c                      # here-b: local, Studio on slot-3
    t = out["tunnels"]
    assert "Lane tunnels" in t and "connection refused" in t and "disk 91%" in t and "lane-a" in t
    assert 'class="trp-slot"' in out["plan"] and "slot-2 · lanes on: worktree and sessions there" in out["plan"]
    assert 'data-act="tr-home"' not in out["old"]       # a coordinator without lane kinds: no buttons
    assert "on slot-2" in out["seat"] and "tunnel down" in out["seat"]


# ----------------------------------------------------------------------- setup wizard

def test_setup_wizard_game_step_writes_the_client_kind_and_links_fleet_setup(app, client):
    s = _read(STATIC / "setup.html")
    game = _between(s, "VIEWS.game = () =>", "VIEWS.machines = () =>")
    assert 'id="gm-client"' in game and '<option value="opencode">OpenCode</option>' in game
    assert "\\n[client]\\nkind = ${q(kind)}\\n" in game
    assert s.count('href="/ui#Fleet%20setup"') == 2
    # the exact TOML the page builds is accepted, and deploys with [client] kind
    toml = ('schema_version = 1\nid = "newgame"\nname = "New game"\n\n[workspace]\nbranch = "main"\n\n'
            '[client]\nkind = "opencode"\n\n[roles]\nimplementer = "Foundry-Kilo-Local"\n')
    H = {"X-Foundry-Automation": "1"}
    r = client.post("/admin/api/automation/projects/newgame/import", json={"toml": toml}, headers=H)
    assert r.status_code == 200, r.text
    d = client.get("/admin/api/automation/projects/newgame/draft").json()
    assert d["doc"]["settings"]["client"]["kind"] == "opencode"
    v = client.post("/admin/api/automation/projects/newgame/validate", json={"doc": d["doc"]}, headers=H).json()
    assert v["ok"], v
    r = client.post("/admin/api/automation/projects/newgame/deploy", json={"etag": d["etag"], "note": "t"}, headers=H)
    assert r.status_code == 200, r.text
    dep = app.state.services.automation.deployed("newgame")["doc"]["settings"]
    assert dep["client"]["kind"] == "opencode"


# ----------------------------------------------------------------------- Foundry Client

@pytest.fixture()
def capi(tmp_path, monkeypatch):
    from foundry_client import api, config, secrets
    from foundry_client.manager import Manager
    from tests.client.test_client import FakeProc
    monkeypatch.setenv("FOUNDRY_CLIENT_CONFIG", str(tmp_path / "client.toml"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setattr(secrets, "_kr", lambda: None)
    c = copy.deepcopy(config.DEFAULTS)
    game = tmp_path / "game"
    game.mkdir()
    (tmp_path / "profiles").mkdir()
    c["client"]["state_dir"] = str(tmp_path / "state")
    c["workspace"].update(game=str(game), profiles=str(tmp_path / "profiles"))
    c["components"]["kilo"]["path"] = sys.executable
    c["components"]["supervisor"]["enabled"] = False
    m = Manager(c, popen=lambda argv, **kw: FakeProc(argv))
    tc = TestClient(api.create_app(m, "t" * 40, cfg_path=tmp_path / "client.toml"),
                    base_url="http://127.0.0.1")
    return tc, {"X-Client-Token": "t" * 40}, m, tmp_path


def test_client_page_has_the_lane_agent_group_and_the_platform_button():
    s = _read(CLIENT_HTML)
    fields = _between(s, "const FIELDS = [", "];\nconst get")
    assert '["Lane agent (only on a slot)", [["lane.enabled"' in fields
    for k in ('"lane.root"', '"lane.secrets_file"', '"lane.listen"'):
        assert k in fields, k
    assert '"select", ["127.0.0.1", "0.0.0.0", "::1", "::"]' in fields
    assert 'if (t === "select")' in s
    assert 'id="platform"' in s and 'api("doctor/platform")' in s


def test_client_settings_put_saves_the_lane_table_and_refuses_a_bad_one(capi):
    tc, h, m, tmp = capi
    rev = tc.get("/api/settings", headers=h).json()["rev"]
    lane = {"enabled": True, "root": str(tmp / "lanes"), "secrets_file": "", "listen": "127.0.0.1"}
    r = tc.put("/api/settings", headers=h, json={"settings": {"lane": lane}, "rev": rev})
    assert r.status_code == 200, r.text
    assert m.cfg["lane"] == lane
    from foundry_client import config
    assert config.load(tmp / "client.toml")["lane"]["root"] == str(tmp / "lanes")
    bad = tc.put("/api/settings", headers=h, json={"settings": {"lane": {"listen": "10.0.0.5"}}})
    assert bad.status_code == 400 and "lane.listen" in bad.json()["error"]
    bad = tc.put("/api/settings", headers=h, json={"settings": {"lane": {"enabled": True, "root": ""}}})
    assert bad.status_code == 400 and "lane.root" in bad.json()["error"]


def test_platform_check_route_returns_doctor_platform_checks(capi, monkeypatch):
    tc, h, m, tmp = capi
    assert tc.get("/api/doctor/platform").status_code == 401       # needs the client token
    from foundry_client import doctor
    real_fn, seen = doctor.platform_checks, []

    def fake():
        seen.append(1)
        return {"ok": False, "facts": {"os": "Windows"}, "checks": [
            {"id": "git", "label": "git", "status": "fail", "detail": "git was not found"}]}
    monkeypatch.setattr(doctor, "platform_checks", fake)
    r = tc.get("/api/doctor/platform", headers=h)
    assert r.status_code == 200 and seen == [1]
    assert r.json()["checks"][0]["status"] == "fail" and r.json()["ok"] is False
    monkeypatch.setattr(doctor, "platform_checks", real_fn)
    real = tc.get("/api/doctor/platform", headers=h).json()        # the real one, read-only
    assert {"ok", "facts", "checks"} <= set(real) and any(c["id"] == "os" for c in real["checks"])
