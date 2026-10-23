"""Cross-project playbook (A1): the playbook repo layout and init commit, the
shared index under @playbook, slices joined to the memory block under the shared
budget with the failure-signature boost, PLAYBOOK_NOTE validation and commits to
the playbook repo (never the game repo), chat add/edit, the deterministic
promotion pipeline with its operator-only decisions, the snapshot publish and
the kb_mcp read-only search. Real git repositories, fake Foundry, no model."""

import json
import os

import pytest

from foundry_coordinator.memory import layout as mem_layout
from foundry_coordinator.playbook import layout as pb_layout
from foundry_coordinator.playbook import promote as PR
from foundry_coordinator.playbook.layout import PLAYBOOK_ID
from tests.coordinator.helpers import coordinator, git, make_repo, write_ok, write_profile

T1 = {"id": "T1", "title": "Make the thing", "spec": "Write src/a.txt containing OK.",
      "scope": ["src/a.txt"]}
FIXES = """# Fixes: rojo

Cross-project playbook entries; one `## heading` per entry.

## rojo_stale: Studio behind HEAD

- When Studio's loaded sources differ from HEAD, restart Rojo through the Foundry Client
  local API; the plugin reconnects and re-syncs. Never edit in Studio to catch up.

## kilo_auth: Kilo 401

- A Kilo 401 means the key rotated; re-run foundry-client restart kilo --wait.
"""
HOWTO = """# How-to: evidence

Cross-project playbook entries; one `## heading` per entry.

## Evidence packets for src files

- Every delivered src/a.txt change ships its project-check output in the packet (src/a.txt).
"""


def _pb(root, rel, text):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8", newline="\n")


@pytest.fixture()
def world(tmp_path):
    """Two game projects and a playbook repo path (uninitialized)."""
    root_a, _ = make_repo(tmp_path, "game", [T1])
    root_b, _ = make_repo(tmp_path, "other", [T1])
    extra = "[automation]\nplaybook_promote = true\n"
    profs = [write_profile(tmp_path, "game", root_a, extra=extra),
             write_profile(tmp_path, "other", root_b, extra=extra)]
    pb_path = tmp_path / "playbook"
    c = coordinator(tmp_path, profs, {"game": write_ok("src/a.txt"),
                                      "other": write_ok("src/a.txt")},
                    playbook=str(pb_path))
    return tmp_path, root_a, pb_path, c


class St:
    def __init__(self, text=""):
        self.final_text = text


# -- layout + init ---------------------------------------------------------------------

def test_init_creates_repo_layout_once_and_commits(world):
    tmp, root, pb_path, c = world
    assert not c.playbook.configured()
    out = c.playbook.init("victor")
    assert not out["error"] and out["sha"]
    assert c.playbook.configured()
    assert git(pb_path, "log", "-1", "--format=%s").startswith("PLAYBOOK: create the playbook")
    assert git(pb_path, "status", "--porcelain") == ""
    again = c.playbook.init()
    assert again["created"] == [] and again["sha"] == ""
    # file_ok: only <known dir>/<name>.md, no traversal, .md optional
    assert pb_layout.file_ok("fixes/rojo") == "fixes/rojo.md"
    assert pb_layout.file_ok("howto\\evidence.md") == "howto/evidence.md"
    assert pb_layout.file_ok("fixes/../x.md") is None
    assert pb_layout.file_ok("DECISIONS.md") is None
    assert pb_layout.file_ok("scripts/sub/dir.md") is None


def test_unconfigured_playbook_is_inert(tmp_path):
    root, _ = make_repo(tmp_path, "game", [T1])
    prof = write_profile(tmp_path, "game", root)
    c = coordinator(tmp_path, [prof], {"game": write_ok("src/a.txt")})
    P = c.projects["game"]
    assert not c.playbook.configured() and not c.playbook.enabled(P)
    c.playbook.tick()                                   # no raise, nothing indexed
    assert c.playbook.entries() == []
    assert c.playbook.prompt_block(P, "T1", "s1") == ""
    # a note is still answered, with the reason, never silently dropped
    got = c.playbook.apply_notes(P, {"task_id": "T1", "session_id": "s1"},
                                 St('PLAYBOOK_NOTE: file=fixes/x.md heading="H" text="t"'))
    assert got == [{"file": "fixes/x.md", "heading": "H", "status": "rejected",
                    "detail": "no playbook is configured for this coordinator"}]


# -- index + slices --------------------------------------------------------------------

def test_index_and_slice_with_signature_boost_and_shared_budget(world):
    tmp, root, pb_path, c = world
    P = c.projects["game"]
    c.playbook.init()
    _pb(pb_path, "fixes/rojo.md", FIXES)
    _pb(pb_path, "howto/evidence.md", HOWTO)
    res = c.playbook.reindex()
    assert set(res["changed"]) == {"fixes/rojo.md", "howto/evidence.md"}
    ents = c.playbook.entries()
    assert {e["file"] for e in ents} == {"fixes/rojo.md", "howto/evidence.md"}
    # indexed under the reserved project id, not under any game project
    assert c.memory.index.entries(PLAYBOOK_ID) and not c.memory.index.entries("game")
    spec = P.adapter.describe("T1")
    # the evidence how-to matches T1's scope path; no signature -> fixes don't lead
    sl = c.playbook.slice(P, spec)
    assert any(e["file"] == "howto/evidence.md" for e in sl["entries"])
    # a recorded failure signature boosts the matching fixes entry to the front
    sl2 = c.playbook.slice(P, spec, signature="rojo_stale")
    assert sl2["entries"] and sl2["entries"][0]["heading"].startswith("rojo_stale")
    # shared budget: what memory used is gone, with the MIN_BUDGET floor
    full = c.playbook.slice(P, spec)["budget"]
    part = c.playbook.slice(P, spec, used_tokens=full - 500)["budget"]
    floor = c.playbook.slice(P, spec, used_tokens=10 ** 6)["budget"]
    assert part == 500 and floor == 400
    # search works over the playbook index
    assert any("401" in e["text"] for e in c.playbook.search("kilo 401"))


def test_memory_block_joins_playbook_and_records_slices(world):
    tmp, root, pb_path, c = world
    P = c.projects["game"]
    c.memory.init(P)
    c.memory.add(P, "KNOWN_ISSUES.md", "src/a.txt newline",
                 "src/a.txt must end with a newline (src/a.txt).", "victor")
    c.playbook.init()
    _pb(pb_path, "howto/evidence.md", HOWTO)
    c.playbook.reindex()
    c.approve("game", "T1", [], "victor")
    block = c._memory_block(P, "T1", "s1")
    assert "## Project memory" in block and "## Playbook" in block
    assert block.index("## Project memory") < block.index("## Playbook")
    assert "PLAYBOOK_NOTE" in block                    # the render says how to contribute
    rows = c.playbook.slices()
    assert rows and rows[0]["session_id"] == "s1" and rows[0]["project_id"] == "game"
    assert rows[0]["entries"][0]["file"] == "howto/evidence.md"
    # a project with [automation] playbook = false gets memory only (a fresh
    # table object: the settings cache keys on the table's identity)
    c.store.x("DELETE FROM playbook_slices", ())
    before = P.profile.raw.get("automation") or {}
    P.profile.raw["automation"] = {**before, "playbook": False}
    try:
        block2 = c._memory_block(P, "T1", "s2")
        assert "## Playbook" not in block2 and "## Project memory" in block2
    finally:
        P.profile.raw["automation"] = before


# -- notes -----------------------------------------------------------------------------

def test_playbook_notes_commit_to_the_playbook_repo_only(world):
    tmp, root, pb_path, c = world
    P = c.projects["game"]
    c.playbook.init()
    game_head = git(root, "rev-parse", "HEAD")
    t = {"task_id": "T1", "session_id": "sess-1"}
    text = ('done.\n'
            'PLAYBOOK_NOTE: file=fixes/gate.md heading="gate_tooling: dead runner" '
            'text="Clear the studio_gate lock when its pid is dead; fast_lane re-polls."\n'
            'PLAYBOOK_NOTE: file=secret.md heading="H" text="nope"\n'
            'PLAYBOOK_NOTE: file=fixes/gate.md heading="Key" text="api_key = \'ABCDEFGHIJKLMNOP\'"\n')
    got = c.playbook.apply_notes(P, t, St(text))
    by = {g["status"] for g in got}
    assert by == {"committed", "rejected"}
    ok = [g for g in got if g["status"] == "committed"]
    assert ok[0]["file"] == "fixes/gate.md" and ok[0]["sha"]
    rej = {g["file"]: g["detail"] for g in got if g["status"] == "rejected"}
    assert "secret.md" in rej and "fixes/<name>.md" in rej["secret.md"]
    assert any("secret" in d for d in rej.values())
    # committed to the playbook repo; the game repo never moved
    assert git(pb_path, "log", "-1", "--format=%s").startswith("PLAYBOOK: playbook notes")
    assert git(root, "rev-parse", "HEAD") == game_head
    assert "dead runner" in (pb_path / "fixes" / "gate.md").read_text(encoding="utf-8")
    # the same notes from the same session (accepted or rejected) are not applied twice
    assert c.playbook.apply_notes(P, t, St(text)) == []
    # indexed right away
    assert any(e["file"] == "fixes/gate.md" for e in c.playbook.entries())


def test_chat_add_and_edit(world):
    tmp, root, pb_path, c = world
    P = c.projects["game"]
    c.playbook.init()
    out = c.playbook.add(P, "formats/commits", "Commit subjects",
                         "Subjects start with the sprint id and a colon.", "victor (control center)")
    assert out["ok"] and out["sha"] and out["path"] == "formats/commits.md"
    bad = c.playbook.edit(P, "formats/commits.md", "No such heading", "x", "victor")
    assert not bad["ok"] and "no heading" in bad["error"]
    out2 = c.playbook.edit(P, "formats/commits.md", "Commit subjects",
                           "- Subjects start with the sprint id; bodies name the evidence.",
                           "victor")
    assert out2["ok"] and out2["sha"] != out["sha"]
    text = (pb_path / "formats" / "commits.md").read_text(encoding="utf-8")
    assert "bodies name the evidence" in text and "start with the sprint id and a colon" not in text
    # CLI-style add without a project still works (no event, no settings)
    out3 = c.playbook.add(None, "howto/x", "H", "body", "operator")
    assert out3["ok"]


# -- promotion pipeline ----------------------------------------------------------------

def test_promotion_failure_signature_twice_opens_operator_decision_and_add_commits(world):
    tmp, root, pb_path, c = world
    A, B = c.projects["game"], c.projects["other"]
    c.playbook.init()
    c.playbook.promote.observe_failure(A, "T1", "rojo_stale", "session_error", "rojo is stale")
    assert c.playbook.promote.view()[0]["status"] == "counting"
    assert not c.store.q("SELECT 1 FROM decisions WHERE kind='playbook_promotion'") \
        if c.store.q("SELECT 1 FROM sqlite_master WHERE name='decisions'") else True
    c.playbook.promote.observe_failure(B, "T1", "rojo_stale", "session_error", "rojo is stale again")
    cd = c.playbook.promote.view()[0]
    assert cd["status"] == "proposed" and cd["count"] == 2
    assert set(cd["projects"]) == {"game", "other"}
    assert cd["file"].startswith("fixes/rojo-stale")
    d = c.obligations.decision(cd["decision_id"])
    assert d["kind"] == "playbook_promotion" and d["status"] == "open"
    assert d["recommendation"].startswith("add")
    assert d["recommended"] is None            # operator-only: never timeout-applied
    # a model actor cannot answer it
    from foundry_coordinator.obligations import DecisionRefused
    with pytest.raises(DecisionRefused):
        c.obligations.answer(d["decision_id"], PR.OPTION_ADD, "kilo-lead", "lead")
    c.obligations.answer(d["decision_id"], PR.OPTION_ADD, "victor", "operator", channel="control")
    assert c.playbook.promote.sweep() == [cd["key"]]
    assert c.playbook.promote.view()[0]["status"] == "promoted"
    body = (pb_path / cd["file"].replace("/", os.sep)).read_text(encoding="utf-8")
    assert "rojo_stale" in body and "game, other" in body
    assert git(pb_path, "log", "-1", "--format=%s").startswith("PLAYBOOK: promoted")
    # the third occurrence of a promoted signature does not re-open a decision
    c.playbook.promote.observe_failure(A, "T1", "rojo_stale", "session_error", "again")
    assert c.playbook.promote.view()[0]["status"] == "promoted"


def test_promotion_skip_backs_off_and_promote_defaults_off(world, tmp_path):
    tmp, root, pb_path, c = world
    A = c.projects["game"]
    c.playbook.init()
    for _ in range(2):
        c.playbook.promote.observe_failure(A, "T1", "kilo_auth", "session_error", "401")
    cd = c.playbook.promote.view()[0]
    c.obligations.answer(cd["decision_id"], PR.OPTION_SKIP, "victor", "operator", channel="control")
    c.playbook.promote.sweep()
    cd = c.playbook.promote.view()[0]
    assert cd["status"] == "counting" and cd["threshold"] == 4
    c.playbook.promote.observe_failure(A, "T1", "kilo_auth", "session_error", "401")
    assert c.playbook.promote.view()[0]["status"] == "counting"      # 3 < 4: not re-proposed
    c.playbook.promote.observe_failure(A, "T1", "kilo_auth", "session_error", "401")
    assert c.playbook.promote.view()[0]["status"] == "proposed"      # 4 >= 4: asks again
    # default: playbook_promote is off -> counting only, no decision
    root2, _ = make_repo(tmp_path, "plain", [T1])
    prof2 = write_profile(tmp_path, "plain", root2)
    c2 = coordinator(tmp_path, [prof2], {"plain": write_ok("src/a.txt")},
                     playbook=str(tmp_path / "pb2"), store_path=tmp_path / "state2" / "c.sqlite")
    c2.playbook.init()
    for _ in range(3):
        c2.playbook.promote.observe_failure(c2.projects["plain"], "T1", "rojo", "session_error", "x")
    assert c2.playbook.promote.view()[0]["status"] == "counting"


def test_promotion_operator_instruction_third_repeat(world):
    tmp, root, pb_path, c = world
    A = c.projects["game"]
    c.playbook.init()
    for sprint in ("UI-01", "UI-02", "UI-03"):
        c.playbook.promote.observe_instruction(
            A, sprint, f"Always run selene before committing {sprint} work.", "victor")
    cd = c.playbook.promote.view()[0]
    assert cd["kind"] == "howto" and cd["status"] == "proposed" and cd["count"] == 3
    assert cd["file"] == "howto/operator-guidance.md"
    # short throwaway instructions never count
    c.playbook.promote.observe_instruction(A, "T1", "continue", "victor")
    assert all(x["kind"] != "howto" or x["key"] == cd["key"] for x in c.playbook.promote.view())
    assert PR.normalize_instruction("Run SELENE  before UI-02b") == \
        PR.normalize_instruction("run selene before UI-03")


# -- snapshot + publish ----------------------------------------------------------------

def test_snapshot_shape_and_publish_dedupe(world):
    tmp, root, pb_path, c = world
    P = c.projects["game"]
    c.playbook.init()
    _pb(pb_path, "howto/evidence.md", HOWTO)
    c.playbook.reindex()
    snap = c.playbook.snapshot()
    assert snap["project_id"] == PLAYBOOK_ID and snap["scope"] == "playbook"
    assert snap["type"] == "memory" and snap["enabled"]
    assert any(e["file"] == "howto/evidence.md" for e in snap["entries"])
    assert "candidates" in snap

    class Sink:
        def __init__(self):
            self.batches = []

        def telemetry(self, batch):
            self.batches.append(batch)

    sink = Sink()
    assert c.playbook.publish(sink, "src-1") == 1
    assert c.playbook.publish(sink, "src-1") == 0            # unchanged: not re-sent
    c.playbook.add(P, "howto/more", "H", "body", "victor")
    assert c.playbook.publish(sink, "src-1") == 1
    obs = sink.batches[0]["observations"][0]
    assert obs["project_id"] == PLAYBOOK_ID


# -- kb_mcp ----------------------------------------------------------------------------

def test_kb_mcp_search_is_read_only(world):
    tmp, root, pb_path, c = world
    c.playbook.init()
    _pb(pb_path, "fixes/rojo.md", FIXES)
    c.playbook.reindex()
    from foundry_coordinator.playbook import kb_mcp
    conn = kb_mcp.open_ro(str(tmp / "state" / "c.sqlite"))
    init = kb_mcp.handle(conn, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    assert init["result"]["serverInfo"]["name"] == "foundry-playbook"
    tools = kb_mcp.handle(conn, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    assert [t["name"] for t in tools["result"]["tools"]] == ["kb_search"]
    hit = kb_mcp.handle(conn, {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                               "params": {"name": "kb_search",
                                          "arguments": {"query": "rojo stale studio"}}})
    text = hit["result"]["content"][0]["text"]
    assert "rojo_stale" in text and "fixes/rojo.md" in text
    with pytest.raises(Exception):
        conn.execute("INSERT INTO memory_entries (project_id, entry_id) VALUES ('x','y')")
    miss = kb_mcp.handle(conn, {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                                "params": {"name": "kb_search",
                                           "arguments": {"query": "zzzz-nothing"}}})
    assert "No playbook entries match" in miss["result"]["content"][0]["text"]
