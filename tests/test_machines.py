"""Access → Machines (machines.py): add a slot, one-time enrollment code, enroll, ready, revoke."""

import json

from foundry_automation import document as D


def _svc(app):
    return app.state.services


def _deployed(app, project="animaluprising"):
    d = D.empty(project)
    d["settings"]["roles"] = {"implementer": "Foundry-Kilo-Local"}
    _svc(app).automation._append(project, d, "test", "", "test")


def _add(client, **kw):
    body = {"kind": "slot", "project": "animaluprising", "slot": "slot-2", "ip": "192.168.0.52",
            "place_link": "roblox-studio:1+launchmode:edit+task:EditPlace+placeId:111+universeId:222",
            **kw}
    return client.post("/admin/api/machines", json=body)


def test_add_slot_writes_settings_and_a_one_time_code(app, client):
    _deployed(app)
    r = _add(client)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["code"].startswith("FNDR-") and len(out["code"]) == 19
    s = _svc(app).automation.deployed("animaluprising")["doc"]["settings"]["slots"]["slot-2"]
    assert s == {"container": "studio-slot", "enabled": False, "place_id": 111,
                 "universe_id": 222, "host": "studio@192.168.0.52",
                 "au_push_url": "ssh://studio@192.168.0.52/srv/studio-slot/AnimalUprising.git",
                 "foundry_push_url": "ssh://studio@192.168.0.52/srv/studio-slot/foundry.git",
                 "foundry_host_dir": "/srv/studio-slot/foundry"}
    assert "bash setup-host.sh" in out["install"]["inside_lxc"]
    # the code is never listed again
    listing = client.get("/admin/api/machines").json()
    assert out["code"] not in json.dumps(listing)
    assert listing["machines"][0]["code_pending"] is True


def test_enroll_once_gives_a_scoped_key_then_ready_switches_the_slot_on(app, client):
    _deployed(app)
    client.post("/admin/api/machines/settings",
                json={"slot_ssh_keys": "ssh-ed25519 " + "A" * 68 + " foundry-slots@test"})
    code = _add(client).json()["code"]
    r = client.post("/v1/foundry/enroll", json={"code": code.lower(), "hostname": "slot2-lxc"})
    assert r.status_code == 200, r.text
    e = r.json()
    assert e["key"].startswith("fnd_") and e["slot"] == "slot-2"
    assert e["settings"]["place_id"] == 111 and e["ssh_authorized_keys"][0].startswith("ssh-ed25519")
    c = next(c for c in _svc(app).config_store.config.clients if c.name == "slot-2")
    assert c.scopes == ["secrets"] and c.projects == ["animaluprising"] and c.slots == ["slot-2"]
    assert e["key"] not in (_svc(app).config_store.path.read_text(encoding="utf-8"))
    # spent: a second use fails
    assert client.post("/v1/foundry/enroll", json={"code": code}).status_code == 403
    # the slot reports ready with its own key -> enabled in the deployed settings
    r = client.post("/v1/foundry/secrets/ready", headers={"Authorization": f"Bearer {e['key']}"})
    assert r.status_code == 200, r.text
    s = _svc(app).automation.deployed("animaluprising")["doc"]["settings"]["slots"]["slot-2"]
    assert s["enabled"] is True
    # its bundle carries the slot settings
    b = client.get("/v1/foundry/secrets/bundle?project=animaluprising&slot=slot-2",
                   headers={"Authorization": f"Bearer {e['key']}"})
    assert b.status_code == 200 and b.json()["config"]["place_id"] == 111


def test_expired_wrong_and_rate_limited_codes(app, client):
    _deployed(app)
    svc = _svc(app)
    code = _add(client).json()["code"]
    svc.db.execute("UPDATE machines SET code_expires=1 WHERE name='slot-2'")
    r = client.post("/v1/foundry/enroll", json={"code": code})
    assert r.status_code == 403 and r.json()["error"]["code"] == "expired_code"
    for _ in range(10):
        client.post("/v1/foundry/enroll", json={"code": "FNDR-AAAA-BBBB-CCCC"})
    r = client.post("/v1/foundry/enroll", json={"code": "FNDR-AAAA-BBBB-CCCC"})
    assert r.status_code == 429


def test_revoke_disables_the_key_and_new_code_reenrolls(app, client):
    _deployed(app)
    code = _add(client).json()["code"]
    key = client.post("/v1/foundry/enroll", json={"code": code}).json()["key"]
    assert client.post("/admin/api/machines/slot-2/revoke").status_code == 200
    h = {"Authorization": f"Bearer {key}"}
    assert client.get("/v1/foundry/secrets/bundle?project=animaluprising&slot=slot-2",
                      headers=h).status_code == 401
    code2 = client.post("/admin/api/machines/slot-2/code").json()["code"]
    key2 = client.post("/v1/foundry/enroll", json={"code": code2}).json()["key"]
    assert key2 != key
    assert client.get("/v1/foundry/secrets/bundle?project=animaluprising&slot=slot-2",
                      headers={"Authorization": f"Bearer {key2}"}).status_code == 200
    assert client.get("/v1/foundry/secrets/bundle?project=animaluprising&slot=slot-2",
                      headers=h).status_code == 401


def test_delete_removes_slot_and_key_and_an_open_draft_keeps_added_slots(app, client):
    _deployed(app)
    svc = _svc(app)
    svc.automation.draft("animaluprising", "victor")          # an open draft exists
    _add(client)
    assert "slot-2" in svc.automation.draft("animaluprising")["doc"]["settings"]["slots"]
    assert client.delete("/admin/api/machines/slot-2").status_code == 200
    assert "slots" not in svc.automation.deployed("animaluprising")["doc"]["settings"]
    assert not any(c.name == "slot-2" for c in svc.config_store.config.clients)


def test_refusals(app, client):
    r = _add(client)
    assert r.status_code == 409                                # nothing deployed yet
    _deployed(app)
    assert _add(client, slot="bad id").status_code == 400
    client.post("/admin/api/setup/clients", json={"name": "slot-3", "scopes": ["inference"]})
    assert _add(client, slot="slot-3").status_code == 409     # a non-machine client has the name
    assert client.post("/admin/api/machines/settings",
                       json={"slot_ssh_keys": "-----BEGIN OPENSSH PRIVATE KEY-----"}).status_code == 400
    r = client.post("/v1/foundry/secrets/ready")
    assert r.status_code == 401


# ----------------------------------------------------------------------------- noVNC (v0.161.1)

def test_vnc_is_on_the_lan_by_default_with_a_password_warning(app, client):
    _deployed(app)
    out = _add(client).json()
    v = out["machine"]["vnc"]
    assert v == {"lan": True, "port": 6080, "password": False, "url": "http://192.168.0.52:6080/"}
    code = out["code"]
    e = client.post("/v1/foundry/enroll", json={"code": code}).json()
    assert e["vnc"]["lan"] is True and e["vnc"]["port"] == 6080      # setup-host.sh reads these


def test_vnc_settings_from_the_add_form_and_the_vnc_button(app, client):
    _deployed(app)
    svc = _svc(app)
    out = _add(client, vnc_lan=False, vnc_port="6090").json()
    s = svc.automation.deployed("animaluprising")["doc"]["settings"]["slots"]["slot-2"]
    assert s["vnc_lan"] is False and s["vnc_port"] == 6090
    assert out["machine"]["vnc"] == {"lan": False, "port": 6090, "password": False, "url": ""}
    r = client.post("/admin/api/machines/slot-2/vnc", json={"vnc_lan": True, "vnc_port": 6081})
    assert r.status_code == 200, r.text
    assert r.json()["vnc"]["url"] == "http://192.168.0.52:6081/"
    assert client.post("/admin/api/machines/slot-2/vnc", json={"vnc_port": 70000}).status_code == 400
    assert client.post("/admin/api/machines/slot-2/vnc", json={}).status_code == 400
    assert client.post("/admin/api/machines/nope/vnc", json={"vnc_lan": True}).status_code == 404
    ev = [x["event"] for x in svc.machines.events(5)]
    assert "vnc_changed" in ev


def test_place_and_universe_from_the_settings_dialog(app, client):
    _deployed(app)
    svc = _svc(app)
    _add(client)
    slot = lambda: svc.automation.deployed("animaluprising")["doc"]["settings"]["slots"]["slot-2"]
    r = client.post("/admin/api/machines/slot-2/host", json={"place_id": "333", "universe_id": "444"})
    assert r.status_code == 200, r.text
    assert slot()["place_id"] == 333 and slot()["universe_id"] == 444
    assert slot()["host"] == "studio@192.168.0.52"          # the rest of the slot is kept
    r = client.post("/admin/api/machines/slot-2/host", json={
        "place_link": "roblox-studio:1+launchmode:edit+task:EditPlace+placeId:555+universeId:666"})
    assert r.status_code == 200, r.text
    assert slot()["place_id"] == 555 and slot()["universe_id"] == 666
    assert client.post("/admin/api/machines/slot-2/host", json={"universe_id": "abc"}).status_code == 400
    assert client.post("/admin/api/machines/slot-2/host", json={"place_id": -1}).status_code == 400
    listed = [m for m in client.get("/admin/api/machines").json()["machines"] if m["name"] == "slot-2"][0]
    assert listed["settings"]["place_id"] == 555
    assert "place_changed" in [x["event"] for x in svc.machines.events(5)]


def test_vnc_password_counts_only_when_the_slot_can_read_it(app, client):
    _deployed(app)
    svc = _svc(app)
    code = _add(client).json()["code"]
    client.post("/v1/foundry/enroll", json={"code": code})          # makes the slot-2 key
    st = svc.secret_store
    st.initialize("a test passphrase only")
    st.set_value("othergame", "SLOT_VNC_PASSWORD", "x" * 10)        # another game: not readable
    m = client.get("/admin/api/machines").json()["machines"][0]
    assert m["vnc"]["password"] is False
    st.set_value("animaluprising", "SLOT_VNC_PASSWORD", "x" * 10)
    m = client.get("/admin/api/machines").json()["machines"][0]
    assert m["vnc"]["password"] is True
    assert "x" * 10 not in json.dumps(m)


def test_vnc_port_is_checked_in_the_settings_schema():
    import pytest
    from foundry_automation.schema import SettingsError, slots_table
    ok = slots_table("t", {"slot-1": {"container": "c", "vnc_lan": False, "vnc_port": 6081}})
    assert ok["slot-1"]["vnc_port"] == 6081
    with pytest.raises(SettingsError):
        slots_table("t", {"slot-1": {"container": "c", "vnc_port": 0}})


def test_slot_files_open_vnc_on_the_lan_with_its_password():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1] / "contrib" / "studio-slot"
    compose = (root / "proxmox" / "docker-compose.yml").read_text(encoding="utf-8")
    assert '"${SLOT_NOVNC_BIND:-127.0.0.1}:${SLOT_NOVNC_PORT:-6080}:6080"' in compose
    setup = (root / "proxmox" / "setup-host.sh").read_text(encoding="utf-8")
    assert "SLOT_NOVNC_BIND=$VNC_BIND" in setup and 'py get vnc.lan' in setup
    entry = (root / "entrypoint.sh").read_text(encoding="utf-8")
    assert "slot_vnc_password.txt" in entry and "-SecurityTypes VncAuth -PasswordFile" in entry
    # the password goes through a file redirect, never a command line
    assert 'vncpasswd -f <"$PW_IN"' in entry
    # Studio's leftover splash is hidden (unmapped) once the editor window is up
    assert "--name '^RobloxStudio$'" in entry and 'xdotool windowunmap "$s"' in entry
    docker = (root / "Dockerfile").read_text(encoding="utf-8")
    assert "COPY novnc-index.html /usr/share/novnc/index.html" in docker
    index = (root / "novnc-index.html").read_text(encoding="utf-8")
    assert "vnc.html?autoconnect=1&resize=scale" in index
