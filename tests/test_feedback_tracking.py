"""Quality-tracking Phase 1: response feedback (ingest endpoint + GUI thumbs),
durable per-event tool-call logging, and the on-demand statistical insight
digest. Everything downstream (review pass, eval harness) writes into these
tables, so their behavior is pinned here."""

import json

from foundry_router.db import Database, utcnow
from foundry_router.insights import (generate_digest, normalize_rating,
                                     record_feedback, render_report)
from foundry_router.usage import RequestLogger


# -- rating normalization ---------------------------------------------------------

def test_normalize_rating_accepts_client_shapes():
    assert normalize_rating("up") == 1
    assert normalize_rating("thumbsUp") == 1
    assert normalize_rating("+1") == 1
    assert normalize_rating(1) == 1
    assert normalize_rating(True) == 1
    assert normalize_rating("down") == -1
    assert normalize_rating("-1") == -1
    assert normalize_rating(-1) == -1
    assert normalize_rating(False) == -1
    assert normalize_rating("whatever") is None
    assert normalize_rating(None) is None
    assert normalize_rating(0) is None


# -- feedback linkage -------------------------------------------------------------

def _log_request(db, persona="Foundry-Chat", summary="identify this plant",
                 model="local-chat"):
    logger = RequestLogger(db, persona, persona, "agent", summary)
    logger.record_model_call(model, "b", 100, 50, 0.0)
    logger.finish("ok")
    return db.query_one("SELECT * FROM request_log ORDER BY id DESC LIMIT 1")


def test_feedback_links_by_request_id(tmp_path):
    db = Database(tmp_path / "f.sqlite")
    row = _log_request(db)
    out = record_feedback(db, 1, request_log_id=row["id"], source="gui")
    assert out["request_log_id"] == row["id"]
    assert out["persona"] == "Foundry-Chat"
    assert out["model"] == "local-chat"        # answering model resolved
    stored = db.query_one("SELECT * FROM response_feedback")
    assert stored["rating"] == 1 and stored["source"] == "gui"


def test_feedback_links_by_message_then_persona_fallback(tmp_path):
    db = Database(tmp_path / "f2.sqlite")
    row = _log_request(db, summary="what is a monad")
    by_msg = record_feedback(db, -1, message="what is a monad")
    assert by_msg["request_log_id"] == row["id"]
    by_persona = record_feedback(db, 1, persona="Foundry-Chat")
    assert by_persona["request_log_id"] == row["id"]
    # unmatched feedback is still stored, unlinked
    orphan = record_feedback(db, -1, persona="Ghost-Persona")
    assert orphan["request_log_id"] is None
    assert len(db.query("SELECT * FROM response_feedback")) == 3


def test_feedback_endpoints(client):
    # facade ingest: bad rating is a 400, good rating lands in the table
    bad = client.post("/v1/feedback", json={"rating": "meh"})
    assert bad.status_code == 400
    ok = client.post("/v1/feedback", json={"rating": "up", "persona": "Foundry-Chat",
                                           "comment": "solid answer"})
    assert ok.status_code == 200 and ok.json()["rating"] == 1
    # GUI route
    gui = client.post("/admin/api/feedback", json={"rating": "down"})
    assert gui.status_code == 200 and gui.json()["rating"] == -1


def test_usage_endpoint_carries_feedback_marks(client, app):
    db = app.state.services.db
    row = _log_request(db)
    record_feedback(db, 1, request_log_id=row["id"], source="gui")
    reqs = client.get("/admin/api/usage").json()["requests"]
    assert reqs[0]["id"] == row["id"]
    assert reqs[0]["feedback"] == 1


# -- durable tool-call log --------------------------------------------------------

def test_record_tool_call_writes_durable_rows(tmp_path):
    db = Database(tmp_path / "t.sqlite")
    logger = RequestLogger(db, "Foundry-Research", "Foundry-Research", "agent", "q")
    logger.record_tool_call("search", "searxng", 420, ok=True)
    logger.record_tool_call("crawl", "crawl4ai", 900, ok=False,
                            error="403 blocked", caller="qwen3:14b")
    rows = db.query("SELECT * FROM tool_call_log ORDER BY id")
    assert len(rows) == 2
    assert rows[0]["caller"] == "brain" and rows[0]["ok"] == 1
    assert rows[1]["caller"] == "qwen3:14b" and rows[1]["ok"] == 0
    assert rows[1]["error"] == "403 blocked"
    assert rows[1]["persona"] == "Foundry-Research"
    # the per-request JSON trail is unchanged by the durable write
    assert len(logger.tool_calls) == 2


# -- insight digest ---------------------------------------------------------------

def _seed_activity(db):
    _log_request(db, persona="Foundry-Chat")
    logger = RequestLogger(db, "Foundry-Research", "Foundry-Research", "agent", "q")
    logger.record_tool_call("search", "searxng", 300, ok=True, caller="qwen3:14b")
    logger.record_tool_call("search", "searxng", 350, ok=False,
                            error="timeout", caller="qwen3:14b")
    logger.record_guardrail("denied claude-opus-4-8: window 90% used")
    logger.finish("ok")
    record_feedback(db, 1, persona="Foundry-Chat")
    record_feedback(db, -1, persona="Foundry-Chat")
    db.execute("INSERT INTO review_log (ts, persona, trigger_reason, review_model, "
               "corrected, verdict, duration_ms) VALUES (?,?,?,?,?,?,?)",
               (utcnow(), "Foundry-Chat", "review_enabled", "claude-haiku-4-5",
                1, "fixed a factual error", 1200))
    db.log_event("error", "mcp", "tool searxng/search failed after 30s")


def test_digest_aggregates_all_signals(tmp_path):
    db = Database(tmp_path / "d.sqlite")
    _seed_activity(db)
    digest = generate_digest(db, days=7)
    chat = digest["personas"]["Foundry-Chat"]
    assert chat["requests"] == 1
    assert chat["feedback_up"] == 1 and chat["feedback_down"] == 1
    assert chat["reviews"] == 1 and chat["reviews_corrected"] == 1
    research = digest["personas"]["Foundry-Research"]
    assert research["guardrail_top"]  # the denial phrase surfaced
    tool = next(t for t in digest["tool_calls"] if t["tool"] == "search")
    assert tool["caller"] == "qwen3:14b"
    assert tool["calls"] == 2 and tool["ok"] == 1
    assert tool["low_sample"] is True
    assert tool["last_error"] == "timeout"
    assert digest["recurring_errors"][0]["source"] == "mcp"


def test_digest_report_renders_and_flags_low_samples(tmp_path):
    db = Database(tmp_path / "d2.sqlite")
    _seed_activity(db)
    report = render_report(generate_digest(db, days=7))
    assert "INSIGHT DIGEST" in report
    assert "Foundry-Chat" in report and "1↑ 1↓" in report
    assert "low sample" in report          # thin data is labeled, not smoothed
    assert "qwen3:14b → searxng/search" in report
    assert "guardrail ×1" in report


def test_insights_endpoint(client, app):
    _seed_activity(app.state.services.db)
    d = client.get("/admin/api/insights?days=7").json()
    assert "report" in d and "INSIGHT DIGEST" in d["report"]
    assert d["digest"]["personas"]["Foundry-Chat"]["feedback_up"] == 1


def test_digest_empty_db_is_clean(tmp_path):
    db = Database(tmp_path / "e.sqlite")
    report = render_report(generate_digest(db, days=7))
    assert "no requests in window" in report
    assert "no MCP tool calls in window" in report
