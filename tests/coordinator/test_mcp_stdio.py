"""mcp_stdio: the stdlib MCP stdio client, against a fake server (never Studio)."""

import json
import os
import sys
import time

import pytest

from foundry_coordinator import mcp_stdio as M

FAKE = os.path.join(os.path.dirname(__file__), "fixtures", "fake_mcp_server.py")


def argv(kind, tmp_path=None, rules=None, extra=()):
    a = [sys.executable, FAKE, "--kind", kind]
    if rules is not None:
        p = tmp_path / f"{kind}_rules.json"
        p.write_text(json.dumps({"rules": rules}), encoding="utf-8")
        a += ["--scenario", str(p)]
    if tmp_path is not None:
        a += ["--log", str(tmp_path / "calls.jsonl")]
    return a + list(extra)


def test_handshake_and_tools_list():
    with M.McpClient(argv("official"), name="official") as c:
        assert c.server_info["serverInfo"]["name"] == "fake-official"
        assert c.server_info["protocolVersion"] == M.PROTOCOL_VERSION
        names = c.tool_names()
        assert "execute_luau" in names and "list_roblox_studios" in names
        assert c.alive
    assert not c.alive


def test_call_tool_text_and_request_ids(tmp_path):
    rules = [{"tool": "eval_server_runtime", "match": "hello", "text": "PASS: hi"}]
    with M.McpClient(argv("community", tmp_path, rules)) as c:
        r1 = c.call_tool("eval_server_runtime", {"code": "return 'hello'"})
        r2 = c.call_tool("eval_server_runtime", {"code": "other"})
        assert r1 == {"text": "PASS: hi", "images": [], "is_error": False, "raw": r1["raw"]}
        assert r2["text"] == ""
        assert c._next_id >= 4                                 # initialize + list? + 2 calls
    log = [json.loads(ln) for ln in (tmp_path / "calls.jsonl").read_text().splitlines()]
    assert [x["args"]["code"] for x in log] == ["return 'hello'", "other"]


def test_images_png_and_jpeg():
    with M.McpClient(argv("community")) as c:
        r = c.call_tool("capture_screenshot", {"format": "png"})
        assert json.loads(r["text"]) == {"width": 1920, "height": 1080}
        (mime, data), = r["images"]
        assert mime == "image/png" and data[:8] == b"\x89PNG\r\n\x1a\n"
    with M.McpClient(argv("official")) as c:
        s = M.OfficialStudio(c)
        r = s.call("screen_capture", {"capture_id": "ScreenCapture_1"})
        assert r["images"][0][0] == "image/jpeg"


def test_is_error_and_unknown_tool(tmp_path):
    rules = [{"tool": "execute_luau", "match": "boom", "text": "runtime error", "is_error": True}]
    with M.McpClient(argv("community", tmp_path, rules)) as c:
        r = c.call_tool("execute_luau", {"code": "boom"})
        assert r["is_error"] and r["text"] == "runtime error"
        with pytest.raises(M.McpError, match="unknown tool"):
            c.call_tool("nope", {})


def test_timeout_then_client_still_usable(tmp_path):
    rules = [{"tool": "eval_client_runtime", "match": "slow", "sleep": 2, "text": "late"}]
    with M.McpClient(argv("community", tmp_path, rules)) as c:
        t0 = time.monotonic()
        with pytest.raises(M.McpTimeout):
            c.call_tool("eval_client_runtime", {"code": "slow"}, timeout=0.3)
        assert time.monotonic() - t0 < 1.5
        assert c.call_tool("eval_client_runtime", {"code": "x"}, timeout=10)["text"] in ("", "late")


def test_init_timeout_raises_and_cleans_up():
    with pytest.raises(M.McpTimeout):
        M.McpClient(argv("official", extra=["--slow-init", "5"]), init_timeout=0.3)


def test_server_exit_fails_pending_call(tmp_path):
    rules = [{"tool": "eval_server_runtime", "match": "die", "exit": True}]
    with M.McpClient(argv("community", tmp_path, rules)) as c:
        with pytest.raises(M.McpError, match="exited"):
            c.call_tool("eval_server_runtime", {"code": "die"}, timeout=10)
        with pytest.raises(M.McpError):
            c.call_tool("eval_server_runtime", {"code": "after"}, timeout=5)


def test_bad_command_raises():
    with pytest.raises(M.McpError, match="cannot start"):
        M.McpClient([os.path.join(os.path.dirname(FAKE), "no_such_exe_xyz")])


def test_official_studio_resolution_and_studio_id(tmp_path):
    rules = [{"tool": "execute_luau", "match": '"datamodel_type": "Edit"', "text": "EDIT OK"}]
    with M.McpClient(argv("official", tmp_path, rules)) as c:
        s = M.OfficialStudio(c, place="animal")
        assert s.execute_luau("return 1")["text"] == "EDIT OK"
        assert s.studio_id == "S1" and s.studio_name == "AnimalUprising"
        with pytest.raises(M.McpError, match="exactly one Studio"):
            M.OfficialStudio(c, place="OtherPlace").resolve()
    log = [json.loads(ln) for ln in (tmp_path / "calls.jsonl").read_text().splitlines()]
    assert log[1]["args"]["studio_id"] == "S1"


def test_registry_commands_and_override(monkeypatch):
    monkeypatch.delenv("FOUNDRY_MCP_OFFICIAL", raising=False)
    monkeypatch.delenv("FOUNDRY_MCP_COMMUNITY", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", r"C:\Users\x\AppData\Local")
    assert M.official_command()[:3] == ["cmd.exe", "/d", "/c"]
    assert M.official_command()[3].endswith(os.path.join("Roblox", "mcp.bat"))
    assert M.community_command()[-2:] == ["-y", "@chrrxs/robloxstudio-mcp@3.4.2"]
    monkeypatch.setenv("FOUNDRY_MCP_COMMUNITY", json.dumps(argv("community")))
    with M.connect("community") as c:
        assert "capture_screenshot" in c.tool_names()
    monkeypatch.setenv("FOUNDRY_MCP_OFFICIAL", "not json")
    with pytest.raises(M.McpError, match="JSON argv"):
        M.official_command()
    with pytest.raises(M.McpError, match="unknown MCP server"):
        M.connect("nope")


def test_non_object_json_lines_and_server_ping_do_not_kill_the_reader(tmp_path):
    """H8: `42`, `[1, 2]`, `"text"`, `null` on stdout and a server->client ping before
    the initialize reply: the reader keeps dispatching (it used to die on TypeError)."""
    t0 = time.monotonic()
    with M.McpClient(argv("community", tmp_path, [], extra=["--noise"]), init_timeout=10) as c:
        assert time.monotonic() - t0 < 8
        assert c.alive and "capture_screenshot" in c.tool_names()
        assert any("non-object" in x for x in c.stderr_tail)


def test_dispatch_error_keeps_the_reader_and_dead_reader_fails_all(tmp_path):
    with M.McpClient(argv("community", tmp_path, [])) as c:
        orig = c._dispatch
        calls = []

        def boom(msg):
            calls.append(msg)
            if len(calls) == 1:
                raise RuntimeError("dispatch bug")
            return orig(msg)
        c._dispatch = boom
        with pytest.raises(M.McpTimeout):                     # its reply was lost in the bug
            c.call_tool("eval_server_runtime", {"code": "a"}, timeout=1)
        assert c.call_tool("eval_server_runtime", {"code": "b"}, timeout=10)["text"] == ""
        assert any("dispatch error" in x for x in c.stderr_tail)


@pytest.mark.skipif(os.name != "nt", reason="Windows process-tree kill")
def test_close_kills_a_server_that_ignores_eof_and_its_grandchild(tmp_path):
    from foundry_coordinator.projects.animaluprising.studio_gate import _pid_alive
    pidfile = tmp_path / "child.pid"
    c = M.McpClient(argv("community", tmp_path, [], extra=["--ignore-eof", "--child-pidfile", str(pidfile)]))
    child = int(pidfile.read_text())
    parent = c.proc.pid
    assert _pid_alive(child) and _pid_alive(parent)
    t0 = time.monotonic()
    c.close(grace=5)
    assert time.monotonic() - t0 < 10
    assert M.wait_until(lambda: not _pid_alive(child) and not _pid_alive(parent), 10, 0.2)


def test_wait_until():
    seen = []
    assert M.wait_until(lambda: seen.append(1) or len(seen) >= 3, 5, 0.01)
    assert not M.wait_until(lambda: False, 0.05, 0.01)


class _StubClient:
    def __init__(self, instances, schema_props=("code",)):
        self.instances = instances
        self.tools = [{"name": n, "inputSchema": {"properties": {p: {} for p in schema_props}}}
                      for n in ("get_connected_instances", "eval_server_runtime")]
        self.calls = []

    def tool_names(self):
        return [t["name"] for t in self.tools]

    def call_tool(self, name, args, timeout=0):
        self.calls.append((name, dict(args)))
        if name == "get_connected_instances":
            return {"text": json.dumps({"instances": self.instances}), "is_error": False, "images": []}
        return {"text": "ok", "is_error": False, "images": []}


def test_community_binding_one_instance_unscoped_several_need_place():
    one = _StubClient([{"id": "I1", "placeName": "AnimalUprising"}])
    s = M.CommunityStudio(one)
    s.call_tool("eval_server_runtime", {"code": "x"})
    assert one.calls[-1] == ("eval_server_runtime", {"code": "x"})      # the verified live path
    assert s.place_name == "AnimalUprising" and s.instance_id is None
    two = [{"id": "I1", "placeName": "AnimalUprising"}, {"id": "I2", "placeName": "Sandbox"}]
    with pytest.raises(M.McpError, match="exactly one Studio instance"):
        M.CommunityStudio(_StubClient(two)).call_tool("eval_server_runtime", {"code": "x"})
    bound = _StubClient(two, ("code", "instance_id"))
    s = M.CommunityStudio(bound, place="sandbox")
    s.call_tool("eval_server_runtime", {"code": "x"})
    assert bound.calls[-1] == ("eval_server_runtime", {"code": "x", "instance_id": "I2"})
    # a tool that can't take instance_id is refused rather than sent to "some" Studio
    with pytest.raises(M.McpError, match="cannot be bound"):
        M.CommunityStudio(_StubClient(two), place="sandbox").call_tool("eval_server_runtime", {})
    with pytest.raises(M.McpError, match="matching 'nope'"):
        M.CommunityStudio(_StubClient(two), place="nope").resolve()


def test_a_studio_with_no_place_open_is_not_a_gate_target():
    """2026-10-30 18:49 MV2-11: a Studio on its start page (name null) next to the TEST
    Studio failed every gate with 'need exactly one Studio, found 2'."""
    class Off:
        def call_tool(self, name, args, timeout=0):
            return {"is_error": False, "images": [], "text": json.dumps({"studios": [
                {"id": "S0", "name": None},
                {"id": "S1", "name": "Animal Uprising [TEST] (placeId: 77417134434265)"}]})}
    s = M.OfficialStudio(Off())
    assert s.resolve() == "S1"
    assert M.OfficialStudio(Off(), place="[TEST]").resolve() == "S1"
    with pytest.raises(M.McpError, match="found 0"):
        M.OfficialStudio(Off(), place="[SLOT-1]").resolve()
    com = _StubClient([{"id": "I0", "placeName": "", "placeId": 0},
                       {"id": "I1", "placeName": "Animal Uprising [TEST]", "placeId": 7}],
                      ("code", "instance_id"))
    c = M.CommunityStudio(com)
    c.call_tool("eval_server_runtime", {"code": "x"})
    assert c.place_name == "Animal Uprising [TEST]"
    assert com.calls[-1] == ("eval_server_runtime", {"code": "x", "instance_id": "I1"})


def test_community_resolve_waits_for_the_plugin_to_connect(monkeypatch):
    """Live 2026-10-20: a fresh server lists no instance, the plugin connects ~8 s later."""
    import json as _json
    from foundry_coordinator import mcp_stdio as M
    replies = [[], [], [{"id": "instance:a", "placeId": 1, "placeName": "Animal Uprising"}]]

    class Stub:
        def tool_names(self):
            return ["get_connected_instances"]

        def call_tool(self, name, args, timeout=30.0):
            return {"is_error": False, "text": _json.dumps({"instances": replies.pop(0)})}

    monkeypatch.setattr(M.time, "sleep", lambda s: None)
    com = M.CommunityStudio(Stub())
    com.resolve()
    assert com.place_name == "Animal Uprising" and not replies


def test_official_resolve_waits_for_studio_to_connect_to_a_fresh_host(monkeypatch):
    """2026-10-31 studio slot: StudioMCP.exe is the MCP host itself in a container and Studio
    connects a few seconds after it starts, so the first list is empty."""
    replies = [[], [], [{"id": "S1", "name": "Animal Uprising [SLOT-1] (placeId: 1)"}]]

    class Off:
        def call_tool(self, name, args, timeout=0):
            return {"is_error": False, "images": [],
                    "text": json.dumps({"studios": replies.pop(0) if replies else []})}
    monkeypatch.setattr(M.time, "sleep", lambda s: None)
    assert M.OfficialStudio(Off(), place="[SLOT-").resolve() == "S1"
    t = [0.0]
    monkeypatch.setattr(M.time, "monotonic", lambda: t.__setitem__(0, t[0] + 5.0) or t[0])
    with pytest.raises(M.McpError, match="found 0"):          # gives up after CONNECT_WAIT_S
        M.OfficialStudio(Off()).resolve()
