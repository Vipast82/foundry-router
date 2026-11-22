"""While the Foundry Client has a Kilo restart pending for a safe point, the coordinator
starts no new session, so the safe point comes (infra.kilo_drain; item 6 of
COORDINATOR_AUTOMATION_NEEDS: Kilo grows to 7-10 GB and has to be restarted, and a
safe-point restart behind a stream of new sessions waited for hours)."""

from __future__ import annotations

import json

from foundry_coordinator import automation, board, client_api, infra
from foundry_coordinator import states as S
from tests.coordinator.test_budget_policy import game, task


class Client:
    """The Foundry Client's local API as the coordinator reads it."""

    def __init__(self, pending="", for_s=0, kilo_gb=8.5):
        self.pending, self.for_s, self.kilo_gb, self.asked = pending, for_s, kilo_gb, 0

    def component(self, name):
        self.asked += 1
        return {"name": name, "state": "running", "pending_restart": self.pending,
                "pending_for_s": self.for_s}

    def status(self):
        return {"components": [self.component("kilo")],
                "resources": {"free_gb": 3.0, "total_gb": 64.0,
                              "kilo": {"gb": self.kilo_gb, "pending": bool(self.pending)}}}


def events(c, kind):
    return [json.loads(r["detail"]) for r in c.store.q(
        "SELECT detail FROM events WHERE kind=? ORDER BY id", (kind,))]


def test_no_session_starts_while_kilo_waits_for_its_safe_point(tmp_path, monkeypatch):
    c, P, clk = game(tmp_path)
    api = Client(pending="safe", for_s=120)
    monkeypatch.setattr(client_api, "discover", lambda timeout=None: api)
    note = infra.kilo_drain(c, P)
    assert note.startswith("waiting: the Kilo server restarts at the next safe point; no new "
                           "session starts until then (at most 5")
    # the engine does not start the sprint, charges nothing, and says why
    c._start_implementation(P, task(c))
    t = task(c)
    assert t["state"] == S.READY and not t.get("session_id") and t["next_action"] == note
    assert len(events(c, "kilo_drain")) == 1
    # asked of the client at most every 30 s, however many sprints wait
    for _ in range(20):
        infra.kilo_drain(c, P)
    assert api.asked == 1
    # Kilo restarted: the next read lets sprints start
    api.pending = ""
    clk.advance(31)
    assert infra.kilo_drain(c, P) == ""
    # a later restart gets its own event
    api.pending = "safe"
    clk.advance(31)
    assert infra.kilo_drain(c, P) and len(events(c, "kilo_drain")) == 2


def test_the_hold_ends_after_its_limit_and_can_be_switched_off(tmp_path, monkeypatch):
    c, P, clk = game(tmp_path)
    api = Client(pending="safe", for_s=59 * 60)
    monkeypatch.setattr(client_api, "discover", lambda timeout=None: api)
    assert "at most 1 more min" in infra.kilo_drain(c, P)
    clk.advance(20)
    assert infra.kilo_drain(c, P)                            # 59 min 20 s of 60
    clk.advance(45)
    api.for_s += 65                                          # the client's own count moves on
    assert infra.kilo_drain(c, P) == ""                      # it waited an hour: sprints go on
    automation.set_live(P, "kilo_restart_drain_minutes", 0)
    api.for_s = 5
    clk.advance(31)
    assert infra.kilo_drain(c, P) == ""                      # never hold starts
    automation.set_live(P, "kilo_restart_drain_minutes", 60)
    # no client on this machine, or one that does not answer: no hold
    monkeypatch.setattr(client_api, "discover", lambda timeout=None: None)
    clk.advance(31)
    assert infra.kilo_drain(c, P) == ""

    def boom(timeout=None):
        raise client_api.ClientError("connection refused")
    monkeypatch.setattr(client_api, "discover", boom)
    clk.advance(31)
    assert infra.kilo_drain(c, P) == ""


def test_the_board_shows_kilos_memory_and_a_pending_restart(tmp_path, monkeypatch):
    monkeypatch.setattr(client_api, "discover", lambda timeout=None: Client(pending="safe"))
    m = board.machine()
    assert m["kilo_gb"] == 8.5 and m["kilo_restart"] == "pending" and "ram_free_gb" in m
    monkeypatch.setattr(client_api, "discover", lambda timeout=None: Client(kilo_gb=0.6))
    m = board.machine()
    assert m["kilo_gb"] == 0.6 and "kilo_restart" not in m
    monkeypatch.setattr(client_api, "discover", lambda timeout=None: None)
    assert "kilo_gb" not in board.machine()
    c, P, clk = game(tmp_path)
    monkeypatch.setattr(client_api, "discover", lambda timeout=None: Client(pending="safe"))
    text = board.text(board.build(c, P))
    assert "kilo 8.5 GB" in text and "kilo_restart pending" in text


def test_the_setting_is_typed_in_the_schema():
    from foundry_automation import schema
    assert schema.find_field("automation.kilo_restart_drain_minutes") is not None
    assert schema._AUTO_DESC["kilo_restart_drain_minutes"]
    assert schema.AUTOMATION_DEFAULTS["kilo_restart_drain_minutes"] == 60
