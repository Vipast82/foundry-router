"""OpenCode beside Kilo (v0.181.247): the agent-backend specs, the OpenCode 1.x and 2.x
clients against fake servers, the per-role ClientSet, and that Kilo's client is unchanged."""

import base64
import json
import time

import pytest

from foundry_automation import agent_backends as AB
from foundry_coordinator.adapters import build_client, build_one
from foundry_coordinator.adapters.clientset import ClientSet, for_role, worker_client_of
from foundry_coordinator.adapters.kilo import KiloClient, KiloError, foundry_denial
from foundry_coordinator.adapters.oc1 import AgentServerError, Oc1Client
from foundry_coordinator.adapters.oc2 import Oc2Client, oc1_config_view, to_oc1_messages
from foundry_coordinator.adapters.opencode import OpenCodeClient, OpenCodeV1Client
from foundry_coordinator.devtools.fake_kilo import FakeKilo
from foundry_coordinator.devtools.fake_opencode import FakeOpenCode2


# -- specs ------------------------------------------------------------------------------------

def test_kilo_spec_holds_the_values_the_stack_used_before():
    k = AB.spec("kilo")
    assert k.binaries == ("kilo", "kilo.cmd") and k.default_port == 4096
    assert k.password_env == "KILO_SERVER_PASSWORD" and k.default_username == "kilo"
    assert k.serve_env == {"KILO_DISABLE_EXTERNAL_SKILLS": "true"}
    assert k.config_name == "kilo.jsonc" and k.skills_dir == ".kilo/skills"
    assert k.default_agent == "code" and k.client_key == "kilo"
    assert k.key_env == "FOUNDRY_KEY_KILO" and k.training_source == "kilo"
    # anything that is not an agent kind (fake, scripted, "") reads as Kilo
    assert AB.spec("") is AB.KILO and AB.spec("scripted") is AB.KILO


def test_opencode_spec_keeps_claude_code_files_and_updates_out():
    o = AB.spec("opencode")
    assert o.default_port == 4097 and o.default_username == "opencode"
    assert o.config_name == "opencode.jsonc" and o.skills_dir == ".opencode/skills"
    for k in ("OPENCODE_DISABLE_CLAUDE_CODE", "OPENCODE_DISABLE_CLAUDE_CODE_SKILLS",
              "OPENCODE_DISABLE_EXTERNAL_SKILLS", "OPENCODE_DISABLE_AUTOUPDATE"):
        assert o.serve_env[k] == "true"
    env = AB.serve_env_for(o, "pw")
    assert env["OPENCODE_SERVER_PASSWORD"] == env["OPENCODE_PASSWORD"] == "pw"
    assert "opencode.jsonc" in AB.all_protected() and "kilo.jsonc" in AB.all_protected()


def test_permission_rules_translate_to_2x_names_in_order():
    rules = [{"permission": "*", "pattern": "*", "action": "allow"},
             {"permission": "bash", "pattern": "git push*", "action": "deny"},
             {"permission": "task", "pattern": "*", "action": "deny"},
             {"permission": "write", "pattern": "kilo.jsonc", "action": "deny"}]
    out = AB.oc2_rules(rules)
    assert out == [{"action": "*", "resource": "*", "effect": "allow"},
                   {"action": "shell", "resource": "git push*", "effect": "deny"},
                   {"action": "subagent", "resource": "*", "effect": "deny"},
                   {"action": "edit", "resource": "kilo.jsonc", "effect": "deny"}]
    assert AB.oc1_rules(out)[1] == {"permission": "bash", "pattern": "git push*",
                                    "action": "deny"}


# -- Kilo unchanged -------------------------------------------------------------------------

def test_kilo_client_is_the_oc1_client_with_kilo_names():
    assert issubclass(KiloClient, Oc1Client) and KiloError is AgentServerError
    c = KiloClient("http://127.0.0.1:1", password="pw")
    assert c.name == "kilo" and c.config_paths == ("/config/effective",)
    assert c._auth == "Basic " + base64.b64encode(b"kilo:pw").decode()
    assert foundry_denial('{"foundry_category": "budget", "reset_seconds": 30}') == \
        {"category": "budget", "retry_after": 30}


def test_kilo_requests_and_bodies_are_byte_for_byte_as_before():
    fake = FakeKilo(password="pw")
    url = fake.start()
    try:
        c = KiloClient(url, password="pw")
        sid = c.start_session(directory="/w", title="t", persona="Foundry-Kilo-Local",
                              agent="code", metadata={"role": "implementer"},
                              permissions=[{"permission": "question", "pattern": "*",
                                            "action": "deny"}])
        info = fake.sessions[sid]["info"]
        assert info["title"] == "t"
        c.prompt(sid, "hello", directory="/w", persona="Foundry-Kilo-Local", agent="code")
        assert fake.prompts[-1]["text"] == "hello"
        assert fake.prompts[-1]["model"] == "Foundry-Kilo-Local"
        assert c.effective_config("/w")["subagent_depth"] == 1
        pf = dict((n, (lvl, d)) for lvl, n, d in c.preflight("/w"))
        assert pf["kilo serve"][0] == "PASS" and pf["enabled_providers"][0] == "PASS"
        st = c.state("nope", directory="/w", since_ts=0)
        assert st.status == "missing" and st.error == "Kilo has no such session"
    finally:
        fake.stop()


# -- OpenCode 1.x --------------------------------------------------------------------------

def test_opencode_1x_uses_its_username_and_merged_config():
    fake = FakeKilo(password="pw", username="opencode", config_path="/config",
                    unsupported=("/config/effective",))
    url = fake.start()
    try:
        c = OpenCodeV1Client(url, password="pw")
        assert c.effective_config("/w")["enabled_providers"] == ["foundry"]
        pf = {n: lvl for lvl, n, d in c.preflight("/w")}
        assert pf["opencode serve"] == "PASS"
        auto = OpenCodeClient(url, password="pw")
        assert auto.detect() == "oc1" and auto.dialect == "oc1"
        sid = auto.start_session(directory="/w", title="x", persona="Foundry-Kilo-Local",
                                 agent="build", metadata={}, permissions=[])
        assert sid in fake.sessions
    finally:
        fake.stop()


# -- OpenCode 2.x --------------------------------------------------------------------------

@pytest.fixture
def oc2():
    fake = FakeOpenCode2(password="pw")
    url = fake.start()
    yield fake, Oc2Client(url, password="pw")
    fake.stop()


def test_detect_finds_2x(oc2):
    fake, _ = oc2
    c = OpenCodeClient(fake.server and f"http://127.0.0.1:{fake.server.server_address[1]}",
                       password="pw")
    assert c.detect() == "oc2" and isinstance(c.impl, Oc2Client)


def test_2x_session_create_sends_location_model_and_2x_permissions(oc2):
    fake, c = oc2
    sid = c.start_session(directory="C:/work/pilot", title="pilot T1 implement #1",
                          persona="Foundry-Kilo-Local", agent="build",
                          metadata={"project": "pilot", "role": "implementer"},
                          permissions=[{"permission": "bash", "pattern": "git push*",
                                        "action": "deny"}])
    s = fake.sessions[sid]
    assert s["location"] == {"directory": "C:/work/pilot"}
    assert s["model"] == {"providerID": "foundry", "id": "Foundry-Kilo-Local"}
    assert s["metadata"] == {"coordinator": {"project": "pilot", "role": "implementer"}}
    # the coordinator's rules first, then OpenCode's own guard (its config, its password)
    assert s["permissions"][0] == {"action": "shell", "resource": "git push*", "effect": "deny"}
    assert {"action": "edit", "resource": "opencode.jsonc", "effect": "deny"} in s["permissions"]
    assert {"action": "shell", "resource": "*OPENCODE_SERVER_PASSWORD*",
            "effect": "deny"} in s["permissions"]
    assert all(r["directory"] == "C:/work/pilot" for r in fake.requests)


def test_2x_prompt_sets_agent_and_model_only_when_they_change(oc2):
    fake, c = oc2
    sid = c.start_session(directory="/w", title="t", persona="P1", agent="build",
                          metadata={}, permissions=[])
    c.prompt(sid, "one", directory="/w", persona="P1", agent="build")
    assert [r["path"] for r in fake.requests][-1] == f"/api/session/{sid}/prompt"
    n = len(fake.requests)
    c.prompt(sid, "two", directory="/w", persona="P2", agent="build")    # frontier switch
    paths = [r["path"] for r in fake.requests[n:]]
    assert paths == [f"/api/session/{sid}/model", f"/api/session/{sid}/prompt"]
    assert fake.prompts[-1]["model"] == "P2"


def test_2x_state_walks_the_same_lifecycle_as_kilo(oc2):
    fake, c = oc2
    sid = c.start_session(directory="/w", title="t", persona="P", agent="build",
                          metadata={}, permissions=[])
    assert c.state("ses_missing", directory="/w", since_ts=0).status == "missing"
    t0 = time.time()
    c.prompt(sid, "go", directory="/w", persona="P", agent="build")
    assert c.state(sid, directory="/w", since_ts=t0).status == "busy"
    fake.retrying(sid, 'provider error {"foundry_category": "budget", "reset_seconds": 60}')
    st = c.state(sid, directory="/w", since_ts=t0)
    assert st.status == "busy" and st.retry and st.denial == {"category": "budget",
                                                              "retry_after": 60}
    fake.active.discard(sid)
    fake.messages[sid].pop()                        # the retry ended; the turn goes on
    fake.active.add(sid)
    fake.finish(sid, 'done\n```coordinator-report\n{"status": "DONE", "summary": "ok"}\n```')
    st = c.state(sid, directory="/w", since_ts=t0)
    assert st.status == "idle" and st.report["status"] == "DONE"
    assert st.tokens == {"input": 1200, "output": 300} and st.context == 1600


def test_2x_compaction_and_failed_runs(oc2):
    fake, c = oc2
    sid = c.start_session(directory="/w", title="t", persona="P", agent="build",
                          metadata={}, permissions=[])
    t0 = time.time()
    c.prompt(sid, "go", directory="/w", persona="P", agent="build")
    fake.compact(sid)
    st = c.state(sid, directory="/w", since_ts=t0)
    assert st.status == "busy" and st.compaction and "OpenCode" in st.error
    fake.active.add(sid)
    fake.finish(sid, "", error={"type": "APIError", "message": "route_unavailable",
                                "status": 503})
    st = c.state(sid, directory="/w", since_ts=t0)
    assert st.status == "error" and st.denial["category"] == "route"


def test_2x_forms_and_permissions_answer_through_the_engine_shapes(oc2):
    fake, c = oc2
    sid = c.start_session(directory="/w", title="t", persona="P", agent="build",
                          metadata={}, permissions=[])
    fid = fake.ask(sid, "Which one?", options=("A", "B"))
    st = c.state(sid, directory="/w", since_ts=0)
    assert st.status == "attention" and st.question["kind"] == "question"
    assert st.question["request_id"] == fid and st.question["options"] == [["A", "B"]]
    c.answer(sid, fid, [["B"]], directory="/w")
    assert fake.replies[-1]["body"] == {"answer": {"answer": "B"}}
    rid = fake.ask_permission(sid, "shell", ["git push origin"])
    st = c.state(sid, directory="/w", since_ts=0)
    assert st.question["kind"] == "permission" and "bash" in st.question["text"]
    c.answer(sid, rid, ["reject", "not in this task"], directory="/w", kind="permission")
    assert fake.replies[-1]["body"] == {"decision": "reject", "message": "not in this task"}


def test_2x_children_carry_the_subagent_title_helpers_parse(oc2):
    from foundry_coordinator.helpers import child_agent
    fake, c = oc2
    sid = c.start_session(directory="/w", title="t", persona="P", agent="build",
                          metadata={}, permissions=[])
    kid = fake.spawn_child(sid, agent="au-review", title="review the diff")
    kids = c.children(sid, directory="/w")
    assert [k["id"] for k in kids] == [kid]
    assert child_agent(kids[0]["title"]) == "au-review" and kids[0]["model"]["id"] == "P"


def test_2x_config_view_and_preflight(oc2):
    fake, c = oc2
    cfg = c.effective_config("/w")
    assert cfg["subagent_depth"] == 1 and "au-helper" in cfg["agent"]
    assert cfg["provider"]["foundry"]["models"]["Foundry-Kilo-Local"]["limit"]["context"] == 262144
    assert c.model_config_problem("/w", "Foundry-Kilo-Local") == ""
    assert "no model" in c.model_config_problem("/w", "Foundry-Kilo-Other")
    pf = {n: lvl for lvl, n, d in c.preflight("/w")}
    assert pf == {"opencode serve": "PASS", "provider": "PASS", "enabled_providers": "PASS",
                  "share": "PASS", "subagent_depth": "PASS"}
    fake.config_docs[0]["info"]["share"] = "manual"
    assert {n: lvl for lvl, n, d in c.preflight("/w")}["share"] == "FAIL"
    c.reload("/w")
    assert fake.reloads == ["/w"]


def test_translation_keeps_tools_and_drops_switch_markers():
    rows = [{"type": "agent-switched", "id": "m0", "agent": "build"},
            {"type": "user", "id": "m1", "text": "hi", "time": {"created": 1}},
            {"type": "assistant", "id": "m2", "agent": "build",
             "model": {"id": "P", "providerID": "foundry"}, "time": {"created": 2, "completed": 3},
             "content": [{"type": "tool", "id": "c1", "name": "read",
                          "state": {"status": "completed", "input": {"filePath": "a.lua"},
                                    "content": [{"type": "text", "text": "x = 1"}]}},
                         {"type": "text", "text": "ok"}],
             "tokens": {"input": 5, "output": 2, "reasoning": 0, "cache": {"read": 1, "write": 0}}},
            {"type": "idle", "id": "m3", "outcome": "succeeded"}]
    out = to_oc1_messages(rows, "ses_1")
    assert [m["info"]["role"] for m in out] == ["user", "assistant"]
    tool = out[1]["parts"][0]
    assert tool["tool"] == "read" and tool["state"]["input"] == {"filePath": "a.lua"}
    assert tool["state"]["output"] == "x = 1" and out[1]["info"]["modelID"] == "P"


def test_config_view_maps_2x_keys():
    v = oc1_config_view({"providers": {"foundry": {"models": {}}},
                         "agents": {"lead": {}}, "experimental": {"subagent_depth": 0},
                         "mcp": {"servers": {"git": {"type": "local"}}, "timeout": 5}})
    assert "foundry" in v["provider"] and "lead" in v["agent"]
    assert v["subagent_depth"] == 0 and v["mcp"] == {"timeout": 5, "git": {"type": "local"}}


# -- per role: ClientSet -------------------------------------------------------------------

class _Prof:
    def __init__(self, client, raw):
        self.client, self.raw, self.id = client, raw, "pilot"

    def secret(self, t, k):
        return "pw"


def test_only_client_table_builds_the_plain_client_as_before():
    p = _Prof({"kind": "kilo", "url": "http://127.0.0.1:4096"}, {})
    assert type(build_client(p)) is KiloClient
    p = _Prof({"kind": "opencode"}, {})
    c = build_client(p)
    assert isinstance(c, OpenCodeClient) and c.url == "http://127.0.0.1:4097"


def test_clientset_runs_a_role_on_its_server_and_follows_the_session():
    kfake, ofake = FakeKilo(password="pw"), FakeOpenCode2(password="pw")
    kurl, ourl = kfake.start(), ofake.start()
    try:
        p = _Prof({"kind": "kilo", "url": kurl, "agent": "code"},
                  {"clients": {"oc": {"kind": "opencode", "url": ourl, "agent": "build",
                                      "worker_client": "opencode"}},
                   "client_roles": {"helper": "oc"}})
        cs = build_client(p)
        assert isinstance(cs, ClientSet)
        ksid = cs.start_session(directory="/w", title="impl", persona="P", agent="code",
                                metadata={"role": "implementer"}, permissions=[])
        assert ksid in kfake.sessions and cs.name == "kilo"
        osid = cs.start_session(directory="/w", title="help", persona="P", agent="code",
                                metadata={"role": "helper"}, permissions=[])
        assert osid in ofake.sessions and cs.name == "opencode"
        assert ofake.sessions[osid]["agent"] == "build"          # code -> the server's agent
        cs.prompt(osid, "look", directory="/w", persona="P", agent="code")
        assert ofake.prompts[-1]["text"] == "look"
        # a new process: ownership found again by asking the servers
        cs2 = build_client(p)
        assert cs2.state(osid, directory="/w", since_ts=0).status == "busy"
        assert worker_client_of(cs2, osid, "kilo") == "opencode"
        assert worker_client_of(cs2, ksid, "kilo") == "kilo"
        assert worker_client_of(KiloClient(kurl), "x", "kilo") == "kilo"
        assert isinstance(for_role(cs2, "helper"), OpenCodeClient)
        assert for_role(cs2, "lead") is cs2.default.client
        pf = [n for _, n, _ in cs2.preflight("/w")]
        assert "kilo serve" in pf and "oc: opencode serve" in pf
    finally:
        kfake.stop()
        ofake.stop()


def test_profile_validation_of_client_tables(tmp_path):
    from foundry_coordinator.profile import ProfileError, _client_tables
    _client_tables("p", {"kind": "kilo"}, {})
    _client_tables("p", {"kind": "kilo"}, {"clients": {"oc": {"kind": "opencode", "api": "oc2"}},
                                           "client_roles": {"helper": "oc"}})
    for raw, msg in (({"clients": {"oc": {"kind": "cline"}}}, "kind"),
                     ({"clients": {"default": {"kind": "kilo"}}}, "not 'default'"),
                     ({"client_roles": {"reviewer": "oc"}}, "roles are"),
                     ({"client_roles": {"helper": "nope"}}, "names no"),
                     ({"clients": {"oc": {"kind": "opencode", "api": "v3"}}}, "api")):
        with pytest.raises(ProfileError, match=msg):
            _client_tables("p", {"kind": "kilo"}, raw)
    with pytest.raises(ProfileError, match="api"):
        _client_tables("p", {"kind": "opencode", "api": "x"}, {})


# -- telemetry -------------------------------------------------------------------------------

def test_project_observer_is_kilos_for_a_kilo_project_and_2x_for_opencode(oc2):
    from foundry_coordinator import telemetry as T
    fake, c = oc2
    sid = c.start_session(directory="/w", title="t", persona="P", agent="build",
                          metadata={}, permissions=[])
    fake.finish(sid, "ok")

    class P:
        pass
    P.profile = _Prof({"kind": "kilo", "url": "http://127.0.0.1:4096"}, {})
    assert type(T.project_observer(P)) is T.KiloObserver
    P.profile = _Prof({"kind": "opencode", "url": c.url, "api": "oc2"}, {})
    o = T.project_observer(P)
    assert isinstance(o, T.Oc2Observer)
    assert o.session(sid, "/w")["id"] == sid
    assert [m["info"]["role"] for m in o.messages(sid, "/w")] == ["assistant"]
    assert o.statuses("/w") == {}
    with pytest.raises(Exception):
        next(iter(o.events("/w", None)))
