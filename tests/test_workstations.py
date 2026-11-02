"""Workstations on Access → Machines (LINUX_WORKSTATION_PLAN W3/W4): add one, enroll it once for
its three keys and the game's clone settings, an optional Studio slot beside it, and one active
workstation per project."""

from foundry_automation import document as D


def _svc(app):
    return app.state.services


def _deployed(app, project="animaluprising", **ws):
    d = D.empty(project)
    d["settings"]["roles"] = {"implementer": "Foundry-Kilo-Local"}
    if ws:
        d["settings"]["workspace"] = ws
    _svc(app).automation._append(project, d, "test", "", "test")


def _clients(app):
    return {c.name: c for c in _svc(app).config_store.config.clients}


def _add(client, **kw):
    return client.post("/admin/api/machines", json={"kind": "workstation", "name": "ws-linux",
                                                    "project": "animaluprising", **kw})


def test_add_and_enroll_a_workstation_gives_three_scoped_keys_once(app, client):
    _deployed(app, clone_url="git@github.com:me/game.git", branch="master")
    r = _add(client, ip="192.168.0.120")
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["machine"]["kind"] == "workstation" and out["code"].startswith("FNDR-")
    assert "workstation.sh" in out["install"]["inside_lxc"]
    assert out["machine"]["keys"] == {"ws-linux": "none", "ws-linux-kilo": "none",
                                      "ws-linux-relay": "none"}
    e = client.post("/v1/foundry/enroll", json={"code": out["code"], "hostname": "ws1"})
    assert e.status_code == 200, e.text
    e = e.json()
    assert e["kind"] == "workstation" and e["project"] == "animaluprising"
    k = e["keys"]
    assert set(k) == {"FOUNDRY_COORDINATOR_KEY", "FOUNDRY_KEY_KILO", "FOUNDRY_KEY",
                      "FOUNDRY_CLIENT_KEY"}
    assert k["FOUNDRY_KEY_KILO"] == k["FOUNDRY_KEY"] != k["FOUNDRY_COORDINATOR_KEY"]
    assert e["game"] == {"id": "animaluprising", "clone_url": "git@github.com:me/game.git",
                         "branch": "master", "remote": "origin", "repo_key": None,
                         "repo_key_problem": ""}
    assert e["studio"] is None
    c = _clients(app)
    assert c["ws-linux"].scopes == ["inference", "orchestration"]
    assert c["ws-linux-kilo"].scopes == ["inference", "media"]
    assert c["ws-linux-relay"].scopes == ["operator"]
    assert all(c[n].projects == ["animaluprising"] for n in ("ws-linux", "ws-linux-kilo",
                                                             "ws-linux-relay"))
    text = _svc(app).config_store.path.read_text(encoding="utf-8")
    assert not any(v in text for v in k.values())            # stored hashed only
    assert client.post("/v1/foundry/enroll", json={"code": out["code"]}).status_code == 403
    # the coordinator key reaches the control API
    r = client.post("/v1/foundry/control/workstation-active",
                    json={"project": "animaluprising", "name": "ws1"},
                    headers={"Authorization": "Bearer " + k["FOUNDRY_COORDINATOR_KEY"]})
    assert r.status_code == 200 and r.json()["active"] is True
    # revoke disables all three; delete removes all three
    assert client.post("/admin/api/machines/ws-linux/revoke").status_code == 200
    assert not any(_clients(app)[n].enabled for n in ("ws-linux", "ws-linux-kilo", "ws-linux-relay"))
    assert client.delete("/admin/api/machines/ws-linux").status_code == 200
    assert not {"ws-linux", "ws-linux-kilo", "ws-linux-relay"} & set(_clients(app))


def test_a_studio_here_adds_its_slot_and_hands_over_its_code(app, client):
    _deployed(app)
    r = _add(client, ip="192.168.0.120", studio=True, place_id="77417134434265",
             universe_id="10772507751")
    assert r.status_code == 200, r.text
    slot = _svc(app).automation.deployed("animaluprising")["doc"]["settings"]["slots"]["ws-linux-studio"]
    assert slot["host"] == "studio@192.168.0.120" and slot["enabled"] is False
    assert slot["place_id"] == 77417134434265
    e = client.post("/v1/foundry/enroll", json={"code": r.json()["code"]}).json()
    assert e["studio"]["slot"] == "ws-linux-studio" and e["studio"]["code"].startswith("FNDR-")
    # that code enrolls the slot like any slot
    s = client.post("/v1/foundry/enroll", json={"code": e["studio"]["code"]})
    assert s.status_code == 200 and s.json()["slot"] == "ws-linux-studio"


def test_workstation_refusals(app, client):
    _deployed(app)
    assert _add(client, name="bad name!").status_code == 400
    assert _add(client, project="").status_code == 400
    assert _add(client, studio=True).status_code == 400              # a Studio needs the IP
    client.post("/admin/api/setup/clients", json={"name": "ws-linux-kilo", "scopes": ["inference"]})
    assert _add(client).status_code == 409                          # a key with that name exists


def test_the_repo_key_comes_from_the_secret_store_once(app, client):
    _deployed(app, clone_url="git@github.com:me/game.git", repo_key_secret="GAME_REPO_KEY")
    code = _add(client).json()["code"]
    e = client.post("/v1/foundry/enroll", json={"code": code}).json()
    assert e["game"]["repo_key"] is None and "GAME_REPO_KEY" in e["game"]["repo_key_problem"]


def test_one_active_workstation_per_project(app, client):
    _deployed(app)

    def key(name):
        r = client.post("/admin/api/setup/clients", json={"name": name, "scopes": ["orchestration"]})
        return {"Authorization": "Bearer " + r.json()["key"]}

    a, b = key("coordinator-a"), key("coordinator-b")

    def ask(h, name, **kw):
        r = client.post("/v1/foundry/control/workstation-active",
                        json={"project": "animaluprising", "name": name, **kw}, headers=h)
        assert r.status_code == 200, r.text
        return r.json()

    assert ask(a, "PC")["active"] is True
    second = ask(b, "LINUX")
    assert second["active"] is False and second["holder"]["name"] == "PC"
    assert ask(a, "PC")["active"] is True                          # the holder keeps it
    listed = client.get("/admin/api/machines").json()["workstation_claims"]
    assert [(x["client"], x["active"]) for x in listed] == [("coordinator-a", 1), ("coordinator-b", 0)]
    # Make active moves it
    assert client.post("/admin/api/machines/active", json={"project": "animaluprising",
                                                           "client": "coordinator-b"}).status_code == 200
    assert ask(b, "LINUX")["active"] is True and ask(a, "PC")["active"] is False
    # release (Export) steps down; the next to ask holds it
    assert ask(b, "LINUX", release=True)["released"] is True
    assert ask(a, "PC")["active"] is True
    assert client.post("/admin/api/machines/active", json={"project": "animaluprising",
                                                           "client": "nobody"}).status_code == 404
    assert client.post("/admin/api/machines/active", json={"project": "animaluprising",
                                                           "client": "coordinator-b",
                                                           "forget": True}).status_code == 200
    # a key limited to another project is refused
    other = client.post("/admin/api/setup/clients", json={"name": "c-other", "scopes": ["orchestration"],
                                                          "projects": ["othergame"]}).json()["key"]
    r = client.post("/v1/foundry/control/workstation-active", json={"project": "animaluprising"},
                    headers={"Authorization": "Bearer " + other})
    assert r.status_code == 403
