"""The router's half of the budget mode switch (foundry_router/budget_caps.py; item 16 of
COORDINATOR_AUTOMATION_NEEDS): a project's saving mode lowers the backends' cutoffs, and
going back to normal puts the Router settings values back."""

from foundry_router import budget_caps
from tests.test_automation_store import A, H, _deploy, _import

BACKENDS = [{"name": "meridian", "type": "anthropic-compatible", "url": "http://127.0.0.1:9"},
            {"name": "cliproxyapi", "type": "openai-compatible", "url": "http://127.0.0.1:9/v1"},
            {"name": "codex2", "type": "openai-compatible", "url": "http://127.0.0.1:9/v1"},
            {"name": "strata", "type": "ollama", "url": "http://127.0.0.1:9"}]


def limits(svc):
    return {b.name: b.usage_limit_pct
            for b in svc.config_store.config.backend_pool.internal.backends}


def setup(client, app, router_cutoffs):
    svc = app.state.services
    svc.config_store.save(lambda raw: raw["backend_pool"]["internal"].__setitem__(
        "backends", [dict(b) for b in BACKENDS]))
    d = client.get(f"{A}/projects/_router/draft").json()
    doc = d["doc"]
    doc["settings"] = {"backends": {k: {"cutoff_pct": v} for k, v in router_cutoffs.items()}}
    put = client.put(f"{A}/projects/_router/draft", headers=H, json={"doc": doc, "etag": d["etag"]})
    assert put.json()["validation"]["ok"], put.json()
    client.post(f"{A}/projects/_router/deploy", headers=H, json={"etag": put.json()["etag"]})
    _import(client)
    assert _deploy(client).status_code == 200
    return svc


def set_mode(client, project="animaluprising", **automation):
    d = client.get(f"{A}/projects/{project}/draft").json()
    doc = d["doc"]
    doc["settings"].setdefault("automation", {}).update(automation)
    put = client.put(f"{A}/projects/{project}/draft", headers=H, json={"doc": doc, "etag": d["etag"]})
    assert put.json()["validation"]["ok"], put.json()
    r = client.post(f"{A}/projects/{project}/deploy", headers=H, json={"etag": put.json()["etag"]})
    assert r.status_code == 200, r.text
    return r.json()


def test_a_saving_mode_lowers_the_cutoffs_and_normal_puts_them_back(client, app):
    svc = setup(client, app, {"meridian": 95, "cliproxyapi": 90})
    assert limits(svc) == {"meridian": 95.0, "cliproxyapi": 90.0, "codex2": None, "strata": None}

    out = set_mode(client, budget_mode="save_claude")
    assert limits(svc)["meridian"] == 75.0 and limits(svc)["cliproxyapi"] == 90.0
    assert out["router_notes"] == ["meridian: cutoff 75% (budget mode of animaluprising "
                                   "(save_claude); Router settings say 95%)"]

    out = set_mode(client, budget_mode="save_all", save_all_cutoff_pct=60)
    assert limits(svc) == {"meridian": 75.0, "cliproxyapi": 60.0, "codex2": None, "strata": None}
    # a paid backend with no cutoff under Router settings is named and left alone; the local
    # one is not mentioned
    assert any("'codex2' has no cutoff under Router settings" in n for n in out["router_notes"])
    assert not any("strata" in n for n in out["router_notes"])

    # the Router settings value stays the source: deploying it again keeps the mode's cap,
    # a value already lower than the mode's wins, and one raised is still capped
    d = client.get(f"{A}/projects/_router/draft").json()
    d["doc"]["settings"]["backends"] = {"meridian": {"cutoff_pct": 50},
                                        "cliproxyapi": {"cutoff_pct": 99}}
    put = client.put(f"{A}/projects/_router/draft", headers=H, json={"doc": d["doc"], "etag": d["etag"]})
    client.post(f"{A}/projects/_router/deploy", headers=H, json={"etag": put.json()["etag"]})
    assert limits(svc)["meridian"] == 50.0 and limits(svc)["cliproxyapi"] == 60.0

    # 0 = the mode leaves that cutoff
    set_mode(client, save_all_cutoff_pct=0)
    assert limits(svc)["cliproxyapi"] == 99.0 and limits(svc)["meridian"] == 50.0

    out = set_mode(client, budget_mode="normal")
    assert limits(svc) == {"meridian": 50.0, "cliproxyapi": 99.0, "codex2": None, "strata": None}
    assert "router_notes" not in out

    # a rollback to the saving revision caps again
    revs = client.get(f"{A}/projects/animaluprising/revisions").json()
    rows = revs["revisions"] if isinstance(revs, dict) else revs
    saving = sorted(r["rev"] for r in rows)[1]                # import, save_claude, ...
    r = client.post(f"{A}/projects/animaluprising/rollback", headers=H, json={"rev": saving})
    assert r.status_code == 200, r.text
    d = client.get(f"{A}/projects/_router/draft").json()
    d["doc"]["settings"]["backends"]["meridian"]["cutoff_pct"] = 95
    put = client.put(f"{A}/projects/_router/draft", headers=H, json={"doc": d["doc"], "etag": d["etag"]})
    client.post(f"{A}/projects/_router/deploy", headers=H, json={"etag": put.json()["etag"]})
    assert limits(svc)["meridian"] == 75.0


def test_plan_caps_from_the_default_when_the_document_gives_no_number():
    """A backend listed with no value is held to the config's default; the mode lowers it
    only when it is lower than that default, and never raises a cutoff."""
    class Cfg:
        class meridian:
            conserve_strong_at = 0.7
        class subscription_usage:
            limit_pct = 85.0
    raw = [dict(b) for b in BACKENDS]
    want = {"meridian": {}, "cliproxyapi": {"cutoff_pct": None}, "gone": {"cutoff_pct": 50}}
    by = {"game": "save_all"}
    values, notes = budget_caps.plan(Cfg, raw, want, {"claude": 75.0, "other": 60.0, "by": by})
    assert values == {"meridian": None, "cliproxyapi": 60.0}      # 75 would raise Claude's 70
    assert "backend 'gone' is not configured; its cutoff waits" in notes
    assert any(n.startswith("cliproxyapi: cutoff 60%") and "say 85%" in n for n in notes)
    values, notes = budget_caps.plan(Cfg, raw, want, {"claude": None, "other": None, "by": {}})
    assert values == {"meridian": None, "cliproxyapi": None} and len(notes) == 1


def test_the_lowest_of_several_projects_counts_and_a_plain_revision_writes_nothing(client, app):
    svc = setup(client, app, {"meridian": 95})
    saves = []
    real = svc.config_store.save
    svc.config_store.save = lambda fn: (saves.append(1), real(fn))[1]
    # a project revision that changes no cutoff does not rewrite config.yaml
    out = set_mode(client, max_agents=3)
    assert saves == [] and "router_notes" not in out
    set_mode(client, budget_mode="save_claude", save_claude_cutoff_pct=80)
    assert saves == [1] and limits(svc)["meridian"] == 80.0

    class Store:
        docs = {"a": {"budget_mode": "save_claude", "save_claude_cutoff_pct": 80},
                "b": {"budget_mode": "save_all", "save_claude_cutoff_pct": 70},
                "c": {"budget_mode": "normal", "save_claude_cutoff_pct": 10},
                "d": {"budget_mode": "save_all", "save_claude_cutoff_pct": 0,
                      "save_all_cutoff_pct": 500},
                "never_deployed": {"budget_mode": "save_all", "save_claude_cutoff_pct": 5}}

        def projects(self):
            return [{"project": "_router", "deployed_rev": 1}] + [
                {"project": p, "deployed_rev": None if p == "never_deployed" else 1}
                for p in self.docs]

        def deployed(self, p):
            return {"doc": {"settings": {"automation": self.docs[p]}}}
    assert budget_caps.caps(Store()) == {
        "claude": 70.0, "other": 75.0, "by": {"a": "save_claude", "b": "save_all", "d": "save_all"}}


def test_the_admission_answer_names_the_cutoff_a_sprint_is_held_to(client, app):
    """`primary_cutoff_pct` per route is what the coordinator's pause compares the windows
    with (foundry_coordinator/modelwatch.py): the cutoff in force, mode included."""
    import inspect

    from foundry_router.facade import work_api
    src = inspect.getsource(work_api)
    assert 'd["primary_cutoff_pct"]' in src and "effective_cutoff(svc.config_store.config" in src
    from foundry_router.guardrails import effective_cutoff
    svc = setup(client, app, {"meridian": 95})
    assert effective_cutoff(svc.config_store.config, "meridian") == 95.0
    set_mode(client, budget_mode="save_claude")
    assert effective_cutoff(svc.config_store.config, "meridian") == 75.0
