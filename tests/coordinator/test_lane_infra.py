"""Stage 1: artifact readiness/lineage, path boundaries, source snapshots and
deterministic event ingestion from a real (fake-Kilo) primary transcript."""

import os

import pytest

from foundry_coordinator import lanes as L
from foundry_coordinator.artifacts import ArtifactStore, authorize, inspect_image, \
    parse_capture_output
from foundry_coordinator.store import Store
from tests.coordinator.helpers import git, make_repo
from tests.coordinator.lane_env import capture_output, jpeg, png, primary_tool, setup


# ------------------------------------------------------------------ images

def test_png_and_jpeg_completeness():
    assert inspect_image(png(5, 7))["complete"] and inspect_image(png(5, 7))["width"] == 5
    part = inspect_image(png(5, 7, truncate=6))
    assert not part["complete"] and "truncated" in part["reason"] or "IEND" in part["reason"]
    bad = bytearray(png(5, 7))
    bad[30] ^= 0xFF                                   # corrupt IHDR body -> CRC mismatch
    assert "CRC" in inspect_image(bytes(bad))["reason"]
    assert inspect_image(jpeg(9, 4))["complete"] and inspect_image(jpeg(9, 4))["height"] == 4
    assert not inspect_image(jpeg(complete=False))["complete"]
    assert not inspect_image(b"GIF89a....")["complete"]


def test_capture_record_parsing():
    data = png(4, 3)
    out = capture_output("C:/ws/docs/evidence/T2/menu.png", data, 4, 3)
    [r] = parse_capture_output(out)
    assert r["valid"] == "ok" and r["file_bytes"] == len(data) and (r["width"], r["height"]) == (4, 3)
    [bad] = parse_capture_output(capture_output("x/menu_INVALID.png", data, 4, 3, valid=False))
    assert bad["valid"] == "invalid" and "occluded" in bad["reason"]
    assert parse_capture_output("pytest -q\n3 passed") == []


# ------------------------------------------------------------------ paths

def test_path_boundaries(tmp_path):
    ws = tmp_path / "ws"
    (ws / "docs" / "evidence" / "T2").mkdir(parents=True)
    (ws / "_secrets").mkdir()
    (ws / "docs" / "evidence" / "T2" / "a.png").write_bytes(png())
    (ws / "_secrets" / "a.png").write_bytes(png())
    allowed = ["docs/evidence/{task}/*.png", "_secrets/*"]
    assert authorize(str(ws), "docs/evidence/T2/a.png", allowed, "T2")[0] == "docs/evidence/T2/a.png"
    assert authorize(str(ws), str(ws / "docs/evidence/T2/a.png"), allowed, "T2")[0]
    assert authorize(str(ws), "docs/evidence/T2/../../../x.png", allowed, "T2")[0] is None
    assert "outside" in authorize(str(ws), str(tmp_path / "other.png"), allowed, "T2")[1]
    assert "denied" in authorize(str(ws), "_secrets/a.png", allowed, "T2")[1]
    assert "not an approved" in authorize(str(ws), "docs/evidence/T3/a.png", allowed, "T2")[1]
    target = tmp_path / "outside"
    target.mkdir()
    (target / "b.png").write_bytes(png())
    link = ws / "docs" / "evidence" / "T2" / "link"
    try:
        os.symlink(target, link, target_is_directory=True)
    except (OSError, NotImplementedError):
        if os.name != "nt":
            pytest.skip("symlinks need privileges here")
        import subprocess            # a directory junction needs no privilege on Windows
        subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], check=True,
                       capture_output=True)
    assert authorize(str(ws), "docs/evidence/T2/link/b.png", ["docs/evidence/T2/**"], "T2")[0] is None


# ------------------------------------------------------------------ artifact lineage

def _arts(tmp_path):
    ws = tmp_path / "ws"
    (ws / "docs" / "evidence" / "T2").mkdir(parents=True)
    store = Store(str(tmp_path / "s.sqlite"))
    return ws, ArtifactStore(store, str(tmp_path / "state"))


def _rec(a, ws, rel, **kw):
    return a.record(project_id="game", task_id="T2", run_id="r1", session_id="s1",
                    workspace=str(ws), path=rel, allowed=["docs/evidence/{task}/*.png"], **kw)


def test_overwrite_is_a_new_generation_and_old_is_superseded(tmp_path):
    ws, a = _arts(tmp_path)
    f = ws / "docs/evidence/T2/menu.png"
    f.write_bytes(png(4, 3, (1, 2, 3)))
    r1 = _rec(a, ws, "docs/evidence/T2/menu.png")
    assert r1["status"] == "recorded" and r1["artifact"]["generation"] == 1
    assert _rec(a, ws, "docs/evidence/T2/menu.png")["status"] == "duplicate"   # rapid duplicate
    f.write_bytes(png(4, 3, (9, 9, 9)))
    r2 = _rec(a, ws, "docs/evidence/T2/menu.png")
    assert r2["artifact"]["generation"] == 2 and r2["superseded"] == [r1["artifact"]["artifact_id"]]
    assert a.get(r1["artifact"]["artifact_id"])["status"] == "superseded"
    assert r2["artifact"]["prev_artifact"] == r1["artifact"]["artifact_id"]
    # the snapshot is outside the workspace and immutable: workspace edits don't change it
    f.write_bytes(png(4, 3, (5, 5, 5)))
    assert a.read_bytes(a.get(r1["artifact"]["artifact_id"])) == png(4, 3, (1, 2, 3))
    assert not a.get(r1["artifact"]["artifact_id"])["store_path"].startswith(str(ws))
    with open(a.get(r1["artifact"]["artifact_id"])["store_path"], "ab") as fh:
        fh.write(b"tamper")
    with pytest.raises(ValueError):
        a.read_bytes(a.get(r1["artifact"]["artifact_id"]))


def test_partial_mismatched_and_oversize_images(tmp_path):
    ws, a = _arts(tmp_path)
    f = ws / "docs/evidence/T2/menu.png"
    f.write_bytes(png(4, 3, truncate=8))
    r = _rec(a, ws, "docs/evidence/T2/menu.png")
    assert r["status"] == "not_ready" and "incomplete" in r["reason"]
    f.write_bytes(png(4, 3))
    r = _rec(a, ws, "docs/evidence/T2/menu.png", record={"file_bytes": 10})
    assert r["status"] == "not_ready" and "fileBytes" in r["reason"]
    r = _rec(a, ws, "docs/evidence/T2/menu.png", record={"width": 1920, "height": 1080})
    assert r["status"] == "rejected" and "capture record says" in r["reason"]
    assert _rec(a, ws, "docs/evidence/T2/menu.png", max_bytes=10)["status"] == "rejected"
    assert _rec(a, ws, "docs/evidence/T2/nothere.png")["status"] == "not_ready"


def test_filesystem_fallback_needs_stability(tmp_path):
    ws, a = _arts(tmp_path)
    clock = [1000.0]
    a.clock = lambda: clock[0]
    (ws / "docs/evidence/T2/m.png").write_bytes(png())
    kw = dict(project_id="game", task_id="T2", run_id="r", session_id="s", workspace=str(ws),
              allowed=["docs/evidence/{task}/*.png"], debounce_s=5)
    assert a.scan(**kw) == []                     # first sight: not ready
    clock[0] += 1
    assert a.scan(**kw) == []                     # inside the debounce
    clock[0] += 10
    [r] = a.scan(**kw)
    assert r["status"] == "recorded" and r["artifact"]["source"] == "filesystem"
    clock[0] += 10
    assert a.scan(**kw) == []                     # once per stable file


def test_dirty_snapshots_sharing_head_differ_and_writer_race(tmp_path):
    root, _ = make_repo(tmp_path, "g", [], extra_files={"src/a.lua": "x = 1\n"})
    from foundry_coordinator.adapters.git import Workspace
    ws = Workspace(str(root))
    a = ArtifactStore(Store(str(tmp_path / "s.sqlite")), str(tmp_path / "state"))
    kw = dict(ws=ws, project_id="g", task_id="T", run_id="r", workspace=str(root),
              patterns=["src/*"])
    s1 = a.source_snapshot(**kw)
    (root / "src/a.lua").write_text("x = 2\n")
    s2 = a.source_snapshot(**kw)
    assert s1["head"] == s2["head"] and s1["snapshot_id"] != s2["snapshot_id"] and s2["dirty"]
    assert a.snapshot_file(s1["snapshot_id"], "src/a.lua").strip() == "x = 1"   # frozen bytes
    assert a.drift(s1["snapshot_id"], str(root), ["src/a.lua", "src/b.lua"]) == \
        {"src/a.lua": "changed", "src/b.lua": "absent"}
    heads = iter([s1["head"], "f" * 40])
    ws.head = lambda: next(heads)                 # HEAD moves between the two reads
    assert a.source_snapshot(**kw)["status"] == "unstable"


# ------------------------------------------------------------------ classifiers

def test_failure_signature_ignores_volatile_details():
    ls = {"failure_commands": ["lune"]}
    a = L.classify(L.tool_view({"type": "tool", "tool": "bash", "id": "p1", "state": {
        "status": "completed", "input": {"command": "lune run tests/lune/run.luau"},
        "output": "LUNE 41/42 FAIL\nFAIL S45 ordering at C:/x/y.luau:12 (0.31s) 12:01:02",
        "metadata": {"exit": 1}}}), "T2", ls)
    b = L.classify(L.tool_view({"type": "tool", "tool": "bash", "id": "p2", "state": {
        "status": "completed", "input": {"command": "lune run tests/lune/run.luau"},
        "output": "LUNE 40/42 FAIL\nFAIL S45 ordering at D:/q/z.luau:99 (1.02s) 13:44:55",
        "metadata": {"exit": 1}}}), "T2", ls)
    fa = [e for e in a if e["kind"] == "failure"][0]
    fb = [e for e in b if e["kind"] == "failure"][0]
    assert fa["signature"] == fb["signature"] and fa["key"] != fb["key"]
    ok = L.classify(L.tool_view({"type": "tool", "tool": "bash", "id": "p3", "state": {
        "status": "completed", "input": {"command": "lune run x"}, "output": "LUNE 42/42 PASS",
        "metadata": {"exit": 0}}}), "T2", ls)
    assert not [e for e in ok if e["kind"] == "failure"]
    running = {"type": "tool", "tool": "bash", "state": {"status": "running"}}
    assert L.tool_view(running) is None


def test_gate_commit_and_api_events():
    ls = {"failure_commands": []}
    ev = L.classify(L.tool_view({"type": "tool", "tool": "bash", "id": "p", "state": {
        "status": "completed", "input": {"command": "git commit -m 'T2: x' src/menu.luau"},
        "output": "[master abc] T2: x\nGATE T2 PASS\nPlayerGui.Frame: Foo is not a valid member of "
                  "Frame\nUnknown global 'wait2'", "metadata": {"exit": 0}}}), "T2", ls)
    kinds = sorted(e["kind"] for e in ev)
    assert kinds == ["api", "api", "commit", "gate"]
    assert {e["signature"] for e in ev if e["kind"] == "api"} == {"Frame.Foo", "global:wait2"}


def test_envelope_and_patch_validation():
    env = L.parse_envelope('x\n```helper-result\n{"findings": [{"severity": "HIGH", '
                           '"confidence": 3, "observed": "o"}], "patch": "--- a/src/m.luau\\n'
                           '+++ b/src/m.luau\\n@@ -1 +1 @@\\n-a\\n+b\\n"}\n```')
    assert env["structured"] and env["findings"][0]["severity"] == "high"
    assert env["findings"][0]["confidence"] == 1.0
    v = L.validate_patch(env["patch"], ["src/m.luau"])
    assert v["well_formed"] and not v["out_of_scope"]
    v = L.validate_patch("--- a/../etc\n+++ b/../etc\n@@ -1 +1 @@\n", ["src/*"])
    assert v["out_of_scope"] == ["../etc"]
    assert not L.parse_envelope("no block")["structured"]


# ------------------------------------------------------------------ ingestion (real transcript)

@pytest.fixture()
def env(tmp_path):
    e = setup(tmp_path)
    yield e
    e["fake"].stop()


def test_ingestion_from_busy_primary_records_capture_once(env):
    c, root = env["c"], env["root"]
    data = png(6, 4)
    f = root / "docs/evidence/T2/menu.png"
    f.write_bytes(data)
    primary_tool(env, "pwsh tools/capture-window.ps1 -Out docs/evidence/T2/menu.png",
                 capture_output(str(f), data, 6, 4))
    c.tick()
    c.tick()                                      # re-reading the transcript adds nothing
    arts = c.helpers.lanes.artifacts.list("game", "T2", "capture")
    assert len(arts) == 1 and arts[0]["valid"] == "ok" and arts[0]["source"] == "tool_result"
    assert arts[0]["sha256"] and arts[0]["generation"] == 1
    caps = c.helpers.lanes.events("game", "T2", "capture")
    assert len(caps) == 1 and caps[0]["status"] == "artifact"
    assert c.store.task("game", "T2")["state"] == "IMPLEMENTING"
    # the primary got no extra prompt while busy
    assert len([p for p in env["fake"].prompts if p["session_id"] == env["primary"]]) == 1


def test_capture_file_not_written_yet_is_retried_not_trusted(env):
    c, root = env["c"], env["root"]
    data = png(6, 4)
    f = root / "docs/evidence/T2/late.png"
    primary_tool(env, "pwsh tools/capture-window.ps1", capture_output(str(f), data, 6, 4))
    c.tick()
    [ev] = c.helpers.lanes.events("game", "T2", "capture")
    assert ev["status"] == "pending" and "not present" in ev["detail"]["not_ready"]
    f.write_bytes(data[:-5])                      # partial write
    c.tick()
    assert c.helpers.lanes.events("game", "T2", "capture")[0]["status"] == "pending"
    f.write_bytes(data)
    c.tick()
    assert c.helpers.lanes.events("game", "T2", "capture")[0]["status"] == "artifact"


def test_lanes_off_ingest_nothing(tmp_path):
    e = setup(tmp_path, lanes="")
    try:
        primary_tool(e, "lune run x", "FAIL a", exit_code=1)
        e["c"].tick()
        assert e["c"].helpers.lanes.events("game", "T2") == []
    finally:
        e["fake"].stop()


def test_head_move_and_context_threshold_are_events(env):
    c, root = env["c"], env["root"]
    c.tick()
    (root / "src/menu.luau").write_text("local M = {x=1}\nreturn M\n")
    git(root, "commit", "-qam", "T2: wip")
    env["fake"].set_status(env["primary"], "busy")
    env["fake"].add_message(env["primary"], "assistant", [{"type": "text", "text": "..."}],
                            completed=False, tokens={"input": 2000, "output": 10,
                                                     "cache": {"read": 99000, "write": 0}})
    c.tick()
    kinds = {e["kind"] for e in c.helpers.lanes.events("game", "T2")}
    assert {"run_start", "commit", "context"} <= kinds
