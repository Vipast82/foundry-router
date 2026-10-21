"""Revamp B13: memory blocks reach a session once (then only what changed), and
the per-tick coordinator checks are reused while nothing they read changed."""

from tests.coordinator.helpers import coordinator, git, make_repo, write_profile

TASK = {"id": "T1", "title": "t", "spec": "s", "scope": ["src/a.txt"]}


def env(tmp_path, extra=""):
    root, _ = make_repo(tmp_path, "game", [TASK], extra_files={"src/a.txt": "x\n",
                                                                "notes.txt": "keep\n"})
    prof = write_profile(tmp_path, "game", root, extra=extra)
    c = coordinator(tmp_path, [prof], {"game": lambda ctx: {"final": ""}})
    c.approve("game", "T1", [], "victor")
    return c, c.projects["game"], root


MEM = "## Ledger\n- au.gate PASS at abc\n\n## Decisions\n- dec-1: use layout B"


def test_memory_reaches_a_session_once_then_only_changes(tmp_path):
    c, P, _ = env(tmp_path)
    first = c._memory_delta(P, "T1", "s1", MEM)
    assert first == MEM                                     # the first prompt: everything
    c._notes_delivered([], "s1")                            # Kilo accepted it
    assert c._memory_delta(P, "T1", "s1", MEM) == ""        # unchanged: nothing resent
    c._notes_delivered([], "s1")
    changed = MEM.replace("dec-1: use layout B", "dec-1: use layout B\n- dec-2: drop S46")
    out = c._memory_delta(P, "T1", "s1", changed)
    assert "dec-2: drop S46" in out and "au.gate PASS" not in out
    assert "changed since your last prompt" in out
    # another session gets the whole memory
    assert c._memory_delta(P, "T1", "s2", changed) == changed


def test_a_refused_prompt_does_not_count_as_delivered(tmp_path):
    c, P, _ = env(tmp_path)
    assert c._memory_delta(P, "T1", "s1", MEM) == MEM
    c._notes_requeue([])                                    # the prompt was refused
    assert c._memory_delta(P, "T1", "s1", MEM) == MEM


def test_the_delta_can_be_turned_off(tmp_path):
    c, P, _ = env(tmp_path, extra="\n[automation]\nmemory_delta = false\n")
    assert c._memory_delta(P, "T1", "s1", MEM) == MEM
    c._notes_delivered([], "s1")
    assert c._memory_delta(P, "T1", "s1", MEM) == MEM


def test_generic_checks_are_reused_until_something_they_read_changes(tmp_path):
    c, P, root = env(tmp_path)
    c.tick()
    t = c.store.task("game", "T1")
    spec = P.adapter.describe("T1")
    calls = []
    real = P.ws.commits
    P.ws.commits = lambda *a: (calls.append(a), real(*a))[1]
    head = P.ws.head()
    t = {**t, "base_sha": head, "data": {**(t["data"] or {}), "task_base": head}}
    a = c._generic_checks(P, t, spec, head, phase="verify")
    b = c._generic_checks(P, t, spec, head, phase="verify")
    assert len(calls) == 1 and [r.check for r in a] == [r.check for r in b]
    (root / "src" / "a.txt").write_text("OK\n")             # the working tree changed
    c._generic_checks(P, t, spec, head, phase="verify")
    assert len(calls) == 2
    git(root, "commit", "-qam", "T1: ok")                   # HEAD moved
    c._generic_checks(P, t, spec, P.ws.head(), phase="verify")
    assert len(calls) == 3
    c.clock = lambda: 10 ** 12                               # too old: recomputed
    c._generic_checks(P, t, spec, P.ws.head(), phase="verify")
    assert len(calls) == 4
