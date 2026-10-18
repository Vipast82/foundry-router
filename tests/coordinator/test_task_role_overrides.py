"""Task-selected writers survive restarts without widening queue authority."""
import pytest

from foundry_coordinator import states as S
from foundry_coordinator.adapters.scripted import ScriptedClient
from foundry_coordinator.adapters.base import SessionState
from foundry_coordinator.profile import ProfileError, load
from tests.coordinator.helpers import (
    FakeFoundry, coordinator, make_repo, run_until, write_ok, write_profile,
)

OVERRIDE = '''
[role_overrides.ui]
tasks = ["UI-01", "UI-02", "UI-03", "UI-04", "UI-05", "UI-06"]
implementer = "Foundry-Kilo-Sonnet"
reviewer = "Foundry-Kilo-Opus"
'''
TASK = {"id": "UI-01", "title": "UI", "scope": ["src/a.txt"]}


def test_ui_selection_restart_and_intervening_work(tmp_path):
    root, _ = make_repo(tmp_path, "game", [TASK])
    prof = write_profile(tmp_path, "game", root, extra=OVERRIDE)
    for _ in range(2):
        p = load(str(prof))
        assert all(p.persona("implementer", f"UI-0{i}") == "Foundry-Kilo-Sonnet"
                   for i in range(1, 7))
        for tid in ("GF-SEC1", "GF-UX1", "UI-010", "UI-07", ""):
            assert p.persona("implementer", tid) == "Foundry-Kilo-Local"


@pytest.mark.parametrize("extra", [
    OVERRIDE.replace('"UI-01", "UI-02"', '"UI-*", "UI-02"'),
    OVERRIDE.replace('implementer = "Foundry-Kilo-Sonnet"', 'implementor = "typo"'),
    OVERRIDE + '\n[role_overrides.other]\ntasks=["UI-01"]\nimplementer="other"\n',
])
def test_invalid_override_is_refused(tmp_path, extra):
    root, _ = make_repo(tmp_path, "game", [TASK])
    prof = write_profile(tmp_path, "game", root, extra=extra)
    with pytest.raises(ProfileError):
        load(str(prof))


def test_lifecycle_uses_ui_writer_and_independent_review_without_approving_next(tmp_path):
    root, _ = make_repo(tmp_path, "game", [TASK, {**TASK, "id": "UI-02"}])
    prof = write_profile(tmp_path, "game", root, extra=OVERRIDE)
    ff = FakeFoundry()
    c = coordinator(tmp_path, [prof], {"game": write_ok("src/a.txt")}, foundry={"game": ff})
    c.approve("game", "UI-01", [], "Victor")
    assert run_until(c, lambda: c.store.task("game", "UI-01")["state"] == S.COMPLETE)
    assert {b["persona"] for b in ff.binds if b["role"] == "implementer"} == {"Foundry-Kilo-Sonnet"}
    assert c.store.reviews("game", "UI-01")[0]["persona"] == "Foundry-Kilo-Opus"
    next_task = c.store.task("game", "UI-02")
    assert next_task is None or next_task["state"] == S.QUEUED


def test_resume_binds_new_writer_before_prompt_and_preserves_author_history(tmp_path):
    root, _ = make_repo(tmp_path, "game", [TASK])
    prof = write_profile(tmp_path, "game", root)
    ff = FakeFoundry()

    class Hang(ScriptedClient):
        def state(self, sid, *, directory, since_ts):
            return SessionState(status="busy")

    client = Hang(lambda ctx: {})
    c = coordinator(tmp_path, [prof], {"game": client}, foundry={"game": ff})
    c.approve("game", "UI-01", [], "Victor")
    assert run_until(c, lambda: c.store.task("game", "UI-01")["state"] == S.IMPLEMENTING)
    sid = c.store.task("game", "UI-01")["session_id"]
    c.pause("game", "UI-01", "approved model switch")
    with prof.open("a") as f:
        f.write(OVERRIDE)
    c.projects["game"].profile = load(str(prof))
    original = client.prompt
    sent = []

    def prompt(sid, text, **kwargs):
        assert ff.binds[-1]["persona"] == kwargs["persona"] == "Foundry-Kilo-Sonnet"
        sent.append(sid)
        return original(sid, text, **kwargs)

    client.prompt = prompt
    result = c.resume("game", "UI-01")
    assert result["session_id"] == sid
    assert sent == [sid]
    session = c.store.session(sid)
    assert session["persona"] == "Foundry-Kilo-Sonnet"
    assert session["data"]["persona_history"] == ["Foundry-Kilo-Local"]
    # A later helper delivery must keep the new writer as well.
    assert c._prompt_primary(c.projects["game"], result, "helper findings", "test")
    assert sent == [sid, sid]


def test_primary_cannot_review_its_own_work_even_without_reviewer_override(tmp_path):
    root, _ = make_repo(tmp_path, "game", [TASK])
    extra = OVERRIDE.replace('reviewer = "Foundry-Kilo-Opus"', '')
    prof = write_profile(tmp_path, "game", root, extra=extra)
    c = coordinator(tmp_path, [prof], {"game": write_ok("src/a.txt")})
    c.approve("game", "UI-01", [], "Victor")
    assert run_until(c, lambda: c.store.task("game", "UI-01")["state"] == S.COMPLETE)
    assert c.store.reviews("game", "UI-01")[0]["persona"] == "Foundry-Kilo-Opus"


def test_binding_failure_never_sends_resumed_turn(tmp_path):
    from foundry_coordinator.adapters.foundry import FoundryError
    root, _ = make_repo(tmp_path, "game", [TASK])
    prof = write_profile(tmp_path, "game", root)
    ff = FakeFoundry()
    client = ScriptedClient(lambda ctx: {})
    c = coordinator(tmp_path, [prof], {"game": client}, foundry={"game": ff})
    c.approve("game", "UI-01", [], "Victor")
    assert run_until(c, lambda: c.store.task("game", "UI-01")["state"] == S.IMPLEMENTING)
    sid = c.store.task("game", "UI-01")["session_id"]
    c.pause("game", "UI-01", "switch")
    before = len(client.sessions[sid]["prompts"])
    def fail(*args, **kwargs):
        raise FoundryError("binding refused")
    ff.bind = fail
    with pytest.raises(RuntimeError, match="binding failed"):
        c.resume("game", "UI-01")
    assert len(client.sessions[sid]["prompts"]) == before
    assert c.store.task("game", "UI-01")["state"] == S.PAUSED
