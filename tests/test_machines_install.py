"""Access → Machines for the installers (machines.py): Windows commands and the os / ssh_user
contract the GUI codes against, repair without a code, several projects on one workstation,
several slots on one host, the project's own game mirror name, OpenCode on workstations."""

from foundry_automation import document as D


def _svc(app):
    return app.state.services


def _deployed(app, project="animaluprising", **settings):
    d = D.empty(project)
    d["settings"]["roles"] = {"implementer": "Foundry-Kilo-Local"}
    d["settings"].update(settings)
    _svc(app).automation._append(project, d, "test", "", "test")


def _slot_settings(app, slot, project="animaluprising"):
    return _svc(app).automation.deployed(project)["doc"]["settings"]["slots"][slot]


def _clients(app):
    return {c.name: c for c in _svc(app).config_store.config.clients}


def _bearer(k):
    return {"Authorization": "Bearer " + k}


def _add_slot(client, **kw):
    return client.post("/admin/api/machines", json={"kind": "slot", "project": "animaluprising",
                                                    "slot": "slot-2", "ip": "192.168.0.52", **kw})


def _add_ws(client, **kw):
    return client.post("/admin/api/machines", json={"kind": "workstation", "name": "ws-a",
                                                    "project": "animaluprising", **kw})


# ----------------------------------------------------------------------------- the contract

def test_install_commands_carry_a_windows_line(app, client):
    _deployed(app)
    out = _add_slot(client).json()
    w = out["install"]["windows"]
    assert w.startswith("irm https://raw.githubusercontent.com/Vipast82/foundry-router/v")
    assert "/install/slot.ps1 -OutFile slot.ps1; powershell -ExecutionPolicy Bypass -File slot.ps1" in w
    assert " -Router http://" in w
    # the Linux lines are still there, now with the router address
    assert "setup-host.sh" in out["install"]["inside_lxc"] and "--router http://" in out["install"]["inside_lxc"]
    ws = _add_ws(client).json()["install"]
    assert "/install/workstation.ps1 -OutFile workstation.ps1;" in ws["windows"]
    assert "-File workstation.ps1 -Router http://" in ws["windows"]
    # New code hands the same keys
    again = client.post("/admin/api/machines/slot-2/code").json()["install"]
    assert set(again) >= {"router_url", "proxmox_host", "inside_lxc", "windows", "code"}


def test_os_and_ssh_user_on_the_row_the_view_and_the_slot_settings(app, client):
    _deployed(app)
    r = _add_slot(client, os="windows", ssh_user="slotadmin")
    assert r.status_code == 200, r.text
    m = r.json()["machine"]
    assert m["os"] == "windows" and m["ssh_user"] == "slotadmin" and m["host"] == "slotadmin@192.168.0.52"
    s = _slot_settings(app, "slot-2")
    assert s["os"] == "windows" and s["studio"] == "native" and s["container"] == ""
    assert s["host"] == "slotadmin@192.168.0.52"
    assert "au_push_url" not in s                 # the coordinator derives scp-style URLs
    listed = {x["name"]: x for x in client.get("/admin/api/machines").json()["machines"]}
    assert listed["slot-2"]["os"] == "windows" and listed["slot-2"]["ssh_user"] == "slotadmin"
    # the enrolment tells the installer it is a Windows slot
    e = client.post("/v1/foundry/enroll", json={"code": r.json()["code"]}).json()
    assert e["os"] == "windows"


def test_os_defaults_to_linux_and_bad_values_are_refused(app, client):
    _deployed(app)
    m = _add_slot(client).json()["machine"]
    assert m["os"] == "linux" and m["ssh_user"] == "" and m["host"] == "studio@192.168.0.52"
    assert _slot_settings(app, "slot-2")["container"] == "studio-slot"
    assert _add_slot(client, slot="slot-3", os="macos").status_code == 400
    assert _add_slot(client, slot="slot-3", os="windows").status_code == 400      # needs ssh_user
    assert _add_slot(client, slot="slot-3", ssh_user="bad user!").status_code == 400
    # ssh_user overrides the older `user`
    m = _add_slot(client, slot="slot-4", ip="192.168.0.60", ssh_user="me").json()["machine"]
    assert m["host"] == "me@192.168.0.60" and m["ssh_user"] == "me"
    # a workstation takes os too; a Windows one has no Studio slot beside it
    w = _add_ws(client, os="windows", ssh_user="victor").json()["machine"]
    assert w["os"] == "windows" and w["ssh_user"] == "victor"
    assert _add_ws(client, name="ws-b", os="windows", ip="192.168.0.9", studio=True).status_code == 400


# ----------------------------------------------------------------------------- slots

def test_a_project_slot_mirror_is_named_after_the_project(app, client):
    _deployed(app, "othergame")
    _deployed(app)
    _add_slot(client, project="othergame", slot="og-1")
    assert _slot_settings(app, "og-1", "othergame")["au_push_url"] == \
        "ssh://studio@192.168.0.52/srv/studio-slot/othergame.git"
    assert _slot_settings(app, "og-1", "othergame")["repo"] == "/home/studio/work/othergame"
    _add_slot(client, slot="au-1", ip="192.168.0.53")
    assert _slot_settings(app, "au-1")["au_push_url"].endswith("/srv/studio-slot/AnimalUprising.git")


def test_a_second_slot_on_a_host_gets_its_own_folder_container_and_ports(app, client):
    _deployed(app)
    _add_slot(client, slot="slot-2")
    first = dict(_slot_settings(app, "slot-2"))
    assert first["container"] == "studio-slot" and first["foundry_host_dir"] == "/srv/studio-slot/foundry"
    r = _add_slot(client, slot="slot-5")
    assert r.status_code == 200, r.text
    s = _slot_settings(app, "slot-5")
    assert s["container"] == "studio-slot-5" and s["vnc_port"] == 6081
    assert s["foundry_host_dir"] == "/srv/studio-slot/slot-5/foundry"
    assert s["au_push_url"] == "ssh://studio@192.168.0.52/srv/studio-slot/slot-5/AnimalUprising.git"
    assert s["lanes_root"] == "/srv/foundry-lanes/slot-5"
    assert (s["agent_port"], s["kilo_port"], s["opencode_port"]) == (47812, 4098, 4099)
    # the first slot is unchanged, and adding it again keeps its single-slot layout
    _add_slot(client, slot="slot-2")
    assert _slot_settings(app, "slot-2")["foundry_host_dir"] == "/srv/studio-slot/foundry"
    assert _slot_settings(app, "slot-2")["container"] == "studio-slot"


def test_slot_repair_reads_its_settings_again_without_a_key(app, client):
    _deployed(app)
    code = _add_slot(client).json()["code"]
    e = client.post("/v1/foundry/enroll", json={"code": code}).json()
    r = client.get("/v1/foundry/secrets/enrollment", headers=_bearer(e["key"]))
    assert r.status_code == 200, r.text
    rep = r.json()
    assert rep["repair"] is True and rep["slot"] == "slot-2" and rep["project"] == "animaluprising"
    assert "key" not in rep and e["key"] not in r.text
    assert rep["settings"]["container"] == "studio-slot" and rep["router_version"]
    # not a slot's key, or a revoked one: refused
    assert client.get("/v1/foundry/secrets/enrollment").status_code in (401, 403)
    client.post("/admin/api/machines/slot-2/revoke")
    assert client.get("/v1/foundry/secrets/enrollment", headers=_bearer(e["key"])).status_code in (401, 403)


# ----------------------------------------------------------------------------- workstations

def _enrol(client, code):
    r = client.post("/v1/foundry/enroll", json={"code": code, "hostname": "box"})
    assert r.status_code == 200, r.text
    return r.json()


def _active(client, key, project):
    return client.post("/v1/foundry/control/workstation-active",
                       json={"project": project, "name": "box"}, headers=_bearer(key))


def test_a_second_project_adds_to_the_keys_instead_of_rotating_them(app, client):
    _deployed(app)
    _deployed(app, "othergame")
    first = _enrol(client, _add_ws(client).json()["code"])
    coord = first["keys"]["FOUNDRY_COORDINATOR_KEY"]
    assert "FOUNDRY_KEY_OPENCODE" in first["keys"] and first["keys_kept"] == []
    r = _add_ws(client, project="othergame")
    assert r.status_code == 200, r.text
    m = r.json()["machine"]
    assert m["code_adds_project"] is True and m["projects"] == ["animaluprising", "othergame"]
    second = _enrol(client, r.json()["code"])
    assert second["keys"] == {} and second["project"] == "othergame"
    assert sorted(second["keys_kept"]) == ["ws-a", "ws-a-kilo", "ws-a-opencode", "ws-a-relay"]
    assert second["projects"] == ["animaluprising", "othergame"]
    c = _clients(app)
    assert all(c[n].projects == ["animaluprising", "othergame"] and c[n].enabled
               for n in ("ws-a", "ws-a-kilo", "ws-a-relay", "ws-a-opencode"))
    # the first project's key still works, for both projects now
    assert _active(client, coord, "animaluprising").json()["active"] is True
    assert _active(client, coord, "othergame").status_code == 200
    # New code is the deliberate re-enrolment: fresh keys covering both projects
    code = client.post("/admin/api/machines/ws-a/code").json()["code"]
    third = _enrol(client, code)
    assert third["keys"]["FOUNDRY_COORDINATOR_KEY"] != coord and third["keys_kept"] == []
    assert _active(client, coord, "animaluprising").status_code == 401
    assert _clients(app)["ws-a"].projects == ["animaluprising", "othergame"]


def test_an_added_project_code_on_a_revoked_key_makes_that_key_again(app, client):
    _deployed(app)
    _deployed(app, "othergame")
    _enrol(client, _add_ws(client).json()["code"])
    client.post("/admin/api/machines/ws-a/revoke")
    out = _enrol(client, _add_ws(client, project="othergame").json()["code"])
    assert set(out["keys"]) >= {"FOUNDRY_COORDINATOR_KEY", "FOUNDRY_CLIENT_KEY"}
    assert out["keys_kept"] == []


def test_a_pending_added_project_survives_new_code_unless_rotate(app, client):
    _deployed(app)
    _deployed(app, "othergame")
    _enrol(client, _add_ws(client).json()["code"])
    _add_ws(client, project="othergame")
    code = client.post("/admin/api/machines/ws-a/code").json()["code"]
    assert _enrol(client, code)["keys"] == {}                    # still adds the project
    code =client.post("/admin/api/machines/ws-a/code", json={"rotate": True}).json()["code"]
    assert _enrol(client, code)["keys"]                           # rotates


def test_workstation_repair_answer_has_no_keys_and_only_its_projects(app, client):
    _deployed(app, workspace={"clone_url": "git@github.com:me/g.git", "repo_key_secret": "K"})
    _deployed(app, "othergame")
    e = _enrol(client, _add_ws(client).json()["code"])
    h = _bearer(e["keys"]["FOUNDRY_COORDINATOR_KEY"])
    r = client.get("/v1/foundry/control/workstation-enrollment", headers=h)
    assert r.status_code == 200, r.text
    rep = r.json()
    assert rep["repair"] is True and rep["keys"] == {} and rep["project"] == "animaluprising"
    assert rep["game"]["clone_url"] == "git@github.com:me/g.git"
    assert rep["game"]["repo_key"] is None and rep["game"]["repo_key_problem"] == ""   # not asked
    assert all(v not in r.text for v in e["keys"].values())
    # another project only once the key covers it
    assert client.get("/v1/foundry/control/workstation-enrollment?project=othergame",
                      headers=h).status_code == 403
    _enrol(client, _add_ws(client, project="othergame").json()["code"])
    r = client.get("/v1/foundry/control/workstation-enrollment?project=othergame", headers=h)
    assert r.status_code == 200 and r.json()["project"] == "othergame"
    # asked for, the repo key is looked up (no value set here: the problem says so)
    r = client.get("/v1/foundry/control/workstation-enrollment?project=animaluprising&repo_key=true",
                   headers=h)
    assert "K" in r.json()["game"]["repo_key_problem"]
    # a key that is not a workstation's coordinator key is refused
    other = client.post("/admin/api/setup/clients", json={"name": "c1", "scopes": ["orchestration"]}).json()["key"]
    assert client.get("/v1/foundry/control/workstation-enrollment",
                      headers=_bearer(other)).status_code == 403


def test_the_coding_client_comes_from_the_projects_settings(app, client):
    _deployed(app, client={"kind": "opencode"})
    e = _enrol(client, _add_ws(client).json()["code"])
    assert e["client_kind"] == "opencode" and e["coding_clients"] == ["opencode"]
    _deployed(app, "othergame")
    e = _enrol(client, _add_ws(client, name="ws-b", project="othergame").json()["code"])
    assert e["client_kind"] == "kilo" and e["coding_clients"] == ["kilo"]
