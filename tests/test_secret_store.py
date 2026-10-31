"""Central secret store (secret_store.py, secrets_api.py): encryption, passphrase lock,
write-only admin API, scoped pulls with audit, transport rule, slot settings."""

import json

import pytest

from foundry_router import client_auth
from foundry_router.secret_store import SecretStore, SecretStoreError

PASS = "correct horse battery staple"
VALUE = "test-value-not-a-real-key-0123456789"


def _store(tmp_path, clock=None):
    return SecretStore(tmp_path / "secrets.sqlite", **({"clock": clock} if clock else {}))


# ----------------------------------------------------------------------------- store

def test_passphrase_lock_and_values(tmp_path):
    st = _store(tmp_path)
    assert st.state() == "uninitialized"
    with pytest.raises(SecretStoreError):
        st.initialize("short")
    st.initialize(PASS)
    st.set_value("animaluprising", "ROBLOX_OPEN_CLOUD_KEY", VALUE + "\n")
    assert st.bundle(["shared", "animaluprising"])[0]["value"] == VALUE   # trailing newline dropped
    st.lock()
    assert st.state() == "locked"
    with pytest.raises(SecretStoreError) as e:
        st.bundle(["animaluprising"])
    assert e.value.status == 503
    with pytest.raises(SecretStoreError) as e:
        st.unlock("wrong passphrase!!")
    assert e.value.code == "wrong_passphrase"
    st.unlock(PASS)
    assert st.bundle(["animaluprising"])[0]["value"] == VALUE


def test_a_new_process_starts_locked_and_nothing_is_plaintext_on_disk(tmp_path):
    st = _store(tmp_path)
    st.initialize(PASS)
    st.set_value("shared", "SOME_TOKEN", VALUE)
    st.conn.close()
    raw = (tmp_path / "secrets.sqlite").read_bytes()
    assert VALUE.encode() not in raw and PASS.encode() not in raw
    again = _store(tmp_path)
    assert again.state() == "locked" and again.known_values() == set()
    again.unlock(PASS)
    assert again.known_values() == {VALUE}


def test_ciphertext_moved_to_another_row_does_not_decrypt(tmp_path):
    st = _store(tmp_path)
    st.initialize(PASS)
    st.set_value("shared", "A_KEY", VALUE)
    st.define("shared", "B_KEY")
    r = st.conn.execute("SELECT ciphertext, nonce, key_id FROM secrets WHERE name='A_KEY'").fetchone()
    st.conn.execute("UPDATE secrets SET ciphertext=?, nonce=?, key_id=? WHERE name='B_KEY'",
                    (r["ciphertext"], r["nonce"], r["key_id"]))
    with pytest.raises(Exception):
        st.bundle(["shared"], ["B_KEY"])


def test_change_passphrase_reencrypts(tmp_path):
    st = _store(tmp_path)
    st.initialize(PASS)
    st.set_value("shared", "A_KEY", VALUE)
    st.change_passphrase(PASS, "another long passphrase")
    st.lock()
    with pytest.raises(SecretStoreError):
        st.unlock(PASS)
    st.unlock("another long passphrase")
    assert st.bundle(["shared"])[0]["value"] == VALUE


def test_unlock_backoff_after_repeated_failures(tmp_path):
    now = [1000.0]
    st = _store(tmp_path, clock=lambda: now[0])
    st.initialize(PASS)
    st.lock()
    for _ in range(5):
        with pytest.raises(SecretStoreError):
            st.unlock("not the passphrase")
    with pytest.raises(SecretStoreError) as e:
        st.unlock(PASS)
    assert e.value.code == "unlock_backoff"
    now[0] += 31
    st.unlock(PASS)
    assert st.state() == "unlocked"


def test_names_files_and_groups_are_checked(tmp_path):
    st = _store(tmp_path)
    for bad in [("Shared", "X_KEY"), ("shared", "lower"), ("../x", "X_KEY")]:
        with pytest.raises(SecretStoreError):
            st.define(*bad)
    with pytest.raises(SecretStoreError):
        st.define("shared", "X_KEY", file="../etc/passwd")
    st.define("shared", "X_KEY", file="x.txt")
    with pytest.raises(SecretStoreError):
        st.define("shared", "Y_KEY", file="x.txt")        # one file per group


# ----------------------------------------------------------------------------- API

def _svc(app):
    return app.state.services


def _add_slot_key(client, **extra):
    body = {"name": "slot-1-key", "scopes": ["secrets"], "projects": ["animaluprising"], **extra}
    r = client.post("/admin/api/setup/clients", json=body)
    assert r.status_code == 200, r.text
    return r.json()["key"]


def _ready(client):
    assert client.post("/admin/api/secrets/init", json={"passphrase": PASS}).status_code == 200
    r = client.put("/admin/api/secrets/animaluprising/ROBLOX_OPEN_CLOUD_KEY",
                   json={"value": VALUE, "description": "Open Cloud"})
    assert r.status_code == 200 and VALUE not in r.text
    client.put("/admin/api/secrets/shared/SHARED_TOKEN", json={"value": VALUE + "-shared"})
    client.put("/admin/api/secrets/othergame/OTHER_KEY", json={"value": VALUE + "-other"})


def _pull(client, key, **params):
    q = "&".join(f"{k}={v}" for k, v in params.items())
    return client.get(f"/v1/foundry/secrets/bundle?{q}", headers={"Authorization": f"Bearer {key}"})


def test_pull_serves_only_what_the_key_may_read_and_audits(app, client):
    _ready(client)
    key = _add_slot_key(client)
    r = _pull(client, key, project="animaluprising", slot="slot-1")
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    got = {s["name"]: s for s in r.json()["secrets"]}
    assert set(got) == {"ROBLOX_OPEN_CLOUD_KEY", "SHARED_TOKEN"}            # not othergame
    assert got["ROBLOX_OPEN_CLOUD_KEY"]["file"] == "roblox_open_cloud_key.txt"
    # same bundle again: 304
    r2 = client.get("/v1/foundry/secrets/bundle?project=animaluprising&slot=slot-1",
                    headers={"Authorization": f"Bearer {key}", "If-None-Match": r.headers["etag"]})
    assert r2.status_code == 304
    # another game's group: refused
    assert _pull(client, key, project="othergame").status_code == 403
    pulls = client.get("/admin/api/secrets/pulls").json()["pulls"]
    assert [p["outcome"] for p in pulls][:3] == ["refused", "unchanged", "ok"]
    assert VALUE not in json.dumps(pulls)
    meta = client.get("/admin/api/secrets").json()
    assert VALUE not in json.dumps(meta)
    row = next(s for s in meta["secrets"] if s["name"] == "ROBLOX_OPEN_CLOUD_KEY")
    assert row["last_pulled_by"] == "slot-1-key" and row["readers"] == ["slot-1-key"]


def test_key_limits_names_slots_and_empty_projects(app, client):
    _ready(client)
    k = _add_slot_key(client, secret_names=["ROBLOX_OPEN_CLOUD_KEY"], slots=["slot-1"])
    names = [s["name"] for s in _pull(client, k, project="animaluprising", slot="slot-1")
             .json()["secrets"]]
    assert names == ["ROBLOX_OPEN_CLOUD_KEY"]
    assert _pull(client, k, project="animaluprising", slot="slot-2").status_code == 403
    r = client.post("/admin/api/setup/clients", json={"name": "shared-only", "scopes": ["secrets"]})
    k2 = r.json()["key"]
    assert [s["name"] for s in _pull(client, k2).json()["secrets"]] == ["SHARED_TOKEN"]
    assert _pull(client, k2, project="animaluprising").status_code == 403


def test_other_scopes_cannot_pull_and_keyless_is_refused(app, client):
    _ready(client)
    r = client.post("/admin/api/setup/clients", json={"name": "kilo", "scopes": ["inference"]})
    assert _pull(client, r.json()["key"], project="animaluprising").status_code == 403
    assert client.get("/v1/foundry/secrets/bundle").status_code == 401
    assert client_auth.scope_for("/v1/foundry/secrets/bundle") == "secrets"


def test_locked_store_answers_503_and_raises_an_alert(app, client):
    _ready(client)
    key = _add_slot_key(client)
    client.post("/admin/api/secrets/lock")
    r = _pull(client, key, project="animaluprising")
    assert r.status_code == 503
    alerts = _svc(app).control.alerts([], limit=50)
    assert any(a["kind"] == "secrets_locked" for a in alerts)
    assert client.post("/admin/api/secrets/unlock", json={"passphrase": PASS}).status_code == 200
    assert _pull(client, key, project="animaluprising").status_code == 200


def test_new_access_alerts(app, client):
    _ready(client)
    key = _add_slot_key(client)
    _pull(client, key, project="animaluprising")
    kinds = [(a["kind"], a["title"]) for a in _svc(app).control.alerts([], limit=50)]
    assert ("secrets_access", "slot-1-key can now read secrets") in kinds
    assert ("secrets_access", "slot-1-key pulled secrets for the first time") in kinds


def test_https_only_refuses_plain_http(app, client):
    _ready(client)
    key = _add_slot_key(client)
    assert client.post("/admin/api/secrets/settings",
                       json={"allow_http": False}).status_code == 400     # HTTPS listener off
    r = client.post("/admin/api/secrets/settings", json={"allow_http": False, "https_enabled": True})
    assert r.status_code == 200 and r.json()["restart_needed"]
    r = _pull(client, key, project="animaluprising")
    assert r.status_code == 403 and r.json()["error"]["code"] == "https_only"
    r = client.put("/admin/api/secrets/shared/SHARED_TOKEN", json={"value": "x" * 20})
    assert r.status_code == 403
    from fastapi.testclient import TestClient
    with TestClient(app, base_url="https://testserver") as tls:
        assert tls.get("/v1/foundry/secrets/bundle?project=animaluprising",
                       headers={"Authorization": f"Bearer {key}"}).status_code == 200


def test_values_never_reach_config_export_or_scrubbed_text(app, client):
    _ready(client)
    _add_slot_key(client)
    svc = _svc(app)
    from foundry_router import configops
    cfg_text = svc.config_store.path.read_text(encoding="utf-8")
    assert VALUE not in cfg_text
    bundle = configops.export_bundle(svc, include_secrets=True)
    assert VALUE not in json.dumps(bundle, default=str)
    assert VALUE in configops.secret_values(svc)              # scrubbed from diagnostics
    assert VALUE not in configops.scrub(f"log line {VALUE}", configops.secret_values(svc))


def test_slot_settings_from_the_deployed_revision(app, client):
    _ready(client)
    key = _add_slot_key(client)
    svc = _svc(app)
    from foundry_automation import document as D
    from foundry_router.secrets_api import slot_settings
    d = D.empty("animaluprising")
    d["settings"]["slots"] = {"slot-1": {"container": "studio-slot-poc", "place_id": 1234,
                                         "secrets": ["ROBLOX_OPEN_CLOUD_KEY"]}}
    svc.automation._append("animaluprising", d, "test", "", "test")
    assert slot_settings(svc, "animaluprising", "slot-1")["place_id"] == 1234
    r = _pull(client, key, project="animaluprising", slot="slot-1").json()
    assert r["config"]["container"] == "studio-slot-poc"
    assert [s["name"] for s in r["secrets"]] == ["ROBLOX_OPEN_CLOUD_KEY"]   # the slot's own list


def test_slots_schema_validates():
    from foundry_automation import document as D
    d = D.empty("animaluprising")
    d["settings"]["slots"] = {"slot-1": {"container": ""}}
    errs = [e["path"] for e in D.validate(d, "animaluprising")["errors"]]
    assert "slots.slot-1.container" in errs
    d["settings"]["slots"] = {"slot-1": {"container": "c", "host": "studio@h"}}
    errs = [e["path"] for e in D.validate(d, "animaluprising")["errors"]]
    assert "slots.slot-1.au_push_url" in errs
    d["settings"]["slots"] = {"slot-1": {"container": "c", "place_id": "x"}}
    errs = [e["path"] for e in D.validate(d, "animaluprising")["errors"]]
    assert "slots.slot-1.place_id" in errs
