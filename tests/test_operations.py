"""Step 6: config history and rollback, migrations, export/import, state
snapshots, diagnostic bundles, per-backend fair queues and circuit breakers."""

import asyncio
import io
import json
import threading
import time
import zipfile

import pytest
import yaml

from foundry_router.pool.internal import FairGate, QueueTimeout
from tests.test_budgets import HITS, STATE, _chat, ledgered  # noqa: F401 (fixture)

SECRET = "sk-test-secret-0123456789abcdef"


def _versions(client):
    return client.get("/admin/api/config/versions").json()["versions"]


# -- history / rollback -------------------------------------------------------------

def test_every_save_is_a_version_with_diff_and_restore(app, client):
    svc = app.state.services
    v0 = _versions(client)
    assert v0 and v0[-1]["source"].startswith("startup")
    r = client.post("/admin/api/config/media", json={"jobs_per_day": 7})
    assert r.status_code == 200
    v1 = _versions(client)
    assert v1[0]["sections"] == ["media"] and v1[0]["source"] == "save"
    before_id = v1[1]["id"]
    d = client.get(f"/admin/api/config/versions/{before_id}").json()
    assert "jobs_per_day: 7" in d["diff_to_current"]
    r = client.post(f"/admin/api/config/versions/{before_id}/restore").json()
    assert r["ok"] and "media" in r["applied"] and r["restart_needed"] == []
    assert svc.config_store.config.media.jobs_per_day == 60
    assert _versions(client)[0]["source"] == f"restore of version {before_id}"
    # restoring is itself undoable
    assert len(_versions(client)) == len(v1) + 1


def test_versions_are_shown_redacted_and_restart_sections_flagged(app, client):
    svc = app.state.services
    def put(raw):
        raw["agent_brain"]["api_key"] = SECRET
        raw["mcp_aggregator"] = {"enabled": True, "token": "${MCP_TOKEN}"}
    svc.config_store.save(put)
    vid = _versions(client)[0]["id"]
    y = client.get(f"/admin/api/config/versions/{vid}").json()["yaml"]
    assert SECRET not in y and "<redacted>" in y and "${MCP_TOKEN}" in y
    first = _versions(client)[-1]["id"]
    r = client.post(f"/admin/api/config/versions/{first}/restore").json()
    assert "mcp_aggregator" in r["restart_needed"] and "agent_brain" in r["applied"]


def test_invalid_save_is_refused_and_nothing_written(app):
    svc = app.state.services
    before = svc.config_store.path.read_text()
    with pytest.raises(Exception):
        svc.config_store.save(lambda raw: raw.update(backend_pool={"mode": "bogus"}))
    assert svc.config_store.path.read_text() == before
    assert svc.config_store.raw.get("backend_pool", {}).get("mode") != "bogus"


def test_config_migration_stamps_version_and_keeps_backup(tmp_path):
    from foundry_router.config import CONFIG_VERSION, ConfigStore
    p = tmp_path / "config.yaml"
    p.write_text("server: {host: 127.0.0.1, port: 1}\n")
    store = ConfigStore(p)
    store.load()
    assert store.raw["config_version"] == CONFIG_VERSION == store.config.config_version
    assert (tmp_path / "config.yaml.v1.bak").exists()
    assert yaml.safe_load(p.read_text())["config_version"] == CONFIG_VERSION
    p.write_text("config_version: 99\n")                 # from a newer Foundry: untouched
    store.load()
    assert store.raw["config_version"] == 99 and store.migration_notes == []


# -- export / import / snapshots -------------------------------------------------------

def test_export_import_keeps_real_secrets_and_snapshots(app, client):
    svc = app.state.services
    svc.config_store.save(lambda raw: raw["agent_brain"].update(api_key=SECRET))
    svc.personas.upsert("Mine", description="original", execution_mode="direct")
    b = client.get("/admin/api/export").json()
    assert SECRET not in json.dumps(b) and b["config"]["agent_brain"]["api_key"] == "<redacted>"
    assert any(p["virtual_name"] == "Mine" for p in b["personas"])
    full = client.get("/admin/api/export?secrets=1").json()
    assert full["config"]["agent_brain"]["api_key"] == SECRET
    # edit the bundle: a persona change + a config change, secret still redacted
    for p in b["personas"]:
        if p["virtual_name"] == "Mine":
            p["description"] = "from bundle"
    b["personas"].append({"virtual_name": "Imported", "description": "new", "enabled": 1})
    b["config"]["media"] = {**b["config"].get("media", {}), "jobs_per_day": 9}
    r = client.post("/admin/api/import", json={"bundle": b}).json()
    assert r["ok"] and r["personas"]["added"] == 1 and r["personas"]["updated"] >= 1
    assert svc.config_store.config.agent_brain.api_key == SECRET        # not "<redacted>"
    assert svc.config_store.config.media.jobs_per_day == 9
    assert svc.personas.get("Mine")["description"] == "from bundle"
    # undo the persona part from the automatic snapshot
    snap = r["snapshot"]
    client.post(f"/admin/api/snapshots/{snap}/restore")
    assert svc.personas.get("Mine")["description"] == "original"
    assert svc.personas.get("Imported") is None
    bad = client.post("/admin/api/import", json={"bundle": {"format": "x"}})
    assert bad.status_code == 400


def test_profile_apply_takes_a_snapshot(app, client):
    client.post("/admin/api/profiles/animaluprising-kilo/apply")
    snaps = client.get("/admin/api/snapshots").json()["snapshots"]
    assert snaps and "animaluprising-kilo" in snaps[0]["reason"]


# -- diagnostics -------------------------------------------------------------------

def test_diagnostics_bundle_has_no_secrets_or_prompts(app, client, monkeypatch):
    from foundry_router.config import ClientConfig
    svc = app.state.services
    monkeypatch.setenv("SOME_SERVICE_TOKEN", "envsecret-998877")
    svc.config_store.save(lambda raw: raw["agent_brain"].update(api_key=SECRET))
    svc.config_store.config.clients = [ClientConfig(name="kilo", key="client-key-555666")]
    svc.db.execute("INSERT INTO request_log (ts, summary, error, request_id) VALUES "
                   "(?, 'my private prompt text', 'upstream said Bearer abcdef123456789', 'r1')",
                   ("2026-01-01T00:00:00+00:00",))
    svc.db.log_event("warning", "test", f"leaked {SECRET} and client-key-555666 and envsecret-998877")
    r = client.get("/admin/api/diagnostics")
    assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
    z = zipfile.ZipFile(io.BytesIO(r.content))
    names = set(z.namelist())
    assert {"manifest.json", "config.redacted.yaml", "status.json", "events.json",
            "requests.json", "README.txt"} <= names
    blob = "".join(z.read(n).decode() for n in names)
    for leak in (SECRET, "client-key-555666", "envsecret-998877", "abcdef123456789",
                 "my private prompt text"):
        assert leak not in blob, leak


# -- fair queue / breakers ----------------------------------------------------------

def test_fair_gate_takes_turns_between_clients():
    async def run():
        g = FairGate(1)
        await g.acquire("hog", 5)                       # in flight
        order = []

        async def waiter(client, tag):
            await g.acquire(client, 5)
            order.append(tag)
        tasks = [asyncio.create_task(waiter("hog", f"h{i}")) for i in range(3)]
        await asyncio.sleep(0.01)
        tasks.append(asyncio.create_task(waiter("kilo", "k1")))
        await asyncio.sleep(0.01)
        assert g.status()["waiting"] == {"hog": 3, "kilo": 1}
        for _ in range(4):
            g.release()
            await asyncio.sleep(0.01)
        await asyncio.gather(*tasks)
        assert order == ["h0", "k1", "h1", "h2"]        # kilo isn't stuck behind the burst
        # timeout removes the waiter; a cancelled waiter doesn't leak the slot
        with pytest.raises(QueueTimeout):
            await g.acquire("late", 0.05)
        assert g.waiting_count() == 0 and g.timeouts == 1
        t = asyncio.create_task(g.acquire("gone", 5))
        await asyncio.sleep(0.01)
        t.cancel()
        await asyncio.sleep(0.01)
        g.release()
        assert g.active == 0 and g.waiting_count() == 0
    asyncio.run(run())


def test_backend_concurrency_queues_and_records_wait(ledgered, client):
    ledgered.pool.backends["strata"].config.max_concurrent = 1
    STATE["delay"] = 0.8
    out = {}

    def call(key, name):
        out[key] = _chat(client, "Foundry-Kilo-Local", key=key)
    t = threading.Thread(target=call, args=("kk", "a"))
    t.start()
    time.sleep(0.3)
    q = client.get("/admin/api/health/functional").json()["queues"]
    strata = next(x for x in q if x["backend"] == "strata")
    assert strata["limited"] and strata["active"] == 1
    call("ck", "b")                                     # waits for the slot, then runs
    t.join()
    assert out["kk"].status_code == 200 and out["ck"].status_code == 200
    rows = ledgered.db.query("SELECT client_id, flags FROM request_log ORDER BY id")
    waited = [json.loads(r["flags"] or "{}").get("queue_ms", 0) for r in rows if r["client_id"] == "cline"]
    assert waited and waited[0] >= 200
    # a queue timeout is a failed attempt (nothing sent), not a hang
    ledgered.pool.backends["strata"].config.queue_timeout_seconds = 0.2
    STATE["delay"] = 1.0
    t = threading.Thread(target=call, args=("kk", "c"))
    t.start()
    time.sleep(0.3)
    r = _chat(client, "Foundry-Kilo-Local", key="ck")
    t.join()
    assert r.status_code >= 500 and "queue" in json.dumps(r.json())
    assert ledgered.db.query("SELECT status FROM usage_events ORDER BY id DESC LIMIT 1")[0]["status"] in (
        "released", "settled")


def test_breaker_state_and_reset(ledgered, client):
    pool = ledgered.pool
    s = pool.backends["meridian"]
    for _ in range(pool.cfg.failure_threshold):
        pool._record_failure(s, "boom")
    br = {b["backend"]: b for b in client.get("/admin/api/health/functional").json()["breakers"]}
    assert br["meridian"]["state"] == "open" and br["meridian"]["retry_in_s"] > 0
    assert br["strata"]["state"] == "closed"
    s.cooldown_until = 0
    br = {b["backend"]: b for b in client.get("/admin/api/health/functional").json()["breakers"]}
    assert br["meridian"]["state"] == "half-open"
    assert client.post("/admin/api/backends/meridian/reset-breaker").json()["ok"]
    assert s.healthy and s.consecutive_failures == 0
    assert client.post("/admin/api/backends/nope/reset-breaker").status_code == 404


def test_epoch_time_columns_filter_correctly(ledgered, client):
    """usage_events keeps epoch seconds; the Data export's "last N hours"
    filter must still include recent rows (datetime() alone read them as
    Julian days)."""
    _chat(client, "Foundry-Kilo-Local")
    r = client.get("/admin/api/data/export?table=usage_events&format=jsonl&hours=1")
    assert r.status_code == 200
    rows = [l for l in r.text.splitlines() if l.strip()]
    assert len(rows) >= 1
    t = next(x for x in client.get("/admin/api/data/tables").json()["tables"]
             if x["table"] == "usage_events")
    assert isinstance(t["oldest"], float)            # the UI formats epoch numbers itself
