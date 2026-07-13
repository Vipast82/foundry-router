"""Category-blended ranking for tool-attached personas (found live: Foundry-Chat,
general_chat + preferred_mcp_tools, routed a search-heavy request to a weaker
tool-caller because ranking only weighed general_chat). When a persona has MCP
tools attached, the within-tier quality score blends the requested category
with tool_calling; tool-less personas are unchanged, and only the within-tier
score moves — tier-first order and per-tier caps are untouched."""

import pytest

from foundry_router.db import Database
from foundry_router.registry.models_db import ModelRegistry


def _reg(tmp_path, name="b.sqlite"):
    return ModelRegistry(Database(tmp_path / name))


def _model(reg, mid, tier="free", *, chat=None, tool=None, tool_conf=0.5):
    reg.upsert_auto(mid, source="discovery", relative_cost_tier=tier)
    if chat is not None:
        reg.upsert_benchmark(mid, "general_chat", chat, "estimated",
                             "community_report", confidence=0.5)
    if tool is not None:
        reg.upsert_benchmark(mid, "tool_calling", tool, "estimated",
                             "community_report", confidence=tool_conf)


def _order(reg, category, ids, **kw):
    return [r["id"] for r in reg.ranked_for_category(category, ids, **kw)]


# -- the mechanism: blend can reorder within a tier -------------------------------

def test_blend_reorders_toward_the_stronger_tool_caller(tmp_path):
    reg = _reg(tmp_path)
    # A wins general_chat; B is the far stronger, higher-confidence tool-caller.
    _model(reg, "A", chat=80, tool=20, tool_conf=0.5)   # primary 40, tool 10
    _model(reg, "B", chat=50, tool=95, tool_conf=0.9)   # primary 25, tool 85.5
    # without blend, primary-only ranking prefers A
    assert _order(reg, "general_chat", ["A", "B"]) == ["A", "B"]
    # with blend: A=.6*40+.4*10=28, B=.6*25+.4*85.5=49.2 -> B leads
    assert _order(reg, "general_chat", ["A", "B"],
                  blend_tool_calling=True) == ["B", "A"]


def test_tool_less_persona_is_unchanged(tmp_path):
    reg = _reg(tmp_path)
    _model(reg, "A", chat=80, tool=20)
    _model(reg, "B", chat=50, tool=95, tool_conf=0.9)
    # blend_tool_calling defaults False — identical to the pre-change behavior
    assert _order(reg, "general_chat", ["A", "B"]) == ["A", "B"]


def test_blend_on_tool_calling_category_is_identity(tmp_path):
    reg = _reg(tmp_path)
    _model(reg, "A", chat=80, tool=20)
    _model(reg, "B", chat=50, tool=95, tool_conf=0.9)
    # requesting tool_calling itself: blending with tool_calling is a no-op,
    # so the flag must not double-count or change the tool_calling ranking
    plain = _order(reg, "tool_calling", ["A", "B"])
    blended = _order(reg, "tool_calling", ["A", "B"], blend_tool_calling=True)
    assert plain == blended == ["B", "A"]


# -- data gaps never penalize -----------------------------------------------------

def test_missing_tool_data_falls_back_to_primary_not_a_penalty(tmp_path):
    reg = _reg(tmp_path)
    # A has NO tool_calling row; B has a mediocre one. If a missing row were
    # treated as 0 (or A's primary were down-weighted to 0.6), B would win.
    _model(reg, "A", chat=100)                          # primary 50, no tool row
    _model(reg, "B", chat=60, tool=100, tool_conf=0.5)  # primary 30, tool 50
    # A keeps its full primary (50) > B's blend (.6*30+.4*50=38) -> A stays first
    assert _order(reg, "general_chat", ["A", "B"],
                  blend_tool_calling=True) == ["A", "B"]


def test_only_tool_data_still_ranks(tmp_path):
    reg = _reg(tmp_path)
    _model(reg, "A", chat=70)                        # primary only -> composite 70
    _model(reg, "B", tool=90, tool_conf=0.9)         # tool only -> composite 90
    # each contributes the signal it has (confidence-weighted average, so a
    # single-signal model's composite is that signal's raw score); B's 90 > A's 70
    assert _order(reg, "general_chat", ["A", "B"],
                  blend_tool_calling=True) == ["B", "A"]


# -- observed rows win automatically (no second change needed) --------------------

def test_observed_tool_row_supersedes_estimated_in_the_blend(tmp_path):
    reg = _reg(tmp_path)
    _model(reg, "A", chat=80, tool=20)              # weak estimated tool row
    _model(reg, "B", chat=50)
    # B accrues a strong OBSERVED tool_calling row (measured, high confidence),
    # exactly what record_tool_call now writes from live traffic
    reg.upsert_benchmark("B", "tool_calling", 95, "measured", "observed",
                         confidence=0.9)
    # B's observed tool signal (85.5) carries the blend past A
    order = _order(reg, "general_chat", ["A", "B"], blend_tool_calling=True)
    assert order == ["B", "A"]


# -- tier-first order is untouched ------------------------------------------------

def test_blend_never_crosses_tiers(tmp_path):
    reg = _reg(tmp_path)
    _model(reg, "local", tier="free", chat=40, tool=40)
    # a premium model that is an elite tool-caller must STILL sort after the
    # free-tier one — blend only moves the within-tier quality score
    _model(reg, "claude", tier="high", chat=99, tool=99, tool_conf=0.95)
    assert _order(reg, "general_chat", ["local", "claude"],
                  blend_tool_calling=True) == ["local", "claude"]
