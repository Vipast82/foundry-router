"""Automation config store (C1): schema, drafts with an optimistic lock,
deploy / rollback / diff / audit, profile import round trip, the router-wide
cutoffs document, and the coordinator's pull + applied report."""

import copy
import tomllib
from pathlib import Path

import pytest

from foundry_automation import FORMAT
from foundry_automation import document as D
from foundry_automation import schema as S
from foundry_router.config import ClientConfig

EXAMPLE = (Path(__file__).resolve().parent.parent / "foundry_coordinator" / "projects" /
           "animaluprising" / "project.example.toml")
H = {"X-Foundry-Automation": "1", "X-Foundry-Actor": "victor"}
COORD = {"Authorization": "Bearer coord-key"}
A = "/admin/api/automation"


def _coord(svc, projects=None):
    svc.config_store.config.clients.append(
        ClientConfig(name="coordinator", key="coord-key", scopes=["orchestration"],
                     projects=projects or []))


def _import(client, project="animaluprising"):
    r = client.post(f"{A}/projects/{project}/import", headers=H,
                    json={"toml": EXAMPLE.read_text(encoding="utf-8")})
    assert r.status_code == 200, r.text
    return r.json()


def _deploy(client, project="animaluprising", note="go"):
    d = client.get(f"{A}/projects/{project}/draft").json()
    r = client.post(f"{A}/projects/{project}/deploy", headers=H,
                    json={"etag": d["etag"], "note": note})
    return r


# --------------------------------------------------------------------------- schema

def test_schema_describes_every_table_and_role(client):
    s = client.get(f"{A}/schema").json()
    assert s["format"] == FORMAT
    keys = {f["key"] for f in s["fields"]}
    for k in ("lead_manager.write_access", "lead_manager.allow_local_route",
              "retry_guidance.local_fallback", "fast_lane.vision_parallel",
              "automation.scope_auto_approve", "automation.decision_timeout_minutes",
              "automation.helpers", "fast_lane.early_review",
              "backends.*.cutoff_pct", "roles.implementer", "role_overrides.*.tasks",
              "limits.max_attempts", "commit.prefix", "review.max_diff_bytes"):
        assert k in keys, k
    f = {x["key"]: x for x in s["fields"]}
    assert f["client.url"]["scope"] == "local" and f["foundry.key_env"]["scope"] == "local"
    assert f["lead_manager.persona"]["role_binding"] == "lead"
    assert f["helpers.personas.*"]["role_binding"] == "helper"
    assert f["lead_manager.max_diagnoses"]["min"] == 1
    assert set(s["roles"]) == {"implementer", "lead", "reviewer", "deep_reviewer",
                               "visual_reviewer", "planner", "researcher", "helper",
                               "coordinator"}
    assert s["role_rules"]["hard_denies"] and set(s["role_rules"]["defaults"]) == set(s["roles"])


def test_validate_reports_types_limits_strict_tables_and_secrets():
    doc = {"format": FORMAT, "project": "p", "settings": {
        "roles": {"implementer": "X"},
        "limits": {"max_attempts": 0, "max_sessions_per_task": "many"},
        "lead_manager": {"bogus": 1},
        "fast_lane": {"lanes": ["nope"]},
        "publish": {"mode": "sideways"},
        "client": {"url": "http://x", "token": "sk-abcdefghijklmnopqrstu"},
        "whatever": {"x": 1}}}
    v = D.validate(doc, "p")
    paths = {e["path"]: e["code"] for e in v["errors"]}
    assert not v["ok"]
    assert "limits.max_attempts" in paths and "limits.max_sessions_per_task" in paths
    assert "lead_manager" in paths                       # unknown key in a strict table
    assert "fast_lane.lanes" in paths
    assert "publish.mode" in paths
    assert paths.get("client.url") == "local_key"
    assert any(c == "secret" for c in paths.values())
    assert any(w["path"] == "whatever.x" for w in v["warnings"])


def test_automation_table_matches_the_coordinator_rules():
    doc = D.empty("p")
    doc["settings"] = {"roles": {"implementer": "X"}, "automation": {
        "helpers": "on_demand", "decision_timeout_minutes": 0,
        "scope_auto_approve": [{"name": "gate", "path": "tests/GateRunner.lua",
                                "blocks": ["EXPECTED_BOOT"]}]}}
    assert D.validate(doc, "p")["ok"]
    for bad in ({"helpers": "always"}, {"nope": 1}, {"decision_timeout_minutes": -1},
                {"scope_auto_approve": [{"path": "../outside"}]},
                {"scope_auto_approve": [{"path": "x", "lines": ["("]}]}):
        doc["settings"]["automation"] = bad
        v = D.validate(doc, "p")
        assert not v["ok"] and any(e["path"].startswith("automation") for e in v["errors"]), bad
    f = {x.key: x for x in S.FIELDS}
    assert f["automation.helpers"].enum == ["on_demand", "proactive"]
    assert f["automation.scope_auto_approve"].default == S.AUTOMATION_DEFAULTS[
        "scope_auto_approve"]


def test_validate_needs_implementer_and_explicit_override_tasks():
    doc = D.empty("p")
    doc["settings"] = {"roles": {"reviewer": "R"},
                       "role_overrides": {"a": {"tasks": ["UI-*"], "implementer": "L"}}}
    v = D.validate(doc, "p")
    msgs = " ".join(e["message"] for e in v["errors"])
    assert "roles.implementer is required" in msgs and "explicit task IDs" in msgs


def test_shared_schema_is_what_the_profile_loader_uses():
    from foundry_coordinator import profile
    assert profile.LEAD_MANAGER_DEFAULTS is S.LEAD_MANAGER_DEFAULTS
    assert profile.FAST_LANE_DEFAULTS is S.FAST_LANE_DEFAULTS
    with pytest.raises(profile.ProfileError, match="unknown \\[lead_manager\\] keys"):
        profile._lead_manager_table("x", {"nope": 1})


# --------------------------------------------------------------------------- import round trip

def test_import_round_trip_keeps_every_setting_and_drops_local_keys():
    raw = tomllib.loads(EXAMPLE.read_text(encoding="utf-8"))
    doc, dropped, _ = D.import_profile(raw)
    assert set(dropped) >= {"workspace.path", "client.url", "client.password_env",
                            "foundry.url", "foundry.key_env", "adapter.module"}
    assert "url" not in doc["settings"]["client"] and "id" not in doc["settings"]
    assert D.validate(doc, "animaluprising")["ok"]
    back = D.overlay(raw, doc)
    assert back.pop("role_rules")
    assert back == raw
    # the router's value wins for project keys; local keys always stay local
    doc2 = copy.deepcopy(doc)
    doc2["settings"]["limits"]["max_attempts"] = 9
    doc2["settings"]["client"]["url"] = "http://evil:1"
    merged = D.overlay(raw, doc2)
    assert merged["limits"]["max_attempts"] == 9
    assert merged["client"]["url"] == raw["client"]["url"]


def test_local_only_tables_and_env_names_never_enter_a_document():
    raw = tomllib.loads(EXAMPLE.read_text(encoding="utf-8"))
    raw["config_pull"] = {"enabled": True, "kilo_config": "D:/x/kilo.jsonc"}
    raw["telemetry"]["key_env"] = "FOUNDRY_TELEMETRY_KEY"
    doc, dropped, _ = D.import_profile(raw)
    assert "config_pull" not in doc["settings"]
    assert "key_env" not in doc["settings"]["telemetry"]
    assert {"config_pull.enabled", "config_pull.kilo_config", "telemetry.key_env"} <= set(dropped)
    assert doc["settings"]["foundry"] == {"require_binding": True}
    assert D.overlay(raw, doc)["config_pull"] == raw["config_pull"]


def test_import_endpoint_saves_a_draft_and_rejects_another_project(client):
    out = _import(client)
    assert out["validation"]["ok"] and "client.url" in out["dropped_local"]
    d = client.get(f"{A}/projects/animaluprising/draft").json()
    assert d["dirty"] and d["doc"]["settings"]["limits"]["max_attempts"] == 3
    r = client.post(f"{A}/projects/other/import", headers=H,
                    json={"toml": EXAMPLE.read_text(encoding="utf-8")})
    assert r.status_code == 409 and r.json()["code"] == "project_mismatch"
    dry = client.post(f"{A}/projects/animaluprising/import", headers=H,
                      json={"toml": EXAMPLE.read_text(encoding="utf-8"), "dry_run": True})
    assert dry.status_code == 200 and "etag" not in dry.json()


# --------------------------------------------------------------------------- drafts / versions

def test_mutations_need_the_automation_header(client):
    r = client.post(f"{A}/projects/animaluprising/import",
                    json={"toml": EXAMPLE.read_text(encoding="utf-8")})
    assert r.status_code == 403 and r.json()["code"] == "csrf"


def test_draft_etag_lock_and_refusals(client):
    _import(client)
    d = client.get(f"{A}/projects/animaluprising/draft").json()
    doc = d["doc"]
    doc["settings"]["limits"]["max_attempts"] = 5
    ok = client.put(f"{A}/projects/animaluprising/draft", headers=H,
                    json={"doc": doc, "etag": d["etag"]})
    assert ok.status_code == 200 and ok.json()["validation"]["ok"]
    stale = client.put(f"{A}/projects/animaluprising/draft", headers=H,
                       json={"doc": doc, "etag": d["etag"]})
    assert stale.status_code == 409 and stale.json()["current_etag"] == ok.json()["etag"]
    # an invalid draft is still saved (work isn't lost) ...
    doc["settings"]["limits"]["max_attempts"] = -1
    bad = client.put(f"{A}/projects/animaluprising/draft", headers=H,
                     json={"doc": doc, "etag": ok.json()["etag"]})
    assert bad.status_code == 200 and not bad.json()["validation"]["ok"]
    # ... but a local key or a secret is never stored
    doc["settings"]["foundry"] = {"url": "http://x"}
    loc = client.put(f"{A}/projects/animaluprising/draft", headers=H,
                     json={"doc": doc, "etag": bad.json()["etag"]})
    assert loc.status_code == 422
    assert loc.json()["validation"]["errors"][0]["code"] == "local_key"
    stored = client.get(f"{A}/projects/animaluprising/draft").json()["doc"]["settings"]
    assert "url" not in (stored.get("foundry") or {})


def test_deploy_rollback_diff_and_audit(client):
    _import(client)
    r1 = _deploy(client, note="first")
    assert r1.status_code == 200 and r1.json()["rev"] == 1
    again = _deploy(client).json()
    assert again["unchanged"] and again["rev"] == 1
    d = client.get(f"{A}/projects/animaluprising/draft").json()
    assert not d["dirty"] and d["base_rev"] == 1
    doc = d["doc"]
    doc["settings"]["lead_manager"]["max_diagnoses"] = 5
    doc["settings"]["limits"]["max_attempts"] = 4
    doc["settings"]["fast_lane"] = {"enabled": True, "tasks": ["UI-D1"], "vision_parallel": 4}
    doc["role_rules"]["implementer"]["edit_paths"]["allow"] = ["C:\\Roblox\\**"]
    put = client.put(f"{A}/projects/animaluprising/draft", headers=H,
                     json={"doc": doc, "etag": d["etag"]})
    diff = client.get(f"{A}/projects/animaluprising/diff").json()["changes"]
    paths = {c["path"]: c for c in diff}
    assert paths["limits.max_attempts"] == {"path": "limits.max_attempts", "op": "changed",
                                            "from": 3, "to": 4, "apply": "live"}
    assert paths["lead_manager.max_diagnoses"]["op"] == "added"
    assert paths["fast_lane.vision_parallel"]["op"] == "added"
    assert "role_rules.implementer.edit_paths.allow" in paths
    r2 = client.post(f"{A}/projects/animaluprising/deploy", headers=H,
                     json={"etag": put.json()["etag"], "note": "second"})
    assert r2.status_code == 200 and r2.json()["rev"] == 2
    revs = client.get(f"{A}/projects/animaluprising/revisions").json()["revisions"]
    assert [r["rev"] for r in revs] == [2, 1] and revs[0]["actor"] == "victor"
    # rollback appends a copy of rev 1 and resets the draft
    rb = client.post(f"{A}/projects/animaluprising/rollback", headers=H, json={"rev": 1})
    assert rb.status_code == 200 and rb.json()["rev"] == 3
    r3 = client.get(f"{A}/projects/animaluprising/revisions/3").json()
    assert r3["source"] == "rollback" and r3["rollback_of"] == 1
    assert r3["hash"] == client.get(f"{A}/projects/animaluprising/revisions/1").json()["hash"]
    assert client.get(f"{A}/projects/animaluprising/diff?from=1&to=deployed").json()[
        "changes"] == []
    assert client.get(f"{A}/projects/animaluprising/draft").json()["dirty"] is False
    actions = [a["action"] for a in
               client.get(f"{A}/projects/animaluprising/audit").json()["audit"]]
    assert actions[:2] == ["rollback", "deploy"] and "import" in actions
    proj = {p["project"]: p for p in client.get(f"{A}/projects").json()["projects"]}
    assert proj["animaluprising"]["deployed_rev"] == 3 and "_router" in proj


def test_deploy_refuses_an_invalid_or_stale_draft(client):
    _import(client)
    d = client.get(f"{A}/projects/animaluprising/draft").json()
    doc = d["doc"]
    doc["settings"]["limits"]["max_attempts"] = 0
    put = client.put(f"{A}/projects/animaluprising/draft", headers=H,
                     json={"doc": doc, "etag": d["etag"]})
    bad = client.post(f"{A}/projects/animaluprising/deploy", headers=H,
                      json={"etag": put.json()["etag"]})
    assert bad.status_code == 422 and not bad.json()["validation"]["ok"]
    stale = client.post(f"{A}/projects/animaluprising/deploy", headers=H,
                        json={"etag": d["etag"]})
    assert stale.status_code == 409
    assert client.get(f"{A}/projects/animaluprising/revisions").json()["revisions"] == []


def test_restart_needed_keys_are_reported(client):
    _import(client)
    _deploy(client)
    d = client.get(f"{A}/projects/animaluprising/draft").json()
    d["doc"]["settings"]["client"]["provider"] = "other"
    v = client.post(f"{A}/projects/animaluprising/validate", headers=H,
                    json={"doc": d["doc"]}).json()
    assert v["ok"] and v["restart_needed"] == ["client.provider"]


# --------------------------------------------------------------------------- role rules API

def test_role_rule_preview_and_path_tester(client):
    rules = {"permissions": {"edit": "allow"}, "edit_paths": {"allow": ["C:\\Roblox\\**"],
                                                             "deny": []}}
    pv = client.post(f"{A}/role-rules/preview", headers=H,
                     json={"role": "implementer", "rules": rules,
                           "workspace": "C:/Roblox/AnimalUprising"}).json()
    assert pv["agent"] == "code"
    assert {"permission": "external_directory", "pattern": "C:/Roblox/*",
            "action": "allow"} in pv["session_permission"]
    t = lambda **k: client.post(f"{A}/role-rules/test-path", headers=H, json={  # noqa: E731
        "role": "implementer", "rules": rules, "workspace": "C:/Roblox/AnimalUprising",
        **k}).json()["decision"]
    assert t(op="edit", path="C:\\Roblox\\Shared\\x.lua") == "allow"
    assert t(op="edit", path="src/x.lua") == "allow"
    assert t(op="edit", path="C:\\Other\\x.lua") == "deny"
    assert t(op="read", path="C:/Roblox/_secrets/key.txt") == "deny"
    assert t(op="bash", command="git push --force origin master") == "deny"
    assert t(op="bash", command="git status") == "allow"
    bad = client.post(f"{A}/role-rules/preview", headers=H, json={"role": "boss"})
    assert bad.status_code == 400


# --------------------------------------------------------------------------- router document

def test_router_document_sets_per_backend_cutoffs(client, app):
    svc = app.state.services
    svc.config_store.save(lambda raw: raw["backend_pool"]["internal"].__setitem__(
        "backends", [{"name": "cliproxyapi", "type": "openai-compatible",
                      "url": "http://127.0.0.1:9/v1"}]))
    d = client.get(f"{A}/projects/_router/draft").json()
    doc = d["doc"]
    doc["settings"] = {"backends": {"cliproxyapi": {"cutoff_pct": 90},
                                    "meridian": {"cutoff_pct": 80}}}
    put = client.put(f"{A}/projects/_router/draft", headers=H,
                     json={"doc": doc, "etag": d["etag"]})
    assert put.json()["validation"]["ok"], put.json()
    dep = client.post(f"{A}/projects/_router/deploy", headers=H,
                      json={"etag": put.json()["etag"]}).json()
    assert dep["rev"] == 1 and any("meridian" in n for n in dep["router_notes"])
    b = next(b for b in svc.config_store.config.backend_pool.internal.backends
             if b.name == "cliproxyapi")
    assert b.usage_limit_pct == 90.0
    # cutoffs are only valid in the router document, and must be 1..100
    d = client.get(f"{A}/projects/_router/draft").json()
    d["doc"]["settings"]["backends"]["cliproxyapi"]["cutoff_pct"] = 150
    assert not client.post(f"{A}/projects/_router/validate", headers=H,
                           json={"doc": d["doc"]}).json()["ok"]
    _import(client)
    p = client.get(f"{A}/projects/animaluprising/draft").json()["doc"]
    p["settings"]["backends"] = {"x": {"cutoff_pct": 50}}
    assert not client.post(f"{A}/projects/animaluprising/validate", headers=H,
                           json={"doc": p}).json()["ok"]


# --------------------------------------------------------------------------- coordinator pull

def test_coordinator_pull_etag_and_applied_report(client, app):
    _coord(app.state.services)
    r = client.get("/v1/foundry/control/config?project=animaluprising", headers=COORD)
    assert r.status_code == 404 and r.json()["code"] == "no_revision"
    assert client.get("/v1/foundry/control/config?project=animaluprising").status_code == 401
    _import(client)
    _deploy(client)
    r = client.get("/v1/foundry/control/config?project=animaluprising", headers=COORD)
    body = r.json()
    assert r.status_code == 200 and body["rev"] == 1 and body["doc"]["format"] == FORMAT
    assert r.headers["etag"] == f'"{body["hash"]}"'
    again = client.get("/v1/foundry/control/config?project=animaluprising",
                       headers={**COORD, "If-None-Match": r.headers["etag"]})
    assert again.status_code == 304
    rep = client.post("/v1/foundry/control/config/applied", headers=COORD, json={
        "project": "animaluprising", "rev": 1, "hash": body["hash"], "status": "applied",
        "source_id": "coordinator:ws", "restart_needed": ["client.provider"]})
    assert rep.status_code == 200
    ap = client.get(f"{A}/projects/animaluprising/applied").json()["applied"]
    assert ap[0]["status"] == "applied" and ap[0]["restart_needed"] == ["client.provider"]
    audit = client.get(f"{A}/projects/animaluprising/audit").json()["audit"]
    assert audit[0]["action"] == "applied" and audit[0]["actor"] == "coordinator:coordinator:ws"


def test_coordinator_key_restricted_to_other_projects_is_refused(client, app):
    _coord(app.state.services, projects=["other"])
    _import(client)
    _deploy(client)
    r = client.get("/v1/foundry/control/config?project=animaluprising", headers=COORD)
    assert r.status_code == 403
