"""Build replay, router side (v0.168.0, docs/plans/BLENDER_BUILD_REPLAY_PLAN.md R2): the bridge's
`build_run` observations are stored with their thumbnails (kept when a later send has none),
scoped to the operator's projects, capped and filtered; the task page's "Build replay" section
is registered, migrated into saved layouts after the sprint sections, and escapes what it shows."""

import base64
import json
import shutil
import subprocess

import pytest

from tests.test_control_center import (BRIDGE_A_KEY, OP_B_KEY, batch, cc, ingest,  # noqa: F401
                                       login)
from tests.test_control_ui import _script

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 24
JPG = b"\xff\xd8\xff\xe0" + b"\x00" * 24


def entry(run_id="br-1", ts=1000.0, **kw):
    e = {"run_id": run_id, "source": "slot", "started_ts": ts, "ended_ts": ts + 40, "seconds": 40,
         "exit": 0, "args": ["-b", "-P", "tools/blender/build_bs_king.py"],
         "where": {"slot": "slot-1", "host": "studio@h"},
         "timings": {"push": 3, "blender": 30, "copy_back": 2},
         "script": {"path": "tools/blender/build_bs_king.py", "sha256": "a" * 64, "copy": True,
                    "diff": "--- a\n+++ b\n+x", "diff_lines": 3, "changed": True},
         "files_back": [["assets/exports/BS-KING/front.png", 1234]], "files_back_total": 1,
         "log_tail": "line\nmesh_report PASS", "key_lines": ["mesh_report PASS"],
         "renders": [{"path": "assets/exports/BS-KING/front.png", "bytes": 1234,
                      "thumb": "thumbs/00.png"}]}
    e.update(kw)
    return e


def obs(run_id="br-1", project="proj", task="T-1", ts=1000.0, thumbs=None, **kw):
    o = {"type": "build_run", "project_id": project, "task_id": task, "run_id": run_id, "ts": ts,
         "entry": entry(run_id, ts, **kw)}
    if thumbs is not None:
        o["thumbs"] = thumbs
    return o


def b64(raw):
    return base64.b64encode(raw).decode()


def test_build_run_is_stored_and_its_thumbnail_kept_when_a_later_send_has_none(client, cc):
    ingest(client, obs(thumbs=[{"name": "thumbs/00.png", "mime": "image/png", "b64": b64(PNG)},
                               {"name": "thumbs/01.jpg", "mime": "image/jpeg", "b64": b64(JPG)}]))
    ingest(client, obs(exit=1))                         # the same run again, without thumbnails
    login(client)
    r = client.get("/control/api/tasks/proj/T-1/builds")
    assert r.status_code == 200, r.text
    runs = r.json()["runs"]
    assert len(runs) == 1 and runs[0]["exit"] == 1
    assert runs[0]["thumbs"] == ["thumbs/00.png", "thumbs/01.jpg"]
    assert runs[0]["script"]["path"] == "tools/blender/build_bs_king.py"
    t = client.get("/control/api/builds/br-1/thumb/thumbs/00.png")
    assert t.status_code == 200 and t.content == PNG
    assert t.headers["content-type"].startswith("image/png")
    assert t.headers["x-content-type-options"] == "nosniff"
    assert "default-src 'none'" in t.headers["content-security-policy"]
    j = client.get("/control/api/builds/br-1/thumb/thumbs/01.jpg")
    assert j.status_code == 200 and j.headers["content-type"].startswith("image/jpeg")
    assert client.get("/control/api/builds/br-1/thumb/thumbs/09.png").status_code == 404
    assert client.get("/control/api/builds/nope/thumb/thumbs/00.png").status_code == 404


def test_odd_or_oversized_thumbnails_are_dropped(client, cc):
    from foundry_router.control_center import MAX_THUMB_B64
    ingest(client, obs(thumbs=[
        {"name": "../../etc/passwd", "mime": "image/png", "b64": b64(PNG)},
        {"name": "thumbs/00.png", "mime": "text/html", "b64": b64(PNG)},
        {"name": "thumbs/01.png", "mime": "image/png", "b64": "A" * (MAX_THUMB_B64 + 4)},
        {"name": "thumbs/02.png", "mime": "image/png", "b64": b64(PNG)},
        "garbage"]))
    login(client)
    runs = client.get("/control/api/tasks/proj/T-1/builds").json()["runs"]
    assert runs[0]["thumbs"] == ["thumbs/02.png"]


def test_runs_come_oldest_first_and_long_text_is_capped(client, cc):
    ingest(client, obs("br-2", ts=2000.0, log_tail="x" * 50000,
                       script={"path": "s.py", "diff": "d" * 70000}),
           obs("br-1", ts=1000.0))
    login(client)
    runs = client.get("/control/api/tasks/proj/T-1/builds").json()["runs"]
    assert [r["run_id"] for r in runs] == ["br-1", "br-2"]
    assert len(runs[1]["log_tail"]) == 40000 and len(runs[1]["script"]["diff"]) == 60000
    assert client.get("/control/api/tasks/proj/T-9/builds").json()["runs"] == []


def test_a_run_without_an_id_is_refused_and_projects_are_isolated(client, cc):
    r = client.post("/v1/foundry/telemetry/batch",
                    headers={"Authorization": f"Bearer {BRIDGE_A_KEY}"},
                    json=batch(obs(project="other"), obs(run_id=""), obs(project="")))
    body = r.json()
    assert body["skipped"].get("project_not_allowed") == 1
    assert body["skipped"].get("invalid:build_run") == 1
    assert body["skipped"].get("project_id_required") == 1
    ingest(client, obs())
    login(client, OP_B_KEY)                              # op-b may only see project "other"
    assert client.get("/control/api/tasks/proj/T-1/builds").status_code == 404
    assert client.get("/control/api/builds/br-1/thumb/thumbs/00.png").status_code == 404


def test_retention_prunes_old_build_runs(client, cc):
    ingest(client, obs())
    cc.db.execute("UPDATE cc_build_runs SET received_ts=0")
    cc.prune()
    assert cc.db.query("SELECT run_id FROM cc_build_runs") == []


def test_a_build_run_reaches_the_live_feed(client, cc):
    ingest(client, obs())
    rows = cc.db.query("SELECT kind, entity_id FROM cc_feed WHERE kind='build_run'")
    assert rows == [{"kind": "build_run", "entity_id": "br-1"}]
    ingest(client, obs())                                # unchanged: no new feed row
    assert len(cc.db.query("SELECT 1 FROM cc_feed WHERE kind='build_run'")) == 1


# ------------------------------------------------------------------ the task page section

def test_build_replay_section_is_registered_and_wired():
    s = _script()
    sections = s.split("const SECTIONS =")[1].split("];")[0]
    assert '"builds"' in sections and sections.index("...SECTIONS_V4") < sections.index('"builds"')
    assert 'builds: "Build replay"' in s.split("const SECTION_TITLES =")[1].split("};")[0]
    assert "builds:" in s.split("const SECTION_HELP =")[1].split("};")[0]
    assert "  builds(s) {" in s and "  builds(sec) { wireSprint(sec); wireBuilds(sec); }," in s
    assert 'case "build_run":' in s and "/builds`" in s


node = pytest.mark.skipif(not shutil.which("node"), reason="node not installed")


def run_js(tmp_path, body):
    f = tmp_path / "t.js"
    f.write_text("""
const ESC_MAP = {"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"};
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ESC_MAP[c]);
const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
""" + body, encoding="utf-8")
    r = subprocess.run(["node", str(f)], capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0, r.stderr[:3000]
    return json.loads(r.stdout.strip().splitlines()[-1])


def between(src, start, end):
    i = src.index(start)
    return src[i:src.index(end, i)]


@node
def test_saved_layouts_get_build_replay_after_the_sprint_sections(tmp_path):
    s = _script()
    block = between(s, "const PREF_KEY", "function loadPrefs")
    out = run_js(tmp_path, block + """
const v4 = {v: 4, sidebar: {order: ["identity", "readiness", "findings", "lead", "context", "helpers"], hidden: [], collapsed: []}};
const m = migratePrefs(JSON.parse(JSON.stringify(v4)));
const kept = migratePrefs({v: 4, sidebar: {order: ["builds", "identity"], hidden: [], collapsed: []}});
console.log(JSON.stringify({order: m.sidebar.order, kept: kept.sidebar.order.slice(0, 2),
  fresh: migratePrefs(null).sidebar.order}));
""")
    assert out["order"][:5] == ["identity", "readiness", "findings", "lead", "builds"]
    assert out["kept"] == ["builds", "identity"]           # an operator's own place is kept
    fresh = out["fresh"]
    assert fresh.index("builds") == fresh.index("lead") + 1


@node
def test_build_replay_renders_steps_and_escapes(tmp_path):
    s = _script()
    method = between(s, "  builds(s) {", "  findings(s) {")
    out = run_js(tmp_path, """
const S = {project: "proj", task: "T-1", openProv: new Set(["bld-log"])}; let loads = 0;
const taskKey = (p, t) => `${p}/${t}`; const nowS = () => 1000;
const loadBuilds = () => { loads++; };
const sprintPill = (t, c) => `<span class="pill ${esc(c || "")}">${esc(t)}</span>`;
const tsText = ts => "T" + ts; const short = (x, n = 10) => x ? String(x).slice(0, n) : "";
const fmtN = v => String(v);
const R = {""" + method + """};
const empty = (S.builds = {key: "proj/T-1", at: 1000, data: {runs: []}}, R.builds(null));
const run = (id, exit, extra) => Object.assign({run_id: id, exit, started_ts: 1, source: "slot",
  where: {slot: "slot-1"}, script: {path: "build.py", sha256: "abcdef0123456789", changed: true,
  diff: "<script>alert(1)</script>"}, thumbs: ["thumbs/00.png", "thumbs/01.png"],
  renders: [{path: "front.png", thumb: "thumbs/00.png"}, {path: "back.png", thumb: "thumbs/01.png"}],
  key_lines: ["mesh_report <b>PASS</b>"], log_tail: "tail <img src=x>", files_back: [["a.png", 5]]}, extra || {});
S.builds = {key: "proj/T-1", at: 1000, data: {runs: [run("r1", 0), run("r2", 75), run("r3", 2, {thumbs: [], renders: []})]}};
const newest = R.builds(null);
S.buildSel = {"proj/T-1": 1}; S.buildView = 1;
const second = R.builds(null);
S.builds.at = 900; R.builds(null);
console.log(JSON.stringify({empty, newest, second, loads}));
""")
    assert "blender.journal" in out["empty"]
    assert "run 3 of 3" in out["newest"] and "exit 2" in out["newest"]
    assert "no renders in this run" in out["newest"]
    sec = out["second"]
    assert "run 2 of 3" in sec and "slot busy (75)" in sec and "slot slot-1" in sec
    assert "<script>" not in sec and "&lt;script&gt;" in sec
    assert "<b>PASS</b>" not in sec and "<img src=x>" not in sec
    assert 'data-bview="1" class="sel"' in sec and "/control/api/builds/r2/thumb/thumbs/01.png" in sec
    assert 'data-prov="bld-log" open' in sec and 'data-prov="bld-diff">' in sec
    assert out["loads"] == 1                               # only the stale read reloads


def test_a_real_journal_entry_reaches_the_task_page_through_the_bridge(client, cc, tmp_path):
    """Round trip: build_journal.record -> the bridge's build_run -> the router -> /builds."""
    import os
    import time

    from PIL import Image

    from foundry_coordinator import build_journal as BJ
    from foundry_coordinator.store import Store
    from foundry_coordinator.telemetry import CoordReader, TelemetryBridge
    repo, state = tmp_path / "repo", tmp_path / "state"
    (repo / "tools" / "blender").mkdir(parents=True)
    (repo / "tools" / "blender" / "build_x.py").write_text("x = 1\n")
    os.makedirs(repo / "r")
    Image.new("RGB", (512, 512), (10, 200, 10)).save(repo / "r" / "X_front.png")
    state.mkdir()
    Store(str(state / "coordinator.sqlite"))
    e = BJ.record(str(state), project_id="proj", task_id="T-1", source="slot", repo=str(repo),
                  args=["-b", "-P", "tools/blender/build_x.py"], exit_code=0,
                  started_ts=time.time() - 3, ended_ts=time.time(),
                  log_lines=["mesh_report: PASS"], files_back=["r/X_front.png"])
    br = TelemetryBridge.__new__(TelemetryBridge)
    br.reader, br.projects, br.fp = CoordReader(str(state / "coordinator.sqlite")), {"proj": 1}, {}
    sent = []
    br._build_facts(sent, [], time.time())
    ingest(client, *sent)
    login(client)
    (run,) = client.get("/control/api/tasks/proj/T-1/builds").json()["runs"]
    assert run["run_id"] == e["run_id"] and run["key_lines"] == ["mesh_report: PASS"]
    assert run["thumbs"] == ["thumbs/00.png"]
    t = client.get(f"/control/api/builds/{e['run_id']}/thumb/thumbs/00.png")
    assert t.status_code == 200 and t.content[1:4] == b"PNG"


def test_a_rerun_action_is_bound_to_a_reported_run_of_the_task(client, cc):
    """BUILD_RERUN_PLAN (R4): the router accepts build_rerun only for a run it holds for that
    task; the coordinator checks the setting, the commit and the slot again."""
    from tests.test_control_center import task_obs
    ingest(client, task_obs("COMPLETE"), obs(run_id="br-9", task="T-1"))
    h = login(client)
    base = {"kind": "build_rerun", "project_id": "proj", "task_id": "T-1"}
    r = client.post("/control/api/actions", headers=h,
                    json={**base, "run_id": "nope", "idempotency_key": "rerun-0000001"})
    assert r.status_code == 404 and r.json()["code"] == "unknown_run"
    r = client.post("/control/api/actions", headers=h,
                    json={**base, "run_id": "br-9", "idempotency_key": "rerun-0000002"})
    assert r.status_code == 200, r.text                         # no task-state binding
    a = r.json()
    assert a["kind"] == "build_rerun"


def test_the_rerun_button_is_in_the_build_replay_section():
    js = _script()
    assert 'data-brerun="${esc(r.run_id)}"' in js and 'kind: "build_rerun"' in js
