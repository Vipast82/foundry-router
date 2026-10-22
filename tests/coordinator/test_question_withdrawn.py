"""A session request Kilo withdraws on its own (it times some out) must not leave
the task AWAITING_HUMAN while the session keeps working: the coordinator returns
it to the state it came from. Since W1.1 an implementer's `question` is answered
by the coordinator itself, so the request that still reaches a person is a Kilo
permission ask."""

import pytest

from foundry_coordinator import states as S
from foundry_coordinator.adapters.scripted import ScriptedClient
from tests.coordinator.helpers import coordinator, make_repo, write_profile

T1 = {"id": "T1", "title": "Make the thing", "spec": "Write src/a.txt containing OK.",
      "scope": ["src/a.txt"]}


def agent(ctx):
    if not ctx.get("answers"):
        return {"question": {"text": "permission edit for .kilo/tally/r1.txt",
                             "kind": "permission"}}
    return {"writes": {"src/a.txt": "OK"}, "commit": "T1: work",
            "final": '```coordinator-report\n{"status": "DONE", "summary": "did it"}\n```'}


class Withdrawing(ScriptedClient):
    def __init__(self, *a, fail_reply=False, **kw):
        super().__init__(*a, **kw)
        self.fail_reply = fail_reply

    def withdraw(self, sid):
        """Kilo drops the request and the session carries on by itself."""
        self.answers[sid] = [["(withdrawn)"]]
        self.sessions[sid]["pending"] = True

    def answer(self, session_id, request_id, answers, *, directory, kind="question"):
        if self.fail_reply:
            raise RuntimeError(f"HTTP 404 QuestionNotFoundError {request_id}")
        return super().answer(session_id, request_id, answers, directory=directory, kind=kind)


@pytest.fixture
def env(tmp_path):
    repo, _ = make_repo(tmp_path, "game", [T1])
    prof = write_profile(tmp_path, "game", repo)
    now = [1_000_000.0]

    def build(**kw):
        client = Withdrawing(agent, project_id="game", **kw)
        c = coordinator(tmp_path, [prof], {"game": client}, clock=lambda: now[0])
        c.approve("game", "T1", [], "victor")
        for _ in range(20):
            c.tick()
            now[0] += 5
            if c.store.task("game", "T1")["state"] == S.AWAITING_HUMAN:
                break
        assert c.store.task("game", "T1")["state"] == S.AWAITING_HUMAN
        return c, client, now
    return build


def _until(c, now, state, step=20, limit=40):
    for _ in range(limit):
        if c.store.task("game", "T1")["state"] == state:
            return True
        c.tick()
        now[0] += step
    return c.store.task("game", "T1")["state"] == state


def test_a_pending_question_keeps_waiting(env):
    c, _, now = env()
    for _ in range(10):
        c.tick()
        now[0] += 30
    assert c.store.task("game", "T1")["state"] == S.AWAITING_HUMAN


def test_a_withdrawn_question_resumes_after_the_grace_period(env):
    c, client, now = env()
    client.withdraw(c.store.task("game", "T1")["session_id"])
    c.tick()                                               # first seen missing
    now[0] += 30
    c.tick()
    assert c.store.task("game", "T1")["state"] == S.AWAITING_HUMAN   # still in the grace
    assert _until(c, now, S.COMPLETE)
    kinds = [e["kind"] for e in c.store.events("game", limit=500)]
    assert "question_withdrawn" in kinds
    assert not c.store.task("game", "T1")["data"].get("question")


def test_zero_grace_never_resumes_on_its_own(env):
    c, client, now = env()
    c.projects["game"].profile.limits["question_withdrawn_grace_seconds"] = 0
    client.withdraw(c.store.task("game", "T1")["session_id"])
    for _ in range(10):
        c.tick()
        now[0] += 60
    assert c.store.task("game", "T1")["state"] == S.AWAITING_HUMAN


def test_answering_a_withdrawn_question_resumes_instead_of_failing(env):
    c, client, now = env(fail_reply=True)
    client.withdraw(c.store.task("game", "T1")["session_id"])
    c.answer("game", "T1", [["Disable for this project"]])
    assert c.store.task("game", "T1")["state"] == S.IMPLEMENTING
    assert _until(c, now, S.COMPLETE)


def test_a_failed_reply_to_a_pending_question_still_raises(env):
    c, _, _ = env(fail_reply=True)
    with pytest.raises(RuntimeError):
        c.answer("game", "T1", [["Disable for this project"]])
    assert c.store.task("game", "T1")["state"] == S.AWAITING_HUMAN
