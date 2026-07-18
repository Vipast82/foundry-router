"""Provenance persistence across restarts: discovery runs on every startup and
used to DOWNGRADE research_agent rows to 'discovery' (upsert_auto set source
unconditionally), after which the reference seed re-clobbered them back to
'reference_seed' — so real research never survived a restart. Source is now
monotonic (only moves up the authority ladder), and lower-authority passes may
refresh their own fields but not overwrite a researched row's provenance."""

from foundry_router.db import Database
from foundry_router.registry.models_db import SOURCE_AUTHORITY, ModelRegistry
from foundry_router.registry.reference_seed import apply_reference_seed


def _reg(tmp_path):
    return ModelRegistry(Database(tmp_path / "s.sqlite"))


def test_discovery_does_not_downgrade_research(tmp_path):
    reg = _reg(tmp_path)
    # a model researched: source=research_agent, real good_for + reasoning_style
    reg.upsert_auto("claude-sonnet-4-6", source="research_agent",
                    good_for="real researched summary",
                    reasoning_style="pragmatic workhorse")
    # a restart: discovery re-runs, refreshing backend facts
    reg.upsert_auto("claude-sonnet-4-6", source="discovery",
                    provider="meridian", relative_cost_tier="medium")
    row = reg.get("claude-sonnet-4-6")
    assert row["source"] == "research_agent"          # NOT downgraded
    assert row["good_for"] == "real researched summary"  # research data intact
    assert row["provider"] == "meridian"              # backend fact still refreshed
    assert row["relative_cost_tier"] == "medium"


def test_seed_does_not_reclobber_after_restart(tmp_path):
    reg = _reg(tmp_path)
    reg.upsert_auto("claude-sonnet-4-6", source="research_agent",
                    good_for="real researched summary")
    reg.upsert_auto("claude-sonnet-4-6", source="discovery", provider="meridian")
    # the startup reference-seed pass must leave the research row alone
    apply_reference_seed(reg)
    row = reg.get("claude-sonnet-4-6")
    assert row["source"] == "research_agent"
    assert row["good_for"] == "real researched summary"


def test_source_still_upgrades(tmp_path):
    reg = _reg(tmp_path)
    reg.upsert_auto("m", source="discovery", provider="x")
    reg.upsert_auto("m", source="reference_seed", good_for="seed est")
    assert reg.get("m")["source"] == "reference_seed"     # discovery -> seed: up
    reg.upsert_auto("m", source="research_agent", good_for="real")
    assert reg.get("m")["source"] == "research_agent"     # seed -> research: up
    assert reg.get("m")["good_for"] == "real"


def test_manual_override_stays_top(tmp_path):
    reg = _reg(tmp_path)
    reg.manual_update("m", good_for="hand set")
    reg.upsert_auto("m", source="research_agent", good_for="research would-be")
    row = reg.get("m")
    assert row["source"] == "manual_override"        # never downgraded
    assert row["good_for"] == "hand set"             # value never clobbered


def test_authority_ladder_ordering():
    a = SOURCE_AUTHORITY
    assert a["discovery"] < a["reference_seed"] < a["research_agent"] < a["manual_override"]
    assert a["openrouter_api"] == a["discovery"]
