"""Operator can wipe the usage log and the event log from the UI. Clearing
usage records an audit line in the (separate) event log; clearing events does
not self-log, so the emptied tab actually stays empty."""


def _db(client):
    return client.app.state.services.db


def test_clear_usage_empties_and_audits(client):
    db = _db(client)
    db.execute("INSERT INTO request_log (ts, persona, client_model, mode, summary, "
               "steps, duration_ms, status) VALUES "
               "('2026-07-18T00:00:00','p','m','agent','hi',0,10,'ok')")
    assert client.get("/admin/api/usage").json()["requests"]        # seeded

    r = client.post("/admin/api/usage/clear")
    assert r.status_code == 200 and r.json()["ok"] is True and r.json()["removed"] == 1
    assert client.get("/admin/api/usage").json()["requests"] == []  # gone

    # the clear itself is accountable, in the event log (a different tab)
    msgs = [e["message"] for e in client.get("/admin/api/events").json()["events"]]
    assert any("usage log cleared" in m for m in msgs)


def test_clear_events_empties_without_self_logging(client):
    db = _db(client)
    db.log_event("warning", "backend_pool", "something failed")
    db.log_event("info", "main", "started")
    assert len(client.get("/admin/api/events").json()["events"]) >= 2

    r = client.post("/admin/api/events/clear")
    assert r.status_code == 200 and r.json()["ok"] is True and r.json()["removed"] >= 2
    # truly empty — the clear did not repopulate the tab it just emptied
    assert client.get("/admin/api/events").json()["events"] == []
