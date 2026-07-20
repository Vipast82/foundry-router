"""Custom header logo, stored as a data: URI in the DB (survives restarts, no
file mount). Set/clear from the UI; size- and type-guarded."""

_PNG = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCA',"  # tiny stub


def test_branding_roundtrip_and_clear(client):
    assert client.get("/admin/api/branding").json()["logo"] is None
    r = client.post("/admin/api/branding/logo", json={"logo": _PNG})
    assert r.status_code == 200 and r.json()["ok"] is True
    assert client.get("/admin/api/branding").json()["logo"] == _PNG
    # clear
    assert client.post("/admin/api/branding/logo", json={"logo": None}).status_code == 200
    assert client.get("/admin/api/branding").json()["logo"] is None


def test_branding_rejects_non_image_and_oversize(client):
    assert client.post("/admin/api/branding/logo",
                       json={"logo": "javascript:alert(1)"}).status_code == 400
    assert client.post("/admin/api/branding/logo",
                       json={"logo": "data:image/png;base64," + "A" * 1_000_000}
                       ).status_code == 400
    assert client.get("/admin/api/branding").json()["logo"] is None   # nothing stored
