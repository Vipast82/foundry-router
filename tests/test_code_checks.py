"""v0.181.164: the code a model writes is checked before the answer reaches the
client, and a failing check goes back to the model for another try."""
import asyncio
import inspect
import json
import sys
from pathlib import Path

import pytest

from foundry_router import code_checks as cc
from foundry_router.config import CodeCheckRuleConfig, CodeChecksConfig
from foundry_router.pool.protocols import ChatResult
from tests.test_main_mode import _make, _run

PY_RULE = {"name": "python", "kind": "python", "languages": ["python", "py"], "enabled": True}
JSON_RULE = {"name": "json", "kind": "json", "languages": ["json"], "enabled": True}


def _cfg(*rules, enabled=True, max_fixes=1, skip_gated=False):
    return CodeChecksConfig(enabled=enabled, max_fixes=max_fixes,
                            skip_gated_tasks=skip_gated,
                            rules=[CodeCheckRuleConfig(**r) for r in rules])


# -- reading the code out of a reply ------------------------------------------------------

def test_the_code_blocks_of_a_reply():
    text = ("Here you go:\n\n```python\nprint(1)\n```\n\nand some JSON:\n\n"
            "~~~json\n{\"a\": 1}\n~~~\n\nand this:\n\n```\nplain\n```\n\n```luau\n```\n")
    assert cc.blocks(text) == [("python", "print(1)\n"), ("json", '{"a": 1}\n'), ("", "plain\n")]
    assert cc.blocks("no code here") == [] and cc.blocks("") == []
    # indented fences (inside a list) are read too
    assert cc.blocks("1. like this:\n\n   ```lua\n   local x = 1\n   ```\n")[0][0] == "lua"


def test_which_rules_cover_which_language():
    c = _cfg(PY_RULE, {"name": "any", "kind": "command", "languages": ["*"], "command": "true"})
    assert [r["name"] for r in cc.rules_for(c, "python")] == ["python", "any"]
    assert [r["name"] for r in cc.rules_for(c, "luau")] == ["any"]
    assert cc.rules_for(c, "") == []                      # an unlabelled block is left alone
    off = _cfg({**PY_RULE, "enabled": False})
    assert cc.rules_for(off, "python") == []
    assert cc.on_for(c, {}) is True and cc.on_for(c, {"code_checks": 0}) is False
    # work a coordinator task owns can be left to that project's own gate
    assert cc.on_for(c, {}, task_bound=True) is True               # off by default
    gated = _cfg(PY_RULE, skip_gated=True)
    assert cc.on_for(gated, {}, task_bound=True) is False
    assert cc.on_for(gated, {}, task_bound=False) is True
    assert cc.on_for(_cfg(PY_RULE, enabled=False), {}) is False
    assert cc.on_for(_cfg(), {}) is False                 # on, but nothing to run
    assert cc.on_for(None, {}) is False


# -- the checks ---------------------------------------------------------------------------

def test_the_built_in_checks_find_what_is_wrong():
    good = "```python\nx = 1\nprint(x)\n```"
    bad = "```python\ndef f(:\n```"
    assert _run_check(_cfg(PY_RULE), good) == []
    probs = _run_check(_cfg(PY_RULE), bad)
    assert len(probs) == 1 and probs[0]["rule"] == "python" and probs[0]["block"] == 1
    assert "line 1" in probs[0]["reason"]
    assert _run_check(_cfg(JSON_RULE), '```json\n{"a": 1}\n```') == []
    assert "Expecting" in _run_check(_cfg(JSON_RULE), '```json\n{"a": 1,}\n```')[0]["reason"]
    # the code is compiled, never run
    assert _run_check(_cfg(PY_RULE), "```python\nraise SystemExit(3)\n```") == []


def _run_check(cfg, text, log=None):
    return asyncio.run(cc.check(cfg, text, log=log))


def test_a_configured_command_is_run_on_the_code():
    ok = {"name": "ok", "kind": "command", "languages": ["luau"],
          "command": f'"{sys.executable}" -c "import sys; sys.exit(0)" {{file}}'}
    no = {"name": "picky", "kind": "command", "languages": ["luau"],
          "command": f'"{sys.executable}" -c '
                     f'"import sys; print(\'bad member at line 2\'); sys.exit(1)" {{file}}'}
    assert _run_check(_cfg(ok), "```luau\nlocal x = 1\n```") == []
    probs = _run_check(_cfg(no), "```luau\nlocal x = 1\n```")
    assert probs[0]["reason"] == "bad member at line 2" and probs[0]["language"] == "luau"
    missing = {"name": "gone", "kind": "command", "languages": ["luau"],
               "command": "definitely-not-installed-xyz {file}"}
    said = []
    assert _run_check(_cfg(missing), "```luau\na\n```", log=said.append) == []   # never the code's fault
    assert any("could not run" in s and "not installed on the router" in s for s in said)


def test_the_checker_sees_the_code_in_a_file_and_the_file_is_removed(tmp_path):
    spy = tmp_path / "seen.txt"
    rule = {"name": "copy", "kind": "command", "languages": ["luau"],
            "command": f'"{sys.executable}" -c '
                       f'"import sys,pathlib; pathlib.Path(r\'{spy}\').write_text('
                       f'sys.argv[1] + chr(10) + open(sys.argv[1]).read())" {{file}}'}
    assert _run_check(_cfg(rule), "```luau\nlocal x = 1\n```") == []
    path, code = spy.read_text().split("\n", 1)
    assert code == "local x = 1\n" and path.endswith(".luau")
    assert not Path(path).exists() and not Path(path).parent.exists()   # cleaned up


def test_a_checker_that_hangs_or_breaks_never_blocks_the_answer():
    slow = {"name": "slow", "kind": "command", "languages": ["luau"], "timeout_seconds": 1,
            "command": f'"{sys.executable}" -c "import time; time.sleep(30)" {{file}}'}
    said = []
    assert _run_check(_cfg(slow), "```luau\na\n```", log=said.append) == []
    assert any("did not finish within 1s" in s for s in said)
    said = []
    broken = {"name": "broken", "kind": "command", "languages": ["luau"], "command": ""}
    assert _run_check(_cfg(broken), "```luau\na\n```") == []        # nothing to run: a pass
    assert said == []


def test_what_the_model_is_told():
    probs = _run_check(_cfg(PY_RULE), "```python\ndef f(:\n```")
    text = cc.feedback(probs)
    assert "did not pass this router's checks" in text and "give the whole answer again" in text
    assert "code block 1 (python), python:" in text and "line 1" in text
    assert cc.summary(probs) == "block 1 python"


# -- in a conversation --------------------------------------------------------------------

def _runner(tmp_path, scripts, cfg, persona_extra=None):
    runner, ctx, pool, mcp = _make(tmp_path, scripts, pins=("main",))
    from types import SimpleNamespace
    runner.guardrails.config_fn = lambda: SimpleNamespace(code_checks=cfg, a2a=None)
    ctx.persona.update(persona_extra or {})
    return runner, ctx, pool


def test_the_model_is_asked_to_fix_code_that_does_not_compile(tmp_path):
    runner, ctx, pool = _runner(tmp_path, {"main": [
        ChatResult(content="Here:\n```python\ndef f(:\n```"),
        ChatResult(content="Sorry:\n```python\ndef f():\n    pass\n```")]}, _cfg(PY_RULE))
    evs = _run(runner.run_main(ctx))
    answers = [e.text for e in evs if e.kind == "answer"]
    assert answers == ["Sorry:\n```python\ndef f():\n    pass\n```"]      # only the good one
    think = " ".join(e.text for e in evs if e.kind == "think")
    assert "did not pass this router's checks (block 1 python)" in think
    assert "passed every check" in think
    asked = pool.seen[-1]["messages"] if hasattr(pool, "seen") else None
    last = pool.asked[-1]
    assert last[0] == "main" and len(pool.asked) == 2
    assert any("code check failed" in g for g in ctx.logger.guardrail_events)


def test_it_gives_up_after_the_allowed_tries_and_still_answers(tmp_path):
    bad = "```python\ndef f(:\n```"
    runner, ctx, pool = _runner(tmp_path, {"main": [ChatResult(content=bad)] * 3},
                                _cfg(PY_RULE, max_fixes=1))
    evs = _run(runner.run_main(ctx))
    assert [e.text for e in evs if e.kind == "answer"] == [bad]        # shown as it is
    assert len(pool.asked) == 2                                        # one fix was asked for
    runner, ctx, pool = _runner(tmp_path / "b" if (tmp_path / "b").mkdir() is None else tmp_path,
                                {"main": [ChatResult(content=bad)]}, _cfg(PY_RULE, max_fixes=0))
    assert [e.text for e in _run(runner.run_main(ctx)) if e.kind == "answer"] == [bad]
    assert len(pool.asked) == 1                                        # never asked


def test_nothing_happens_when_it_is_off_or_the_persona_opts_out(tmp_path):
    bad = "```python\ndef f(:\n```"
    for cfg, extra in ((_cfg(PY_RULE, enabled=False), {}), (_cfg(PY_RULE), {"code_checks": 0}),
                       (_cfg(), {})):
        runner, ctx, pool = _runner(tmp_path, {"main": [ChatResult(content=bad)]}, cfg, extra)
        assert [e.text for e in _run(runner.run_main(ctx)) if e.kind == "answer"] == [bad]
        assert len(pool.asked) == 1


def test_a_checked_answer_is_not_streamed(tmp_path):
    src = inspect.getsource(__import__("foundry_router.brain.agent", fromlist=["x"]))
    assert "and not checking)" in src
    assert "checking = code_checks.on_for(cc, ctx.persona, code_checks.task_bound())" in src


def test_settings_route_and_page():
    from foundry_router.ui import routes
    src = inspect.getsource(routes)
    assert '@router.post("/admin/api/code-checks")' in src and "code_checks.run_one(rule, code" in src
    assert 'raw["code_checks"] = new' in src
    html = (Path(routes.__file__).parent / "static" / "index.html").read_text(encoding="utf-8")
    for fid in ("cc-on", "cc-fixes", "cc-rules", "cc-save", "cc-test", "cc-test-code", "cc-test-rule"):
        assert f'id="{fid}"' in html, fid
    assert 'data-jform="codeChecks"' in html and 'codeChecks: {kind: "rows", required: "name"' in html
    assert CodeChecksConfig().enabled is False and CodeChecksConfig().max_fixes == 1


@pytest.mark.asyncio
async def test_the_route_tries_one_rule(client):
    r = client.post("/admin/api/code-checks", json={"test": {
        **PY_RULE, "code": "def f(:", "language": "python"}})
    assert r.json() == {"ok": True, "passed": False, "reason": r.json()["reason"]}
    assert "line 1" in r.json()["reason"]
    good = client.post("/admin/api/code-checks", json={"test": {**PY_RULE, "code": "x = 1"}})
    assert good.json()["passed"] is True
    saved = client.post("/admin/api/code-checks", json={
        "enabled": True, "max_fixes": 2, "rules": [PY_RULE]})
    assert saved.json() == {"ok": True}
    assert client.get("/admin/api/config").json()["code_checks"]["max_fixes"] == 2
    bad = client.post("/admin/api/code-checks", json={"enabled": True, "max_fixes": 99, "rules": []})
    assert bad.status_code == 400


def test_a_checker_never_sees_the_routers_keys(tmp_path):
    """The router's environment holds the backends' API keys; a checker is given a
    small environment of its own."""
    import os as _os
    spy = tmp_path / "env.txt"
    script = ("import os, pathlib, sys; "
              "pathlib.Path(sys.argv[2]).write_text(repr(sorted(os.environ)))")
    rule = {"name": "env", "kind": "command", "languages": ["luau"],
            "command": f'"{sys.executable}" -c "{script}" {{file}} "{spy}"'}
    _os.environ["FOUNDRY_SECRET_PROBE"] = "sk-must-not-be-passed"
    try:
        assert _run_check(_cfg(rule), "```luau\nlocal a = 1\n```") == []
    finally:
        _os.environ.pop("FOUNDRY_SECRET_PROBE", None)
    seen = spy.read_text()
    assert "FOUNDRY_SECRET_PROBE" not in seen and "NO_COLOR" in seen and "PATH" in seen


def test_a_bound_request_is_recognised(monkeypatch):
    """The router knows which requests a coordinator sprint owns."""
    from foundry_router import client_auth
    monkeypatch.setattr(client_auth, "current", lambda: {})
    assert cc.task_bound() is False
    monkeypatch.setattr(client_auth, "current", lambda: {"work_task_id": "UI-7", "run_id": "UI-7-a1"})
    assert cc.task_bound() is True
    monkeypatch.setattr(client_auth, "current", lambda: {"project_id": "animaluprising"})
    assert cc.task_bound() is False                      # a project alone is not a task

    def boom():
        raise RuntimeError("no request here")
    monkeypatch.setattr(client_auth, "current", boom)
    assert cc.task_bound() is False                      # never raises into a reply


def test_sprint_work_can_be_left_to_its_gate(tmp_path, monkeypatch):
    from foundry_router import client_auth
    bad = "```python\ndef f(:\n```"
    monkeypatch.setattr(client_auth, "current", lambda: {"work_task_id": "UI-7"})
    runner, ctx, pool = _runner(tmp_path, {"main": [ChatResult(content=bad)]},
                                _cfg(PY_RULE, skip_gated=True))
    assert [e.text for e in _run(runner.run_main(ctx)) if e.kind == "answer"] == [bad]
    assert len(pool.asked) == 1                          # not checked, not asked to fix
    # the same request without that switch is checked
    runner, ctx, pool = _runner(tmp_path, {"main": [ChatResult(content=bad)] * 2}, _cfg(PY_RULE))
    _run(runner.run_main(ctx))
    assert len(pool.asked) == 2


def test_the_persona_switch_is_stored_and_in_the_editor(tmp_path):
    from foundry_router.db import Database
    from foundry_router.personas import PERSONA_FIELDS, PersonaStore
    db = Database(tmp_path / "p.sqlite")
    cols = {r["name"] for r in db.query("PRAGMA table_info(personas)")}
    assert "code_checks" in cols and "code_checks" in PERSONA_FIELDS
    store = PersonaStore(db)
    store.upsert("Foundry-Chat", enabled=1)
    assert store.get("Foundry-Chat")["code_checks"] == 1            # on unless turned off
    store.upsert("Foundry-Kilo-Local", enabled=1, code_checks=0)
    assert store.get("Foundry-Kilo-Local")["code_checks"] == 0
    html = (Path(__file__).parent.parent / "foundry_router" / "ui" / "static" / "index.html").read_text(encoding="utf-8")
    assert 'id="pe-codechecks"' in html and 'code_checks: $("#pe-codechecks").checked ? 1 : 0,' in html
    assert '$("#pe-codechecks").checked = p.code_checks == null ? true : !!p.code_checks;' in html
    assert 'id="cc-skip-gated"' in html and 'skip_gated_tasks: $("#cc-skip-gated").checked,' in html


# -- the checkers in a container of their own ---------------------------------------------

def _service_rule(url, command="luau-lsp analyze {file}"):
    return {"name": "luau", "kind": "service", "languages": ["luau"], "url": url,
            "command": command, "timeout_seconds": 5}


def test_the_service_is_asked_and_its_words_are_what_the_model_is_told(monkeypatch):
    import httpx

    from foundry_router import code_checks as mod
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        if "bad" in seen["body"]["code"]:
            return httpx.Response(200, json={"status": 1, "output": "TypeError: unknown member .Foo"})
        return httpx.Response(200, json={"status": 0, "output": ""})

    _mock_httpx(monkeypatch, handler)
    rule = _service_rule("http://code-checks:8099/")
    assert _run_check(_cfg(rule), "```luau\nlocal x = 1\n```") == []
    assert seen["url"] == "http://code-checks:8099/run"
    assert seen["body"] == {"command": "luau-lsp analyze {file}", "code": "local x = 1\n",
                            "suffix": ".luau", "timeout": 5.0}
    probs = _run_check(_cfg(rule), "```luau\nlocal x = bad\n```")
    assert probs[0]["reason"] == "TypeError: unknown member .Foo" and probs[0]["rule"] == "luau"


def _mock_httpx(monkeypatch, handler):
    """Every httpx.AsyncClient answers through `handler`, whatever was patched before."""
    import httpx
    base = getattr(httpx.AsyncClient, "_foundry_base", httpx.AsyncClient)

    class Client(base):
        _foundry_base = base

        def __init__(self, *a, **k):
            k.pop("transport", None)
            super().__init__(*a, transport=httpx.MockTransport(handler), **k)
    monkeypatch.setattr(httpx, "AsyncClient", Client)


def test_a_service_that_is_not_up_never_blames_the_model(monkeypatch):
    import httpx

    def refused(request):
        raise httpx.ConnectError("connection refused")
    _mock_httpx(monkeypatch, refused)
    said = []
    assert _run_check(_cfg(_service_rule("http://code-checks:8099")), "```luau\na\n```",
                      log=said.append) == []
    assert any("could not be reached" in s and "could not run" in s for s in said)


def test_a_service_that_refuses_the_check_is_not_the_models_fault(monkeypatch):
    import httpx

    def refuses(request):
        return httpx.Response(400, json={"error": "'rm' is not one of this service's checkers"})
    _mock_httpx(monkeypatch, refuses)
    said = []
    assert _run_check(_cfg(_service_rule("http://x", "rm -rf /")), "```luau\na\n```",
                      log=said.append) == []
    assert any("refused this check" in s for s in said)


def test_a_service_rule_with_no_address():
    said = []
    assert _run_check(_cfg(_service_rule("")), "```luau\na\n```", log=said.append) == []
    assert any("no service address" in s for s in said)


def test_the_service_itself_only_runs_its_own_checkers(tmp_path):
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "cc_server", Path(__file__).parent.parent / "contrib" / "code-checks" / "server.py")
    srv = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(srv)
    assert srv.ALLOWED == {"luau-lsp", "selene", "stylua"}
    status, body = srv.run({"command": "rm -rf /", "code": "x"})
    assert status == 400 and "not one of this service's checkers" in body["error"]
    status, body = srv.run({"command": "sh -c 'echo hi'", "code": "x"})
    assert status == 400 and "not one of" in body["error"]
    assert srv.run({"command": "", "code": "x"})[1]["error"] == "command is required"
    assert srv.run({"command": "selene {file}", "code": ""})[1]["error"] == "code is required"
    assert "larger than" in srv.run({"command": "selene {file}", "code": "x" * 2_000_000})[1]["error"]
    srv.shutil.which = lambda name: None                 # as if nothing were installed
    status, body = srv.run({"command": "selene {file}", "code": "local x = 1"})
    assert status == 400 and "not installed in this service" in body["error"]


def test_the_container_is_optional_and_pinned():
    import yaml
    root = Path(__file__).parent.parent
    compose = yaml.safe_load((root / "docker-compose.yml").read_text(encoding="utf-8"))
    svc = compose["services"]["code-checks"]
    assert svc["profiles"] == ["code-checks"]            # not started by a plain `up`
    assert "ports" not in svc                            # no port on the host
    assert svc["read_only"] is True and svc["cap_drop"] == ["ALL"]
    assert svc["security_opt"] == ["no-new-privileges:true"]
    docker = (root / "contrib" / "code-checks" / "Dockerfile").read_text(encoding="utf-8")
    for tool in ("LUAU_LSP", "SELENE", "STYLUA"):
        assert f"{tool}_VERSION=" in docker and f"{tool}_SHA256=" in docker
    assert docker.count("sha256sum -c -") == 4           # every download is checked
    # Roblox's type definitions: without them luau-lsp calls `game` an unknown global
    assert "GLOBAL_TYPES_SHA256=" in docker and "globalTypes.d.luau" in docker
    assert "/usr/local/share/luau/globalTypes.d.luau" in docker
    assert "USER checker" in docker
    html = (root / "foundry_router" / "ui" / "static" / "index.html").read_text(encoding="utf-8")
    assert '{key: "url", label: "service address"' in html
    assert '"service", "command", "python", "json"' in html

