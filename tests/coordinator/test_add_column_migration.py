"""Additive column migrations ignore only "duplicate column name"; any other
error is raised instead of leaving a half-migrated table (the one-off
"no such column: phase" in test_lead_full_authority came from a swallowed one)."""
import sqlite3

import pytest

from foundry_coordinator.store import add_column


def _conn():
    c = sqlite3.connect(":memory:")
    c.execute("CREATE TABLE t (a TEXT)")
    return c


def _cols(c):
    return [r[1] for r in c.execute("PRAGMA table_info(t)")]


def test_adds_once_and_ignores_the_duplicate():
    c = _conn()
    assert add_column(c, "t", "phase TEXT") is True
    assert add_column(c, "t", "phase TEXT") is False
    assert _cols(c) == ["a", "phase"]


def test_other_errors_are_raised_not_swallowed():
    c = _conn()
    with pytest.raises(sqlite3.OperationalError, match="no such table"):
        add_column(c, "missing", "phase TEXT")
    with pytest.raises(sqlite3.OperationalError):
        add_column(c, "t", "x INTEGER DEFAULT (")        # malformed definition


def test_a_locked_database_is_raised(tmp_path):
    db = str(tmp_path / "s.db")
    a = sqlite3.connect(db, isolation_level=None, timeout=0)
    a.execute("CREATE TABLE t (a TEXT)")
    b = sqlite3.connect(db, isolation_level=None, timeout=0)
    b.execute("BEGIN EXCLUSIVE")
    try:
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            add_column(a, "t", "phase TEXT")
    finally:
        b.execute("ROLLBACK")
        a.close()
        b.close()


def test_helper_and_artifact_tables_reopen_cleanly(tmp_path):
    """Re-opening an existing state DB re-runs the migrations: duplicates only, no error."""
    from foundry_coordinator.artifacts import ArtifactStore
    from foundry_coordinator.store import Store
    for _ in range(2):
        s = Store(str(tmp_path / "state.db"))
        ArtifactStore(s, str(tmp_path))
        cols = {r["name"] for r in s.q("PRAGMA table_info(source_snapshots)")}
        assert {"content_fp", "workspace"} <= cols
        s.conn.close()
