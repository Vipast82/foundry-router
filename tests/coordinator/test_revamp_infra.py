"""Revamp B12: an infrastructure preflight before a session, infrastructure
failures that cost no attempt and retry with backoff, and a context-ceiling
handoff. Scripted sessions, temp repos and a local socket only."""

import json
import socket

from foundry_coordinator import infra
from foundry_coordinator import states as S
from foundry_coordinator.adapters.scripted import ScriptedClient
from tests.coordinator.helpers import coordinator, make_repo, write_profile
from tests.coordinator.test_lead_e2e import Clock

TASK = {"id": "T1", "title": "t", "spec": "Write src/a.txt with OK.", "scope": ["src/a.txt"]}
DONE = "```coordinator-report\n" + json.dumps({"status": "DONE", "summary": "ok"}) + "\n```"


def good(ctx):
    return {"writes": {"src/a.txt": "OK\n"}, "commit": "T1: ok", "final": DONE}


class FlakyKilo(ScriptedClient):
    def __init__(self, script, down=True):
        super().__init__(script, project_id="game")
        self.down = down
        self.starts = []

    def preflight(self, directory):
        return [("FAIL", "kilo serve", "connection refused")] if self.down else \
            [("PASS", "kilo serve", "reachable")]

    def start_session(self, **kw):
        sid = super().start_session(**kw)
        self.starts.append(kw["metadata"])
        return sid


def env(tmp_path, client, clock, extra=""):
    root, _ = make_repo(tmp_path, "game", [TASK], extra_files={"src/a.txt": "x\n"})
    prof = write_profile(tmp_path, "game", root, extra=extra)
    c = coordinator(tmp_path, [prof], {"game": client}, clock=clock)
    c.approve("game", "T1", [], "victor")
    return c


def events(c, kind):
    return [json.loads(r["detail"]) for r in c.store.q(
        "SELECT detail FROM events WHERE kind=? ORDER BY rowid", (kind,))]


def test_a_failed_preflight_starts_nothing_and_retries_with_backoff(tmp_path):
    clock = Clock()
    k = FlakyKilo(good)
    c = env(tmp_path, k, clock)
    for _ in range(4):
        c.tick()
    t = c.store.task("game", "T1")
    assert t["state"] == S.READY and t["attempt"] == 0 and not k.starts
    w = t["data"]["infra_wait"]
    assert w["n"] == 1 and w["kind"] == "preflight" and "connection refused" in w["why"]
    assert "no attempt is used" in t["next_action"]
    clock.advance(61)
    for _ in range(2):
        c.tick()
    assert c.store.task("game", "T1")["data"]["infra_wait"]["n"] == 2     # 120 s next
    k.down = False
    clock.advance(121 + infra.CACHE_SECONDS)
    for _ in range(12):
        c.tick()
    t = c.store.task("game", "T1")
    assert t["state"] == S.COMPLETE and k.starts[0]["attempt"] == 1
    assert events(c, "infra_ready")


def test_the_operator_hears_once_when_infra_stays_down(tmp_path):
    clock = Clock()
    c = env(tmp_path, FlakyKilo(good), clock, extra="\n[automation]\ninfra_retry_max = 2\n")
    for _ in range(6):
        c.tick()
        clock.advance(1000)
    notes = events(c, "operator_notice")
    assert len(notes) == 1 and "uses no attempt" in notes[0]["text"]


def test_endpoints_are_probed(tmp_path):
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    try:
        assert infra.check_endpoint(f"tcp://127.0.0.1:{port}") == ""
    finally:
        srv.close()
    assert infra.check_endpoint(f"tcp://127.0.0.1:{port}")          # closed now
    clock = Clock()
    c = env(tmp_path, FlakyKilo(good, down=False), clock,
            extra=f'\n[automation]\npreflight_endpoints = ["rojo=tcp://127.0.0.1:{port}"]\n')
    for _ in range(3):
        c.tick()
    w = c.store.task("game", "T1")["data"]["infra_wait"]
    assert w["why"].startswith("rojo: tcp://127.0.0.1")


def test_an_infra_session_failure_costs_no_attempt(tmp_path):
    clock = Clock()
    n = []

    def script(ctx):
        n.append(ctx)
        if len(n) == 1:
            return {"error": "provider error: HTTP 503 Service Unavailable"}
        return good(ctx)
    k = FlakyKilo(script, down=False)
    c = env(tmp_path, k, clock)
    for _ in range(4):
        c.tick()
    t = c.store.task("game", "T1")
    assert t["state"] == S.CHANGES_REQUIRED and t["data"]["free_retry"] is True
    assert events(c, "infra_free_retry")
    for _ in range(3):                      # the backoff holds the next session
        c.tick()
    assert len(k.starts) == 1
    clock.advance(61)
    for _ in range(12):
        c.tick()
    t = c.store.task("game", "T1")
    assert t["state"] == S.COMPLETE and [s["attempt"] for s in k.starts] == [1, 1]
    assert not t["data"].get("infra_failures")              # cleared by the good session


def test_infra_free_retries_are_bounded(tmp_path):
    clock = Clock()
    k = FlakyKilo(lambda ctx: {"error": "ECONNRESET connection reset"}, down=False)
    c = env(tmp_path, k, clock, extra="\n[automation]\ninfra_retry_max = 2\n")
    for _ in range(40):
        c.tick()
        clock.advance(1000)
    attempts = [s["attempt"] for s in k.starts]
    assert attempts[:3] == [1, 1, 1] and attempts[3] == 2, attempts
    assert events(c, "infra_retry_exhausted")


def test_a_session_over_the_context_ceiling_hands_off_free(tmp_path):
    clock = Clock()
    n = []

    def script(ctx):
        n.append(ctx["prompt"])
        if len(n) == 1:
            return {"final": "```coordinator-report\n" + json.dumps(
                {"status": "NOT DONE", "summary": "half the HUD"}) + "\n```",
                    "context": 170000}
        return good(ctx)
    k = FlakyKilo(script, down=False)
    c = env(tmp_path, k, clock)
    for _ in range(14):
        c.tick()
    t = c.store.task("game", "T1")
    assert t["state"] == S.COMPLETE and [s["attempt"] for s in k.starts] == [1, 1]
    [h] = events(c, "context_handoff")
    assert h["context"] == 170000
    assert "context ceiling" in n[1] and "half the HUD" in n[1]


def test_a_blocked_report_over_the_ceiling_is_an_ordinary_stop():
    from types import SimpleNamespace
    P = SimpleNamespace(profile=SimpleNamespace(raw={}))
    s = SimpleNamespace(context=190000)
    assert infra.over_ceiling(P, s, {"status": "BLOCKED", "blocker": "which layout?"}) is None
    assert infra.over_ceiling(P, s, {"status": "DONE"}) is None
    assert infra.over_ceiling(P, s, {"status": "NOT DONE"}).startswith("context ceiling")
    assert infra.over_ceiling(P, SimpleNamespace(context=1000), {"status": "NOT DONE"}) is None
