"""tracks/analysis.py: plan parsing in the Animal Uprising format (files list,
NEEDS (MCP) line, a `## Needs` block, Depends on, stories, suites), the needs
inference, file-overlap conflicts minus append-safe ledgers, ordered tracks and
the agent-count proposal. Deterministic, no model, no git."""

import os

import pytest

from foundry_coordinator.tracks import analysis as A

UI_PLAN = """# UI-03a — Hero build: Inventory, Skills, loot toast, Profile — PLAN

**Authorized:** Victor 2026-10-20.
**Tier:** B (client UI, shared config, tests).
File:line facts verified at `2a2534b`. Depends on UI-02a (router, T1/T3/T5/T8, `ButtonActions`-bound Button,
Rail/Menu) and the UI-D1 references.
**NEEDS (MCP):** ON git, Roblox_Studio, robloxstudio-mcp (live playtest + capture), roblox-docs. OFF comfy-mcp, stable-audio, acestep-music, Kokoro, blender-mcp.

## Goal
Rebuild the hero-build screens.

## Files this sprint may change
- `src/client/UI/v3/screens/Inventory/**`
- `src/client/UI/v3/stories/inventory.story.luau`
- `src/client/UI/v3/App.lua` (screen-registration rows only)
- `src/shared/Config/Strings.lua` (`HeroBuild` rows)
- `tests/RegressionIndex.lua` (add UI_03a)
- `docs/progress/STATE.md`
- `docs/MASTER_ORDER.md`
- `docs/evidence/UI-v3/UI-03a/**`

## Stories (fixture = key in `stories/fixtures/*.lua`)
- `inventory.loading` (`inv_loading`), `inventory.empty` (`inv_empty`)
- `toast.loot` (`loot_rare`)

## Affected suites (file intersection, verified by grep)
S48 (SkillTreeUi), S49, GF_UI1 / GF_UI1b (panel ids), UI_01 and UI_02a (Config/Ui, Strings).

## Done when
`GATE UI-03a PASS`.
"""

UI_NEXT_PLAN = """# UI-04a — Match end — PLAN
**Tier:** B
**Depends on:** UI-03a
**NEEDS (MCP):** ON git, Roblox_Studio, robloxstudio-mcp, roblox-docs. OFF blender-mcp.

## Files this sprint may change
- `src/client/UI/v3/screens/MatchEnd/**`
- `src/shared/Config/Strings.lua`
- `docs/progress/STATE.md`
"""

BLENDER_PLAN = """# AS-P4a — Boar block-out — PLAN
**Tier:** C · **Size:** one context.
**NEEDS (MCP):** git (baseline). blender-mcp **ON only for step 6b** (playing the clips); everything else runs Blender in background mode from PowerShell. OFF: comfy-mcp, audio servers, robloxstudio-mcp.

## Files this sprint may change
- `assets/source/boar/**`
- `assets/exports/boar.fbx`
- `docs/design/ASSET_LEDGER.md`
- `docs/progress/STATE.md`
- `docs/MASTER_ORDER.md`
"""

AUDIO_PLAN = """# AS-A1 — Ambient loops — PLAN
**Tier:** C

## Needs
- audio

## Files this sprint may change
- `audio/ambient/**`
- `docs/design/ASSET_LEDGER.md`
- `docs/progress/STATE.md`
"""

BACKEND_PLAN = """# GF-SEC3 — Rate limits — PLAN
**Tier:** A · **Depends on:** GF-SEC2 · **NEEDS (MCP):** ON git, roblox-docs; Roblox_Studio idle (no Studio work). OFF robloxstudio-mcp, comfy-mcp, blender-mcp.

## Files this sprint may change
- `src/server/Services/RateLimitService.lua`
- `tests/GF_SEC3_check.lua`
- `docs/progress/STATE.md`
- `docs/MASTER_ORDER.md`
"""

INFER_UI_PLAN = """# UI-Z — PLAN
## Files this sprint may change
- `src/client/UI/v3/screens/Z/**`
- `docs/progress/UI-Z_gate.json`
"""

INFER_BLEND_PLAN = """# AS-Z — PLAN
## Files this sprint may change
- `assets/source/z/model.blend`
"""

INFER_BACKEND_PLAN = """# S99 — PLAN
## Files this sprint may change
- `src/server/Services/Z.lua`
"""


def plans(**texts):
    return [A.parse_plan(tid, text) for tid, text in texts.items()]


def test_parse_the_au_plan_fields():
    p = A.parse_plan("UI-03a", UI_PLAN, "docs/progress/UI-03a_PLAN.md")
    assert p.title.startswith("UI-03a — Hero build")
    assert p.tier == "B"
    assert p.files[:3] == ["src/client/UI/v3/screens/Inventory/**",
                           "src/client/UI/v3/stories/inventory.story.luau",
                           "src/client/UI/v3/App.lua"]
    assert "docs/evidence/UI-v3/UI-03a/**" in p.files
    assert p.stories == ["inventory.loading", "inventory.empty", "toast.loot"]
    assert p.suites[:4] == ["S48", "S49", "GF_UI1", "GF_UI1b"]
    assert p.deps == ["UI-02a", "T1", "T3", "T5", "T8", "UI-D1"]
    assert p.needs == ["studio"] and p.needs_source == "mcp_line"


def test_needs_from_the_mcp_line_every_spelling():
    assert A.parse_plan("AS-P4a", BLENDER_PLAN).needs == ["blender"]
    assert A.parse_plan("GF-SEC3", BACKEND_PLAN).needs == ["backend"]      # Studio idle
    assert A.parse_mcp_line("- NEEDS (MCP): git, Roblox_Studio, roblox-docs, robloxstudio-mcp "
                            "(multi-client regression). Turn robloxstudio-mcp off at close.") == ["studio"]
    assert A.parse_mcp_line("**Tier:** A · **NEEDS MCP:** yes — Roblox Studio (`robloxstudio-mcp`"
                            " `start_stop_play`) · **Size:** two contexts") == ["studio"]
    assert A.parse_mcp_line("**NEEDS (MCP):** git, Roblox_Studio, roblox-docs (baseline). No "
                            "robloxstudio-mcp. Do NOT call comfy-mcp / stable-audio.") == ["studio"]
    # the real AS-P4a line: blender-mcp OFF, but Blender itself runs in background mode
    assert A.parse_mcp_line("**NEEDS (MCP):** git (baseline: git, Roblox_Studio idle, roblox-docs)."
                            " OFF: blender-mcp (Blender runs in background mode), comfy-mcp.") == ["blender"]
    assert A.parse_mcp_line("**NEEDS (MCP):** git (baseline: Roblox_Studio idle, roblox-docs). "
                            "OFF: blender-mcp, comfy-mcp.") == ["backend"]
    assert A.parse_mcp_line("no such line") is None


def test_needs_block_wins_and_implies_gpu_for_audio():
    p = A.parse_plan("AS-A1", AUDIO_PLAN)
    assert p.needs == ["audio", "gpu"] and p.needs_source == "block"
    assert A.parse_needs_block("## Needs\nnone\n") == ["none"]
    assert A.parse_needs_block("## Needs\n- studio\n- blender\n") == ["studio", "blender"]


def test_inference_when_nothing_is_declared():
    assert A.parse_plan("UI-Z", INFER_UI_PLAN).needs == ["studio"]
    assert A.parse_plan("AS-Z", INFER_BLEND_PLAN).needs == ["blender"]
    p = A.parse_plan("S99", INFER_BACKEND_PLAN)
    assert p.needs == ["backend"] and p.needs_source == "inferred"
    assert A.infer_needs(["audio/sfx/*.ogg"], []) == ["audio", "gpu"]
    assert A.infer_needs(["src/x.lua"], ["a.b"]) == ["studio"]


def test_file_overlap_rules():
    assert A.files_overlap("src/a.lua", "src/a.lua")
    assert not A.files_overlap("src/a.lua", "src/b.lua")
    assert A.files_overlap("src/client/UI/v3/App.lua", "src/client/UI/v3/**")
    assert A.files_overlap("src/client/UI/v3/**", "src/client/UI/v3/screens/X/**")
    assert not A.files_overlap("src/client/UI/v3/screens/A/**", "src/client/UI/v3/screens/B/**")
    assert A.files_overlap("tests/*.lua", "tests/S1_check.lua")
    assert not A.files_overlap("tests/*.lua", "src/x.lua")


def test_shared_ledgers_are_not_conflicts_but_strings_lua_is():
    ps = plans(**{"UI-03a": UI_PLAN, "UI-04a": UI_NEXT_PLAN, "AS-P4a": BLENDER_PLAN,
                  "AS-A1": AUDIO_PLAN, "GF-SEC3": BACKEND_PLAN})
    out = A.analyze(ps)
    names = [t["sprints"] for t in out["tracks"]]
    # UI-04a depends on UI-03a AND shares Strings.lua: one chain, ordered by dependency
    assert ["UI-03a", "UI-04a"] in names
    assert ["AS-P4a"] in names and ["AS-A1"] in names and ["GF-SEC3"] in names
    reasons = {(s["a"], s["b"]): s["reason"] for s in out["serial"]}
    assert reasons[("UI-04a", "UI-03a")] == "UI-04a depends on UI-03a"
    assert not any("STATE.md" in r or "MASTER_ORDER" in r or "ASSET_LEDGER" in r
                   for r in reasons.values())
    ui = next(t for t in out["tracks"] if "UI-03a" in t["sprints"])
    assert ui["needs"] == ["studio"] and ui["studio_at_merge"]
    assert next(t for t in out["tracks"] if "AS-A1" in t["sprints"])["needs"] == ["audio", "gpu"]
    assert out["tracks"][0]["kind"] == "main" and all(t["kind"] == "parallel"
                                                      for t in out["tracks"][1:])
    assert any("GF-SEC2" in w for w in out["warnings"])         # external dependency


def test_strings_lua_overlap_without_a_dependency_is_serial_with_the_file_named():
    other = UI_NEXT_PLAN.replace("**Depends on:** UI-03a\n", "")
    out = A.analyze(plans(**{"UI-03a": UI_PLAN, "UI-04a": other}))
    assert len(out["tracks"]) == 1 and out["tracks"][0]["sprints"] == ["UI-03a", "UI-04a"]
    s = out["serial"][0]
    assert s["files"] == ["src/shared/Config/Strings.lua"]
    assert "both change src/shared/Config/Strings.lua" in s["reason"]
    # widening append_safe makes them parallel
    out2 = A.analyze(plans(**{"UI-03a": UI_PLAN, "UI-04a": other}),
                     append_safe=A.DEFAULT_APPEND_SAFE + ["src/shared/Config/Strings.lua"])
    assert len(out2["tracks"]) == 2


def test_done_sprints_satisfy_deps_and_leave_the_tracks():
    ps = plans(**{"UI-03a": UI_PLAN, "UI-04a": UI_NEXT_PLAN})
    ps[0].done = True
    out = A.analyze(ps)
    assert out["done"] == ["UI-03a"] and out["open"] == ["UI-04a"]
    assert [t["sprints"] for t in out["tracks"]] == [["UI-04a"]]


def test_master_order_and_main_sprint_order_the_tracks():
    ps = plans(**{"UI-03a": UI_PLAN, "AS-P4a": BLENDER_PLAN, "GF-SEC3": BACKEND_PLAN})
    out = A.analyze(ps, order=["GF-SEC3", "AS-P4a", "UI-03a"])
    assert [t["sprints"][0] for t in out["tracks"]] == ["GF-SEC3", "AS-P4a", "UI-03a"]
    out = A.analyze(ps, order=["GF-SEC3", "AS-P4a", "UI-03a"], main_sprint="UI-03a")
    assert [t["sprints"][0] for t in out["tracks"]] == ["UI-03a", "GF-SEC3", "AS-P4a"]
    assert out["tracks"][0]["kind"] == "main"


def test_propose_cuts_to_max_agents_and_capacity_one_resources():
    ps = plans(**{"UI-03a": UI_PLAN, "AS-P4a": BLENDER_PLAN, "AS-A1": AUDIO_PLAN,
                  "GF-SEC3": BACKEND_PLAN, "AS-Z": INFER_BLEND_PLAN})
    out = A.analyze(ps, main_sprint="UI-03a")
    one = A.propose(out, 1)
    assert one["agents"] == 1 and one["tracks"] == [] and one["main"]["sprints"] == ["UI-03a"]
    three = A.propose(out, 3)
    assert three["agents"] == 3 and len(three["tracks"]) == 2
    chosen = {t["name"] for t in three["tracks"]}
    assert not ({"as-p4a", "as-z"} <= chosen)                   # two blender tracks never together
    assert any("over max_agents" in n for n in three["notes"])
    five = A.propose(out, 5)
    assert len(five["tracks"]) == 3 and any("would share blender" in n for n in five["notes"])
    running = A.propose(out, 5, running=["AS-A1"])
    assert all("AS-A1" not in t["sprints"] for t in running["tracks"])
    assert any("already running" in n for n in running["notes"])


def test_collect_plans_reads_a_workspace_and_render_prints(tmp_path):
    ws = tmp_path / "game"
    (ws / "docs" / "progress").mkdir(parents=True)
    (ws / "docs" / "progress" / "UI-03a_PLAN.md").write_text(UI_PLAN, encoding="utf-8")
    (ws / "docs" / "progress" / "AS-P4a_PLAN.md").write_text(BLENDER_PLAN, encoding="utf-8")
    (ws / "docs" / "MASTER_ORDER.md").write_text("| AS-P4a | ... |\n| UI-03a | ... |\n",
                                                encoding="utf-8")

    class Prof:
        workspace = str(ws)

    class Adapter:
        def dependency_done(self, dep):
            return dep == "AS-P4a"

    class P:
        profile = Prof()
        adapter = Adapter()
    out = A.analyze_project(P())
    assert out["done"] == ["AS-P4a"] and out["open"] == ["UI-03a"]
    assert out["sprints"]["UI-03a"]["path"] == "docs/progress/UI-03a_PLAN.md"
    text = A.render(out)
    assert "ui-03a" in text and "studio" in text and "done: AS-P4a" in text


def test_plan_from_a_generic_task_spec():
    from foundry_coordinator.adapters.base import TaskSpec
    s = TaskSpec("p", "T1", "t", "spec", ["src/a.txt"], tags=["needs:blender"], deps=["T0"])
    p = A.plan_from_spec(s)
    assert p.needs == ["blender"] and p.needs_source == "task" and p.deps == ["T0"]
    s2 = TaskSpec("p", "T2", "t", "spec", ["audio/x.ogg"], data={"needs": "audio, images"})
    assert A.plan_from_spec(s2).needs == ["audio", "images", "gpu"]
    s3 = TaskSpec("p", "T3", "t", "spec", ["src/a.txt"])
    assert A.plan_from_spec(s3).needs == ["backend"]


AU_PLANS = r"C:\Roblox\AnimalUprising\docs\progress"


@pytest.mark.skipif(not os.path.isdir(AU_PLANS), reason="the AU repo is not on this machine")
def test_smoke_every_real_au_plan_parses_read_only():
    import glob
    seen = {}
    for path in glob.glob(os.path.join(AU_PLANS, "*_PLAN.md")):
        tid = os.path.basename(path)[:-len("_PLAN.md")]
        with open(path, encoding="utf-8", errors="replace") as f:
            seen[tid] = A.parse_plan(tid, f.read())
    assert len(seen) > 20
    assert "studio" in seen["UI-03a"].needs and seen["UI-03a"].deps[0] == "UI-02a"
    assert seen["AS-P4a"].needs == ["blender"]
    out = A.analyze(list(seen.values()))
    assert out["tracks"] and all(t["sprints"] for t in out["tracks"])


def test_plan_deps_ignore_phase_labels_unless_a_plan_exists(tmp_path):
    text = ("**Tier:** A · **Depends on:** the A0 entry gate (boot 48/27), GF-SEC2 and B2\n")
    assert A.plan_deps(text, "GF-GATE1") == ["GF-SEC2"]
    assert A.plan_deps(text, "GF-GATE1", has_plan=lambda d: d == "B2") == ["GF-SEC2", "B2"]
    (tmp_path / "B2_PLAN.md").write_text("# B2\n", encoding="utf-8")
    info = A.parse_plan("GF-GATE1", text, path=str(tmp_path / "GF-GATE1_PLAN.md"))
    assert info.deps == ["GF-SEC2", "B2"]


def test_depends_on_none_and_semicolon_commentary_are_no_dependencies():
    """S57a 2026-10-28 replay: "none; must land before S52's Studio gate" blocked S57a on S52."""
    from foundry_coordinator.projects.animaluprising.adapter import deps_in_field
    assert deps_in_field(" none; must land before S52's Studio gate (owner rule)") == []
    assert deps_in_field(" AS-C20 (batch pipeline); consumed by UI-SKIN1") == ["AS-C20"]
    assert deps_in_field(" AS-R1 + AS-P5 + ref sheets") == ["AS-R1", "AS-P5"]
    assert deps_in_field(" GF-ENT1, AS-P5 ** ignored S9") == ["GF-ENT1", "AS-P5"]
