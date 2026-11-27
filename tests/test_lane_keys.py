"""A slot's lane keys (foundry_router/lane_keys.py), Distributed Foundry phase b B5: two client
keys per slot for its own Kilo / OpenCode, their values only in that slot's bundle."""

import json

from foundry_automation import document as D
from foundry_router import client_keys

PASS = "correct horse battery staple"
P = "animaluprising"


def _svc(app):
    return app.state.services


def _deployed(app):
    d = D.empty(P)
    d["settings"]["roles"] = {"implementer": "Foundry-Kilo-Local"}
    _svc(app).automation._append(P, d, "test", "", "test")


def _slot(client, slot, ip):
    body = {"kind": "slot", "project": P, "slot": slot, "ip": ip,
            "place_link": "roblox-studio:1+launchmode:edit+task:EditPlace+placeId:111+universeId:222"}
    code = client.post("/admin/api/machines", json=body).json()["code"]
    return client.post("/v1/foundry/enroll", json={"code": code}).json()["key"]


def _kilo_key(app, personas):
    def mutate(raw):
        raw.setdefault("clients", [])
        raw["clients"] = (raw["clients"] or []) + [
            {"name": "kilo", "key_hash": client_keys.hash_key(client_keys.generate_key()),
             "key_hint": "x", "scopes": ["inference", "media"], "personas": personas,
             "enabled": True, "note": "the workstation's kilo"}]
    _svc(app).config_store.save(mutate)


def _bundle(client, key, slot):
    r = client.get(f"/v1/foundry/secrets/bundle?project={P}&slot={slot}",
                   headers={"Authorization": f"Bearer {key}"})
    assert r.status_code == 200, r.text
    return {s["name"]: s for s in r.json()["secrets"]}


def _clients(app):
    return {c.name: c for c in _svc(app).config_store.config.clients}


def _lanes(client, slot, **body):
    return client.post(f"/admin/api/machines/{slot}/lanes", json=body)


def setup(app, client):
    _deployed(app)
    _kilo_key(app, ["Foundry-Kilo-Implementer", "Foundry-Kilo-Local"])
    k2 = _slot(client, "slot-2", "192.168.0.52")
    k3 = _slot(client, "slot-3", "192.168.0.53")
    return k2, k3


def test_lanes_need_the_secret_store_unlocked(app, client):
    setup(app, client)
    r = _lanes(client, "slot-2", on=True)
    assert r.status_code == 409 and r.json()["error"]["code"] == "secrets_locked"
    assert "slot-2-kilo" not in _clients(app)


def test_lanes_on_makes_two_scoped_keys_with_the_kilo_personas(app, client):
    k2, k3 = setup(app, client)
    client.post("/admin/api/secrets/init", json={"passphrase": PASS})
    r = _lanes(client, "slot-2", on=True)
    assert r.status_code == 200, r.text
    assert r.json()["changed"] is True and r.json()["on"] is True
    c = _clients(app)
    k, o = c["slot-2-kilo"], c["slot-2-opencode"]
    assert k.scopes == ["inference", "media"] and o.scopes == ["inference"]
    assert k.personas == o.personas == ["Foundry-Kilo-Implementer", "Foundry-Kilo-Local"]
    assert k.projects == [P] and k.slots == ["slot-2"] and k.enabled
    # the values reach slot-2's bundle, match the keys, and are nowhere in the config
    b = _bundle(client, k2, "slot-2")
    assert b["FOUNDRY_KEY_KILO"]["file"] == "foundry_key_kilo.txt"
    assert b["FOUNDRY_KEY_OPENCODE"]["file"] == "foundry_key_opencode.txt"
    assert client_keys.hash_key(b["FOUNDRY_KEY_KILO"]["value"]) == k.key_hash
    assert client_keys.hash_key(b["FOUNDRY_KEY_OPENCODE"]["value"]) == o.key_hash
    cfg_text = _svc(app).config_store.path.read_text(encoding="utf-8")
    assert b["FOUNDRY_KEY_KILO"]["value"] not in cfg_text
    # the Machines page shows the state, never a value
    listing = client.get("/admin/api/machines").json()
    m = next(x for x in listing["machines"] if x["name"] == "slot-2")
    assert m["lanes"]["on"] is True and m["lanes"]["values"] == {"FOUNDRY_KEY_KILO": True,
                                                                 "FOUNDRY_KEY_OPENCODE": True}
    assert b["FOUNDRY_KEY_KILO"]["value"] not in json.dumps(listing)
    # no other slot's key reads them
    assert "FOUNDRY_KEY_KILO" not in _bundle(client, k3, "slot-3")
    r = client.get(f"/v1/foundry/secrets/bundle?project={P}&slot=slot-2",
                   headers={"Authorization": f"Bearer {k3}"})
    assert r.status_code == 403


def test_on_again_changes_nothing_and_rotate_makes_new_values(app, client):
    k2, _ = setup(app, client)
    client.post("/admin/api/secrets/init", json={"passphrase": PASS})
    _lanes(client, "slot-2", on=True)
    h1 = _clients(app)["slot-2-kilo"].key_hash
    v1 = _bundle(client, k2, "slot-2")["FOUNDRY_KEY_KILO"]["value"]
    r = _lanes(client, "slot-2", on=True)
    assert r.json()["changed"] is False and _clients(app)["slot-2-kilo"].key_hash == h1
    _lanes(client, "slot-2", on=True, rotate=True)
    v2 = _bundle(client, k2, "slot-2")["FOUNDRY_KEY_KILO"]["value"]
    assert v2 != v1 and client_keys.hash_key(v2) == _clients(app)["slot-2-kilo"].key_hash


def test_off_deletes_keys_and_values(app, client):
    k2, _ = setup(app, client)
    client.post("/admin/api/secrets/init", json={"passphrase": PASS})
    _lanes(client, "slot-2", on=True)
    r = _lanes(client, "slot-2", on=False)
    assert r.status_code == 200 and r.json()["on"] is False
    assert "slot-2-kilo" not in _clients(app) and "slot-2-opencode" not in _clients(app)
    assert "FOUNDRY_KEY_KILO" not in _bundle(client, k2, "slot-2")
    assert _lanes(client, "slot-2", on=False).json()["changed"] is False


def test_revoke_disables_and_delete_removes_the_lane_keys(app, client):
    setup(app, client)
    client.post("/admin/api/secrets/init", json={"passphrase": PASS})
    _lanes(client, "slot-2", on=True)
    _lanes(client, "slot-3", on=True)
    assert client.post("/admin/api/machines/slot-2/revoke").status_code == 200
    c = _clients(app)
    assert not c["slot-2-kilo"].enabled and not c["slot-2-opencode"].enabled
    assert c["slot-3-kilo"].enabled                            # another slot's are untouched
    assert client.delete("/admin/api/machines/slot-2").status_code == 200
    c = _clients(app)
    assert "slot-2-kilo" not in c and "slot-3-kilo" in c
    names = {(s["group"], s["name"]) for s in _svc(app).secret_store.list()}
    assert ("lane-slot-2", "FOUNDRY_KEY_KILO") not in names
    assert ("lane-slot-3", "FOUNDRY_KEY_KILO") in names


def test_a_key_with_the_name_that_is_not_a_lane_key_is_never_replaced(app, client):
    setup(app, client)
    client.post("/admin/api/secrets/init", json={"passphrase": PASS})

    def mutate(raw):
        raw["clients"] = (raw.get("clients") or []) + [
            {"name": "slot-2-kilo", "key_hash": "h", "key_hint": "x", "scopes": ["inference"],
             "enabled": True, "note": "somebody's own key"}]
    _svc(app).config_store.save(mutate)
    r = _lanes(client, "slot-2", on=True)
    assert r.status_code == 409 and r.json()["error"]["code"] == "name_taken"
    assert _clients(app)["slot-2-kilo"].key_hash == "h"
    _lanes(client, "slot-2", on=False)                       # off never deletes it either
    assert _clients(app)["slot-2-kilo"].key_hash == "h"


def test_no_kilo_or_opencode_key_to_copy_from_is_refused(app, client):
    """An empty persona list means every persona: never made up for a lane key."""
    _deployed(app)
    _slot(client, "slot-2", "192.168.0.52")
    client.post("/admin/api/secrets/init", json={"passphrase": PASS})
    r = _lanes(client, "slot-2", on=True)
    assert r.status_code == 409 and r.json()["error"]["code"] == "no_source_key"
    assert "slot-2-kilo" not in _clients(app)
    assert not [s for s in _svc(app).secret_store.list() if s["group"] == "lane-slot-2"]


def test_lanes_are_for_slots_only(app, client):
    _deployed(app)
    client.post("/admin/api/secrets/init", json={"passphrase": PASS})
    r = client.post("/admin/api/machines", json={"kind": "workstation", "project": P,
                                                 "name": "ws-2", "ip": "192.168.0.60"})
    assert r.status_code == 200, r.text
    r = _lanes(client, "ws-2", on=True)
    assert r.status_code == 400 and "Studio slots only" in r.json()["error"]["message"]


def test_readers_of_a_lane_group_are_that_slots_key_only():
    from foundry_router.secrets_api import readers
    keys = [{"name": "slot-2", "enabled": True, "projects": [P], "secret_names": [],
             "slots": ["slot-2"]},
            {"name": "slot-3", "enabled": True, "projects": [P], "secret_names": [],
             "slots": ["slot-3"]},
            {"name": "any", "enabled": True, "projects": [P], "secret_names": [], "slots": []}]
    assert readers(keys, "lane-slot-2", "FOUNDRY_KEY_KILO") == ["slot-2"]
    assert readers(keys, P, "X") == ["slot-2", "slot-3", "any"]


def test_the_machines_page_has_the_lanes_button():
    import pathlib
    html = (pathlib.Path(__file__).resolve().parents[1] / "foundry_router" / "ui" / "static"
            / "index.html").read_text(encoding="utf-8")
    assert "data-mc-lanes" in html and "machines/${encodeURIComponent(n)}/lanes" in html
