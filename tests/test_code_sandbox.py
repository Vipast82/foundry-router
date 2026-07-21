"""Code-execution sandbox integration (code-sandbox spec). The sandbox is a
NORMAL mcp_servers entry — no name-based special-casing. The only new machinery
is generic: an operator `executes_code` flag, `call_defaults` force-merged over
the model's arguments (config wins — a model can't widen its own sandbox), the
full-code audit trail in tool_call_log, and the per-persona/UI danger flag."""

import json

from foundry_router.config import MCPServerConfig
from foundry_router.db import Database
from foundry_router.tools.mcp_client import MCPManager
from foundry_router.usage import RequestLogger


# -- config back-compat -----------------------------------------------------------

def test_mcp_config_defaults_are_backcompat():
    s = MCPServerConfig(name="searxng", url="http://x/mcp")
    assert s.executes_code is False          # normal servers unchanged
    assert s.call_defaults == {}


def test_sandbox_config_parses():
    s = MCPServerConfig(name="code-sandbox", url="http://gen-ai:8975/mcp",
                        executes_code=True,
                        call_defaults={"network": False, "cpus": 1, "memory_mb": 512})
    assert s.executes_code is True
    assert s.call_defaults["network"] is False


# -- force-merge: config wins over the model (the safety gate) --------------------

def _mgr(tmp_path, **server_kwargs):
    db = Database(tmp_path / "s.sqlite")
    server = MCPServerConfig(name="code-sandbox", url="http://x/mcp",
                             **server_kwargs)
    return MCPManager([server], db), db


def test_executes_code_flag_lookup(tmp_path):
    m, _ = _mgr(tmp_path, executes_code=True)
    assert m.executes_code("code-sandbox") is True
    assert m.executes_code("nonexistent") is False


def test_call_defaults_override_model_arguments(tmp_path):
    m, _ = _mgr(tmp_path, executes_code=True,
                call_defaults={"network": False, "cpus": 1})
    # the model asks for network + more CPU; config forces its policy
    merged = m._apply_call_defaults(
        "code-sandbox", {"code": "print(1)", "network": True, "cpus": 8})
    assert merged["network"] is False        # model CANNOT enable network
    assert merged["cpus"] == 1
    assert merged["code"] == "print(1)"      # non-policy args pass through


def test_override_is_logged_as_security_event(tmp_path):
    m, db = _mgr(tmp_path, executes_code=True, call_defaults={"network": False})
    m._apply_call_defaults("code-sandbox", {"code": "x", "network": True})
    ev = db.query_one("SELECT * FROM event_log WHERE source='mcp' "
                      "ORDER BY id DESC LIMIT 1")
    assert ev and "enforced" in ev["message"] and "network" in ev["message"]


def test_no_defaults_is_passthrough(tmp_path):
    m, _ = _mgr(tmp_path)               # no call_defaults
    args = {"code": "print(1)"}
    assert m._apply_call_defaults("code-sandbox", args) is args


def test_defaults_apply_but_only_code_server_logs_override(tmp_path):
    # a non-code server with call_defaults still merges, but the override
    # security event is reserved for executes_code servers
    m, db = _mgr(tmp_path, executes_code=False, call_defaults={"lang": "py"})
    merged = m._apply_call_defaults("code-sandbox", {"lang": "js"})
    assert merged["lang"] == "py"          # config still wins
    assert db.query("SELECT * FROM event_log WHERE source='mcp'") == []


# -- audit trail: submitted code captured in tool_call_log ------------------------

def test_record_tool_call_captures_code(tmp_path):
    db = Database(tmp_path / "a.sqlite")
    logger = RequestLogger(db, "Foundry-Agent", "Foundry-Agent", "agent", "run this")
    code = "import math\nprint(math.factorial(20))"
    logger.record_tool_call("run_python", "code-sandbox", 1200, ok=True,
                            caller="qwen3:14b",
                            arguments={"code": code, "network": False},
                            executes_code=True)
    row = db.query_one("SELECT * FROM tool_call_log")
    assert row["executed_code"] == 1
    assert json.loads(row["arguments"])["code"] == code   # verbatim
    assert row["server"] == "code-sandbox" and row["caller"] == "qwen3:14b"
    # the per-request trail marks it too
    assert logger.tool_calls[0]["executes_code"] is True


def test_read_only_call_arguments_capped_tighter(tmp_path):
    db = Database(tmp_path / "b.sqlite")
    logger = RequestLogger(db, "Foundry-Research", "Foundry-Research", "agent", "q")
    logger.record_tool_call("search", "searxng", 300, ok=True,
                            arguments={"query": "x" * 5000}, executes_code=False)
    row = db.query_one("SELECT * FROM tool_call_log")
    assert row["executed_code"] == 0
    assert len(row["arguments"]) <= RequestLogger._ARG_CAP + 40   # capped + notice


def test_huge_code_is_capped_with_notice(tmp_path):
    db = Database(tmp_path / "c.sqlite")
    logger = RequestLogger(db, "P", "P", "agent", "q")
    logger.record_tool_call("run", "code-sandbox", 1, ok=True,
                            arguments={"code": "y" * 50000}, executes_code=True)
    row = db.query_one("SELECT * FROM tool_call_log")
    assert "truncated" in row["arguments"]
    assert len(row["arguments"]) <= RequestLogger._CODE_ARG_CAP + 60


# -- endpoints: persistence + audit view + persona-detection data -----------------

def test_upsert_persists_sandbox_fields(client):
    r = client.post("/admin/api/mcp_servers", json={
        "name": "code-sandbox", "url": "http://gen-ai:8975/mcp",
        "executes_code": True, "timeout_seconds": 30,
        "call_defaults": {"network": False, "cpus": 1, "memory_mb": 512}})
    assert r.status_code == 200
    servers = {s["name"]: s for s in
               client.get("/admin/api/mcp_servers").json()["servers"]}
    sb = servers["code-sandbox"]
    assert sb["executes_code"] is True
    assert sb["call_defaults"]["network"] is False
    assert sb["timeout_seconds"] == 30


def test_code_execution_audit_endpoint(client, app):
    db = app.state.services.db
    RequestLogger(db, "Foundry-Agent", "Foundry-Agent", "agent", "q").record_tool_call(
        "run_python", "code-sandbox", 900, ok=True,
        arguments={"code": "print('hi')"}, executes_code=True)
    RequestLogger(db, "Foundry-Research", "Foundry-Research", "agent", "q").record_tool_call(
        "search", "searxng", 200, ok=True, arguments={"query": "cats"})
    # executed_code=1 filters to sandbox calls only
    only_code = client.get("/admin/api/tool_calls?executed_code=1").json()["tool_calls"]
    assert len(only_code) == 1 and only_code[0]["server"] == "code-sandbox"
    assert json.loads(only_code[0]["arguments"])["code"] == "print('hi')"
    # unfiltered shows both
    allc = client.get("/admin/api/tool_calls").json()["tool_calls"]
    assert len(allc) == 2
