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


# -- head to head with the local model (v0.181.119) ------------------------------------

class _Registry:
    def __init__(self, rows):
        self.rows = rows

    def benchmarks(self, model_id):
        return [dict(b, model_id=model_id) for b in self.rows.get(model_id, [])]


def _b(category, score, confidence=0.6, source_type="research", url=""):
    return {"category": category, "score": score, "confidence": confidence,
            "source_type": source_type, "source_url": url}


def test_head_to_head_with_the_local_model(tmp_path):
    db = _db(tmp_path)
    for s in pricing.list_services(db):
        pricing.delete_service(db, s["id"])
    for name, sid, pin, pout in (("Cheap Flash", "deepseek/deepseek-v4.1-flash", 0.05, 0.5),
                                 ("Claude Sonnet 5.5", "anthropic/claude-sonnet-5.5", 2.0, 10.0),
                                 ("Claude Opus 5.5", "anthropic/claude-opus-5.5", 4.0, 20.0),
                                 ("Mystery", "x/unknown-1", 1.0, 1.0)):
        db.execute("INSERT INTO service_pricing (name, input_per_1m, output_per_1m, "
                   "cached_input_per_1m, enabled, updated_at, source, source_id) "
                   "VALUES (?,?,?,?,1,'2026-11-01T00:00:00+00:00','openrouter',?)",
                   (name, pin, pout, None, sid))
    ph.record_sample(db, model="qwen-local", prompt_tokens=1_000_000, completion_tokens=100_000,
                     cached_tokens=0, wall_ms=3_600_000)
    ph.record_sample(db, model="claude-sonnet-5-5", prompt_tokens=10_000, completion_tokens=1_000,
                     cached_tokens=0, wall_ms=1_000)
    costs = pricing.compute_costs(db, hours=72, watts=400, kwh_rate=0.15,
                                  backend_type=lambda m: "openai-compatible-local"
                                  if m == "qwen-local" else "anthropic-compatible")
    reg = _Registry({
        "qwen-local": [_b("coding", 72.0), _b("agentic", 64.0)],
        "deepseek/deepseek-v4.1-flash": [_b("coding", 70.0)],
        # stored under the router's own spelling, and a second, less trusted row
        "claude-sonnet-5-5": [_b("coding", 90.0, 0.7)],
        "anthropic/claude-sonnet-5.5": [_b("coding", 55.0, 0.1)],
        "anthropic/claude-opus-5.5": [_b("coding", 93.0, 0.24, "community_report",
                                         "conflation:same-score-across-categories")],
    })
    h = pricing.head_to_head(db, reg, costs, "coding")
    assert h["local"]["model"] == "qwen-local" and h["local"]["score"] == 72.0
    assert h["local"]["cost"] == costs["local"]["cost"] > 0
    rows = {r["name"]: r for r in h["rows"]}
    assert [r["name"] for r in h["rows"]] == ["Claude Opus 5.5", "Claude Sonnet 5.5",
                                             "Cheap Flash", "Mystery"]      # best first, unrated last
    assert rows["Claude Sonnet 5.5"]["score"] == 90.0                       # the trusted row wins
    assert rows["Claude Sonnet 5.5"]["delta"] == 18.0
    assert rows["Claude Opus 5.5"]["low_confidence"] is True
    assert rows["Claude Opus 5.5"]["source"] == "conflation"
    assert rows["Cheap Flash"]["delta"] == -2.0 and rows["Cheap Flash"]["per_point"] is None
    assert rows["Mystery"]["score"] is None and h["unrated"] == ["Mystery"]
    son = rows["Claude Sonnet 5.5"]
    assert son["extra"] == round(son["cost"] - h["local"]["cost"], 4) and son["times"] > 1
    assert son["per_point"] == round(son["extra"] / 18.0, 4)
    # like for like: the cheapest paid model rated at least as high as the local one
    assert h["like_for_like"]["name"] == "Claude Sonnet 5.5"
    assert h["like_for_like"]["saving"] == son["extra"]
    assert h["nearest"]["name"] == "Cheap Flash"                            # closest score
    # another category: only the local model is rated there
    a = pricing.head_to_head(db, reg, costs, "agentic")
    assert a["local"]["score"] == 64.0 and a["like_for_like"] is None and len(a["unrated"]) == 4
    # an unknown category falls back to coding; an unrated local model says so
    assert pricing.head_to_head(db, reg, costs, "nonsense")["category"] == "coding"
    n = pricing.head_to_head(db, _Registry({}), costs, "coding")
    assert n["local"]["score"] is None and "no coding score" in n["note"]
    # no local traffic in the window
    none = pricing.head_to_head(db, reg, {"by_model": [], "services": []}, "coding")
    assert none["local"] is None and "No local model" in none["note"]
