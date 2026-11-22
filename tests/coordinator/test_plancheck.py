"""The plan check at a sprint's start (plancheck.py; item 22 of
COORDINATOR_AUTOMATION_NEEDS): wrong folders, sprints that do not exist, a run step that
states another difficulty or seed than its table row."""

from __future__ import annotations

import json
import os

from foundry_coordinator import automation, plancheck
from foundry_coordinator import states as S
from tests.coordinator.test_budget_policy import game, task

RUNS = """# BAL-2e9

| Id | Persona x players | Length | Difficulty | Seed | Extra option | What it decides |
|---|---|---|---|---|---|---|
| `CQb` | Conqueror x4 | Full | Normal | 737125272 (b) | - | R4 |
| `CQc` | Conqueror x4 | Full | Normal | 31415926 (c) | - | R4 |
| `VTa` | Veteran x4 | Full | Hard | 20261016 (a) | - | R6 |
| `B4b` | Builder x4 | Skirmish | Normal | 737125272 (b) | - | R5 |

- [ ] **CQb:** Conqueror x4, Full, Normal, seed b (737125272). S1 first, then the recipe.
- [ ] **CQc:** Conqueror x4, Full, Normal, seed c (20261016). S1 first.
- [ ] **VTa (later):** Veteran x4, Full, Normal, seed a (20261016).
- [x] **B4b:** (done: Victory) Builder x4, Full, Normal, seed b (737125272).

```json
{"runs": [{"id": "CQb", "seed": 737125272, "difficulty": "normal"},
          {"id": "VTa", "seed": 20261016, "difficulty": "normal"}]}
```
"""


def test_replay_a_run_step_that_copied_the_wrong_difficulty_and_seed():
    """A new BAL plan copied the wrong difficulty and seeds into its steps."""
    got = [f["text"] for f in plancheck.check_runs(RUNS)]
    assert got == [
        "CQc: the table says seed 31415926, its step says 20261016",
        "VTa: the table says difficulty Hard, its step says normal",
        "VTa: the table says difficulty hard, a JSON block says normal",
        "B4b: the table says length Skirmish, its step says full",
    ]
    # a plan with no run table, or a table without the judged columns, says nothing
    assert plancheck.check_runs("# X\n\n| Id | Note |\n|---|---|\n| a | b |\n") == []
    assert plancheck.check_runs("no tables here") == []


def workspace(tmp_path):
    c, P, clk = game(tmp_path)
    ws = P.profile.workspace
    for d in ("docs/evidence/FIN-0/map", "docs/progress", "src/server"):
        os.makedirs(os.path.join(ws, *d.split("/")), exist_ok=True)
    for f in ("docs/evidence/FIN-0/map/overview.png", "src/server/Loader.lua",
              "docs/progress/FIN-5a1_PLAN.md", "docs/progress/FIN-4a_PLAN.md"):
        with open(os.path.join(ws, *f.split("/")), "w", encoding="utf-8") as fh:
            fh.write("x")
    return c, P, clk, ws


PLAN = """# T1: the map pass

**Depends on:** FIN-5a (the den), FIN-4a, FIN-3b.

Read `docs/evidence/FIN/map/overview.png` and `src/server/Loader.lua` first. Write
`docs/evidence/T1/after.png`, `docs/progress/T1_SUMMARY.md` and `src/server/NewThing.lua`.
The report goes to `docs/progress/PT_REPORTS/T1_<stamp>.md`; see `https://x/y` and `a/b`.
Also `src/server/Gone.lua` and `src/server/loader.lua`.
"""


def test_replay_a_wrong_folder_and_a_sprint_that_does_not_exist(tmp_path):
    """FIN plans pointed at docs/evidence/FIN/map/ for FIN-0/map/, and named FIN-5a (it is
    FIN-5a1) and FIN-3b (never existed)."""
    c, P, clk, ws = workspace(tmp_path)
    paths = [f["text"] for f in plancheck.check_paths(PLAN, "T1", ws, ["src/server/NewThing.lua"])]
    assert paths[0] == ("`docs/evidence/FIN/map/overview.png` does not exist; "
                        "docs/evidence/FIN-0/map/overview.png exists")
    assert "`src/server/Gone.lua` does not exist" in paths[1]
    # its own outputs, its file list, placeholders, URLs and non-repo paths are not judged
    assert len(paths) == (3 if os.path.normcase("A") != os.path.normcase("a") else 2)
    sprints = [f["text"] for f in plancheck.check_sprints(c, P, PLAN, "T1")]
    assert sprints == ["FIN-5a: no such sprint; did you mean FIN-5a1?", "FIN-3b: no such sprint"]


def test_at_start_warns_once_per_plan_version_and_blocks_only_when_asked(tmp_path):
    c, P, clk, ws = workspace(tmp_path)
    with open(os.path.join(ws, "docs", "progress", "T1_PLAN.md"), "w", encoding="utf-8") as f:
        f.write(PLAN)
    t = task(c)
    assert plancheck.at_start(c, P, t) == ""                  # warn: the sprint starts
    [e] = [json.loads(r["detail"]) for r in c.store.q(
        "SELECT detail FROM events WHERE kind='plan_check'")]
    assert e["mode"] == "warn" and e["count"] >= 4 and e["plan"] == "docs/progress/T1_PLAN.md"
    assert e["text"].startswith("docs/progress/T1_PLAN.md: ") and "FIN-5a: no such sprint" in \
        " ".join(e["findings"])
    # the same plan again: nothing new is written
    assert plancheck.at_start(c, P, task(c)) == ""
    assert c.store.q("SELECT COUNT(*) AS n FROM events WHERE kind='plan_check'")[0]["n"] == 1
    # block: the engine stops the start with the findings, from the remembered check
    automation.set_live(P, "plan_check", "block")
    c._start_implementation(P, task(c))
    t = task(c)
    assert t["state"] == S.BLOCKED and "unblock" in t["blocker"]
    assert t["blocker"].startswith("plan check (docs/progress/T1_PLAN.md): ")
    # once per plan version: after an unblock the same plan starts
    assert plancheck.at_start(c, P, task(c)) == ""
    # the plan is fixed: the check runs again and passes
    with open(os.path.join(ws, "docs", "progress", "T1_PLAN.md"), "w", encoding="utf-8") as f:
        f.write("# T1\n\n**Depends on:** FIN-4a.\n\nRead `src/server/Loader.lua`.\n")
    assert plancheck.at_start(c, P, task(c)) == ""
    assert task(c)["data"]["plan_check"]["findings"] == []
    # off: not even read
    automation.set_live(P, "plan_check", "off")
    with open(os.path.join(ws, "docs", "progress", "T1_PLAN.md"), "w", encoding="utf-8") as f:
        f.write(PLAN)
    assert plancheck.at_start(c, P, task(c)) == ""
    assert c.store.q("SELECT COUNT(*) AS n FROM events WHERE kind='plan_check'")[0]["n"] == 1


def test_a_sprint_without_a_plan_file_or_a_check_that_breaks_never_stops_a_start(tmp_path,
                                                                                monkeypatch):
    c, P, clk, ws = workspace(tmp_path)
    assert plancheck.at_start(c, P, task(c)) == ""            # no plan file
    with open(os.path.join(ws, "docs", "progress", "T1_PLAN.md"), "w", encoding="utf-8") as f:
        f.write(PLAN)
    automation.set_live(P, "plan_check", "block")
    monkeypatch.setattr(plancheck, "check_paths", lambda *a: 1 / 0)
    monkeypatch.setattr(plancheck, "check_sprints", lambda *a: 1 / 0)
    assert plancheck.at_start(c, P, task(c)) == ""            # parts failed: nothing found


def test_the_settings_are_in_the_schema():
    import pytest

    from foundry_automation import schema
    f = schema.find_field("automation.plan_check")
    assert f is not None and list(f.enum) == ["warn", "block", "off"]
    assert schema.AUTOMATION_DEFAULTS["plan_check"] == "warn"
    assert schema.AUTOMATION_DEFAULTS["push_chunk_mb"] == 1500
    assert schema.AUTOMATION_DEFAULTS["count_pins"] == []
    for k in ("plan_check", "push_chunk_mb", "count_pins"):
        assert schema._AUTO_DESC[k] and schema.find_field(f"automation.{k}") is not None
    ok = schema.automation_table("x", {"count_pins": [r"a.lua :: n = (\d+) :: src/*.lua"]})
    assert len(ok["count_pins"]) == 1
    for bad in (["a.lua :: no group :: src/*.lua"], ["just text"], [r"a :: ( :: b"]):
        with pytest.raises(schema.SettingsError):
            schema.automation_table("x", {"count_pins": bad})
    with pytest.raises(schema.SettingsError):
        schema.automation_table("x", {"plan_check": "loud"})
