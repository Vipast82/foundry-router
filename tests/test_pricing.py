"""Local-vs-cloud cost calculator: seeding, CRUD, and the token→cost math."""

from foundry_router import perf_history as ph
from foundry_router import pricing
from foundry_router.db import Database


def _db(tmp_path):
    return Database(tmp_path / "c.sqlite")


def test_seed_and_crud(tmp_path):
    db = _db(tmp_path)
    names = {s["name"] for s in pricing.list_services(db)}
    assert "Claude Opus 4.8" in names and "GPT-5.6" in names
    n0 = len(pricing.list_services(db))
    pricing.upsert_service(db, name="My Local Cloud", input_per_1m=1.0,
                           output_per_1m=2.0, cached_input_per_1m=0.1)
    assert len(pricing.list_services(db)) == n0 + 1
    row = next(s for s in pricing.list_services(db) if s["name"] == "My Local Cloud")
    pricing.upsert_service(db, id=row["id"], name="My Local Cloud",
                           input_per_1m=1.5, output_per_1m=2.0)
    row = next(s for s in pricing.list_services(db) if s["name"] == "My Local Cloud")
    assert row["input_per_1m"] == 1.5
    pricing.delete_service(db, row["id"])
    assert "My Local Cloud" not in {s["name"] for s in pricing.list_services(db)}


def test_seed_respects_deletions(tmp_path):
    # A deleted default must NOT be resurrected on the next call (ensure_seed only
    # runs when the table is empty).
    db = _db(tmp_path)
    pricing.list_services(db)
    row = next(s for s in pricing.list_services(db) if s["name"] == "Grok 4.6")
    pricing.delete_service(db, row["id"])
    assert "Grok 4.6" not in {s["name"] for s in pricing.list_services(db)}


def test_compute_costs_token_math(tmp_path):
    db = _db(tmp_path)
    # keep just one known service for a deterministic assertion
    for s in pricing.list_services(db):
        pricing.delete_service(db, s["id"])
    pricing.upsert_service(db, name="Test", input_per_1m=10.0, output_per_1m=30.0,
                           cached_input_per_1m=1.0)
    # 2 calls: 1,000,000 input / 100,000 output total; 400,000 cached input
    ph.record_sample(db, model="m", prompt_tokens=600_000, completion_tokens=60_000,
                     cached_tokens=400_000, wall_ms=10_000)
    ph.record_sample(db, model="m", prompt_tokens=400_000, completion_tokens=40_000,
                     cached_tokens=0, wall_ms=10_000)
    r = pricing.compute_costs(db, hours=72, watts=400, kwh_rate=0.15)
    assert r["tokens"]["input"] == 1_000_000
    assert r["tokens"]["output"] == 100_000
    assert r["tokens"]["cached"] == 400_000
    svc = r["services"][0]
    # naive: 1M input * $10 + 0.1M output * $30 = 10 + 3 = $13
    assert svc["cost"] == 13.0
    # cached: 0.6M @ $10 + 0.4M @ $1 + 0.1M @ $30 = 6 + 0.4 + 3 = $9.40
    assert svc["cost_cached"] == 9.4
    # local electricity: 0.4kW * (20000ms/3.6e6 h) * $0.15
    assert r["local"]["active_hours"] == round(20_000 / 3.6e6, 2)
    assert r["local"]["cost"] >= 0


def test_compute_costs_respects_window(tmp_path):
    import datetime as dt
    db = _db(tmp_path)
    ph.record_sample(db, model="m", prompt_tokens=1000, completion_tokens=100, wall_ms=100)
    db.execute("UPDATE perf_samples SET ts=?",
               ((dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=3)).isoformat(),))
    assert pricing.compute_costs(db, hours=1)["tokens"]["input"] == 0
    assert pricing.compute_costs(db, hours=6)["tokens"]["input"] == 1000
