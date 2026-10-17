"""Training-data pipeline: the acceptance items, end to end through the real
modules (adapters -> store -> correlation -> episodes -> curation -> export
-> validation). See tests/training/helpers.py for what is real and what is
built."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import struct
import zlib
from pathlib import Path

import pytest

from foundry_training import config as TC
from foundry_training.adapters import foundry as FA
from foundry_training.export import export
from foundry_training.validate import validate_dataset
from tests.training.helpers import (KILO_EXPORT, KILO_SANITIZED, LOCAL, SESSION, capture_db,
                                    capture_rows, clone_session, coordinator, file_state, git,
                                    kilo_db, kilo_export, pipeline, profile, workspace,
                                    write_json)

PID = "fixture-game"


@pytest.fixture()
def world(tmp_path):
    ws, base, head = workspace(tmp_path)
    prof = profile(tmp_path, PID, ws)
    return tmp_path, ws, base, head, prof


def _episodes(p, pid=PID, kind=None):
    q = "SELECT * FROM episodes WHERE project_id=?" + (" AND kind=?" if kind else "")
    rows = p.store.cur.q(q, (pid, kind) if kind else (pid,))
    for r in rows:
        r["data"] = json.loads(r["data"])
        r["reasons"] = json.loads(r["reasons"] or "[]")
    return rows


def _lines(manifest, store):
    sub = "eval" if manifest["purpose"] == "eval" else "datasets"
    path = store.root / sub / manifest["dataset_id"] / "data.jsonl"
    return [json.loads(l) for l in path.read_text().splitlines() if l.strip()], str(path)


def _full(world, rows=None):
    tmp, ws, base, head, prof = world
    st = coordinator(tmp, PID, "FX-1", SESSION, base, head)
    p = pipeline(tmp, [prof])
    p.ingest_kilo(exports=[str(KILO_EXPORT)])
    p.ingest_foundry(capture_db=capture_db(tmp, rows if rows is not None
                                           else capture_rows(kilo_export())))
    p.ingest_coordinator(st.path)
    p.ingest_git(PID)
    return p, p.curate(PID)


# 1 ------------------------------------------------------------------------------------

def test_real_kilo_export_imported_without_touching_the_source(world, tmp_path):
    src = tmp_path / "kilo-export.json"
    shutil.copy(KILO_EXPORT, src)
    before = file_state(str(src))
    p = pipeline(tmp_path, [world[4]])
    res = p.ingest_kilo(exports=[str(src)])
    assert file_state(str(src)) == before
    assert res["records"] == 1 and res["new_events"] > 0
    ev = p.store.events(PID)
    types = [e["event_type"] for e in ev]
    for t in ("session_start", "user_message", "tool_call", "tool_result", "file_edit",
              "assistant_message", "reasoning"):
        assert t in types, t
    start = next(e for e in ev if e["event_type"] == "session_start")
    assert start["facts"]["kilo_version"] == "7.8.8"
    assert set(start["facts"]["missing"]) == {"system_prompt", "tool_definitions"}
    assert start["task_id"] == "FX-1" and start["session_id"] == SESSION
    bash = next(e for e in ev if e["event_type"] == "tool_result" and e["facts"]["name"] == "bash")
    assert bash["facts"]["exit"] == 0 and bash["capture_status"] == "complete"
    # the gap is recorded, not papered over
    assert any(g["kind"] == "no-system-prompt" for g in p.store.gaps(PID))


def test_kilo_database_is_read_through_a_readonly_snapshot(world, tmp_path):
    db = kilo_db(tmp_path, [kilo_export()], credential="sk-kilo-credential-DO-NOT-READ-123456")
    before = file_state(db)
    p = pipeline(tmp_path, [world[4]])
    res = p.ingest_kilo(db=db)
    assert res["records"] == 1 and res["new_events"] > 0
    assert file_state(db) == before                       # Kilo's DB is never written
    for f in (p.store.root / "raw" / "raw.sqlite", p.store.root / "normalized.sqlite"):
        assert b"DO-NOT-READ" not in f.read_bytes()       # credential/share tables never read
    # the snapshot directory is removed afterwards
    import tempfile
    tmpdir = tempfile.gettempdir()                         # /tmp on POSIX, %TEMP% on Windows
    assert not [d for d in os.listdir(tmpdir) if d.startswith("kilo-ro-")
                and os.path.exists(os.path.join(tmpdir, d, "kilo-snapshot.db"))]


def test_kilo_database_with_an_unexpected_schema_is_refused(tmp_path, world):
    db = tmp_path / "kilo.db"
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE session (id text)")
    c.close()
    p = pipeline(tmp_path, [world[4]])
    with pytest.raises(Exception, match="schema changed"):
        p.ingest_kilo(db=str(db))


def test_sanitized_export_is_provenance_only(world, tmp_path):
    p = pipeline(tmp_path, [world[4]])
    p.ingest_kilo(exports=[str(KILO_SANITIZED)])
    assert any(g["kind"] == "sanitized-export" for g in p.store.gaps(PID))
    start = next(e for e in p.store.events(PID) if e["event_type"] == "session_start")
    assert start["capture_status"] == "partial"


# 2 ------------------------------------------------------------------------------------

def test_foundry_and_kilo_records_of_the_same_calls_are_reconciled(world):
    p, cur = _full(world)
    links = p.store.correlations(PID, "same_model_call")
    assert len(links) == 3 and all(l["confidence"] == 0.95 and not l["ambiguity"] for l in links)
    traj = _episodes(p, kind="trajectory")
    assert len(traj) == 1                                  # one episode, not one per source
    ep = traj[0]["data"]
    assert ep["sources"] == ["coordinator", "foundry", "kilo"]
    assert ep["context_source"] == "foundry-capture"
    assert ep["messages"][0]["role"] == "system"           # Foundry saw the system prompt
    assert [t["name"] for t in ep["tools"]] == ["write", "bash"]


def test_identical_retries_are_recorded_as_ambiguous_not_merged(world):
    rows = capture_rows(kilo_export())
    dup = dict(rows[-1], record_uid="retry-of-last", ts=rows[-1]["ts"] + 2, attempt=2)
    p, _ = _full(world, rows + [dup])
    links = p.store.correlations(PID, "same_model_call")
    amb = [l for l in links if l["ambiguity"]]
    assert len(amb) == 1 and amb[0]["confidence"] < 0.9
    assert len(json.loads(amb[0]["ambiguity"])) == 2


# 3 ------------------------------------------------------------------------------------

def test_parent_and_helper_sessions_are_linked(world, tmp_path):
    exp = kilo_export()
    helper = clone_session(exp, "ses_helper000000000000000001", parent=SESSION, role="researcher",
                           write_content="OK helper\n")
    p = pipeline(tmp_path, [world[4]])
    p.ingest_kilo(exports=[str(KILO_EXPORT), write_json(tmp_path / "h.json", helper)])
    p.curate(PID)
    link, = p.store.correlations(PID, "parent_child")
    assert (link["a"], link["b"], link["observed"]) == (SESSION, helper["info"]["id"], 1)
    eps = {e["data"]["session_id"]: e for e in _episodes(p, kind="trajectory")}
    assert eps[helper["info"]["id"]]["data"]["parent_session_id"] == SESSION
    assert eps[SESSION]["data"]["child_sessions"] == [helper["info"]["id"]]
    # helper and parent share a group, so they can never straddle train/eval
    assert eps[SESSION]["group_id"] == eps[helper["info"]["id"]]["group_id"]


# 4 + 12 -------------------------------------------------------------------------------

def test_complete_tool_round_trip_exports_as_valid_chatml_tools(world):
    p, cur = _full(world)
    ep, = _episodes(p, kind="trajectory")
    assert ep["status"] == "selected", ep["reasons"]
    out = ep["data"]["outcome"]
    assert out["verification"]["status"] == "PASS" and out["review"]["verdict"] == "APPROVED"
    assert out["verification"]["revision"] == world[3] == ep["data"]["revision"]["head"]
    assert "src/greeting.txt" in ep["data"]["diff"]["patch"]
    m = export(p.store, [PID], "sft", "chatml-tools", {PID: p._cfg(PID)})
    assert m["validation"]["ok"], m["validation"]
    lines, path = _lines(m, p.store)
    assert len(lines) == 1
    msgs = lines[0]["messages"]
    assert [x["role"] for x in msgs] == ["system", "user", "assistant", "tool", "assistant", "tool",
                                         "assistant"]
    assert [x["function"]["name"] for x in msgs[2]["tool_calls"]] == ["write"]
    assert msgs[3]["tool_call_id"] == msgs[2]["tool_calls"][0]["id"]
    assert json.loads(msgs[4]["tool_calls"][0]["function"]["arguments"])["command"].startswith("git add")
    assert msgs[-1]["content"].startswith("Finished the implement steps.")
    assert lines[0]["tools"][0]["function"]["name"] == "write"
    assert lines[0]["meta"]["authors"] == [["local", LOCAL]]
    assert "reasoning" not in json.dumps(msgs)             # Kilo's reasoning parts left out
    man = json.loads((Path(path).parent / "manifest.json").read_text())
    assert man["reasoning_included"] is False and man["restrictions"]
    # 12: the validator rejects a broken file of the same target
    bad = Path(path).with_name("bad.jsonl")
    broken = dict(lines[0], messages=[x for x in msgs if x["role"] != "tool"])
    bad.write_text(json.dumps(broken) + "\n")
    v = validate_dataset(str(bad), "chatml-tools")
    assert not v["ok"] and any("tool" in e for e in v["errors"])
    bad.write_text(json.dumps(dict(lines[0], messages=msgs + [{"role": "assistant", "content": "x",
                                                               "reasoning_content": "y"}])) + "\n")
    assert any("reasoning" in e for e in validate_dataset(str(bad), "chatml-tools")["errors"])


def test_kilo_only_episode_is_kept_but_excluded_for_missing_context(world, tmp_path):
    tmp, ws, base, head, prof = world
    st = coordinator(tmp, PID, "FX-1", SESSION, base, head)
    p = pipeline(tmp, [prof])
    p.ingest_kilo(exports=[str(KILO_EXPORT)])
    p.ingest_coordinator(st.path)
    p.curate(PID)
    ep, = _episodes(p, kind="trajectory")
    assert ep["data"]["context_source"] == "kilo-reconstruction"
    assert ep["status"] == "excluded" and ep["reasons"] == ["missing_context"]
    assert ep["data"]["flags"]["complete_tool_sequence"] is True


# 5 ------------------------------------------------------------------------------------

def test_truncated_tool_output_is_rejected(world, tmp_path):
    tmp, ws, base, head, prof = world
    exp = kilo_export()
    for m in exp["messages"]:
        for part in m["parts"]:
            if part.get("tool") == "bash":
                part["state"]["metadata"]["truncated"] = True
    st = coordinator(tmp, PID, "FX-1", SESSION, base, head)
    p = pipeline(tmp, [prof])
    p.ingest_kilo(exports=[write_json(tmp / "t.json", exp)])
    p.ingest_foundry(capture_db=capture_db(tmp, capture_rows(kilo_export())))
    p.ingest_coordinator(st.path)
    p.curate(PID)
    ep, = _episodes(p, kind="trajectory")
    assert ep["status"] == "excluded" and "truncated_tool_output" in ep["reasons"]
    assert export(p.store, [PID], "sft", "chatml-tools", {PID: p._cfg(PID)})["examples"] == 0


def test_cut_off_stream_and_unfinished_tool_are_rejected(world, tmp_path):
    tmp, ws, base, head, prof = world
    rows = capture_rows(kilo_export())
    rows[-1]["status"], rows[-1]["reconstruction"] = "cancelled", "incomplete"
    exp = kilo_export()
    for m in exp["messages"]:
        for part in m["parts"]:
            if part.get("tool") == "bash":
                part["state"] = {"status": "running", "input": part["state"]["input"]}
    st = coordinator(tmp, PID, "FX-1", SESSION, base, head)
    p = pipeline(tmp, [prof])
    p.ingest_kilo(exports=[write_json(tmp / "t.json", exp)])
    p.ingest_foundry(capture_db=capture_db(tmp, rows))
    p.ingest_coordinator(st.path)
    p.curate(PID)
    ep, = _episodes(p, kind="trajectory")
    f = ep["data"]["flags"]
    assert f["complete_tool_sequence"] is False and f["missing_results"]
    assert {"missing_tool_result", "broken_tool_sequence"} <= set(ep["reasons"])
    # the partial call is stored with its status, never completed by guesswork
    call = [e for e in p.store.events(PID) if e["event_type"] == "model_call"][-1]
    assert call["capture_status"] == "partial"
    res = next(e for e in p.store.events(PID) if e["event_type"] == "tool_result"
               and e["facts"]["name"] == "bash")
    assert res["capture_status"] == "missing" and res["facts"]["output"] is None


# 6 ------------------------------------------------------------------------------------

def test_verified_correction_carries_true_revisions_and_authorship(world, tmp_path):
    tmp, ws, base, head1, prof = world
    import time as _t
    finding = {"severity": "major", "text": "greet a friend, not the world"}
    st = coordinator(tmp, PID, "FX-1", SESSION, base, head1, verdict="CHANGES_REQUIRED",
                     findings=[finding])
    (Path(ws) / "src" / "greeting.txt").write_text("OK hello friend\n")
    git(ws, "commit", "-qam", "FX-1: greet a friend (review)")
    head2 = git(ws, "rev-parse", "HEAD")
    s2 = "ses_repair00000000000000000002"
    repair = clone_session(kilo_export(), s2, attempt=2, start_ms=int(_t.time() * 1000) + 5000,
                           write_content="OK hello friend\n", final_text="Repaired.")
    coordinator(tmp, PID, "FX-1", s2, head1, head2, attempt=2, store=st)
    p = pipeline(tmp, [prof])
    p.ingest_kilo(exports=[str(KILO_EXPORT), write_json(tmp / "r.json", repair)])
    p.ingest_coordinator(st.path)
    p.ingest_git(PID)
    p.curate(PID)
    corr, = _episodes(p, kind="correction")
    d = corr["data"]
    assert corr["status"] == "selected", corr["reasons"]
    assert d["original"]["revision"] == {"base": base, "head": head1}
    assert d["correction"]["revision"] == {"base": head1, "head": head2}
    assert d["original"]["outcome"]["review"]["verdict"] == "CHANGES_REQUIRED"
    assert d["outcome"]["verification"]["revision"] == head2
    assert d["feedback"]["items"] == [finding["text"]]
    assert d["feedback"]["author"] == {"kind": "frontier", "model": "Foundry-Kilo-Sonnet"}
    assert "friend" in d["correction"]["diff"]["patch"]
    m = export(p.store, [PID], "correction", "canonical", {PID: p._cfg(PID)})
    assert m["validation"]["ok"] and m["examples"] == 1
    line, = _lines(m, p.store)[0]
    assert line["accepted_correction"]["revision"]["head"] == head2
    assert line["original"]["revision"]["head"] == head1


# 7 ------------------------------------------------------------------------------------

SECRETS = {
    "github": "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8",
    "bearer": "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.c2lnbmF0dXJlLXRlc3Q",
    "pem": "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEAsyntheticsynthetic\n-----END RSA PRIVATE KEY-----",
    "aws": "AKIA" + "ABCDEFGHIJKLMNOP",
    "known": "foundry-known-value-7f3a9c",
    "roblox": "_|WARNING:-DO-NOT-SHARE-THIS.--Sharing-this-will-allow-someone-to-log-in-as-you|_ABCDEF0123456789",
}


def test_synthetic_secrets_are_redacted_before_storage_and_at_export(world, tmp_path):
    tmp, ws, base, head, prof = world
    exp = kilo_export()
    leak = "\n".join(SECRETS.values())
    for m in exp["messages"]:
        for part in m["parts"]:
            if part.get("tool") == "bash":
                part["state"]["output"] = "env dump:\n" + leak
    rows = capture_rows(exp)
    st = coordinator(tmp, PID, "FX-1", SESSION, base, head)
    p = pipeline(tmp, [prof], known=[SECRETS["known"]])
    p.ingest_kilo(exports=[write_json(tmp / "s.json", exp)])
    p.ingest_foundry(capture_db=capture_db(tmp, rows))
    p.ingest_coordinator(st.path)
    p.curate(PID)
    blobs = b"".join(f.read_bytes() for f in p.store.root.rglob("*.sqlite*"))
    for name, s in SECRETS.items():
        needle = s.split("\n")[1] if name == "pem" else s.split()[-1]
        assert needle.encode() not in blobs, name
    red = [e for e in p.store.events(PID) if e["redaction_status"] == "redacted"]
    assert red and any(e["event_type"] == "tool_result" for e in red)
    m = export(p.store, [PID], "sft", "canonical", {PID: p._cfg(PID)}, known_secrets=[SECRETS["known"]])
    lines, path = _lines(m, p.store)
    text = Path(path).read_text()
    assert all((s.split("\n")[1] if n == "pem" else s.split()[-1]) not in text
               for n, s in SECRETS.items())


# 8 ------------------------------------------------------------------------------------

def _png(w=3, h=2) -> bytes:
    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xffffffff)
    raw = b"".join(b"\x00" + b"\x00\x00\x00" * w for _ in range(h))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def test_missing_screenshot_is_marked_missing_never_assumed(world, tmp_path):
    tmp, ws, base, head, _ = world
    prof = profile(tmp, PID, ws, extra='[training.policy.multimodal]\nrequire = ["artifacts_present"]',
                   eval_fraction=0.0)
    ev_dir = Path(ws) / "docs" / "evidence" / "FX-1"
    ev_dir.mkdir(parents=True)
    (ev_dir / "present.png").write_bytes(_png())
    (Path(ws) / "outside.png").write_bytes(_png())
    exp = clone_session(kilo_export(), SESSION, directory=ws)
    files = [("docs/evidence/FX-1/present.png", "present"), ("docs/evidence/FX-1/gone.png", "missing"),
             ("outside.png", "not-permitted")]
    exp["messages"][0]["parts"] += [{"type": "file", "id": f"prt_file{i}", "mime": "image/png",
                                     "filename": os.path.basename(f), "url": f}
                                    for i, (f, _) in enumerate(files)]
    p = pipeline(tmp, [prof])
    p.ingest_kilo(exports=[write_json(tmp / "img.json", exp)])
    refs = sorted((e["facts"]["filename"], e["facts"]["artifact_status"], e["capture_status"],
                   bool(e["artifact_references"]))
                  for e in p.store.events(PID) if e["event_type"] == "file_ref")
    assert refs == [("gone.png", "missing", "missing", False),
                    ("outside.png", "not-permitted", "missing", False),
                    ("present.png", "present", "complete", True)]
    present = next(e for e in p.store.events(PID) if e["event_type"] == "file_ref"
                   and e["artifact_references"])["artifact_references"][0]
    assert (present["width"], present["height"], present["mime"]) == (3, 2, "image/png")
    assert p.store.artifact_path(present["sha256"]).read_bytes() == _png()
    p.curate(PID)
    ep, = _episodes(p, kind="trajectory")
    assert "missing_artifact" in ep["reasons"]


# 9 ------------------------------------------------------------------------------------

def test_projects_are_never_merged(world, tmp_path):
    tmp, ws, base, head, prof = world
    ws2, b2, h2 = workspace(tmp / "other", "other-game")
    prof2 = profile(tmp, "other-game", ws2)
    other = clone_session(kilo_export(), "ses_other0000000000000000003", project="other-game")
    stranger = clone_session(kilo_export(), "ses_strange000000000000000004", project="not-configured")
    # Foundry rows for project B that even reuse A's session id
    rows_b = capture_rows(kilo_export(), pid="other-game")
    st = coordinator(tmp, PID, "FX-1", SESSION, base, head)
    p = pipeline(tmp, [prof, prof2])
    p.ingest_kilo(exports=[str(KILO_EXPORT), write_json(tmp / "o.json", other),
                           write_json(tmp / "x.json", stranger)])
    p.ingest_foundry(capture_db=capture_db(tmp, rows_b))
    p.ingest_coordinator(st.path)
    p.curate(PID)
    p.curate("other-game")
    assert "not-configured" not in p.store.projects()
    assert not p.store.events("not-configured")
    a, = _episodes(p, PID, "trajectory")
    b = _episodes(p, "other-game", "trajectory")
    assert a["data"]["context_source"] == "kilo-reconstruction"   # B's Foundry rows not used
    assert "foundry" not in a["data"]["sources"]
    # dedupe never crosses projects (B's own two records of one session may match each other)
    assert all(e["duplicate_of"] in (None, *[x["episode_id"] for x in b]) for e in b)
    assert a["status"] != "duplicate"
    for pid in (PID, "other-game"):
        for l in p.store.correlations(pid):
            assert {e["project_id"] for e in p.store.events(pid, "session_id IN (?,?)",
                                                            (l["a"], l["b"]))} <= {pid}
    m = export(p.store, [PID], "sft", "canonical", {PID: p._cfg(PID)})
    assert all(json.loads(x)["project_id"] == PID for x in
               (p.store.root / "datasets" / m["dataset_id"] / "data.jsonl").read_text().splitlines())


# 10 -----------------------------------------------------------------------------------

def test_reingestion_is_idempotent(world):
    p, cur1 = _full(world)
    tmp, ws, base, head, prof = world
    n_events = p.store.norm.q("SELECT COUNT(*) n FROM events")[0]["n"]
    n_raw = p.store.raw.q("SELECT COUNT(*) n FROM raw_records")[0]["n"]
    again = p.ingest_kilo(exports=[str(KILO_EXPORT)])
    assert again["new_events"] == 0 and again["existing_events"] > 0
    assert p.ingest_foundry(capture_db=str(tmp / "foundry-data/training/capture.sqlite"))["records"] == 0
    assert p.ingest_coordinator(str(tmp / "coordinator.sqlite"))["new_events"] == 0
    cur2 = p.curate(PID)
    assert p.store.norm.q("SELECT COUNT(*) n FROM events")[0]["n"] == n_events
    assert p.store.raw.q("SELECT COUNT(*) n FROM raw_records")[0]["n"] == n_raw
    assert cur2["episodes"] == cur1["episodes"]
    assert len(_episodes(p)) == cur1["episodes"]


# 11 -----------------------------------------------------------------------------------

def test_train_eval_leakage_is_prevented(world, tmp_path):
    tmp, ws, base, head, _ = world
    prof = profile(tmp, PID, ws, eval_fraction=1.0)
    st = coordinator(tmp, PID, "FX-1", SESSION, base, head)
    p = pipeline(tmp, [prof])
    p.ingest_kilo(exports=[str(KILO_EXPORT)])
    p.ingest_foundry(capture_db=capture_db(tmp, capture_rows(kilo_export())))
    p.ingest_coordinator(st.path)
    p.curate(PID)
    ep, = _episodes(p, kind="trajectory")
    assert (ep["status"], ep["split"]) == ("eval", "eval")
    cfg = p._cfg(PID)
    assert export(p.store, [PID], "sft", "chatml-tools", {PID: cfg})["examples"] == 0
    ev = export(p.store, [PID], "sft", "chatml-tools", {PID: cfg}, purpose="eval")
    assert ev["examples"] == 1 and ev["validation"]["ok"]
    assert (p.store.root / "eval" / ev["dataset_id"]).is_dir()
    # the split policy changes later: the episode was in an evaluation set, so
    # it stays out of training (by group and by content)
    cfg.split["eval_fraction"] = 0.0
    p.curate(PID)
    ep, = _episodes(p, kind="trajectory")
    assert ep["status"] == "selected"
    tr = export(p.store, [PID], "sft", "chatml-tools", {PID: cfg})
    assert tr["examples"] == 0 and tr["skipped"] == {"eval_leakage_blocked": 1}
    # a near-duplicate under another task joins the canonical group
    import time as _t
    dup = clone_session(kilo_export(), "ses_dup0000000000000000000005", task="FX-9",
                        start_ms=int(_t.time() * 1000))
    dup["messages"][0]["parts"][0]["text"] += "\nPlease be careful."   # same work, reworded ask
    p.ingest_kilo(exports=[write_json(tmp / "d.json", dup)])
    p.ingest_foundry(capture_db=capture_db(tmp, capture_rows(dup, tid="FX-9",
                                                              session=dup["info"]["id"]),
                                           name="capture2.sqlite"))
    p.curate(PID)
    d = next(e for e in _episodes(p, kind="trajectory") if e["data"]["session_id"] == dup["info"]["id"])
    assert d["status"] == "duplicate" and d["group_id"] == ep["group_id"]


# 13 -----------------------------------------------------------------------------------

def test_interrupted_ingestion_recovers_without_loss_or_duplicates(world, tmp_path):
    tmp, ws, base, head, prof = world
    db = capture_db(tmp, capture_rows(kilo_export()))
    p = pipeline(tmp, [prof])
    key = f"foundry:{db}"
    with pytest.raises(KeyboardInterrupt):
        p.ing.run(key, p._filter(FA.records(FA.from_db(db)), "foundry"), FA.normalize,
                  cursor_of=lambda rid, payload: payload["id"], stop_after=2)
    assert len(p.store.interrupted_runs()) == 1
    assert p.store.checkpoint(key) == "2"
    assert p.store.norm.q("SELECT COUNT(*) n FROM events")[0]["n"] == 2
    res = p.ingest_foundry(capture_db=db)                  # the same command, run again
    assert res["records"] == 4 and res["existing_events"] == 0
    assert p.store.interrupted_runs() == []               # resolved by the completed re-run
    got = {r["event_id"] for r in p.store.norm.q("SELECT event_id FROM events")}
    fresh = pipeline(tmp / "fresh", [prof])
    fresh.ingest_foundry(capture_db=db)
    assert got == {r["event_id"] for r in fresh.store.norm.q("SELECT event_id FROM events")}


def test_ingestion_error_leaves_a_record_and_raises(world, tmp_path):
    p = pipeline(tmp_path, [world[4]])

    def boom(*a):
        raise RuntimeError("normalizer bug")
    with pytest.raises(RuntimeError):
        p.ing.run("kilo:test", [("r1", PID, {"x": 1}, {"adapter": "kilo"})], boom)
    run = p.store.norm.q("SELECT * FROM ingest_runs ORDER BY id DESC LIMIT 1")[0]
    assert run["status"] == "error" and "normalizer bug" in run["error"]


# 14 -----------------------------------------------------------------------------------

def test_second_project_profile_changes_policy_not_code(world, tmp_path):
    """The fixture-game example profile and the AnimalUprising profile load
    into the same engine; their policies decide differently on the same
    Kilo-only episode."""
    tmp, ws, base, head, _ = world
    root = Path(__file__).resolve().parents[2]
    tpl = (root / "contrib/coordinator-pilot/fixture-game/project.toml.template").read_text()
    fx = tmp / "fx.toml"
    fx.write_text(tpl.replace("@WORKSPACE@", ws.replace("\\", "/")).replace("@KILO_URL@", "http://k.invalid")
                  .replace("@FOUNDRY_URL@", "http://f.invalid"))
    au = TC.load(str(root / "foundry_coordinator/projects/animaluprising/project.toml"))
    fxc = TC.load(str(fx))
    assert (fxc.id, au.id) == ("fixture-game", "animaluprising")
    assert "system_and_tools_known" not in fxc.require("sft")
    assert "system_and_tools_known" in au.require("sft")
    assert au.artifact_roots == ["docs/evidence"] and au.capture["on_failure"] == "warn"
    st = coordinator(tmp, PID, "FX-1", SESSION, base, head)
    from foundry_training.pipeline import Pipeline
    from foundry_training.store import TrainingStore
    fxc.split["eval_fraction"] = 0.0            # keep the single example out of eval here
    p = Pipeline(TrainingStore(str(tmp / "store")), [fxc])
    p.ingest_kilo(exports=[str(KILO_EXPORT)])
    p.ingest_coordinator(st.path)
    p.curate(PID)
    ep, = _episodes(p, kind="trajectory")
    assert ep["status"] == "selected", ep["reasons"]
    # the same episode under AnimalUprising's policy
    from foundry_training.curate import requirements
    assert "missing_context" in requirements(ep["data"], au)


# coordinator closeout -> spool -> pipeline -------------------------------------------

def test_closeout_manifest_is_processed_and_completeness_recorded(world, tmp_path):
    tmp, ws, base, head, prof = world
    from types import SimpleNamespace

    from foundry_coordinator import training_hook
    st = coordinator(tmp, PID, "FX-1", SESSION, base, head)
    st.update_task(PID, "FX-1", head_sha=head, reviewed_sha=head)
    st.add_session("ses_never_captured", PID, "FX-1", "FX-1-close0", "implementer", "closeout", 1,
                   "kilo", "Foundry-Kilo-Local")
    P = SimpleNamespace(id=PID, profile=SimpleNamespace(
        raw={"training": {"enabled": True, "spool": str(tmp / "spool")}}, review={"required": True}))
    m = training_hook.finalize(st, P, st.task(PID, "FX-1"), "COMPLETE", str(tmp))
    assert m["head_sha"] == head and set(m["sessions"]) == {SESSION, "ses_never_captured"}
    assert m["completeness"]["reviews"] == 1 and not m["completeness"]["gaps"]
    assert "messages" not in json.dumps(m)                # pointers, not content
    p = pipeline(tmp, [prof])
    p.ingest_kilo(exports=[str(KILO_EXPORT)])
    res, = p.process_queue(str(tmp / "spool"))
    assert res["sessions_missing"] == ["ses_never_captured"]
    assert (tmp / "spool" / "done").glob("*.json")
    assert not list((tmp / "spool").glob("*.json"))
    assert any(g["kind"] == "session-not-captured" for g in p.store.gaps(PID))
    # a project without training enabled is skipped, the manifest kept as processed
    st2 = coordinator(tmp / "b", "elsewhere", "T", "ses_b", base, head)
    P2 = SimpleNamespace(id="elsewhere", profile=SimpleNamespace(
        raw={"training": {"enabled": True, "spool": str(tmp / "spool")}}, review={}))
    training_hook.finalize(st2, P2, st2.task("elsewhere", "T"), "FAILED", str(tmp))
    res, = p.process_queue(str(tmp / "spool"))
    assert res["skipped"]
