"""Turn assist tooling: global/tunable config, rule exemptions, the model's
next action after an intervention, data export / clear / retention, the
subscription budget readout, timestamp windows, and the Cline session replay."""

import io
import json
import zipfile
from datetime import datetime, timedelta, timezone

from foundry_router import turn_assist as ta
from foundry_router.pool.protocols import ChatResult

from tests.test_turn_assist import FAIL, FILE, ScriptPool, history, post, tc  # noqa: F401


def test_config_precedence_and_unknown_keys_kept():
    from foundry_router.config import TurnAssistConfig
    g = TurnAssistConfig(defaults={"sup_loop_repeats": 4, "auditor": "observe"},
                         block_rules=[{"pattern": r"\bdrop table\b", "rule": "sql drop",
                                       "advice": "ask", "enabled": True}],
                         error_patterns=[r"KABOOM"])
    cfg = ta.config({"assist": {"sup_loop_repeats": 5,
                                "auditor_required_patterns": ["PASS"], "my_new_knob": 1}}, g)
    assert cfg["sup_loop_repeats"] == 5 and cfg["auditor"] == "observe"
    assert cfg["auditor_required_patterns"] == ["PASS"] and cfg["my_new_knob"] == 1
    j = ta.prejudge([tc("run_commands", {"commands": ["sqlite3 x 'DROP TABLE t'"]})],
                    {**cfg, "prejudge": "enforce"})
    assert [r for _, r, _ in j.blocked] == ["sql drop"]
    assert ta.is_error("all good KABOOM", ta.error_rx(cfg))
    assert not ta.is_error(FAIL, ta.error_rx(cfg))       # built-ins replaced


def test_rule_unless_exempts_scratch_and_disabled_rules():
    cfg = {"prejudge": "enforce"}

    def blocked(c, extra=None):
        return ta.prejudge([tc("run_commands", {"commands": [c]})], {**cfg, **(extra or {})}).blocked

    assert blocked("Set-Content src\\a.lua -Value x")
    assert not blocked("Set-Content _probe.lua -Value x")
    assert not blocked("node -e \"process.env.NODE_ENV='test'\"")    # not a credential read
    assert blocked("Get-Content .env")
    assert not blocked("Get-ChildItem dist -Recurse -Include *.mp3")  # listing, not deleting
    assert not blocked("Remove-Item $env:TEMP\\s21 -Recurse -Force")
    assert not blocked("git add -A", {"prejudge_disabled_rules": ["staging everything"]})


def test_turn_assist_config_api_validates_and_reports(app, client):
    r = client.post("/admin/api/config/turn_assist", json={"block_rules": [{"pattern": "("}]})
    assert r.status_code == 400
    r = client.post("/admin/api/config/turn_assist", json={"defaults": {"sup_loop_repeats": 4}})
    assert r.json()["turn_assist"]["defaults"] == {"sup_loop_repeats": 4}
    g = client.get("/admin/api/config/turn_assist").json()
    assert g["builtin_defaults"]["sup_play_calls"] == 10 and g["builtin_block_rules"]


def test_model_next_action_recorded_after_intervention(app, client):
    svc = app.state.services
    svc.config_store.config.agent_brain.direct_stream = False
    svc.pool = ScriptPool({"qwen": [
        ChatResult(content="", tool_calls=[tc("run_commands", {"commands": ["git reset --hard"]})]),
        ChatResult(content="Using git stash instead.",
                   tool_calls=[tc("run_commands", {"commands": ["git stash"]}, "t2")]),
    ]})
    svc.personas.upsert("Act", execution_mode="direct", model_allowlist=["qwen"],
                        pinned_models=[], assist={"prejudge": "enforce"})
    post(client, history(("read_files", {"files": []}, FILE)))
    row = svc.db.query_one("SELECT * FROM interventions WHERE kind='prejudge_block'")
    nxt = json.loads(row["next_action"])
    assert nxt["text"] == "Using git stash instead."
    assert nxt["tool_calls"][0]["name"] == "run_commands"
    line = json.loads(client.get("/admin/api/interventions/export?hours=1").text.splitlines()[0])
    assert line["model_next_action"]["tool_calls"][0]["name"] == "run_commands"


def test_data_export_clear_prune(app, client):
    from foundry_router import interventions as iv
    from foundry_router.datatools import prune
    db = app.state.services.db
    iv.record(db, conversation_key="k1", persona="Act", kind="dedupe", detail="a")
    iv.record(db, conversation_key="k2", persona="Act", kind="dedupe", detail="b")
    db.execute("UPDATE interventions SET ts='2020-01-01T00:00:00+00:00' "
               "WHERE conversation_key='k1'")
    t = {r["table"]: r for r in client.get("/admin/api/data/tables").json()["tables"]}
    assert "kv" not in t and t["interventions"]["rows"] == 2 and t["interventions"]["log"]
    csv_text = client.get("/admin/api/data/export?table=interventions&format=csv"
                          "&conversation_key=k2").text
    assert csv_text.splitlines()[0].startswith("id,ts,conversation_key") and "k2" in csv_text
    assert "k1" not in csv_text
    assert client.get("/admin/api/data/export?table=kv").status_code == 400
    z = zipfile.ZipFile(io.BytesIO(client.get("/admin/api/data/export-all?format=jsonl").content))
    assert "manifest.json" in z.namelist() and "interventions.jsonl" in z.namelist()
    assert "kv.jsonl" not in z.namelist()
    assert client.post("/admin/api/data/clear", json={"table": "personas"}).status_code == 400
    assert prune(db, {"interventions": 30}) == {"interventions": 1}
    assert client.post("/admin/api/data/clear",
                       json={"table": "interventions"}).json()["deleted"] == 1


def test_subscription_budget_readout(app, client):
    from foundry_router.config import BackendConfig
    from foundry_router.usage import log_subscription_usage
    svc = app.state.services
    svc.config_store.config.backend_pool.internal.backends = [
        BackendConfig(name="cpa", type="openai-compatible", url="http://x", subscription=True,
                      call_budget_5h=10),
        BackendConfig(name="local", type="ollama", url="http://y")]
    log_subscription_usage(svc.db, "gpt", "cpa", 1, 1)
    b = client.get("/admin/api/subscription-budgets").json()["backends"]
    assert [x["backend"] for x in b] == ["cpa"]
    assert b[0]["5h"] == {"used": 1, "budget": 10, "remaining": 9}
    assert b[0]["daily"]["budget"] is None


def test_usage_windows_compare_real_times(app):
    from foundry_router.usage import observed_subscription_usage, spend_since
    db = app.state.services.db
    old = (datetime.now(timezone.utc) - timedelta(hours=6)).isoformat()
    db.execute("INSERT INTO claude_usage_log (ts, model, backend, prompt_tokens, "
               "completion_tokens) VALUES (?,?,?,?,?)", (old, "m", "b", 1, 1))
    u = observed_subscription_usage(db)
    assert u["last_5h"]["calls"] == 0 and u["last_7d"]["calls"] == 1
    assert spend_since(db, "-1 hours") == 0


def test_replay_converts_cline_sessions_and_scores_findings(tmp_path):
    from foundry_router import replay as rp
    stored = [{"role": "user", "content": [{"type": "text", "text": "do S43"}]}]
    for i in range(4):
        stored.append({"role": "assistant", "content": [
            {"type": "tool_use", "id": f"u{i}", "name": "run_commands",
             "input": {"commands": ["lune run t"]}}]})
        stored.append({"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": f"u{i}", "name": "run_commands",
             "content": [{"query": "lune run t", "result": "[Command exited with code 1] x",
                          "success": False}]}]})
    d = tmp_path / "s1"
    d.mkdir()
    (d / "s1.messages.json").write_text(json.dumps({"messages": stored}), encoding="utf-8")
    (d / "s1.json").write_text(json.dumps({"prompt": "do S43", "status": "failed"}),
                               encoding="utf-8")
    r = rp.replay(str(tmp_path))
    assert r["totals"] == {"sessions": 1, "calls": 4, "errors": 4}
    loop = r["detectors"]["loop"]
    assert loop["fired"] == 1 and loop["kept_going_pct"] == 100.0   # a 4th identical call followed
    assert "loop" in rp.report(r)
