"""The coordinator's live Kilo checkpoint: while an implementer session works,
the coordinator audits it itself and hands the result to the adapter (a file
the session reads), rate-limited, advisory, never touching the task."""

from foundry_coordinator import states as S
from foundry_coordinator.adapters.base import SessionState
from foundry_coordinator.adapters.scripted import ScriptedClient
from tests.coordinator.helpers import coordinator, make_repo, write_ok, write_profile

T1 = {"id": "T1", "title": "Make the thing", "spec": "Write src/a.txt containing OK.",
      "scope": ["src/a.txt"]}


class Busy(ScriptedClient):
    """Reports busy for the first `n` polls of each prompted session."""
    def __init__(self, *a, n=6, **kw):
        super().__init__(*a, **kw)
        self.n = n

    def state(self, sid, *, directory, since_ts):
        s = self.sessions.get(sid)
        if s and s.get("pending") and s.setdefault("polls", 0) < self.n:
            s["polls"] += 1
            return SessionState(status="busy")
        return super().state(sid, directory=directory, since_ts=since_ts)

    def transcript(self, sid, *, directory=None):
        return [{"info": {"role": "assistant", "id": "m1"}, "parts": []}]


def _run(tmp_path, every=None, fail=False):
    repo, _ = make_repo(tmp_path, "game", [T1])
    prof = write_profile(tmp_path, "game", repo)
    now = [1_000_000.0]
    c = coordinator(tmp_path, [prof], {"game": Busy(write_ok("src/a.txt"), project_id="game")},
                    clock=lambda: now[0])
    calls = []

    def live(sid, transcript, revision):
        if fail:
            raise RuntimeError("audit broke")
        calls.append((sid, len(transcript), revision, now[0]))
    c.projects["game"].adapter.live_session_audit = live
    if every is not None:
        c.projects["game"].profile.limits["live_checkpoint_seconds"] = every
    c.approve("game", "T1", [], "victor")
    for _ in range(40):
        c.tick()
        now[0] += 50                                   # 50 s between ticks
        if c.store.task("game", "T1")["state"] == S.COMPLETE:
            break
    return c, calls


def test_working_session_is_audited_every_two_minutes(tmp_path):
    c, calls = _run(tmp_path)
    assert c.store.task("game", "T1")["state"] == S.COMPLETE
    assert calls, "a busy implementer session gets a live checkpoint"
    stamps = [t for *_, t in calls]
    assert all(b - a >= 120 for a, b in zip(stamps, stamps[1:]))
    assert len(calls) < 6                              # six busy polls, rate-limited
    assert all(n == 1 and rev for _, n, rev, _ in calls)


def test_zero_interval_turns_it_off(tmp_path):
    _, calls = _run(tmp_path, every=0)
    assert calls == []


def test_a_failed_audit_is_advisory(tmp_path):
    c, _ = _run(tmp_path, fail=True)
    assert c.store.task("game", "T1")["state"] == S.COMPLETE
    kinds = [e["kind"] for e in c.store.events("game", limit=500)]
    assert "live_checkpoint_failed" in kinds
