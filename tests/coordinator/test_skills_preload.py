"""Skill pages are put into implement/repair prompts (v0.181.23). 2026-11-02: about 1 in 7 AU
implement sessions loaded any skill; repair, hand-off and local-model sessions loaded none, and
the 27 roblox-* skills installed that day were never opened, because AU's condensed brief drops
the plan's Skills section."""
import os
from types import SimpleNamespace

from foundry_coordinator import skills_preload as SP
from tests.coordinator.helpers import coordinator, make_repo, run_until, write_profile
from tests.coordinator.test_engine import write_ok

PLAN = """# S1
## Goal
Build it.
## Skills
Load `au-base`, then `world-a` and `big-one`; routing in `.kilo/rules/x.md`, all under `roblox-*`,
and `not-installed`.
## Acceptance
- PASS: ok
"""


def _skill(root, name, body, desc="What it is for, long enough to read."):
    d = os.path.join(root, ".kilo", "skills", name)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "SKILL.md"), "w", encoding="utf-8") as f:
        f.write(f"---\nname: {name}\ndescription: {desc}\n---\n{body}\n")
    return d


class _C:
    class tracks:                                                # noqa: N801
        @staticmethod
        def main_of(P):
            return P


def _P(ws, **skills):
    return SimpleNamespace(id="game", profile=SimpleNamespace(workspace=str(ws),
                                                              raw={"skills": skills}))


def test_the_plan_skills_section_names_skills_not_globs_or_paths():
    assert SP.plan_skills(PLAN) == ["au-base", "world-a", "big-one", "not-installed"]
    assert SP.plan_skills("## Goal\n`x-y`\n") == []


def test_the_block_carries_the_pages_within_the_budget(tmp_path):
    _skill(tmp_path, "au-base", "BASE RULES")
    os.makedirs(_skill(tmp_path, "world-a", "WORLD RULES") + "/references")
    _skill(tmp_path, "big-one", "X" * 3000)
    P = _P(tmp_path, preload_always=["au-base"], preload_max_kb=2)
    spec = SimpleNamespace(spec="", data={"skills": SP.plan_skills(PLAN)})
    text, d = SP.block(_C, P, spec)
    assert d["loaded"] == ["au-base", "world-a"] and d["over_budget"] == ["big-one"]
    assert d["missing"] == ["not-installed"]
    assert "BASE RULES" in text and "WORLD RULES" in text and "X" * 100 not in text
    assert "name: au-base" not in text and "What it is for" in text       # frontmatter folded
    assert ".kilo/skills/world-a/references/" in text and "`big-one`" in text
    off = _P(tmp_path, preload=False, preload_always=["au-base"])
    assert SP.block(_C, off, spec) == ("", {"loaded": [], "over_budget": [], "missing": [],
                                            "bytes": 0})


def test_every_implement_session_gets_the_pages_without_choosing_to(tmp_path):
    t1 = {"id": "T1", "title": "Make the thing", "spec": "Write src/a.txt.\n" + PLAN,
          "scope": ["src/a.txt"]}
    root, _ = make_repo(tmp_path, "game", [t1])
    _skill(root, "au-base", "BASE RULES")
    _skill(root, "world-a", "WORLD RULES")
    prof = write_profile(tmp_path, "game", root,
                         extra='[skills]\npreload_always = ["au-base"]\n')
    c = coordinator(tmp_path, [prof], {"game": write_ok("src/a.txt")})
    c.approve("game", "T1", [], "victor")
    assert run_until(c, lambda: any(s["prompts"] for s in c.projects["game"].client.sessions
                                     .values()))
    first = next(s["prompts"][0] for s in c.projects["game"].client.sessions.values()
                 if s["prompts"])
    assert "## Skills (preloaded by the coordinator)" in first
    assert "BASE RULES" in first and "WORLD RULES" in first
    ev = [e for e in c.store.events("game") if e["kind"] == "skills_preloaded"]
    assert ev and ev[0]["detail"]["loaded"] == ["au-base", "world-a"]
