"""The Automation page (/ui/automation) and its read-side endpoints: operator
session + CSRF like the control center, the stub seed until C1 is merged,
dry run / path test / diff over a posted document, and a page script that
parses and keeps secrets out of browser storage."""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

from foundry_router.config import ClientConfig

PAGE = Path(__file__).resolve().parents[1] / "foundry_router" / "ui" / "static" / "automation.html"
OP_KEY = "op-key-automation-1234"


@pytest.fixture()
def op(app, client):
    app.state.services.config_store.config.clients.append(
        ClientConfig(name="victor", key=OP_KEY, scopes=["operator"]))
    r = client.post("/control/api/login", json={"key": OP_KEY})
    assert r.status_code == 200, r.text
    return {"x-foundry-csrf": r.json()["csrf"]}


def _script() -> str:
    return re.findall(r"<script(?![^>]*src)[^>]*>(.*?)</script>", PAGE.read_text(encoding="utf-8"), re.S)[0]


def test_page_served_with_csp_allowing_only_the_pinned_cdn(client):
    r = client.get("/ui/automation")
    assert r.status_code == 200 and "Automation" in r.text
    csp = r.headers["content-security-policy"]
    assert "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net;" in csp
    assert "connect-src 'self'" in csp and "frame-ancestors 'none'" in csp
    # the one external script is pinned to an exact version with an integrity hash
    tag = re.search(r'<script src="([^"]+)"([^>]*)>', r.text)
    assert re.search(r"sortablejs@\d+\.\d+\.\d+/", tag.group(1))
    assert 'integrity="sha384-' in tag.group(2) and 'crossorigin="anonymous"' in tag.group(2)


@pytest.mark.skipif(not shutil.which("node"), reason="node not installed")
def test_page_script_parses(tmp_path):
    f = tmp_path / "auto.js"
    f.write_text(_script(), encoding="utf-8")
    r = subprocess.run(["node", "--check", str(f)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr[:2000]


def test_browser_storage_holds_preferences_and_the_stub_draft_only():
    s = _script()
    keys = set(re.findall(r'store\("([^"]+)"', s)) | set(re.findall(r"store\((LOCAL_DRAFT)", s))
    assert keys <= {"fr-theme", "fr.auto.ws", "fr.auto.project", "LOCAL_DRAFT"}, keys  # prefs + stub draft
    assert 'const LOCAL_DRAFT = "fr.auto.draft"' in s
    assert "sessionStorage" not in s and "foundryAdminKey" not in s
    assert '$("#login-key").value = "";' in s


def test_endpoints_need_the_operator_session_and_csrf(client, op):
    client.cookies.clear()
    assert client.get("/control/api/automation/usage").status_code == 401
    assert client.get("/control/api/automation/seed").status_code == 401
    r = client.post("/control/api/login", json={"key": OP_KEY})
    csrf = {"x-foundry-csrf": r.json()["csrf"]}
    # read-only POSTs still need the CSRF header
    assert client.post("/control/api/automation/check", json={"doc": {}}).status_code == 403
    assert client.post("/control/api/automation/check", json={"doc": {}}, headers=csrf).status_code == 200


def test_seed_usage_dry_run_path_test_and_diff(client, op):
    u = client.get("/control/api/automation/usage")
    assert u.status_code == 200, u.text
    assert isinstance(u.json()["usage"], dict)
    s = client.get("/control/api/automation/seed").json()
    assert s["stub"] is True
    doc = s["doc"]
    assert [r["id"] for r in doc["roles"]][:2] == ["implementer", "lead"]
    assert doc["role_rules"]["implementer"]["permissions"]["edit"] == "allow"
    assert doc["hard_denies"]

    doc["personas"]["P"] = {"routes": [{"backend": "nowhere", "model": "m", "enabled": True}]}
    doc["roles"][0]["persona"] = "P"
    r = client.post("/control/api/automation/dry-run", headers=op,
                    json={"doc": doc, "role": "implementer"}).json()["results"][0]
    assert r["chosen"] is None and "not in the pool" in r["hops"][0]["reason"]
    every = client.post("/control/api/automation/dry-run", headers=op, json={"doc": doc}).json()
    assert len(every["results"]) == len(doc["roles"])

    pt = client.post("/control/api/automation/path-test", headers=op, json={
        "doc": doc, "role": "implementer", "op": "edit", "workspace": "C:/Roblox/AnimalUprising",
        "path": "C:\\Roblox\\AnimalUprising\\src\\x.lua"}).json()
    assert pt["decision"] == "allow"
    sh = client.post("/control/api/automation/path-test", headers=op, json={
        "doc": doc, "role": "implementer", "op": "bash", "path": "git push -f origin"}).json()
    assert sh["decision"] == "deny" and sh["locked"] is True
    assert client.post("/control/api/automation/path-test", headers=op,
                       json={"doc": doc, "role": "implementer", "path": ""}).status_code == 400
    assert client.post("/control/api/automation/dry-run", headers=op,
                       json={"doc": "nope"}).status_code == 400

    after = {**doc, "backends": {**doc["backends"], "x": {"enabled": False}}}
    ch = client.post("/control/api/automation/diff", headers=op,
                     json={"before": doc, "after": after}).json()["changes"]
    assert [c["path"] for c in ch] == ["backends.x"]


def test_dashboard_and_control_center_link_to_automation():
    static = PAGE.parent
    assert "/ui/automation" in (static / "index.html").read_text(encoding="utf-8")
    assert "/ui/automation" in (static / "control.html").read_text(encoding="utf-8")
