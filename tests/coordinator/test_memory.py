"""Project memory (Team Lead piece 4): the docs/memory layout, the heading index,
slices by files / needs / track with the last decisions, per-entry delta hashing
in prompts, MEMORY_NOTE validation and commits, the STATE.md import, and the
control channel's memory actions and snapshot publish. Real git repositories,
the generic adapter, a scripted coding client; no model, no Kilo."""

import json
import os
import textwrap

import pytest

from foundry_coordinator import states as S
from foundry_coordinator.adapters.base import TaskSpec
from foundry_coordinator.control import ControlChannel
from foundry_coordinator.memory import index as MI
from foundry_coordinator.memory import layout, notes, slices
from tests.coordinator.helpers import (FakeFoundry, coordinator, git, make_repo, run_until,
                                       write_ok, write_profile)

T1 = {"id": "T1", "title": "Make the thing", "spec": "Write src/a.txt containing OK.",
      "scope": ["src/a.txt"]}
T2 = {"id": "T2", "title": "Blender wolf", "spec": "Model the wolf.", "scope": ["assets/wolf/*"],
      "deps": ["T1"]}

DECISIONS = """# Decisions — game

Project-wide decisions, newest last.

## T1: text files hold OK

- Every verified text file under src/ contains the word OK (T1); the project check reads it.

## Studio: stories before the gate

- Stories run before the gate; the gate never re-runs a story that passed at the same head.

## Blender: export scale

- Blender exports use scale 1.0 and Y-up for assets/wolf/*.fbx (T2).

## Audio: loudness

- Audio masters are -16 LUFS integrated.

## Rojo: one serve

- One Rojo serve per checkout; Studio's plugin reconnects by hand after a restart.
"""
KNOWN = """# Known issues — game

## src/a.txt newline

- src/a.txt must end with a newline or verify.py reports FAIL on Windows.

## Blender: GPU

- Blender and Comfy share one gpu; never run both.

## Resolved
"""


def _mem(root, rel, text):
    p = root / "docs" / "memory" / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8", newline="\n")


@pytest.fixture()
def game(tmp_path):
    root, remote = make_repo(tmp_path, "game", [T1, T2])
    prof = write_profile(tmp_path, "game", root)
    c = coordinator(tmp_path, [prof], {"game": write_ok("src/a.txt")})
    return tmp_path, root, prof, c


# -- layout -------------------------------------------------------------------------

def test_init_creates_the_layout_once_and_commits_it(game):
    tmp, root, prof, c = game
    P = c.projects["game"]
    before = git(root, "rev-parse", "HEAD")
    out = c.memory.init(P, "victor")
    assert set(out["created"]) >= {f"docs/memory/{f}" for f in layout.FIXED_FILES}
    assert out["sha"] and out["sha"] != before and not out["error"]
    msg = git(root, "log", "-1", "--format=%s%n%b")
    assert msg.startswith("MEMORY: create the docs/memory layout")
    assert "AU_ALLOW_SCOPE" in msg                        # the AU hook's override is declared
    assert git(root, "status", "--porcelain") == ""      # nothing left uncommitted
    again = c.memory.init(P)
    assert again["created"] == [] and again["sha"] == ""
    assert c.memory.enabled(P)
    assert layout.file_ok("docs/memory/sprints/UI-02b") == "sprints/UI-02b.md"
    assert layout.file_ok("tracks/../x.md") is None and layout.file_ok("x/y.md") is None


# -- index --------------------------------------------------------------------------

def test_index_parses_headings_tags_and_reindexes_only_changed_files(game):
    tmp, root, prof, c = game
    P = c.projects["game"]
    c.memory.init(P)
    _mem(root, "DECISIONS.md", DECISIONS)
    _mem(root, "KNOWN_ISSUES.md", KNOWN)
    res = c.memory.reindex(P)
    assert set(res["changed"]) == {"DECISIONS.md", "KNOWN_ISSUES.md"}
    ents = c.memory.entries(P, "DECISIONS.md")
    assert [e["heading"] for e in ents] == ["T1: text files hold OK", "Studio: stories before the gate",
                                            "Blender: export scale", "Audio: loudness", "Rojo: one serve"]
    e = ents[0]
    assert "T1" in e["tags"] and "decisions" in e["tags"] and e["hash"] == MI._h(e["text"])
    blender = ents[2]
    assert {"T2", "blender", "assets/wolf"} <= set(blender["tags"])
    # the preamble ("Project-wide decisions...") is not an entry; tables drop their header
    assert all(e["heading"] for e in c.memory.entries(P))
    assets = MI.parse_markdown("ASSETS.md", layout.TEMPLATES["ASSETS.md"].format(name="g")
                               + "| Wolf | assets/wolf/wolf.fbx | T2 | imported | ok |\n")
    assert [a.text for a in assets] == ["| Wolf | assets/wolf/wolf.fbx | T2 | imported | ok |"]
    assert assets[0].heading == "Registry" and "T2" in assets[0].tags
    # search (FTS5 when the sqlite build has it, else the LIKE fallback)
    hits = c.memory.search(P, "gate stories")
    assert hits and hits[0]["heading"] == "Studio: stories before the gate"
    assert c.memory.search(P, "gpu")[0]["file"] == "KNOWN_ISSUES.md"
    # unchanged files are not re-parsed; a changed one is; a removed one is dropped
    assert c.memory.reindex(P)["changed"] == []
    _mem(root, "DECISIONS.md", DECISIONS + "\n## New: thing\n\n- A new fact.\n")
    assert c.memory.reindex(P)["changed"] == ["DECISIONS.md"]
    assert len(c.memory.entries(P, "DECISIONS.md")) == 6
    os.remove(root / "docs" / "memory" / "KNOWN_ISSUES.md")
    assert c.memory.reindex(P)["removed"] == ["KNOWN_ISSUES.md"]
    assert c.memory.entries(P, "KNOWN_ISSUES.md") == []
    fp1 = c.memory.index.fingerprint("game")
    assert fp1 and fp1 == c.memory.index.fingerprint("game")


# -- slices -------------------------------------------------------------------------

def test_slice_matches_files_needs_deps_and_track_plus_recent_decisions(game):
    tmp, root, prof, c = game
    P = c.projects["game"]
    c.memory.init(P)
    _mem(root, "DECISIONS.md", DECISIONS)
    _mem(root, "KNOWN_ISSUES.md", KNOWN)
    _mem(root, "sprints/T1.md", "# Sprint T1\n\n## Ledger\n\n- T1 ran once before and was reverted.\n")
    _mem(root, "tracks/ui.md", "# Track ui\n\n## Notes\n\n- The ui track owns src/client/UI.\n")
    _mem(root, "CONVENTIONS.md", "# Conventions\n\n## Commits\n\n- Commit by explicit path.\n")
    c.memory.reindex(P)
    spec = P.adapter.describe("T1")
    sl = c.memory.slice(P, spec, track="ui", budget_tokens=2000)
    got = {(e["file"], e["heading"]) for e in sl["entries"]}
    assert ("sprints/T1.md", "Ledger") in got                        # the sprint's own notes
    assert ("tracks/ui.md", "Notes") in got                          # the track's notes
    assert ("DECISIONS.md", "T1: text files hold OK") in got         # id match
    assert ("KNOWN_ISSUES.md", "src/a.txt newline") in got           # path match
    # the last N decisions ride along, but unrelated known issues do not
    assert ("DECISIONS.md", "Rojo: one serve") in got
    assert ("KNOWN_ISSUES.md", "Blender: GPU") not in got
    assert ("CONVENTIONS.md", "Commits") not in got                  # nothing matched it
    why = {(e["file"], e["heading"]): e["why"] for e in sl["entries"]}
    assert why[("sprints/T1.md", "Ledger")] == "this sprint's notes"
    assert why[("tracks/ui.md", "Notes")] == "track notes"
    assert sl["tokens"] <= sl["budget"] and sl["dropped"] == 0
    text = sl["text"]
    assert text.startswith("## Project memory") and "### DECISIONS.md > T1: text files hold OK" in text
    assert "MEMORY_NOTE" in text                                     # how to write back
    # T2 (blender, depends on T1): its deps and needs words pull the blender facts
    spec2 = P.adapter.describe("T2")
    sl2 = c.memory.slice(P, spec2, budget_tokens=2000)
    got2 = {(e["file"], e["heading"]) for e in sl2["entries"]}
    assert ("DECISIONS.md", "Blender: export scale") in got2
    assert ("KNOWN_ISSUES.md", "Blender: GPU") in got2
    assert ("DECISIONS.md", "T1: text files hold OK") in got2        # dependency
    assert ("tracks/ui.md", "Notes") not in got2
    # a tight budget drops the lowest matches (the always-included ones first in line), never raises
    _mem(root, "CONVENTIONS.md", "# Conventions\n\n## Commits\n\n- Commit by explicit path.\n\n"
                                 "## src/a.txt format\n\n- " + "The OK file is long. " * 60 + "\n")
    c.memory.reindex(P)
    tight = c.memory.slice(P, spec, track="ui", budget_tokens=200)
    assert tight["dropped"] > 0 and tight["tokens"] <= tight["budget"]
    assert ("sprints/T1.md", "Ledger") in {(e["file"], e["heading"]) for e in tight["entries"]}
    # a slice is deterministic: same entries, same hash
    sl_b = c.memory.slice(P, spec, track="ui", budget_tokens=2000)
    assert sl_b["hash"] == c.memory.slice(P, spec, track="ui", budget_tokens=2000)["hash"]
    # no model, no spec data needed: a bare TaskSpec works for tl-tracks' track sessions
    bare = TaskSpec("game", "X9", "x", "x", ["assets/wolf/a.fbx"], data={"needs": ["blender"]})
    bare_sl = c.memory.slice(P, bare, track="assets")
    assert ("DECISIONS.md", "Blender: export scale") in {(e["file"], e["heading"])
                                                          for e in bare_sl["entries"]}


def test_prompt_carries_the_slice_once_then_only_changed_entries(game):
    tmp, root, prof, c = game
    P = c.projects["game"]
    c.memory.init(P)
    _mem(root, "DECISIONS.md", DECISIONS)
    c.memory.reindex(P)
    sid = "sess-1"
    p1, _ = c._with_operator_notes("game", "T1", "PROMPT", sid)
    assert "## Project memory" in p1 and "T1: text files hold OK" in p1
    c._notes_delivered([], sid)                       # Kilo accepted the prompt
    p2, _ = c._with_operator_notes("game", "T1", "PROMPT", sid)
    assert p2 == "PROMPT"                              # nothing changed: nothing re-sent
    _mem(root, "DECISIONS.md", DECISIONS + "\n## T1: second thought\n\n- src/a.txt also needs OK twice.\n")
    c.memory.reindex(P)
    p3, _ = c._with_operator_notes("game", "T1", "PROMPT", sid)
    assert "entries changed since your last prompt" in p3
    assert "second thought" in p3 and "stories before the gate" not in p3
    rows = c.memory.slices(P)
    assert rows and rows[0]["session_id"] == sid and rows[0]["prompts"] == 3
    assert rows[0]["entries"] and rows[0]["tokens"] > 0
    # a refused prompt is not counted as sent
    sid2 = "sess-2"
    c._with_operator_notes("game", "T1", "PROMPT", sid2)
    c._notes_requeue([])
    again, _ = c._with_operator_notes("game", "T1", "PROMPT", sid2)
    assert "T1: text files hold OK" in again
    # memory off: no block at all
    P.profile.raw["automation"] = {"project_memory": False}
    assert c._with_operator_notes("game", "T1", "PROMPT", "sess-3")[0] == "PROMPT"
    P.profile.raw.pop("automation")


# -- notes --------------------------------------------------------------------------

def test_note_parsing_and_validation():
    text = ('done.\nMEMORY_NOTE: file=DECISIONS.md heading="Studio: gate" text="Stories first, \\"quoted\\"."\n'
            'MEMORY_NOTE: file=ASSETS.md heading=Registry text=| Wolf | assets/wolf.fbx | T2 | imported | ok |\n'
            'MEMORY_NOTE: {"file": "sprints/T1.md", "heading": "Ledger", "text": "x y z"}\n'
            'MEMORY_NOTE: nonsense here\n')
    ns = notes.parse_notes(text)
    assert [n.get("file") for n in ns] == ["DECISIONS.md", "ASSETS.md", "sprints/T1.md", None]
    assert ns[0]["text"] == 'Stories first, "quoted".' and ns[0]["heading"] == "Studio: gate"
    assert ns[1]["text"].startswith("| Wolf") and ns[1]["text"].endswith("ok |")
    assert ns[3]["error"]
    assert notes.validate(ns[0]) == "" and notes.validate(ns[1]) == ""
    assert "unknown memory file" in notes.validate({"file": "README.md", "heading": "a", "text": "b"})
    assert "heading" in notes.validate({"file": "DECISIONS.md", "heading": "", "text": "b"})
    assert "over 10" in notes.validate({"file": "DECISIONS.md", "heading": "a", "text": "b" * 11}, 10)
    for secret in ("token = abcdefghijklmnop123456", "sk-AAAAAAAAAAAAAAAAAAAAAAAAAAAA", "AKIAABCDEFGHIJKLMNOP",
                   "Authorization: Bearer abcdefghijklmnopqrstuvwxyz"):
        assert "secret" in notes.validate({"file": "DECISIONS.md", "heading": "a", "text": secret})
    # a git sha is not a secret
    assert notes.validate({"file": "DECISIONS.md", "heading": "a",
                           "text": "commit 1b92595c0ffee1b92595c0ffee1b92595c0ffee1"}) == ""
    # appends: under an existing heading (table rows join the table), or a new section
    base = layout.TEMPLATES["ASSETS.md"].format(name="g")
    out = notes.append_to_section(base, "Registry", "| Wolf | p | T2 | imported | ok |")
    assert out.rstrip().endswith("|---|---|---|---|---|\n| Wolf | p | T2 | imported | ok |")
    out2 = notes.append_to_section(out, "Registry", "| Bear | p | T3 | planned | |")
    assert out2.count("\n| ") == 3 and out2.index("| Wolf") < out2.index("| Bear")
    out3 = notes.append_to_section(out2, "Rules", "Never commit .blend files.")
    assert out3.endswith("## Rules\n\n- Never commit .blend files.\n")
    assert notes.replace_section(out3, "Rules", "- Replaced.").endswith("## Rules\n\n- Replaced.\n")
    assert notes.replace_section(out3, "Nope", "x") is None
    crlf = "# T\r\n\r\n## A\r\n\r\n- one\r\n"
    assert notes.append_to_section(crlf, "A", "two") == "# T\r\n\r\n## A\r\n\r\n- one\r\n- two\r\n"


def test_session_notes_are_committed_by_the_coordinator_and_indexed(tmp_path):
    root, remote = make_repo(tmp_path, "game", [T1, T2])
    prof = write_profile(tmp_path, "game", root)

    def script(ctx):
        return {"writes": {"src/a.txt": "OK\n"}, "commit": "T1: work",
                "final": ('```coordinator-report\n{"status": "DONE", "summary": "did it"}\n```\n'
                          'MEMORY_NOTE: file=DECISIONS.md heading="T1: text files hold OK" '
                          'text="src/a.txt holds OK; verify.py reads it (T1)."\n'
                          'MEMORY_NOTE: file=ASSETS.md heading=Registry '
                          'text=| a.txt | src/a.txt | T1 | verified | the first file |\n'
                          'MEMORY_NOTE: file=README.md heading=x text=not a memory file\n'
                          'MEMORY_NOTE: file=KNOWN_ISSUES.md heading=Leak text="api_key = '
                          'abcdefghijklmnopqrstuvwxyz0123"\n')}
    c = coordinator(tmp_path, [prof], {"game": script}, foundry={"game": FakeFoundry()})
    P = c.projects["game"]
    c.memory.init(P)
    c.approve("game", "T1", [], "victor")
    assert run_until(c, lambda: c.store.task("game", "T1")["state"] == S.COMPLETE)
    dec = (root / "docs" / "memory" / "DECISIONS.md").read_text(encoding="utf-8")
    assert "## T1: text files hold OK\n\n- src/a.txt holds OK; verify.py reads it (T1).\n" in dec
    assets = (root / "docs" / "memory" / "ASSETS.md").read_text(encoding="utf-8")
    assert "|---|---|---|---|---|\n| a.txt | src/a.txt | T1 | verified | the first file |\n" in assets
    assert "Leak" not in (root / "docs" / "memory" / "KNOWN_ISSUES.md").read_text(encoding="utf-8")
    log = git(root, "log", "--format=%s", "-8").splitlines()
    mem = [m for m in log if m.startswith("T1: memory notes from session")]
    assert len(mem) == 1 and "ASSETS.md, DECISIONS.md" in mem[0]
    t = c.store.task("game", "T1")
    sha = git(root, "log", "--format=%H", "--grep=memory notes", "-1")
    assert sha in t["data"]["coord_commits"]                     # a coordinator commit, not the agent's
    rows = c.memory.notes(P, "T1")
    by = {(r["file"], r["heading"]): r for r in rows}
    assert by[("DECISIONS.md", "T1: text files hold OK")]["status"] == "committed"
    assert by[("ASSETS.md", "Registry")]["sha"] == sha
    assert by[("README.md", "x")]["status"] == "rejected" and "unknown memory file" in by[("README.md", "x")]["detail"]
    assert by[("KNOWN_ISSUES.md", "Leak")]["status"] == "rejected" and "secret" in by[("KNOWN_ISSUES.md", "Leak")]["detail"]
    kinds = [e["kind"] for e in c.store.events("game", 200)]
    assert kinds.count("memory_note_rejected") == 2 and "memory_notes_committed" in kinds
    assert [e["heading"] for e in c.memory.entries(P, "DECISIONS.md")] == ["T1: text files hold OK"]
    # the same report polled again never applies the notes twice
    class St:
        final_text = script({})["final"]
    assert c.memory.apply_notes(P, {"task_id": "T1", "session_id": t["session_id"]}, St()) == []
    assert len(git(root, "log", "--format=%s", "--grep=memory notes").splitlines()) == 1


# -- chat edits ---------------------------------------------------------------------

def test_add_and_edit_commit_with_the_memory_prefix(game):
    tmp, root, prof, c = game
    P = c.projects["game"]
    c.memory.init(P)
    out = c.memory.add(P, "CONVENTIONS.md", "Commits", "Commit by explicit path, never git add -A.",
                       "victor via operator chat")
    assert out["ok"] and out["sha"] and out["path"] == "docs/memory/CONVENTIONS.md"
    assert git(root, "log", "-1", "--format=%s").startswith("MEMORY: memory add to CONVENTIONS.md > Commits")
    out2 = c.memory.add(P, "sprints/T2.md", "Plan notes", "The wolf has four legs.", "victor")
    assert out2["ok"] and (root / "docs" / "memory" / "sprints" / "T2.md").is_file()
    assert c.memory.entries(P, "sprints/T2.md")[0]["text"] == "- The wolf has four legs."
    ed = c.memory.edit(P, "CONVENTIONS.md", "Commits", "- Commit by explicit path.\n- Prefix with the task id.",
                       "victor")
    assert ed["ok"] and len(c.memory.entries(P, "CONVENTIONS.md")) == 2
    assert not c.memory.edit(P, "CONVENTIONS.md", "Nope", "x", "victor")["ok"]
    assert not c.memory.edit(P, "sprints/T9.md", "x", "x", "victor")["ok"]      # does not exist
    bad = c.memory.add(P, "DECISIONS.md", "Keys", "password = hunter2hunter2hunter2", "victor")
    assert not bad["ok"] and "secret" in bad["error"]
    same = c.memory.edit(P, "CONVENTIONS.md", "Commits", "- Commit by explicit path.\n- Prefix with the task id.",
                         "victor")
    assert same["ok"] and same["unchanged"]


# -- import -------------------------------------------------------------------------

STATE = textwrap.dedent("""\
    # Game — Build State

    **This is the handoff file.**

    ## Sprint ledger

    | Sprint | Title | Status | Date | Commit | Summary |
    |---|---|---|---|---|---|
    | S00 | Repo scaffold | COMPLETE | 2026-08-28 | (first commit) | docs/progress/S00_SUMMARY.md |
    | S01 | Toolchain & Rojo sync | COMPLETE | 2026-08-28 | 1b92595 | Rojo serves src/; stylua on. |
    | T1 | Make the thing | IN PROGRESS | 2026-10-20 | | started |

    *(Append one row per sprint.)*

    ## Current API surface

    ### Shared

    - `ReplicatedStorage.Shared.Util.Signal` (S02) — `.new() -> sig`; `sig:Connect(fn)`.
    - `ReplicatedStorage.Shared.Util.Trove` (S02) — `.new() -> trove`.

    ### Server services

    - `MatchService` (S06) — `Start()`, `Stop()`.

    ## Reference files

    - see docs/progress/state/
    """)
STATE_DECISIONS = textwrap.dedent("""\
    # Decisions made during the build

    > Split out of STATE.md.

    | Sprint | Question | Decision |
    |---|---|---|
    | S01 | Where does ProfileService install? | Under [server-dependencies], mapped to ServerScriptService.ServerPackages. |
    | S03 | How are round-trip tests run? | In a Studio PLAY session via eval_server_runtime; never in edit mode. |
    """)
STATE_KNOWN = textwrap.dedent("""\
    # Known issues

    > Split out of STATE.md.

    ## Rojo

    - Rojo 7.4 drops the plugin connection on a sourcemap rebuild; reconnect by hand.

    ## Studio

    - execute_luau has no server VM in edit mode.
    """)


def test_import_state_folds_ledger_decisions_known_issues_and_operator_notes(tmp_path):
    root, remote = make_repo(tmp_path, "game", [T1, T2], extra_files={
        "docs/progress/STATE.md": STATE, "docs/progress/state/DECISIONS.md": STATE_DECISIONS,
        "docs/progress/state/KNOWN_ISSUES.md": STATE_KNOWN})
    prof = write_profile(tmp_path, "game", root)
    c = coordinator(tmp_path, [prof], {"game": write_ok("src/a.txt")})
    P = c.projects["game"]
    c.store.add_operator_message("act-1", "game", "T1", "", "task", "next_session", "victor",
                                 "Keep the OK on the first line.")
    c.store.mark_operator_messages([c.store.operator_message("act-1")["msg_id"]], "delivered", "s1")
    state_before = (root / "docs" / "progress" / "STATE.md").read_bytes()
    out = c.memory.import_state(P, "victor")
    assert out["sha"] and not out.get("error"), out
    assert {"docs/memory/sprints/S00.md", "docs/memory/sprints/S01.md", "docs/memory/sprints/T1.md",
            "docs/memory/DECISIONS.md", "docs/memory/CONVENTIONS.md",
            "docs/memory/KNOWN_ISSUES.md"} <= set(out["written"])
    mem = root / "docs" / "memory"
    s01 = (mem / "sprints" / "S01.md").read_text(encoding="utf-8")
    assert "## Ledger (imported from docs/progress/STATE.md)" in s01
    assert "- Toolchain & Rojo sync — Status COMPLETE, 2026-08-28, commit 1b92595. Rojo serves src/; stylua on." in s01
    t1 = (mem / "sprints" / "T1.md").read_text(encoding="utf-8")
    assert "## Operator notes" in t1 and "victor] Keep the OK on the first line." in t1
    conv = (mem / "CONVENTIONS.md").read_text(encoding="utf-8")
    assert "## API surface: Shared" in conv and "## API surface: Server services" in conv
    assert "`MatchService` (S06)" in conv
    dec = (mem / "DECISIONS.md").read_text(encoding="utf-8")
    assert "## S01: Where does ProfileService install" in dec
    assert "- Decided (S01): Under [server-dependencies]" in dec and "- Question: Where does" in dec
    known = (mem / "KNOWN_ISSUES.md").read_text(encoding="utf-8")
    assert known.index("\n## Rojo\n") < known.index("\n## Resolved\n") and "sourcemap rebuild" in known
    assert "<!-- memory import: docs/progress/STATE.md" in dec
    # one coordinator commit, the index knows everything, STATE.md itself untouched
    assert git(root, "log", "-1", "--format=%s").startswith("MEMORY: import STATE.md ledger")
    assert git(root, "status", "--porcelain") == ""
    assert (root / "docs" / "progress" / "STATE.md").read_bytes() == state_before
    ents = c.memory.entries(P)
    assert any(e["file"] == "sprints/S01.md" and "S01" in e["tags"] for e in ents)
    assert c.memory.search(P, "ProfileService")[0]["file"] == "DECISIONS.md"
    # a T1 session now gets its ledger row, its operator note and the matching decision
    sl = c.memory.slice(P, P.adapter.describe("T1"))
    files = {e["file"] for e in sl["entries"]}
    assert "sprints/T1.md" in files
    # idempotent
    assert "skipped" in c.memory.import_state(P)


# -- control channel: typed actions and the router snapshot --------------------------

class MemFoundry:
    url = "inproc://foundry"

    def __init__(self):
        self.actions, self.reports, self.batches, self.beats = [], [], [], []

    def heartbeat(self, p):
        self.beats.append(p)
        return {}

    def claim_actions(self, source_id, projects, limit=20):
        out, self.actions = self.actions, []
        return out

    def action_status(self, action_id, source_id, status, detail, result=None):
        self.reports.append((action_id, status, detail, result))

    def telemetry(self, batch):
        self.batches.append(batch)
        return {"ok": True}


def test_control_memory_actions_commit_and_the_snapshot_reaches_the_router(game):
    tmp, root, prof, c = game
    P = c.projects["game"]
    c.memory.init(P)
    _mem(root, "DECISIONS.md", DECISIONS)
    c.memory.reindex(P)
    ff = MemFoundry()
    ch = ControlChannel(c, foundry=ff, source_id="coordinator:test")
    beat = ch._beat()
    assert "memory_add" in beat["source"]["capabilities"]["actions"] and "project_memory" in beat["features"]
    ch.poll()
    assert len(ff.batches) == 1
    obs = ff.batches[0]["observations"][0]
    assert obs["type"] == "memory" and obs["project_id"] == "game"
    assert {e["heading"] for e in obs["entries"]} >= {"Rojo: one serve", "Audio: loudness"}
    assert {f["file"] for f in obs["files"]} >= {"DECISIONS.md", "ASSETS.md"}
    ch.poll()
    assert len(ff.batches) == 1                                  # unchanged: not re-sent
    ff.actions = [{"action_id": "act-m1", "kind": "memory_add", "project_id": "game", "task_id": "",
                   "actor": "victor", "payload": {"file": "KNOWN_ISSUES.md", "heading": "Rojo",
                                                 "text": "Rojo drops the plugin on a sourcemap rebuild."}},
                  {"action_id": "act-m2", "kind": "memory_edit", "project_id": "game", "task_id": "",
                   "actor": "victor", "payload": {"file": "DECISIONS.md", "heading": "Nope", "text": "x"}},
                  {"action_id": "act-m3", "kind": "memory_add", "project_id": "game", "task_id": "",
                   "actor": "victor", "payload": {"file": "DECISIONS.md", "heading": "Keys",
                                                 "text": "token = abcdefghijklmnopqrstuvwxyz"}}]
    ch.poll()
    rep = {r[0]: r for r in ff.reports}
    assert rep["act-m1"][1] == "applied" and rep["act-m1"][3]["sha"]
    assert "docs/memory/KNOWN_ISSUES.md > Rojo: committed" in rep["act-m1"][2]
    assert rep["act-m2"][1] == "rejected" and "no heading" in rep["act-m2"][2]
    assert rep["act-m3"][1] == "rejected" and "secret" in rep["act-m3"][2]
    known = (root / "docs" / "memory" / "KNOWN_ISSUES.md").read_text(encoding="utf-8")
    assert "## Rojo\n\n- Rojo drops the plugin on a sourcemap rebuild.\n" in known
    assert git(root, "log", "-1", "--format=%s").startswith("MEMORY: memory add to KNOWN_ISSUES.md > Rojo")
    assert len(ff.batches) == 2                                  # the change was published
    snap = ff.batches[-1]["observations"][0]
    assert any(e["file"] == "KNOWN_ISSUES.md" and e["heading"] == "Rojo" for e in snap["entries"])
    assert snap["notes"][0]["status"] == "committed"
    # a replayed action reports the stored outcome, never re-applies
    ff.actions = [{"action_id": "act-m1", "kind": "memory_add", "project_id": "game", "task_id": "",
                   "actor": "victor", "payload": {"file": "KNOWN_ISSUES.md", "heading": "Rojo",
                                                 "text": "Rojo drops the plugin on a sourcemap rebuild."}}]
    ch.poll()
    assert known == (root / "docs" / "memory" / "KNOWN_ISSUES.md").read_text(encoding="utf-8")


def test_lead_and_review_prompts_carry_memory(game):
    tmp, root, prof, c = game
    P = c.projects["game"]
    c.memory.init(P)
    _mem(root, "KNOWN_ISSUES.md", KNOWN)
    c.memory.reindex(P)
    spec = P.adapter.describe("T1")
    note = c._memory_review_note(P, spec)
    assert note.startswith("project memory (") and "src/a.txt must end with a newline" in note
    assert "Blender" not in note
    lead = c.memory.prompt_block(P, "T1", "", role="lead")
    assert "## Project memory (T1, lead)" in lead and "src/a.txt must end with a newline" in lead
    assert slices.compact([], 100) == ""
