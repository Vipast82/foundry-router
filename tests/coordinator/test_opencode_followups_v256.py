"""OpenCode follow-ups (v0.181.256): the local MCP servers listed switched off in
opencode.jsonc with an [automation] switch each, the Lead-off abort retry, the control page's
agent label, the OpenCode session-start hold, reviewer context files in the generic adapter,
and OpenCode 2.x's event stream (with part ids that match the message list)."""

import json
import os
import threading

import pytest

from foundry_automation import agent_backends as AB
from foundry_automation import jsonc
from foundry_automation import schema as SCH
from foundry_coordinator import agent_config as AC
from foundry_coordinator import kilo_config as KC
from foundry_coordinator.adapters import oc2 as O
from foundry_coordinator.devtools.fake_opencode import FakeOpenCode2

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class _Prof:
    def __init__(self, ws, client, raw=None, automation=None):
        self.workspace, self.client, self.id = ws, client, "pilot"
        self.raw = raw or {}
        if automation is not None:
            self.raw["automation"] = automation

    def persona(self, role, task):
        return "Foundry-Kilo-Local"

    def secret(self, t, k):
        return ""


GLOBAL_KILO = """{"mcp": {
  "Roblox_Studio": {"type": "local", "enabled": false, "timeout": 60000,
                    "command": ["cmd.exe", "/d", "/c", "C:/Tools/studio-mcp.cmd"]},
  "robloxstudio-mcp": {"type": "local", "enabled": false, "timeout": 60000,
                       "command": ["cmd.exe", "/d", "/c", "npx.cmd", "-y", "@x/robloxstudio-mcp@3.4.2"]},
  "blender-mcp": {"type": "local", "enabled": false, "timeout": 60000,
                  "command": ["cmd.exe", "/d", "/c", "uvx", "blender-mcp"],
                  "environment": {"BLENDER_TOKEN": "never-copied-123"}},
  "Media_Gen": {"type": "remote", "url": "http://192.168.0.112:9000/mcp", "enabled": false,
                "headers": {"authorization": "Bearer never-copied-456"}},
  "git": {"type": "local", "enabled": true, "command": ["python.exe", "-m", "git_mcp"]}}}
"""
PROJECT_KILO = """{"mcp": {
  "foundry-media": {"type": "local", "enabled": true, "timeout": 120000,
                    "command": ["powershell.exe", "-NoProfile", "-File", "C:/m/foundry-media.ps1"]},
  "Roblox_Studio": {"type": "local", "enabled": true,
                    "command": ["cmd.exe", "/d", "/c", "C:/Tools/studio-mcp.cmd"]}},
 "provider": {"foundry": {"npm": "@ai-sdk/openai-compatible",
   "options": {"baseURL": "http://router/v1", "apiKey": "{env:FOUNDRY_KEY_KILO}"},
   "models": {"Foundry-Kilo-Local": {"limit": {"context": 262144, "output": 8192}}}}}}
"""


def _setup(tmp_path, automation=None):
    gk = tmp_path / "global-kilo.jsonc"
    gk.write_text(GLOBAL_KILO, encoding="utf-8")
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "kilo.jsonc").write_text(PROJECT_KILO, encoding="utf-8")
    prof = _Prof(str(ws), {"kind": "opencode", "agent": "build"}, automation=automation or {})
    return prof, str(gk), str(ws / "kilo.jsonc")


# -- 1. local MCP servers: listed, switched off ------------------------------------------------

def test_agent_config_lists_the_local_mcp_servers_switched_off(tmp_path):
    prof, gk, pk = _setup(tmp_path)
    out = AC.write(prof, provider_from=pk, mcp_from=gk, apply=True)
    cfg = jsonc.parse(open(out["path"], encoding="utf-8").read())
    mcp = cfg["mcp"]
    for name in ("Roblox_Studio", "robloxstudio-mcp", "blender-mcp", "foundry-media"):
        assert mcp[name]["type"] == "local" and mcp[name]["enabled"] is False, name
        assert mcp[name]["command"]
    # the project's kilo.jsonc wins over the global file; environment values never copied
    assert mcp["foundry-media"]["timeout"] == 120000
    assert "environment" not in mcp["blender-mcp"]
    text = open(out["path"], encoding="utf-8").read()
    assert "never-copied" not in text
    assert "git" not in mcp                       # only the servers Kilo runs for the game
    assert mcp["Media_Gen"] == {"type": "remote", "url": "http://192.168.0.112:9000/mcp",
                                "enabled": False}


def test_a_switch_that_is_on_lists_its_server_on(tmp_path):
    prof, gk, pk = _setup(tmp_path, {"opencode_mcp_studio": True})
    out = AC.write(prof, provider_from=pk, mcp_from=gk)
    mcp = jsonc.parse(out["text"])["mcp"]
    assert mcp["Roblox_Studio"]["enabled"] is True and mcp["robloxstudio-mcp"]["enabled"] is True
    assert mcp["blender-mcp"]["enabled"] is False and mcp["foundry-media"]["enabled"] is False


def test_switches_ship_off_and_have_schema_controls():
    for key, names in AB.OPENCODE_LOCAL_MCP.items():
        assert SCH.AUTOMATION_DEFAULTS[key] is False
        f = SCH.find_field(f"automation.{key}")
        assert f is not None and f.type == "bool" and f.default is False
        assert "Kilo projects are not affected" in f.description
    assert AB.OPENCODE_LOCAL_MCP["opencode_mcp_studio"] == ("Roblox_Studio", "robloxstudio-mcp")


def test_a_track_of_an_opencode_project_keeps_switched_off_servers_off(tmp_path):
    prof, gk, pk = _setup(tmp_path)
    src = AC.write(prof, provider_from=pk, mcp_from=gk, apply=True)["path"]
    wt = tmp_path / "track"
    path = KC.track_kilo_config({"worktree": str(wt), "needs": ["studio", "blender"]},
                                profile=prof, role_rules=None, source=src)
    assert os.path.basename(path) == "opencode.jsonc"     # OpenCode reads no kilo.jsonc
    assert not (wt / "kilo.jsonc").exists()
    mcp = jsonc.parse(open(path, encoding="utf-8").read())["mcp"]
    assert mcp["Roblox_Studio"]["enabled"] is False and mcp["blender-mcp"]["enabled"] is False
    assert mcp["Media_Gen"]["enabled"] is True            # remote, per sprint as before
    # with the switch on, the track follows its needs like Kilo's
    prof.raw["automation"] = {"opencode_mcp_studio": True}
    path = KC.track_kilo_config({"worktree": str(wt), "needs": ["studio"]}, profile=prof,
                                role_rules=None, source=src)
    mcp = jsonc.parse(open(path, encoding="utf-8").read())["mcp"]
    assert mcp["robloxstudio-mcp"]["enabled"] is True
    # the official Studio server keeps Kilo's on-demand rule (no plan here asks for it)
    assert mcp["Roblox_Studio"]["enabled"] is False
    assert mcp["blender-mcp"]["enabled"] is False


def test_a_kilo_track_is_written_exactly_as_before(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    src = ws / "kilo.jsonc"
    src.write_text(PROJECT_KILO, encoding="utf-8")
    prof = _Prof(str(ws), {"kind": "kilo"})
    path = KC.track_kilo_config({"worktree": str(tmp_path / "t"), "needs": ["studio"]},
                                profile=prof, role_rules=None, source=str(src))
    assert os.path.basename(path) == "kilo.jsonc"
    # robloxstudio-mcp is in the global file only: a stub, as before
    assert jsonc.parse(open(path, encoding="utf-8").read())["mcp"]["robloxstudio-mcp"] == \
        {"enabled": True}
    # the pure part with no mcp_off is the same text as before
    a = KC.track_text(PROJECT_KILO, ["studio"], None, str(ws))
    b = KC.track_text(PROJECT_KILO, ["studio"], None, str(ws), mcp_off=frozenset())
    assert a["text"] == b["text"]


def test_sync_switches_the_main_opencode_file_and_never_a_kilo_file(tmp_path):
    prof, gk, pk = _setup(tmp_path)
    path = AC.write(prof, provider_from=pk, mcp_from=gk, apply=True)["path"]
    kilo_before = open(pk, encoding="utf-8").read()
    assert KC.sync_opencode_mcp(prof) == []                    # all off already
    prof.raw["automation"] = {"opencode_mcp_blender": True}
    out = KC.sync_opencode_mcp(prof, clock=lambda: 1_700_000_000)
    assert [r["changed"] for r in out] == [["blender-mcp"]] and out[0]["enabled"] == ["blender-mcp"]
    assert os.path.isfile(out[0]["backup"])
    mcp = jsonc.parse(open(path, encoding="utf-8").read())["mcp"]
    assert mcp["blender-mcp"]["enabled"] is True and mcp["Roblox_Studio"]["enabled"] is False
    prof.raw["automation"] = {}
    assert [r["changed"] for r in KC.sync_opencode_mcp(prof, clock=lambda: 1_700_000_100)] == \
        [["blender-mcp"]]
    assert open(pk, encoding="utf-8").read() == kilo_before
    # a Kilo project has no OpenCode file: nothing to do
    assert KC.sync_opencode_mcp(_Prof(str(tmp_path / "ws"), {"kind": "kilo"})) == []


def test_config_pull_syncs_when_an_opencode_switch_changes():
    src = open(os.path.join(ROOT, "foundry_coordinator", "config_pull.py"), encoding="utf-8").read()
    assert 'c["path"].startswith("automation.opencode_mcp_")' in src
    assert "self._sync_opencode_mcp(pid, body)" in src


# -- 2. Lead manager off: no AttributeError every tick ----------------------------------------

def test_lead_off_tick_logs_no_abort_retry_error_and_still_retries_a_queued_abort(tmp_path,
                                                                                  monkeypatch):
    from foundry_coordinator.lead import LeadManager
    from tests.coordinator.test_budget_policy import game
    c, P, clk = game(tmp_path)
    assert c.lead is None
    logs = []
    monkeypatch.setattr(c, "log", lambda m: logs.append(str(m)))
    c.tick()
    assert not [m for m in logs if "lead abort retry failed" in m], logs
    # an abort queued while the Lead manager was on is still retried with it off
    tried = []
    monkeypatch.setattr(LeadManager, "retry_aborts", lambda self: tried.append(1) or [])
    c.store.set_meta(LeadManager.ABORT_PENDING_KEY, json.dumps(
        [{"sid": "ses_x", "directory": "w", "project_id": "game", "ts": clk()}]))
    c.tick()
    assert not [m for m in logs if "lead abort retry failed" in m]
    assert tried or not c.store.q("SELECT 1 FROM sqlite_master WHERE name='recovery_cases'")


# -- 3. the control page names the agent server ------------------------------------------------

def test_bridge_heartbeat_names_the_agent_kind():
    from foundry_coordinator import telemetry as T
    b = T.TelemetryBridge.__new__(T.TelemetryBridge)
    b.obs = {"game": T.KiloObserver("http://127.0.0.1:4096")}
    assert b._agent_label() == "kilo"
    b.obs = {"pilot": T.Oc2Observer("http://127.0.0.1:4097")}
    assert b._agent_label() == "opencode"
    b.obs = {"game": T.KiloObserver("http://127.0.0.1:4096"),
             "pilot": T.MultiObserver([T.OpenCodeV1Observer("http://127.0.0.1:4098"),
                                       T.KiloObserver("http://127.0.0.1:4096")])}
    assert b._agent_label() == "kilo+opencode"
    html = open(os.path.join(ROOT, "foundry_router", "ui", "static", "control.html"),
                encoding="utf-8").read()
    assert 'String(k.agent || "kilo")' in html and "API as seen by the bridge" in html


# -- 4. an OpenCode restart pending holds new session starts -----------------------------------

def test_opencode_restart_pending_holds_an_opencode_project_only(tmp_path, monkeypatch):
    from foundry_coordinator import client_api, infra
    from tests.coordinator.test_budget_policy import game
    c, P, clk = game(tmp_path)

    class Client:
        def component(self, name):
            return {"name": name, "pending_restart": "safe" if name == "opencode" else "",
                    "pending_for_s": 30}
    monkeypatch.setattr(client_api, "discover", lambda timeout=None: Client())
    assert infra.agent_components(P) == ["kilo"]
    assert infra.kilo_drain(c, P) == ""                  # a Kilo project: Kilo is not pending
    P.profile.client["kind"] = "opencode"
    try:
        assert infra.agent_components(P) == ["opencode"]
        why = infra.kilo_drain(c, P)
        assert why.startswith("waiting: the OpenCode server restarts at the next safe point")
    finally:
        P.profile.client["kind"] = "kilo"


# -- 5. reviewers see the unchanged files a task names -----------------------------------------

class _Ws:
    def __init__(self, files):
        self.files = files

    def show_at(self, rev, rel):
        return self.files.get(rel)

    def blob_at(self, rev, rel):
        return "b" * 40 if rel in self.files else None


def _generic(tmp_path, tasks):
    from foundry_coordinator.adapters.project import GenericAdapter
    (tmp_path / "tasks.json").write_text(json.dumps(tasks), encoding="utf-8")

    class Prof:
        workspace = str(tmp_path)
        id = "pilot"
        tasks = {"file": "tasks.json"}
        raw: dict = {}

        def in_workspace(self, rel):
            return not rel.startswith("..") and not os.path.isabs(rel)
    a = GenericAdapter.__new__(GenericAdapter)
    a.p = Prof()
    a.cfg = {}
    return a


def test_op2_reviewer_gets_the_code_the_doc_describes(tmp_path):
    a = _generic(tmp_path, [{"id": "OP-2", "title": "Document the helper",
                             "spec": "Add a short section \"Greeting\" to docs/USAGE.md that "
                                     "explains greet() from src/pilot/greet.py with one "
                                     "example. Do not change any code.",
                             "scope": ["docs/USAGE.md"]}])
    spec = a.describe("OP-2")
    assert a.context_paths(spec) == [("src/pilot/greet.py", "named in the task spec")]
    ws = _Ws({"src/pilot/greet.py": "def greet(name):\n    return f'Hello, {name}!'\n",
              "docs/USAGE.md": "# Usage\n"})
    items = a.review_evidence(spec, "c" * 40, ws)
    assert len(items) == 1
    it = items[0]
    assert it["path"] == "src/pilot/greet.py" and it["status"] == "ok"
    assert "def greet(name)" in it["text"] and it["revision"] == "c" * 40
    assert "reviewed commit" in it["note"]


def test_context_list_globs_and_missing_files(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("A", encoding="utf-8")
    (tmp_path / "src" / "b.py").write_text("B", encoding="utf-8")
    a = _generic(tmp_path, [{"id": "T", "title": "t", "spec": "see lib/gone.py",
                             "scope": ["docs/x.md"], "context": ["src/*.py", "spec/API.md"]}])
    spec = a.describe("T")
    paths = [p for p, _ in a.context_paths(spec)]
    assert paths == ["src/a.py", "src/b.py", "spec/API.md", "lib/gone.py"]
    items = a.review_evidence(spec, "d" * 40, _Ws({"src/a.py": "A", "src/b.py": "B\x00"}))
    by = {i["path"]: i["status"] for i in items}
    # binary skipped, a listed file missing at the revision is said so, a mentioned one is not
    assert by == {"src/a.py": "ok", "spec/API.md": "missing"}


def test_au_adapter_review_evidence_is_its_own():
    from foundry_coordinator.adapters.project import GenericAdapter
    from foundry_coordinator.projects.animaluprising.adapter import AnimalUprisingAdapter
    assert AnimalUprisingAdapter.review_evidence is not GenericAdapter.review_evidence


# -- 6. OpenCode 2.x events -------------------------------------------------------------------

def test_part_ids_count_each_kind_like_2x_events_do():
    # checked live on 2.3.2: a reasoning block and the text after it both start at ordinal 0
    m = {"type": "assistant", "id": "msg_1", "model": {"id": "P", "providerID": "foundry"},
         "time": {"created": 1, "completed": 2},
         "content": [{"type": "reasoning", "text": "think"},
                     {"type": "tool", "id": "call_9", "name": "shell",
                      "state": {"status": "completed", "input": {"command": "ls"}},
                      "time": {"created": 1}},
                     {"type": "text", "text": "ok"}]}
    parts = O.to_oc1_message(m, "ses_1")["parts"]
    assert [p["id"] for p in parts] == ["msg_1:reasoning:0", "msg_1:tool:call_9", "msg_1:text:0"]
    assert all(p["sessionID"] == "ses_1" and p["messageID"] == "msg_1" for p in parts)
    ev = {"type": "session.text.started", "created": 5,
          "data": {"sessionID": "ses_1", "assistantMessageID": "msg_1", "ordinal": 0}}
    assert O.to_oc1_events(ev)[0]["properties"]["part"]["id"] == "msg_1:text:0"
    ev = {"type": "session.reasoning.delta",
          "data": {"sessionID": "ses_1", "assistantMessageID": "msg_1", "ordinal": 0,
                   "delta": "th"}}
    assert O.to_oc1_events(ev) == [{"type": "message.part.delta", "properties": {
        "sessionID": "ses_1", "messageID": "msg_1", "partID": "msg_1:reasoning:0",
        "field": "text", "delta": "th"}}]
    user = O.to_oc1_message({"type": "user", "id": "msg_0", "text": "hi"}, "ses_1")
    assert user["parts"][0]["id"] == "msg_0:part:0"


def test_other_events_ask_for_a_fresh_read_and_stream_noise_does_not():
    tool = {"type": "session.tool.called", "data": {"sessionID": "ses_1",
                                                    "assistantMessageID": "m", "id": "c"}}
    assert O.to_oc1_events(tool) == [{"type": "oc2.session.tool.called",
                                      "properties": {"sessionID": "ses_1", "oc2_refetch": True}}]
    form = {"type": "form.created", "data": {"form": {"id": "frm_1", "sessionID": "ses_2"}}}
    assert O.to_oc1_events(form)[0]["properties"]["sessionID"] == "ses_2"
    tick = {"type": "session.step.streamed", "data": {"sessionID": "ses_1"}}
    assert O.to_oc1_events(tick)[0]["properties"]["oc2_refetch"] is False
    assert O.to_oc1_events({"type": "provider.updated", "data": {}}) == []


def test_observer_reads_the_fake_event_stream_for_its_folder_only():
    from foundry_coordinator import telemetry as T
    fake = FakeOpenCode2(password="pw")
    url = fake.start()
    try:
        fake.sse_events = [
            {"type": "server.connected", "data": {}},
            {"type": "session.text.started", "location": {"directory": "C:/work/pilot"},
             "data": {"sessionID": "ses_1", "assistantMessageID": "msg_1", "ordinal": 0}},
            {"type": "session.text.delta", "location": {"directory": "C:/work/pilot"},
             "data": {"sessionID": "ses_1", "assistantMessageID": "msg_1", "ordinal": 0,
                      "delta": "Hel"}},
            {"type": "session.text.delta", "location": {"directory": "C:/other"},
             "data": {"sessionID": "ses_9", "assistantMessageID": "msg_9", "ordinal": 0,
                      "delta": "no"}},
            {"type": "session.execution.succeeded", "location": {"directory": "C:/work/pilot"},
             "data": {"sessionID": "ses_1"}}]
        o = T.Oc2Observer(url, password="pw")
        evs = list(o.events("C:/work/pilot", threading.Event()))
        assert [e["type"] for e in evs] == ["message.part.updated", "message.part.delta",
                                            "oc2.session.execution.succeeded"]
        assert o.caps["event_stream"] is True
        assert fake.requests[-1]["directory"] == "C:/work/pilot"
        # the bridge: streamed text lands on the live part, the run end asks for a read
        b = T.TelemetryBridge.__new__(T.TelemetryBridge)
        b._lock, b._live, b._dirty, b._refetch = threading.Lock(), {}, set(), set()
        b.watch = {"ses_1": {}}
        for e in evs:
            b._on_event(e)
        assert b._live["msg_1:text:0"]["text"] == "Hel"
        assert b._refetch == {"ses_1"} and "ses_1" in b._dirty
    finally:
        fake.stop()


def test_kilo_events_are_handled_as_before():
    from foundry_coordinator import telemetry as T
    b = T.TelemetryBridge.__new__(T.TelemetryBridge)
    b._lock, b._live, b._dirty, b._refetch = threading.Lock(), {}, set(), set()
    b.watch = {"ses_k": {}}
    b._on_event({"type": "message.part.updated", "properties": {
        "part": {"id": "prt_1", "sessionID": "ses_k", "type": "text", "text": "a"}}})
    b._on_event({"type": "message.part.delta", "properties": {
        "sessionID": "ses_k", "partID": "prt_1", "field": "text", "delta": "b"}})
    assert b._live["prt_1"]["text"] == "ab" and b._refetch == set()
