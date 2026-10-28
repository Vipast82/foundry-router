"""2026-10-27: GF-GATE1's session went 207k -> 230k tokens in one Studio call and died in
Kilo's compaction ("Compaction exhausted"), losing two hours of uncommitted triage. The
handoff ceiling only applied to an IDLE session. Now a BUSY implement session past the stop
line (min(Red 90%, usable - 24k)) is stopped, asked to commit and report, then handed off
to a fresh session without charging an attempt."""

import json

import pytest

from foundry_coordinator import context_window as CW
from foundry_coordinator import states as S
from foundry_coordinator.adapters.base import SessionState
from tests.coordinator.test_lead_e2e import Clock
from tests.coordinator.test_revamp_infra import FlakyKilo, env, events, good

NOT_DONE = "```coordinator-report\n" + json.dumps(
    {"status": "NOT DONE", "summary": "rows 1-40 triaged, committed", "next": "row 41"}) + "\n```"


class BusyKilo(FlakyKilo):
    """The first prompt keeps the session busy while its context grows; abort() ends
    the turn like Kilo does (MessageAbortedError)."""

    def __init__(self, script, contexts):
        super().__init__(script, down=False)
        self.contexts = list(contexts)
        self.ctx = 0
        self.aborts = []

    def state(self, session_id, *, directory, since_ts):
        s = self.sessions.get(session_id)
        if s is not None and s.get("busy"):
            if self.contexts:
                self.ctx = self.contexts.pop(0)
            return SessionState(status="busy")
        return super().state(session_id, directory=directory, since_ts=since_ts)

    def prompt(self, session_id, text, *, directory, persona, agent):
        super().prompt(session_id, text, directory=directory, persona=persona, agent=agent)
        s = self.sessions[session_id]
        if len(s["prompts"]) == 1 and not self.aborts and len(self.starts) == 1:
            s["busy"], s["pending"] = True, False

    def abort(self, session_id, *, directory):
        self.aborts.append(session_id)
        s = self.sessions[session_id]
        if s.get("busy"):
            s["busy"] = False
            s["state"] = SessionState(status="error", error=json.dumps(
                {"name": "MessageAbortedError", "data": {"message": "Aborted"}}))
        return True

    def transcript(self, session_id, *, directory):
        return [{"info": {"role": "assistant", "tokens": {
            "input": 500, "output": 90, "cache": {"read": self.ctx - 500, "write": 0}}}}]


@pytest.fixture(autouse=True)
def window(monkeypatch):
    monkeypatch.setattr(CW, "model_limits", lambda P, persona: (262144, 32768))


def test_the_stop_line_keeps_a_large_tool_result_of_headroom():
    from types import SimpleNamespace
    P = SimpleNamespace(profile=SimpleNamespace(limit=lambda k, d: d))
    assert CW.wrapup(P, "Foundry-Kilo-Local") == 229376 - 24000          # 205,376 < Red
    assert CW.hard_stop(P, "Foundry-Kilo-Local") == 229376 - 6000
    assert CW.context_of([{"info": {"role": "user"}},
                          {"info": {"role": "assistant", "tokens": {
                              "input": 21806, "cache": {"read": 207906}}}}]) == 229712


def test_a_busy_session_near_its_window_commits_and_hands_off_free(tmp_path):
    clock = Clock()
    n = []

    def script(ctx):
        n.append(ctx["prompt"])
        if "The coordinator stopped your last step" in ctx["prompt"]:
            return {"writes": {"src/a.txt": "half\n"}, "commit": "T1: wip triage rows 1-40",
                    "final": NOT_DONE}
        return good(ctx)
    k = BusyKilo(script, [150000, 190000, 207000])
    c = env(tmp_path, k, clock)
    for _ in range(30):
        c.tick()
        clock.advance(31)
    t = c.store.task("game", "T1")
    assert t["state"] == S.COMPLETE, (t["state"], t["next_action"])
    assert len(k.aborts) == 1                             # stopped once, at 207k
    [w] = events(c, "context_wrapup")
    assert w["context"] == 207000 and w["threshold"] == 205376
    wrap = [p for p in n if "The coordinator stopped your last step" in p]
    assert len(wrap) == 1 and "207,000 tokens of 229,376" in wrap[0]
    [h] = events(c, "context_handoff")
    assert "context wrap-up" in h["why"]
    # the fresh session continues from the WIP commit; no attempt was charged
    assert [s["attempt"] for s in k.starts] == [1, 1]
    assert any("rows 1-40 triaged" in p for p in n[-1:])
    assert "context_wrapup" not in (t["data"] or {})


def test_under_the_stop_line_nothing_happens(tmp_path):
    clock = Clock()
    k = BusyKilo(lambda ctx: good(ctx), [120000, 150000])
    c = env(tmp_path, k, clock)
    for _ in range(6):
        c.tick()
        clock.advance(31)
    assert not k.aborts and not events(c, "context_wrapup")
    assert c.store.task("game", "T1")["state"] == S.IMPLEMENTING
