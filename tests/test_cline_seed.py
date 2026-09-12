"""Cline persona seed defaults. A fresh install should land the plan/act pair on
the speed/quality defaults: PLAN reasons deeply and forces it; ACT disables
thinking and forces it (fast local edits). The v6 upgrade must also preserve an
operator's manual customization rather than clobbering it."""

from foundry_router.db import Database, utcnow


def _p(db, name):
    return db.query_one("SELECT reasoning_effort, force_reasoning_effort, "
                        "execution_mode FROM personas WHERE virtual_name=?", (name,))


def test_fresh_install_cline_defaults(tmp_path):
    db = Database(tmp_path / "d.sqlite")
    plan, act = _p(db, "claude-cline-plan"), _p(db, "claude-cline-act")
    assert plan["execution_mode"] == "direct" and act["execution_mode"] == "direct"
    assert plan["reasoning_effort"] == "high" and plan["force_reasoning_effort"] == 1
    assert act["reasoning_effort"] == "off" and act["force_reasoning_effort"] == 1


def test_v6_upgrade_preserves_manual_choice(tmp_path):
    # Simulate an older install that seeded plan=high/act=low (v5) but where the
    # operator then hand-set ACT to 'medium'. The v6 upgrade must NOT touch it.
    path = tmp_path / "d.sqlite"
    db = Database(path)
    # Undo the v6 flag + revert to the v5 state, then hand-customize ACT.
    db.execute("DELETE FROM kv WHERE key='persona_seed_v6_cline_effort_force'")
    db.execute("UPDATE personas SET reasoning_effort='high', force_reasoning_effort=0 "
               "WHERE virtual_name='claude-cline-plan'")
    db.execute("UPDATE personas SET reasoning_effort='medium', force_reasoning_effort=0 "
               "WHERE virtual_name='claude-cline-act'")
    db._seed_cline_effort_v2()
    plan, act = _p(db, "claude-cline-plan"), _p(db, "claude-cline-act")
    assert plan["reasoning_effort"] == "high" and plan["force_reasoning_effort"] == 1
    # ACT was manually 'medium' (not the seeded 'low'), so v6 leaves it alone
    assert act["reasoning_effort"] == "medium" and act["force_reasoning_effort"] == 0
