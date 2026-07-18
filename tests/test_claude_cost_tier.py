"""Claude models must carry their real cost tier (Haiku<Sonnet<Opus<Fable),
not a flat 'high' for all — otherwise the UI shows every tier as identically
priced and 'prefer the cheaper Claude tier' routing has nothing to sort on.
The tier is stamped at discovery (register_discovered, every startup/pool
change) and reinforced by the reference seed; both use claude_cost_tier."""

from foundry_router.db import Database
from foundry_router.registry.models_db import ModelRegistry
from foundry_router.registry.reference_data import REFERENCE_SEED
from foundry_router.registry.reference_seed import apply_reference_seed, best_match
from foundry_router.usage import claude_cost_tier


# -- the tier helper --------------------------------------------------------------

def test_claude_cost_tier_ladder():
    assert claude_cost_tier("claude-haiku-4-5") == "low"
    assert claude_cost_tier("claude-sonnet-4-6") == "medium"
    assert claude_cost_tier("claude-opus-4-8") == "high"
    assert claude_cost_tier("claude-fable-5") == "very_high"
    assert claude_cost_tier("claude-mythos-1") == "very_high"


def test_version_bumps_keep_the_same_tier():
    # only quality differs across a family's versions, not cost tier
    assert claude_cost_tier("claude-opus-4-6") == claude_cost_tier("claude-opus-4-8") == "high"


def test_unknown_claude_stays_conservative_high():
    # an unrecognized tier must NOT look cheap
    assert claude_cost_tier("claude-3.5-experimental") == "high"


# -- reference data carries the tier (the literal fix asked for) ------------------

def test_reference_data_claude_entries_have_tier():
    want = {"claude-fable": "very_high", "claude-opus": "high",
            "claude-sonnet": "medium", "claude-haiku": "low"}
    for key, tier in want.items():
        entry = next(e for e in REFERENCE_SEED if key in e["match"])
        assert entry.get("relative_cost_tier") == tier, key


# -- the seed applies it, and doesn't clobber non-Claude tiers --------------------

def test_seed_corrects_claude_tier_from_flat_high(tmp_path):
    reg = ModelRegistry(Database(tmp_path / "c.sqlite"))
    # simulate what discovery used to do: everything Claude stamped flat "high"
    for mid in ("claude-haiku-4-5", "claude-sonnet-4-6",
                "claude-opus-4-8", "claude-fable-5"):
        reg.upsert_auto(mid, source="discovery", relative_cost_tier="high",
                        display_name=mid)
    apply_reference_seed(reg)
    tiers = {m["id"]: m["relative_cost_tier"] for m in reg.list_models()}
    assert tiers["claude-haiku-4-5"] == "low"
    assert tiers["claude-sonnet-4-6"] == "medium"
    assert tiers["claude-opus-4-8"] == "high"
    assert tiers["claude-fable-5"] == "very_high"


def test_seed_preserves_non_claude_tier(tmp_path):
    reg = ModelRegistry(Database(tmp_path / "c2.sqlite"))
    # a cloud model whose tier came from real pricing (no tier in reference data)
    reg.upsert_auto("gpt-5", source="discovery", relative_cost_tier="medium",
                    display_name="gpt-5")
    # a local model discovered free
    reg.upsert_auto("qwen3.6:35b", source="discovery", relative_cost_tier="free",
                    display_name="qwen3.6:35b")
    assert best_match("gpt-5") is not None and \
        best_match("gpt-5").get("relative_cost_tier") is None   # entry has no tier
    apply_reference_seed(reg)
    tiers = {m["id"]: m["relative_cost_tier"] for m in reg.list_models()}
    assert tiers["gpt-5"] == "medium"          # pricing-derived tier untouched
    assert tiers["qwen3.6:35b"] == "free"      # local free untouched
