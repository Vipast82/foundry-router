"""Foundry-FineTune: the training-data generator endpoint persona is seeded with
verbatim, local-only defaults, and the one-time seed never overwrites edits."""

import json

from foundry_router.db import Database, FINETUNE_PERSONA_DEFAULTS
from foundry_router.personas import PersonaStore


def test_finetune_persona_seeded_with_defaults(tmp_path):
    db = Database(tmp_path / "d.sqlite")
    p = PersonaStore(db).get("Foundry-FineTune")
    assert p is not None and p["enabled"] == 1
    assert p["execution_mode"] == "direct"                 # no brain / cache / review
    assert json.loads(p["guardrail_overrides"]) == {"max_paid_calls_per_request": 0}
    assert json.loads(p["preferred_mcp_tools"]) == []
    assert p["mcp_tools_in_direct"] == 0                   # schema default is 1
    assert p["force_reasoning_effort"] == 0                # generator's think level wins
    assert p["reasoning_effort"] == "medium"
    assert p["max_output_tokens"] == 16384
    assert p["context_window"] == 32768
    assert json.loads(p["sampling_options"]) == {"temperature": 0.7, "top_p": 0.95,
                                                 "min_p": 0.05}
    assert p["escalate_when_local_busy"] == 0 and p["review_enabled"] == 0


def test_finetune_seed_keeps_operator_edits(tmp_path):
    path = tmp_path / "d.sqlite"
    db = Database(path)
    PersonaStore(db).upsert("Foundry-FineTune", max_output_tokens=4096,
                            mcp_tools_in_direct=1)
    db.execute("DELETE FROM kv WHERE key='persona_seed_v8_finetune'")
    db._seed_finetune_defaults()                           # re-run: row was edited
    p = PersonaStore(db).get("Foundry-FineTune")
    assert p["max_output_tokens"] == 4096 and p["mcp_tools_in_direct"] == 1
    assert set(FINETUNE_PERSONA_DEFAULTS) <= set(p)        # every default is a real column


def test_finetune_seed_runs_once(tmp_path):
    path = tmp_path / "d.sqlite"
    Database(path)
    db = Database(path)                                    # restart: seed flag already set
    assert db.kv_get("persona_seed_v8_finetune")
    assert PersonaStore(db).get("Foundry-FineTune")["execution_mode"] == "direct"
