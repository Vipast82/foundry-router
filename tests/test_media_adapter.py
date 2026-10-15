"""The local media adapter (contrib/media-adapter/foundry_media_mcp.py): its
tools against a real Foundry app (HTTP swapped for the test client), its
folder guard, and its stdio MCP protocol in a subprocess."""

import base64
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.test_media import HITS, STATE, media, png  # noqa: F401 (fixture)

ADAPTER = Path(__file__).resolve().parent.parent / "contrib" / "media-adapter" / "foundry_media_mcp.py"


@pytest.fixture()
def adapter(media, client, tmp_path, monkeypatch):    # noqa: F811
    spec = importlib.util.spec_from_file_location("foundry_media_mcp", ADAPTER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "state"))

    def fake_http(cfg, method, path, body=None, timeout=90, raw=False):
        r = client.request(method, path, json=body)
        if r.status_code >= 400:
            err = r.json().get("error") or {}
            raise mod.FoundryError(err.get("message"), r.status_code, err.get("code"))
        return (r.content, dict(r.headers)) if raw else r.json()
    monkeypatch.setattr(mod, "http", fake_http)
    project = tmp_path / "AnimalUprising"
    (project / "assets").mkdir(parents=True)
    monkeypatch.setenv("FOUNDRY_MEDIA_ROOTS", str(project))
    monkeypatch.setenv("FOUNDRY_URL", "http://foundry.test")
    for k in ("FOUNDRY_MEDIA_DEFAULT", "FOUNDRY_MEDIA_WAIT", "FOUNDRY_KEY"):
        monkeypatch.delenv(k, raising=False)
    cfg = mod.load_config(["--config", str(tmp_path / "none.json")])
    return mod, cfg, project


def _payload(res):
    return json.loads(res["content"][0]["text"])


def test_generate_saves_into_project_with_sidecar(adapter):
    mod, cfg, project = adapter
    res = mod.call_tool(cfg, "generate_image", {"prompt": "Wolf Pack Banner", "wait_seconds": 20})
    assert res["isError"] is False, res
    out = _payload(res)
    saved, = out["saved"]
    p = Path(saved["path"])
    assert p.parent == project / "assets" / "generated" / "_unapproved"
    assert p.name.startswith("wolf-pack-banner-") and p.suffix == ".png"
    assert p.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    side = json.loads(Path(str(p) + ".foundry.json").read_text())
    assert side["status"] == "unapproved" and side["job_id"] == out["job_id"]
    assert "UNAPPROVED" in out["next"]
    assert "b64" not in json.dumps(res)            # paths only, no image data
    # status for the same job does not download twice or submit again
    again = _payload(mod.call_tool(cfg, "image_job_status", {"job_id": out["job_id"]}))
    assert again["saved"][0]["already_saved"] is True and len(HITS) == 1


def test_save_as_is_confined_to_authorised_folders(adapter):
    mod, cfg, project = adapter
    ok = _payload(mod.call_tool(cfg, "generate_image", {
        "prompt": "icon", "save_as": "assets/ui/icon.jpg", "wait_seconds": 20}))
    # the real format decides the extension
    assert ok["saved"][0]["path"] == str(project / "assets" / "ui" / "icon.png")
    second = _payload(mod.call_tool(cfg, "generate_image", {
        "prompt": "icon", "save_as": "assets/ui/icon.png", "allow_duplicate": True,
        "wait_seconds": 20}))
    assert second["saved"][0]["path"].endswith("icon-v1.png")        # never overwritten
    hits = len(HITS)
    for bad in ("../outside.png", str(Path(project).parent / "x.png")):
        res = mod.call_tool(cfg, "generate_image", {"prompt": "esc " + bad, "save_as": bad,
                                                    "wait_seconds": 20})
        assert res["isError"] and "outside the authorised folders" in res["content"][0]["text"]
    assert len(HITS) == hits                      # refused before any image was paid for
    # the same request again dedupes to the latest job (the variation) and
    # returns the file it was already saved as, instead of writing a copy
    again = _payload(mod.call_tool(cfg, "generate_image", {
        "prompt": "icon", "save_as": "assets/ui/icon.png", "wait_seconds": 20}))
    assert again["job_id"] == second["job_id"]
    assert again["saved"][0]["path"] == second["saved"][0]["path"]
    assert again["saved"][0].get("already_saved") is True


def test_edit_reads_only_authorised_inputs(adapter, tmp_path):
    mod, cfg, project = adapter
    (project / "assets" / "ref.png").write_bytes(png(20, 20))
    res = mod.call_tool(cfg, "edit_image", {"prompt": "add a hat", "images": ["assets/ref.png"],
                                            "wait_seconds": 20})
    assert res["isError"] is False, res
    path, sent = HITS[-1]
    assert path == "/v1/images/edits"
    assert base64.b64decode(sent["images"][0]["image_url"].split(",", 1)[1]) == png(20, 20)
    (tmp_path / "secret.png").write_bytes(png())
    res = mod.call_tool(cfg, "edit_image", {"prompt": "x", "images": [str(tmp_path / "secret.png")]})
    assert res["isError"] and "outside the authorised folders" in res["content"][0]["text"]


def test_running_job_returns_id_and_status_saves_later(adapter):
    mod, cfg, project = adapter
    STATE.update(mode="slow", delay=1.5)
    first = _payload(mod.call_tool(cfg, "generate_image", {"prompt": "slow art", "wait_seconds": 0}))
    assert first["status"] in ("queued", "running") and "Do NOT call" in first["next"]
    done = _payload(mod.call_tool(cfg, "image_job_status", {"job_id": first["job_id"],
                                                            "wait_seconds": 20}))
    assert done["status"] == "succeeded" and Path(done["saved"][0]["path"]).is_file()
    assert len(HITS) == 1


def test_budget_refusal_is_reported_not_retried(adapter):
    mod, cfg, _project = adapter
    STATE["mode"] = "429"
    res = mod.call_tool(cfg, "generate_image", {"prompt": "nope", "wait_seconds": 20})
    assert res["isError"] and "quota_exceeded" in res["content"][0]["text"]
    assert len(HITS) == 1


def test_stdio_protocol(tmp_path):
    env = {**os.environ, "FOUNDRY_URL": "http://127.0.0.1:9", "FOUNDRY_MEDIA_ROOTS": str(tmp_path),
           "LOCALAPPDATA": str(tmp_path / "state")}
    msgs = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                    "clientInfo": {"name": "t", "version": "1"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
         "params": {"name": "image_job_status", "arguments": {"job_id": "bogus"}}},
        {"jsonrpc": "2.0", "id": 4, "method": "nope"},
    ]
    p = subprocess.run([sys.executable, str(ADAPTER)], input="\n".join(json.dumps(m) for m in msgs) + "\n",
                       capture_output=True, text=True, env=env, timeout=30)
    replies = {r["id"]: r for r in map(json.loads, p.stdout.splitlines())}
    assert replies[1]["result"]["serverInfo"]["name"] == "foundry-media"
    assert replies[1]["result"]["protocolVersion"] == "2025-06-18"
    assert {t["name"] for t in replies[2]["result"]["tools"]} == {
        "generate_image", "edit_image", "image_job_status", "list_image_jobs", "image_settings"}
    assert replies[3]["result"]["isError"] is True
    assert replies[4]["error"]["code"] == -32601
