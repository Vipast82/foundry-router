"""Backend read-timeout config route — the operator lever for ReadTimeout drops."""


def test_set_backend_timeout_persists(client):
    r = client.post("/admin/api/config/backend-timeout",
                    json={"request_timeout_seconds": 1800}).json()
    assert r["ok"] and r["restart_required"] is True
    cfg = client.get("/admin/api/config").json()
    assert cfg["backend_pool"]["request_timeout_seconds"] == 1800


def test_set_backend_timeout_rejects_too_low(client):
    r = client.post("/admin/api/config/backend-timeout",
                    json={"request_timeout_seconds": 5})
    assert r.status_code == 400


def test_set_backend_timeout_rejects_nonint(client):
    r = client.post("/admin/api/config/backend-timeout",
                    json={"request_timeout_seconds": "soon"})
    assert r.status_code == 400
