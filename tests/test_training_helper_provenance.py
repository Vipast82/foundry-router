"""UI-T0 review fixes for foundry_training (H7, M12, redaction low):
frontier / helper code never becomes a training target, gate labels are
recomputed from their checks, and capture file names are the only
address-like tokens left unredacted. Every rule fails closed."""

from __future__ import annotations

import json
from pathlib import Path

from foundry_training import labels as L
from foundry_training.export import export
from tests.test_training_ui_labels import (FAST_LANE, MEMORY_DDL, PID, TID, _episodes, _ingest,
                                           _lines, _ui_session, _verdict, world)  # noqa: F401
from tests.training.helpers import (SESSION, clone_session, coordinator, git, kilo_export,
                                    pipeline, profile, workspace)

HELPER_BODY = [f"local function helperStep{i}(frame, size) return frame.Size.X.Offset * {i} end"
               for i in range(30)]
LOCAL_BODY = [f"local localThing{i} = computeLocal({i}, \"row {i}\")" for i in range(170)]


def _patch(path: str, lines: list[str]) -> str:
    return (f"--- a/{path}\n+++ b/{path}\n@@ -0,0 +1,{len(lines)} @@\n" +
            "".join("+" + l + "\n" for l in lines))


def _attrs(text: str, author="helper:hu-1", basis="prework-patch", patch="p.patch", paths=()):
    blocks, small = L._hunk_index(text)
    return [{"author": author, "basis": basis, "patch": patch, "paths": list(paths),
             "blocks": blocks, "small": small, "verifiable": True, "event_id": "e"}]


def _mem(st, rel, unit="hu-1", mem="mem-1"):
    st.conn.execute(MEMORY_DDL)
    st.x("INSERT INTO task_memory VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
         (mem, PID, TID, "note", "PREWORK ...", json.dumps([unit, rel]), "",
          f"helper:{unit}", "coordinator", "active", None, 1.0))


# ---------------------------------------------------------------- unit: matching rule

def test_whole_file_write_embedding_a_helper_hunk_is_helper_authored():
    attrs = _attrs(_patch("src/ui/Inv.lua", HELPER_BODY))
    whole = "\n".join(LOCAL_BODY[:90] + ["    " + l for l in HELPER_BODY] + LOCAL_BODY[90:])
    # 30 helper lines in a 200-line file: 15%, the old ratio rule missed it
    hit = L.step_author([{"name": "write", "arguments": {"filePath": "src/ui/Inv.lua",
                                                          "content": whole}}], attrs)
    assert hit and hit["author"] == "helper:hu-1"
    # three contiguous helper lines are enough; two are not
    three = "\n".join(LOCAL_BODY[:5] + HELPER_BODY[10:13] + LOCAL_BODY[5:9])
    two = "\n".join(LOCAL_BODY[:5] + HELPER_BODY[10:12] + LOCAL_BODY[5:9])
    assert L.step_author([{"name": "edit", "arguments": {"newString": three}}], attrs)
    assert L.step_author([{"name": "edit", "arguments": {"newString": two}}], attrs) is None
    # one long line (>= 200 chars) is a block on its own
    long = "local CONFIG = { " + ", ".join(f"key{i} = {i}" for i in range(30)) + " }"
    assert len(long) >= 200
    a2 = _attrs(_patch("src/c.lua", [long]))
    assert L.step_author([{"name": "write", "arguments": {"content": "x = 1\n" + long}}], a2)


def test_cline_tools_heredocs_and_pasted_text_are_matched():
    attrs = _attrs(_patch("src/ui/Inv.lua", HELPER_BODY))
    block = "\n".join(HELPER_BODY[3:8])
    assert L.step_author([{"name": "write_to_file", "arguments": {
        "path": "src/x.lua", "content": block}}], attrs)
    sr = (f"<<<<<<< SEARCH\nold line one here\n=======\n{block}\n>>>>>>> REPLACE\n")
    assert L.step_author([{"name": "replace_in_file", "arguments": {
        "path": "src/x.lua", "diff": sr}}], attrs)
    # the SEARCH side is the old code, never a match on its own
    sr_old = (f"<<<<<<< SEARCH\n{block}\n=======\nlocal fresh = 1\n>>>>>>> REPLACE\n")
    assert L.step_author([{"name": "replace_in_file", "arguments": {
        "path": "src/x.lua", "diff": sr_old}}], attrs) is None
    heredoc = f"cat > src/x.lua <<'EOF'\n{block}\nEOF"
    assert L.step_author([{"name": "bash", "arguments": {"command": heredoc}}], attrs)
    assert L.step_author([], attrs, content=f"Here is the code:\n{block}\n")


def test_helper_tag_marks_the_step_and_small_hunks_need_the_same_path():
    assert L.step_author([{"name": "bash", "arguments": {
        "command": "git commit -m 'UI-03a: tab row (helper:hu-4)'"}}], [])["author"] == \
        "helper:hu-4"
    small = _attrs(_patch("src/ui/Tab.lua", ["local OFFSET = 12"]))
    assert small[0]["small"] and not small[0]["blocks"]
    on_path = [{"name": "edit", "arguments": {"filePath": "/w/src/ui/Tab.lua",
                                               "newString": "local OFFSET = 12"}}]
    elsewhere = [{"name": "edit", "arguments": {"filePath": "/w/src/ui/Other.lua",
                                                 "newString": "local OFFSET = 12"}}]
    assert L.step_author(on_path, small) and L.step_author(elsewhere, small) is None


def test_large_patch_is_hashed_whole_never_truncated():
    filler = [f"local pad{i} = '{'x' * 80}'" for i in range(1200)]          # > 60k chars
    text = _patch("src/big.lua", filler + HELPER_BODY)
    assert len(text) > 100_000
    facts = L.index_facts(text)
    attrs = [{"author": "helper:hu-2", "basis": "prework-patch", "patch": "big.patch",
              "paths": [], "blocks": set(facts["blocks"]),
              "small": [], "verifiable": True, "event_id": "e"}]
    tail = "\n".join(HELPER_BODY[-4:])                     # only past the old 60k cut
    assert L.step_author([{"name": "write", "arguments": {"content": tail}}], attrs)


# ---------------------------------------------------------------- integration

def test_patch_captured_at_closeout_survives_tmp_cleanup_and_diffs_are_withheld(world):
    tmp, ws, base, head = world
    prof = profile(tmp, PID, ws, extra=FAST_LANE)
    filler = [f"local pad{i} = '{'y' * 80}'" for i in range(900)]
    rel = f".plan/tmp/prework/{TID}/hu-1.patch"
    (Path(ws) / rel).parent.mkdir(parents=True)
    (Path(ws) / rel).write_text(_patch("src/greeting.txt", filler + HELPER_BODY))
    st = coordinator(tmp, PID, TID, SESSION, base, head)
    st.register_project(PID, PID, "p.toml", "h", "1", ws)
    _mem(st, rel)
    whole = "\n".join(LOCAL_BODY[:50] + HELPER_BODY + LOCAL_BODY[50:])
    exp = _ui_session(clone_session(kilo_export(), SESSION, write_content=whole))
    p = pipeline(tmp, [prof])
    _ingest(p, st, exp)
    auth, = [e for e in p.store.events(PID) if e["event_type"] == "authorship"]
    assert auth["facts"]["blocks"] and auth["facts"]["patch_text"] == ""   # > 60k: hashes only
    assert auth["facts"]["patch_text_omitted"] is True
    (Path(ws) / rel).unlink()                                  # tmp cleaned after closeout
    p.ingest_coordinator(st.path)
    p.curate(PID)
    ep, = _episodes(p, "trajectory")
    steps = ep["data"]["flags"]["helper_steps"]
    assert [(s["author"], s["tool"]) for s in steps] == [("helper:hu-1", "write")]
    assert ep["status"] == "selected", ep["reasons"]
    cfgs = {PID: p._cfg(PID)}
    canon = export(p.store, [PID], "sft", "canonical", cfgs)
    assert canon["validation"]["ok"], canon["validation"]
    cl, = _lines(canon, p.store)[0]
    assert cl["diff"]["patch"] == "" and cl["diff"]["helper_authored"] is True
    assert cl["diff"]["target"] is False and steps[0]["index"] not in cl["targets"]


def test_patch_gone_before_capture_excludes_the_episode(world):
    tmp, ws, base, head = world
    prof = profile(tmp, PID, ws, extra=FAST_LANE)
    st = coordinator(tmp, PID, TID, SESSION, base, head)
    st.register_project(PID, PID, "p.toml", "h", "1", ws)
    _mem(st, f".plan/tmp/prework/{TID}/hu-1.patch")            # never existed on disk
    p = pipeline(tmp, [prof])
    _ingest(p, st, _ui_session())
    auth, = [e for e in p.store.events(PID) if e["event_type"] == "authorship"]
    assert auth["facts"]["content"] == "missing"
    ep, = _episodes(p, "trajectory")
    assert ep["status"] == "excluded" and "helper_patch_unverifiable" in ep["reasons"]


def test_helper_tagged_commit_marks_the_step_that_wrote_its_code(tmp_path):
    ws, base, _ = workspace(tmp_path)
    ui = Path(ws) / "src" / "ui"
    ui.mkdir(parents=True)
    (ui / "Tab.lua").write_text("\n".join(HELPER_BODY) + "\n")
    git(ws, "add", ".")
    git(ws, "commit", "-q", "-m", "UI-03a: tab row step 3", "-m", "drafted (helper:hu-4)")
    (Path(ws) / "src" / "local.txt").write_text("local work\n")
    git(ws, "add", ".")
    git(ws, "commit", "-q", "-m", "UI-03a: local work")
    head = git(ws, "rev-parse", "HEAD")
    prof = profile(tmp_path, PID, ws, extra=FAST_LANE)
    st = coordinator(tmp_path, PID, TID, SESSION, base, head)
    body = "\n".join(LOCAL_BODY[:20] + HELPER_BODY[5:12])
    p = pipeline(tmp_path, [prof])
    _ingest(p, st, _ui_session(clone_session(kilo_export(), SESSION, write_content=body)))
    auth = [e for e in p.store.events(PID) if e["event_type"] == "authorship"]
    assert [(e["facts"]["author"], e["facts"]["basis"]) for e in auth] == \
        [("helper:hu-4", "helper-commit")]                    # found in base..head, body tag
    ep, = _episodes(p, "trajectory")
    assert [s["author"] for s in ep["data"]["flags"]["helper_steps"]] == ["helper:hu-4"]


def test_precode_files_copied_verbatim_are_helper_authored(world):
    tmp, ws, base, head = world
    d = Path(ws) / "docs" / "progress" / "UI-03a_PRECODE"
    d.mkdir(parents=True)
    (d / "InventoryModel.lua").write_text("\n".join(HELPER_BODY) + "\n")
    prof = profile(tmp, PID, ws, extra=FAST_LANE)
    st = coordinator(tmp, PID, TID, SESSION, base, head)
    body = "\n".join(LOCAL_BODY[:10] + HELPER_BODY[:6] + LOCAL_BODY[10:20])
    p = pipeline(tmp, [prof])
    _ingest(p, st, _ui_session(clone_session(kilo_export(), SESSION, write_content=body)))
    ep, = _episodes(p, "trajectory")
    steps = ep["data"]["flags"]["helper_steps"]
    assert [(s["author"], s["tool"]) for s in steps] == [("helper:precode-harvest", "write")]
    # a shell copy of the pre-code file is helper-authored too
    attrs = L.attributions(p.store.events(PID), TID)
    rel = "docs/progress/UI-03a_PRECODE/InventoryModel.lua"
    assert L.step_author([{"name": "bash", "arguments": {
        "command": f"cp {rel} src/ui/InventoryModel.lua"}}], attrs)["basis"] == \
        "copied a harvested model"


def test_gate_label_result_is_recomputed_and_a_forged_pass_excludes(world):
    tmp, ws, base, head = world
    prof = profile(tmp, PID, ws, extra=FAST_LANE)
    st = coordinator(tmp, PID, TID, SESSION, base, head)
    _verdict(ws, head, "PASS", checks=[
        {"id": "host.clean_tree", "result": "PASS", "required": True, "detail": ""},
        {"id": "wiring.inventory_open", "result": "FAIL", "required": True,
         "detail": "remote never fired"}])
    p = pipeline(tmp, [prof])
    _ingest(p, st, _ui_session())
    lab, = [e for e in p.store.events(PID) if e["event_type"] == "label"]
    assert lab["facts"]["inconsistent"] is True and lab["facts"]["computed_result"] == "FAIL"
    assert lab["verification_status"] == "UNKNOWN" and lab["facts"]["result"] == "UNKNOWN"
    ep, = _episodes(p, "trajectory")
    g, = ep["data"]["labels"]["gate"]
    assert g["inconsistent"] and g["result"] == "UNKNOWN"
    assert ep["status"] == "excluded" and "gate_label_inconsistent" in ep["reasons"]


def test_gate_label_without_required_checks_is_not_a_pass():
    from foundry_training.adapters.gate import overall
    assert overall([]) == "FAIL"
    assert overall([{"id": "a", "result": "PASS", "required": False}]) == "FAIL"
    assert overall([{"id": "a", "result": "SKIP", "required": True}]) == "FAIL"
    assert overall([{"id": "a", "result": "PASS", "required": True}], error="boom") == "ERROR"
    old = {"event_id": "e", "source_revision": "h" * 40, "timestamp": 1.0,
           "facts": {"label": "gate_verdict", "result": "PASS",       # stored before the fix
                     "checks": [{"id": "a", "result": "FAIL", "required": True}]}}
    s = L.summarize(old)
    assert s["inconsistent"] and s["result"] == "UNKNOWN"


def test_capture_name_exemption_is_anchored_to_image_file_names():
    from foundry_training.redact import Redactor
    r = Redactor()
    for keep in ("Hud.normal@1920x1080.png", "Inventory.open@390x844.jpg"):
        assert keep in r.text(f"see docs/evidence/{keep} now")
    for addr in ("x@1920x1080.example.com", "someone@1920x1080.png.evil.com",
                 "ops@390x844.co"):
        assert addr not in r.text(f"mail {addr} today"), addr
