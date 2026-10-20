"""UI v3 training labels (AU_UI_REWORK_PLAN section 14, UI-T0 Builder C):
studio_gate verdict JSON and vision verdicts as episode labels, tags
(fast_lane, ui_v3, templates / widgets, device sizes), per-step authorship
(helper prework patches and harvested pre-code models are context, never
targets; allow_frontier_targets = false enforced for every kind), capture
artifacts linked to their episodes, and correction triples (local attempt ->
gate / vision findings -> corrected local code)."""

from __future__ import annotations

import copy
import json
import os
import struct
import time
import zlib
from pathlib import Path

import pytest

from foundry_training.export import export
from foundry_training.validate import validate_dataset
from tests.training.helpers import (SESSION, capture_db, capture_rows, clone_session,
                                    coordinator, git, kilo_export, pipeline, profile,
                                    workspace, write_json)

PID = "fixture-game"
TID = "UI-03a"
FAST_LANE = '\n[fast_lane]\nenabled = true\ntasks = ["UI-D1", "UI-03a"]\n'
MEMORY_DDL = """CREATE TABLE IF NOT EXISTS task_memory (
  mem_id TEXT PRIMARY KEY, project_id TEXT, task_id TEXT, kind TEXT, text TEXT, refs TEXT,
  revision TEXT, actor TEXT, authority TEXT, status TEXT, superseded_by TEXT,
  created_ts REAL)"""


def _png(w=4, h=3) -> bytes:
    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d))
    raw = b"".join(b"\x00" + b"\x00\x00\x00" * w for _ in range(h))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def _verdict(ws, head, result="PASS", checks=None, captures=None, sprint=TID, finished=None,
             mode="sprint", legacy=False):
    v = {"schema": 1, "sprint": sprint, "head": head, "mode": mode, "run_id": f"run-{mode}",
         "started": time.time() - 60, "finished": finished or time.time(), "result": result,
         "checks": checks if checks is not None else [
             {"id": "host.clean_tree", "result": "PASS", "required": True, "detail": "clean",
              "artifacts": []}],
         "captures": captures or [], "regression": {"selection": "ui", "suites": {"S45": "PASS"},
                                                    "tally": "S45 PASS"},
         "summary_line": f"GATE {sprint} {result}", "error": None}
    d = Path(ws) / ".plan" / "tmp" / "gate" / sprint
    d.mkdir(parents=True, exist_ok=True)
    if legacy:                                                    # pre-mode runner: no mode key
        v.pop("mode"), v.pop("run_id")
        (d / f"{head[:12]}.json").write_text(json.dumps(v))
    else:
        (d / f"{head[:12]}.{mode}.json").write_text(json.dumps(v))
        (d / f"{head[:12]}.{mode}.raw.log").write_text("raw")
        (d / "latest").mkdir(exist_ok=True)
        (d / "latest" / f"{mode}.json").write_text(json.dumps(v))  # a copy: never ingested
    (d / "latest.json").write_text(json.dumps(v))                 # a copy: never ingested twice
    (d / "status.json").write_text(json.dumps({"sprint": sprint, "phase": "done"}))
    return v


def _capture(ws, story, size, sprint=TID) -> str:
    rel = f"docs/evidence/UI-v3/{sprint}/{story}@{size}.png"
    p = Path(ws) / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    w, h = (int(x) for x in size.split("x"))
    p.write_bytes(_png(w // 100, h // 100))
    return rel


def _episodes(p, kind=None):
    q = "SELECT * FROM episodes WHERE project_id=?" + (" AND kind=?" if kind else "")
    rows = p.store.cur.q(q, (PID, kind) if kind else (PID,))
    for r in rows:
        r["data"] = json.loads(r["data"])
        r["reasons"] = json.loads(r["reasons"] or "[]")
    return rows


def _lines(m, store):
    path = store.root / "datasets" / m["dataset_id"] / "data.jsonl"
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()], \
        str(path)


def _ui_session(exp=None, sid=SESSION):
    return clone_session(exp or kilo_export(), sid, task=TID)


def _ingest(p, st, exp, rows=None, ws_gate=True):
    tmp = Path(st.path).parent
    p.ingest_kilo(exports=[write_json(tmp / f"k-{exp['info']['id']}.json", exp)])
    if rows is not False:
        p.ingest_foundry(capture_db=capture_db(tmp, rows if rows is not None else
                                               capture_rows(exp, tid=TID),
                                               name=f"cap-{time.time_ns()}.sqlite"))
    p.ingest_coordinator(st.path)
    p.ingest_git(PID)
    if ws_gate:
        p.ingest_gate(PID)
    return p.curate(PID)


@pytest.fixture()
def world(tmp_path):
    ws, base, head = workspace(tmp_path)
    return tmp_path, ws, base, head


# 1 ingestion + labels + tags + multimodal ----------------------------------------------

def test_gate_and_vision_verdicts_are_labels_with_tags_and_linked_captures(world):
    tmp, ws, base, head = world
    prof = profile(tmp, PID, ws, extra=FAST_LANE)
    plan = Path(ws) / "docs" / "progress" / f"{TID}_PLAN.md"
    plan.parent.mkdir(parents=True)
    plan.write_text("# UI-03a (UI-v3)\nScreens use T2 CatalogGrid with a T8 ConfirmDialog; the "
                    "HUD keeps StatusPill and CurrencyChip.\n", encoding="utf-8")
    big, phone = _capture(ws, "Inventory.normal", "1920x1080"), \
        _capture(ws, "Inventory.normal", "390x844")
    _verdict(ws, head, "PASS", checks=[
        {"id": "host.clean_tree", "result": "PASS", "required": True, "detail": "", "artifacts": []},
        {"id": "story.Inventory.normal@390x844", "result": "PASS", "required": True,
         "detail": "0 defects", "artifacts": [phone]}],
        captures=[{"story": "Inventory.normal", "size": "1920x1080", "path": big,
                   "geometry_defects": 0, "layout_lines": []},
                  {"story": "Inventory.normal", "size": "390x844", "path": phone,
                   "geometry_defects": 0, "layout_lines": ["PASS: UI-LAYOUT ..."]}])
    st = coordinator(tmp, PID, TID, SESSION, base, head)
    import hashlib
    st.event(PID, TID, "model_visual_review", session_id="visual-approval-x", detail={
        "verdict": "approve", "persona": "Foundry-Vision", "served_model": "claude-vision",
        "revision": head, "findings": [],
        "artifacts": {big: hashlib.sha256((Path(ws) / big).read_bytes()).hexdigest()}})
    st.event(PID, TID, "playtest_critique", detail={"by": "victor",
                                                    "findings": ["tabs feel slow on pad"]})
    p = pipeline(tmp, [prof])
    _ingest(p, st, _ui_session())
    ep, = _episodes(p, "trajectory")
    d = ep["data"]
    assert ep["status"] == "selected", ep["reasons"]
    g, = d["labels"]["gate"]
    assert (g["result"], g["head"], g["summary_line"]) == ("PASS", head, f"GATE {TID} PASS")
    assert [c["id"] for c in g["checks"]] == ["host.clean_tree", "story.Inventory.normal@390x844"]
    assert g["failed_checks"] == []
    v, = d["labels"]["vision"]
    assert (v["verdict"], v["findings"], v["persona"]) == ("APPROVED", [], "Foundry-Vision")
    assert v["frames"][0].get("matches_recorded") is True, v["frames"]
    assert d["labels"]["critiques"][0]["findings"] == [{"n": 1, "text": "tabs feel slow on pad"}]
    assert {"fast_lane", "ui_v3", "template:T2", "template:T8", "widget:CatalogGrid",
            "widget:ConfirmDialog", "widget:StatusPill", "widget:CurrencyChip",
            "device:1920x1080", "device:390x844"} <= set(d["tags"])
    assert "template:T1" not in d["tags"] and "helper_steps" not in d["tags"]
    # captures are content-addressed artifacts linked to the episode
    arts = d["flags"]["label_artifacts"]
    assert {(a["path"], a["status"]) for a in arts} == {(big, "present"), (phone, "present")}
    for a in arts:
        assert p.store.artifact_path(a["sha256"]).read_bytes() == (Path(ws) / a["path"]).read_bytes()
    # latest.json / status.json are not ingested as separate verdicts
    gate_events = [e for e in p.store.events(PID) if e["source_adapter"] == "gate"]
    assert len(gate_events) == 1
    cfgs = {PID: p._cfg(PID)}
    mm = export(p.store, [PID], "multimodal", "canonical", cfgs)
    assert mm["examples"] == 1 and mm["validation"]["ok"], mm
    line, = _lines(mm, p.store)[0]
    assert line["labels"]["gate"][0]["result"] == "PASS" and "ui_v3" in line["tags"]
    # the Roblox-UI subset: a tag filter on export
    assert export(p.store, [PID], "sft", "chatml-tools", cfgs, tags=["ui_v3", "fast_lane"])[
        "examples"] == 1
    m = export(p.store, [PID], "sft", "chatml-tools", cfgs, tags=["template:T7"])
    assert m["examples"] == 0 and m["skipped"] == {"tag_filter": 1}
    # re-ingesting the same verdict is idempotent
    assert p.ingest_gate(PID)["new_events"] == 0


def test_failed_gate_and_vision_changes_carry_details_and_numbered_findings(world):
    tmp, ws, base, head = world
    prof = profile(tmp, PID, ws, extra=FAST_LANE)
    _verdict(ws, head, "FAIL", checks=[
        {"id": "host.clean_tree", "result": "PASS", "required": True, "detail": ""},
        {"id": "story.Hud.normal@390x844", "result": "FAIL", "required": True,
         "detail": "close button overlaps tab row by 12px", "artifacts": []},
        {"id": "focus.walk", "result": "UNKNOWN", "required": True, "detail": "no studio"},
        {"id": "leak.connections", "result": "FAIL", "required": False, "detail": "advisory"}],
        captures=[{"story": "Hud.normal", "size": "390x844",
                   "path": f"docs/evidence/UI-v3/{TID}/Hud.normal@390x844.png"}])   # file gone
    bad = Path(ws) / ".plan" / "tmp" / "gate" / TID / "deadbeefdead.json"
    bad.write_text("{not json")
    st = coordinator(tmp, PID, TID, SESSION, base, head, checks=(("au.gate", "FAIL"),),
                     verdict=None)
    st.event(PID, TID, "model_visual_review", detail={
        "verdict": "changes_required", "persona": "Foundry-Vision", "served_model": "v",
        "revision": head, "artifacts": {},
        "findings": ["the tab row overlaps the close button at 390x844", "contrast too low"]})
    p = pipeline(tmp, [prof])
    _ingest(p, st, _ui_session())
    ep, = _episodes(p, "trajectory")
    g, = ep["data"]["labels"]["gate"]
    assert g["result"] == "FAIL"
    assert g["failed_checks"] == [
        {"id": "story.Hud.normal@390x844", "result": "FAIL",
         "detail": "close button overlaps tab row by 12px", "artifacts": []},
        {"id": "focus.walk", "result": "UNKNOWN", "detail": "no studio", "artifacts": []}]
    assert g["captures"][0]["status"] == "missing"            # never assumed present
    v, = ep["data"]["labels"]["vision"]
    assert v["verdict"] == "CHANGES"
    assert v["findings"] == [{"n": 1, "text": "the tab row overlaps the close button at 390x844"},
                             {"n": 2, "text": "contrast too low"}]
    gaps = [e for e in p.store.events(PID) if e["event_type"] == "capture_gap"
            and e["source_adapter"] == "gate"]
    assert gaps and "deadbeefdead.json" in gaps[0]["facts"]["path"]
    # a red episode is never positive data; the missing capture blocks multimodal
    assert ep["status"] == "excluded" and "verification_failed" in ep["reasons"]


def test_gate_ingests_only_sprint_mode_verdict_files(world):
    from foundry_training.adapters import gate
    tmp, ws, base, head = world
    _verdict(ws, head, "PASS")                                    # <head12>.sprint.json
    _verdict(ws, head, "FAIL", mode="stories")                    # never a gate label
    _verdict(ws, head, "FAIL", mode="regression")
    other = "ab" * 20
    _verdict(ws, other, "PASS", legacy=True)                      # legacy <head12>.json = sprint
    d = Path(ws) / ".plan" / "tmp" / "gate" / TID
    noise = {"gate_expect.json": {"x": 1}, "gate_expect_full.json": {"x": 1},
             f"{head[:12]}.vision.json": {"verdict": "approve"}}
    for name, body in noise.items():
        (d / name).write_text(json.dumps(body))
    names = sorted(os.path.relpath(p, d).replace("\\", "/") for p in gate.verdict_files(ws))
    assert names == sorted([f"{head[:12]}.sprint.json", f"{other[:12]}.json"])
    recs = list(gate.records(ws, PID))
    assert not any(r[2].get("unreadable") for r in recs)
    modes = {r[2]["verdict"]["head"]: r[2]["verdict"].get("mode") for r in recs}
    assert modes == {head: "sprint", other: None}
    # a sprint-named file whose content says another mode is skipped too
    (d / f"{head[:12]}.sprint.json").write_text(json.dumps(
        dict(json.loads((d / "latest" / "stories.json").read_text()))))
    assert [r[2]["verdict"]["head"] for r in gate.records(ws, PID)] == [other]
    legacy, = [r[2] for r in recs if r[2]["verdict"]["head"] == other]
    ev, = gate.normalize(legacy, PID, "ref", {})
    assert ev.facts["mode"] == "sprint" and ev.facts["run_id"] is None


# 3 per-step authorship -----------------------------------------------------------------

def _apply_patch_session(patch_rel):
    exp = kilo_export()
    for m in exp["messages"]:
        for part in m["parts"]:
            if part.get("tool") == "bash":
                part["state"]["input"]["command"] = (f"git apply {patch_rel} && git add "
                                                     "src/greeting.txt && git commit -q -m x")
    return _ui_session(exp)


def test_helper_prework_patch_step_is_context_and_local_steps_stay_targets(world):
    tmp, ws, base, head = world
    prof = profile(tmp, PID, ws, extra=FAST_LANE)
    rel = f".plan/tmp/prework/{TID}/hu-7.patch"
    (Path(ws) / rel).parent.mkdir(parents=True)
    # one small line, not the one the local write wrote (that would be a match)
    (Path(ws) / rel).write_text("--- a/src/greeting.txt\n+++ b/src/greeting.txt\n"
                                "+OK helper greeting\n")
    st = coordinator(tmp, PID, TID, SESSION, base, head)
    st.register_project(PID, PID, "p.toml", "h", "1", ws)        # adapter reads the patch text
    st.conn.execute(MEMORY_DDL)
    st.x("INSERT INTO task_memory VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
         ("mem-1", PID, TID, "note", f"PREWORK for step \"greeting\" (helper hu-7): ...",
          json.dumps(["hu-7", rel]), head, "helper:hu-7", "coordinator", "active", None, 1.0))
    st.x("INSERT INTO task_memory VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
         ("mem-2", PID, TID, "note", "triage: the tab row is off by 12px (guidance only)",
          json.dumps(["hu-8"]), head, "helper:hu-8", "coordinator", "active", None, 2.0))
    exp = _apply_patch_session(rel)
    p = pipeline(tmp, [prof])
    _ingest(p, st, exp)
    auth = [e for e in p.store.events(PID) if e["event_type"] == "authorship"]
    assert [(e["facts"]["author"], e["facts"]["basis"]) for e in auth] == \
        [("helper:hu-7", "prework-patch")]                       # guidance is not authorship
    assert "+OK helper greeting" in auth[0]["facts"]["patch_text"]
    ep, = _episodes(p, "trajectory")
    d = ep["data"]
    assert ep["status"] == "selected", ep["reasons"]
    steps = d["flags"]["helper_steps"]
    assert [(s["author"], s["tool"]) for s in steps] == [("helper:hu-7", "bash")]
    idx = steps[0]["index"]
    assert d["messages"][idx]["author"]["kind"] == "helper"
    assert d["messages"][idx]["author"]["written_by"] == "qwen3.8-flash-next-iq3_s"
    assert sorted(a[0] for a in d["flags"]["authors"]) == ["helper", "local"]
    assert "helper_steps" in d["tags"]
    cfgs = {PID: p._cfg(PID)}
    m = export(p.store, [PID], "sft", "chatml-tools", cfgs)
    assert m["validation"]["ok"], m["validation"]
    line, = _lines(m, p.store)[0]
    msgs = line["messages"]
    assert msgs[idx]["weight"] == 0 and line["meta"]["context_only"] == [idx]
    others = [i for i, x in enumerate(msgs) if x["role"] == "assistant" and i != idx]
    assert others and all(msgs[i]["weight"] == 1 for i in others)   # local steps stay targets
    assert line["meta"]["frontier_targets_allowed"] is False
    canon = export(p.store, [PID], "sft", "canonical", cfgs)
    cl, = _lines(canon, p.store)[0]
    assert idx not in cl["targets"] and set(others) <= set(cl["targets"])


def test_patch_content_and_precode_harvest_are_matched_without_paths(world):
    tmp, ws, base, head = world
    prof = profile(tmp, PID, ws, extra=FAST_LANE)
    body = "local M = {}\nfunction M.layout(size)\n  return size.X > 800\nend\nreturn M\n"
    rel = f".plan/tmp/prework/{TID}/hu-9.patch"
    (Path(ws) / rel).parent.mkdir(parents=True)
    (Path(ws) / rel).write_text("+++ b/src/greeting.txt\n" +
                                "".join("+" + l + "\n" for l in body.splitlines()))
    st = coordinator(tmp, PID, TID, SESSION, base, head)        # no project row: read via cfg
    st.conn.execute(MEMORY_DDL)
    st.x("INSERT INTO task_memory VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
         ("mem-1", PID, TID, "note", "PREWORK ...", json.dumps(["hu-9", rel]), head,
          "helper:hu-9", "coordinator", "active", None, 1.0))
    exp = _ui_session(clone_session(kilo_export(), SESSION, write_content=body))
    p = pipeline(tmp, [prof])
    _ingest(p, st, exp)
    ep, = _episodes(p, "trajectory")
    assert [(s["author"], s["tool"]) for s in ep["data"]["flags"]["helper_steps"]] == \
        [("helper:hu-9", "write")]
    # harvested pre-code model: writing the harvested file is frontier-authored
    tmp2 = tmp / "two"
    ws2, b2, h2 = workspace(tmp2)
    prof2 = profile(tmp2, PID, ws2, extra=FAST_LANE)
    st2 = coordinator(tmp2, PID, TID, SESSION, b2, h2)
    st2.conn.execute(MEMORY_DDL)
    st2.x("INSERT INTO task_memory VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
          ("mem-h", PID, TID, "note", "harvested pre-code models", json.dumps(
              ["src/greeting.txt"]), "", "helper:precode-harvest", "coordinator", "active",
           None, 1.0))
    p2 = pipeline(tmp2, [prof2])
    _ingest(p2, st2, _ui_session())
    ep2, = _episodes(p2, "trajectory")
    steps = ep2["data"]["flags"]["helper_steps"]
    assert [(s["author"], s["basis"]) for s in steps] == \
        [("helper:precode-harvest", "wrote a harvested model")]
    assert ep2["status"] == "selected", ep2["reasons"]          # the commit + report stay local


def test_frontier_text_is_never_a_target_whatever_the_policy(world):
    tmp, ws, base, head = world
    # this policy does not list local_author: the export rule still applies
    prof = profile(tmp, PID, ws, extra=FAST_LANE +
                   '[training.policy.sft]\nrequire = ["verification_pass"]\n')
    st = coordinator(tmp, PID, TID, SESSION, base, head)
    exp = _ui_session()
    rows = capture_rows(exp, tid=TID, persona="Foundry-Kilo-Helper-Claude",
                        model="claude-sonnet-x")
    p = pipeline(tmp, [prof])
    _ingest(p, st, exp, rows=rows)
    ep, = _episodes(p, "trajectory")
    assert ep["status"] == "excluded" and "non_local_author" in ep["reasons"]
    assert export(p.store, [PID], "sft", "chatml-tools", {PID: p._cfg(PID)})["examples"] == 0
    # only an explicit project decision makes frontier turns targets
    prof_allow = profile(tmp / "allow", PID, ws, extra=FAST_LANE +
                         '[training.policy.sft]\nrequire = ["verification_pass"]\n'
                         '[training.export]\nallow_frontier_targets = true\n')
    p2 = pipeline(tmp / "allow", [prof_allow])
    _ingest(p2, st, exp, rows=rows)
    ep2, = _episodes(p2, "trajectory")
    assert ep2["status"] == "selected", ep2["reasons"]


def test_validator_rejects_frontier_targets_and_unweighted_helper_steps(tmp_path):
    base = {"messages": [{"role": "user", "content": "u"},
                         {"role": "assistant", "content": "a", "weight": 1}],
            "meta": {"episode_id": "e", "frontier_targets_allowed": False,
                     "authors": [["non-local", "claude"]], "context_only": []}}
    path = tmp_path / "d.jsonl"
    path.write_text(json.dumps(base) + "\n")
    v = validate_dataset(str(path), "chatml-tools")
    assert not v["ok"] and any("frontier-authored" in e for e in v["errors"])
    helper = copy.deepcopy(base)
    helper["meta"]["authors"] = [["helper", "helper:hu-1"], ["local", "q"]]
    path.write_text(json.dumps(helper) + "\n")
    v = validate_dataset(str(path), "chatml-tools")
    assert not v["ok"] and any("not weighted out" in e for e in v["errors"])
    helper["meta"]["context_only"] = [1]
    path.write_text(json.dumps(helper) + "\n")
    assert any("context-only" in e for e in validate_dataset(str(path), "chatml-tools")["errors"])
    helper["messages"][1]["weight"] = 0
    path.write_text(json.dumps(helper) + "\n")
    assert validate_dataset(str(path), "chatml-tools")["ok"]


# 5 correction triples ------------------------------------------------------------------

def _session_with_feedback(feedback: str, fix_content: str, sid=SESSION) -> dict:
    """One fast-lane session: attempt (write + commit), the findings sent into
    the SAME session, the local fix (write + commit) and the final report."""
    s = _ui_session(sid=sid)
    user, write, bash, final = s["messages"]
    fb = copy.deepcopy(user)
    fb["info"]["id"] += "fb"
    fb["info"].pop("summary", None)
    fb["parts"] = [{"type": "text", "text": feedback, "id": "prt_feedback0001"}]
    fix_w, fix_b = copy.deepcopy(write), copy.deepcopy(bash)
    for m, tag in ((fix_w, "fw"), (fix_b, "fb")):
        m["info"]["id"] += tag
        for part in m["parts"]:
            part["id"] += tag
            if part["type"] == "tool":
                part["callID"] += tag
                if part["tool"] == "write":
                    part["state"]["input"]["content"] = fix_content
    msgs = [user, write, bash, fb, fix_w, fix_b, final]
    t0 = user["info"]["time"]["created"]
    for i, m in enumerate(msgs):
        m["info"]["time"]["created"] = t0 + i * 10_000
        for part in m["parts"]:
            if part["type"] == "tool":
                part["state"]["time"] = {"start": t0 + i * 10_000 + 100,
                                         "end": t0 + i * 10_000 + 500}
    s["messages"] = msgs
    return s


def _inline_world(tmp, ws, base, head1, label_kind):
    (Path(ws) / "src" / "greeting.txt").write_text("OK hello friend\n")
    git(ws, "commit", "-qam", "UI-03a: fix after the gate")
    head2 = git(ws, "rev-parse", "HEAD")
    from foundry_coordinator.adapters.base import VerificationResult
    from foundry_coordinator.store import Store
    st = Store(str(tmp / "coordinator.sqlite"))
    st.approve(PID, TID, [], "tester")
    st.add_session(SESSION, PID, TID, f"{TID}-a1", "implementer", "implement", 1, "kilo",
                   "Foundry-Kilo-Local")
    st._record(PID, TID, "READY", "IMPLEMENTING", {"session_id": SESSION, "base_sha": base},
               "engine")
    st._record(PID, TID, "IMPLEMENTING", "VERIFYING", {"session_id": SESSION, "head_sha": head1},
               "engine")
    st._record(PID, TID, "VERIFYING", "IMPLEMENTING", {"session_id": SESSION,
                                                       "reason": "red gate"}, "engine")
    st._record(PID, TID, "IMPLEMENTING", "VERIFYING", {"session_id": SESSION, "head_sha": head2},
               "engine")
    st.save_verification(PID, TID, head2, "verify", [VerificationResult("au.gate", "PASS")])
    st.save_review(PID, TID, head2, "reviewer", "Foundry-Kilo-Sonnet", "APPROVED", True, "",
                   {"verdict": "APPROVED", "findings": []}, "{}")
    t = time.time()
    if label_kind == "gate":
        _verdict(ws, head1, "FAIL", finished=t - 100, checks=[
            {"id": "story.Hud.normal@390x844", "result": "FAIL", "required": True,
             "detail": "close button overlaps tab row"}])
        feedback = ("The studio gate is red. Failed checks:\n- story.Hud.normal@390x844: FAIL "
                    "close button overlaps tab row\nFix them in this session.")
    else:
        _verdict(ws, head1, "PASS", finished=t - 100)
        st.event(PID, TID, "model_visual_review", detail={
            "verdict": "changes_required", "persona": "Foundry-Vision", "served_model": "v",
            "revision": head1, "artifacts": {},
            "findings": ["the tab row overlaps the close button at 390x844"]})
        feedback = ("Vision review: CHANGES.\n1. the tab row overlaps the close button at "
                    "390x844\nFix it in this session.")
    _verdict(ws, head2, "PASS", finished=t)
    return st, head2, _session_with_feedback(feedback, "OK hello friend\n")


@pytest.mark.parametrize("label_kind", ["gate", "vision"])
def test_inline_correction_triple_from_one_fast_lane_session(world, label_kind):
    tmp, ws, base, head1 = world
    prof = profile(tmp, PID, ws, extra=FAST_LANE)
    st, head2, exp = _inline_world(tmp, ws, base, head1, label_kind)
    p = pipeline(tmp, [prof])
    _ingest(p, st, exp, rows=False)                  # Kilo records only: correction policy ok
    corr, = _episodes(p, "correction")
    d = corr["data"]
    assert corr["status"] == "selected", corr["reasons"]
    assert d["inline"] is True and d["session_id"] == SESSION
    assert d["original"]["revision"] == {"base": base, "head": head1}
    assert d["correction"]["revision"] == {"base": head1, "head": head2}
    assert "friend" in d["correction"]["diff"]["patch"]
    idx = d["context_until"]
    assert d["messages"][idx]["role"] == "user"
    if label_kind == "gate":
        assert d["feedback"]["source"] == "gate"
        assert d["feedback"]["items"] == ["story.Hud.normal@390x844: FAIL close button overlaps "
                                          "tab row"]
        assert d["feedback"]["author"] == {"kind": "coordinator", "model": "studio_gate"}
        assert d["original"]["outcome"]["label"]["result"] == "FAIL"
    else:
        assert d["feedback"]["source"] == "vision"
        assert d["feedback"]["items"] == ["1. the tab row overlaps the close button at 390x844"]
        assert d["feedback"]["author"] == {"kind": "frontier", "model": "Foundry-Vision"}
    assert d["outcome"]["verification"]["status"] == "PASS"
    cfgs = {PID: p._cfg(PID)}
    m = export(p.store, [PID], "correction", "chatml-tools", cfgs)
    assert m["validation"]["ok"] and m["examples"] == 1, m
    line, = _lines(m, p.store)[0]
    w = [(i, x.get("weight")) for i, x in enumerate(line["messages"]) if x["role"] == "assistant"]
    # the attempt is context, the corrected local code is the target
    assert all(wt == 0 for i, wt in w if i < idx) and all(wt == 1 for i, wt in w if i > idx)
    assert any(wt == 1 for _, wt in w)
    canon = export(p.store, [PID], "correction", "canonical", cfgs)
    cl, = _lines(canon, p.store)[0]
    assert cl["context_until"] == idx and cl["feedback"]["items"] == d["feedback"]["items"]
    assert all(i > idx for i in cl["targets"])


def test_cross_session_correction_takes_red_gate_findings(world):
    tmp, ws, base, head1 = world
    prof = profile(tmp, PID, ws, extra=FAST_LANE)
    st = coordinator(tmp, PID, TID, SESSION, base, head1, verdict=None,
                     checks=(("au.gate", "FAIL"),))
    _verdict(ws, head1, "FAIL", checks=[{"id": "wiring.inventory_open", "result": "FAIL",
                                         "required": True, "detail": "remote never fired"}])
    (Path(ws) / "src" / "greeting.txt").write_text("OK hello friend\n")
    git(ws, "commit", "-qam", "UI-03a: wire it")
    head2 = git(ws, "rev-parse", "HEAD")
    s2 = "ses_repair00000000000000000002"
    repair = clone_session(kilo_export(), s2, task=TID, attempt=2,
                           start_ms=int(time.time() * 1000) + 5000,
                           write_content="OK hello friend\n", final_text="Repaired.")
    coordinator(tmp, PID, TID, s2, head1, head2, attempt=2, store=st)
    _verdict(ws, head2, "PASS")
    p = pipeline(tmp, [prof])
    p.ingest_kilo(exports=[write_json(tmp / "a1.json", _ui_session()),
                           write_json(tmp / "a2.json", repair)])
    p.ingest_coordinator(st.path)
    p.ingest_git(PID)
    p.ingest_gate(PID)
    p.curate(PID)
    corr, = _episodes(p, "correction")
    d = corr["data"]
    assert corr["status"] == "selected", corr["reasons"]
    assert d["feedback"]["source"] == "gate"
    assert d["feedback"]["items"] == ["wiring.inventory_open: FAIL remote never fired"]
    assert d["original"]["revision"]["head"] == head1 and d["revision"]["head"] == head2
    assert d["labels"]["gate"][-1]["result"] == "PASS" and "fast_lane" in d["tags"]


def test_capture_file_names_are_not_redacted_as_emails_but_addresses_are():
    from foundry_training.redact import Redactor
    r = Redactor()
    s = "see docs/evidence/UI-v3/UI-02a/Hud.normal@1920x1080.png and mail bob@example.org"
    out = r.text(s)
    assert "Hud.normal@1920x1080.png" in out and "bob@example.org" not in out
