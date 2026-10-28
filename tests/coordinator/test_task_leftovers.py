"""2026-10-27 22:33: GF-GATE1's session died mid-work (context overflow) and left its own
untracked docs/progress/GF_GATE1_hashes.json. The next session started with it in the
"pre-existing dirty" baseline, updated it, and coordinator.unrelated_preserved failed
("pre-existing uncommitted changes were modified") -> a decision and a lead case. A file
the task's own earlier session left is task work; work dirty before the task began is
still preserved byte for byte."""

import json
import sys

REGEN = "open('docs/progress/T1_hashes.json', 'w').write('{\"a\": 2}')"

from foundry_coordinator import states as S
from foundry_coordinator.profile import Profile
from tests.coordinator.test_lead_e2e import Clock
from tests.coordinator.test_revamp_infra import FlakyKilo, env

NOT_DONE = "```coordinator-report\n" + json.dumps(
    {"status": "NOT DONE", "summary": "half", "next": "finish"}) + "\n```"
DONE = "```coordinator-report\n" + json.dumps({"status": "DONE", "summary": "ok"}) + "\n```"


def _checks(c, tid="T1"):
    t = c.store.task("game", tid)
    return {v["check_name"]: v for v in c.store.verification(t["verification_id"], "game", tid)}


def test_a_file_the_tasks_own_earlier_session_left_is_task_work(tmp_path):
    clock = Clock()
    n = []

    def script(ctx):
        n.append(1)
        if len(n) == 1:      # dies leaving its own untracked evidence file (no commit)
            return {"writes": {"docs/progress/T1_hashes.json": "{\"a\": 1}\n"},
                    "error": "ContextOverflowError: Compaction exhausted"}
        # the next session regenerates it (left untracked, like the real one) and
        # commits its code
        return {"run": [[sys.executable, "-c", REGEN]], "writes": {"src/a.txt": "OK\n"},
                "commit": "T1: ok", "final": DONE}
    k = FlakyKilo(script, down=False)
    c = env(tmp_path, k, clock)
    for _ in range(30):
        c.tick()
        clock.advance(61)
    t = c.store.task("game", "T1")
    assert t["state"] == S.COMPLETE, (t["state"], t["blocker"], t["next_action"])
    first = json.loads(c.store.q("SELECT detail FROM events WHERE kind='verification' "
                                 "ORDER BY rowid LIMIT 1")[0]["detail"])
    assert first["results"]["coordinator.unrelated_preserved"] == "PASS"


def test_work_dirty_before_the_task_began_is_still_preserved(tmp_path):
    clock = Clock()

    import sys
    edit = "open('docs/progress/T1_hashes.json', 'w').write('{\"mine\": 2}')"

    def script(ctx):      # edits the owner's uncommitted file without committing it
        return {"run": [[sys.executable, "-c", edit]], "writes": {"src/a.txt": "OK\n"},
                "commit": "T1: ok", "final": DONE}
    k = FlakyKilo(script, down=False)
    c = env(tmp_path, k, clock)
    root = tmp_path / "game"
    if not root.exists():
        root = next(p for p in tmp_path.iterdir() if (p / ".git").exists())
    (root / "docs" / "progress").mkdir(parents=True, exist_ok=True)
    (root / "docs" / "progress" / "T1_hashes.json").write_text("{\"owner\": 1}\n")
    for _ in range(12):
        c.tick()
        clock.advance(61)
    first = json.loads(c.store.q("SELECT detail FROM events WHERE kind='verification' "
                                 "ORDER BY rowid LIMIT 1")[0]["detail"])
    assert first["results"]["coordinator.unrelated_preserved"] == "FAIL"


def test_task_patterns_match_the_underscore_form_of_the_id():
    p = Profile(id="au", name="AU", version="1", path="x.toml", hash="h", workspace=".",
                allowed_dirs=[], branch="master", remote="origin",
                raw={}, closeout={"exempt_paths": ["docs/progress/{task}_*", "docs/STATE.md"]})
    assert p.exempt("GF-GATE1") == ["docs/progress/GF-GATE1_*", "docs/progress/GF_GATE1_*",
                                    "docs/STATE.md"]
    assert p.exempt("T1") == ["docs/progress/T1_*", "docs/STATE.md"]
