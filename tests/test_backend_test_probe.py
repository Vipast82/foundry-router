"""Per-backend cheap liveness probe (Phase 2b): the Backends tab's 'test' button
lists a backend's models (free, no generation) to confirm reachability, without
waiting for the health-check interval to flip its status."""


def test_backend_test_validates_and_reports(client):
    # name required
    assert client.post("/admin/api/backends/test", json={}).status_code == 400
    # unknown backend -> clean 404, not a 500
    assert client.post("/admin/api/backends/test",
                       json={"name": "nope"}).status_code == 404


def test_backend_test_probes_a_configured_backend(client, monkeypatch):
    svc = client.app.state.services

    class _Proto:
        async def list_models(self):
            return ["m1", "m2", "m3"]

    class _State:
        protocol = _Proto()

    # inject a fake backend into the pool
    svc.pool.backends["fake"] = _State()
    body = client.post("/admin/api/backends/test", json={"name": "fake"}).json()
    assert body["ok"] is True and body["models"] == 3
    assert body["sample"] == ["m1", "m2", "m3"] and "latency_ms" in body


def test_backend_test_reports_error_cleanly(client):
    svc = client.app.state.services

    class _Proto:
        async def list_models(self):
            raise RuntimeError("connection refused")

    class _State:
        protocol = _Proto()

    svc.pool.backends["broken"] = _State()
    body = client.post("/admin/api/backends/test", json={"name": "broken"}).json()
    assert body["ok"] is False and "connection refused" in body["error"]
