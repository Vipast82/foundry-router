"""Revamp B13: a control-plane action the router no longer knows (HTTP 404
"unknown action for this source") is retired once instead of being reported
again on every poll (28k log lines for act-ui-input-phase-20261019)."""

from types import SimpleNamespace

from foundry_coordinator.adapters.foundry import FoundryError
from foundry_coordinator.control import ControlChannel
from foundry_coordinator.store import Store


class Router:
    def __init__(self):
        self.calls = []

    def action_status(self, action_id, source_id, status, detail, result=None):
        self.calls.append((action_id, status))
        raise FoundryError(f"POST /v1/foundry/control/actions/{action_id}/status: HTTP 404 "
                           f"not_found unknown action for this source")

    def heartbeat(self, beat):
        return {}

    def claim_actions(self, source, projects):
        return []


def test_unknown_action_is_retired_after_one_404(tmp_path):
    store = Store(str(tmp_path / "c.sqlite"))
    c = SimpleNamespace(store=store, projects={}, clock=lambda: 1000.0)
    router = Router()
    logs = []
    ch = ControlChannel(c, foundry=router, source_id="coordinator:test", log=logs.append,
                        clock=lambda: 1000.0)
    m = store.add_operator_message("act-ui-input-phase-20261019", "game", "UI-02", "", "task",
                                   "next_session", "victor", "text")
    store.mark_operator_messages([m["msg_id"]], "rejected", detail="not deliverable")
    for _ in range(20):
        ch.report_messages()
    assert router.calls == [("act-ui-input-phase-20261019", "rejected")]
    assert store.operator_message("act-ui-input-phase-20261019")["reported"] == "retired"
    assert len([x for x in logs if "retired" in x]) == 1
    ev = store.q("SELECT kind FROM events WHERE kind='control_action_retired'")
    assert len(ev) == 1


def test_other_report_errors_are_still_retried(tmp_path):
    store = Store(str(tmp_path / "c.sqlite"))
    c = SimpleNamespace(store=store, projects={}, clock=lambda: 1000.0)

    class Flaky(Router):
        def action_status(self, action_id, source_id, status, detail, result=None):
            self.calls.append((action_id, status))
            raise FoundryError("POST x: HTTP 503 unavailable")
    router = Flaky()
    ch = ControlChannel(c, foundry=router, source_id="s", clock=lambda: 1000.0)
    m = store.add_operator_message("act-1", "game", "T1", "", "task", "next_session", "v", "x")
    store.mark_operator_messages([m["msg_id"]], "rejected", detail="nope")
    ch.report_messages()
    ch.report_messages()
    assert len(router.calls) == 2
    assert (store.operator_message("act-1")["reported"] or "") != "retired"
