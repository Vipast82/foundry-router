"""An exhausted repair budget gets at most one scoped frontier consultation."""
import json

import pytest

from foundry_coordinator import states as S
from foundry_coordinator.adapters.foundry import FoundryError
from tests.coordinator.helpers import FakeFoundry, coordinator, make_repo, run_until, write_profile


class Adviser(FakeFoundry):
    def __init__(self, answer):
        super().__init__()
        self.answer, self.guidance_calls = answer, []
        self.admission = {'Foundry-Kilo-Opus': {'admit': True}}

    def chat(self, persona, messages, *, timeout=None, **kwargs):
        if persona == 'Foundry-Kilo-Opus':
            self.guidance_calls.append({'messages': messages, 'timeout': timeout})
            return self.answer, {'served_model': 'opus'}
        return super().chat(persona, messages, **kwargs)


REPAIR = json.dumps({'action': 'repair', 'summary': 'Use the known correct output.',
                     'steps': ['Write the required OK result, then run the existing project check.']})


def setup(tmp_path, answer=REPAIR, enabled=True, succeeds=True, extra=1):
    task = {'id': 'T1', 'title': 'Make the thing', 'spec': 'Write src/a.txt containing OK.',
            'scope': ['src/a.txt']}
    root, _ = make_repo(tmp_path, 'game', [task])
    profile = write_profile(tmp_path, 'game', root, extra=f'''
        [retry_guidance]
        enabled = {str(enabled).lower()}
        persona = "Foundry-Kilo-Opus"
        extra_attempts = {extra}
    ''')
    def script(ctx):
        return {'writes': {'src/a.txt': 'OK' if succeeds else 'bad'}, 'commit': 'T1: repair'}
    foundry = Adviser(answer)
    c = coordinator(tmp_path, [profile], {'game': script}, foundry={'game': foundry})
    c.approve('game', 'T1', [], 'victor')
    # Set an exhausted, idle repair checkpoint; no inference has run.
    c.tick()
    c.store.update_task('game', 'T1', state=S.CHANGES_REQUIRED, attempt=3, sessions=3,
                        data={'findings': ['check project-check: FAIL'], 'baseline_dirty': {}})
    return c, foundry


def test_frontier_guidance_allows_one_verified_extra_repair(tmp_path):
    c, f = setup(tmp_path)
    assert run_until(c, lambda: c.store.task('game', 'T1')['state'] == S.COMPLETE)
    t = c.store.task('game', 'T1')
    assert t['attempt'] == 4 and len(f.guidance_calls) == 1
    assert t['data']['retry_guidance']['extra_attempts'] == 1
    assert f.guidance_calls[0]['timeout'] == 60
    b = next(b for b in f.binds if b['purpose'] == 'debug_advice')
    assert b['role'] == 'helper' and b['persona'] == 'Foundry-Kilo-Opus'
    assert t['reviewed_sha'] == t['head_sha']  # ordinary independent review still required


def test_failed_guided_repair_stops_without_resetting_budget(tmp_path):
    c, f = setup(tmp_path, succeeds=False)
    assert run_until(c, lambda: c.store.task('game', 'T1')['state'] == S.FAILED)
    assert c.store.task('game', 'T1')['attempt'] == 4
    assert len(f.guidance_calls) == 1
    c.tick()
    assert len(f.guidance_calls) == 1


@pytest.mark.parametrize('answer', ['not json', '{}',
    json.dumps({'action': 'repair', 'summary': 'Guess', 'steps': []}),
    json.dumps({'action': 'needs_human', 'summary': 'Needs a product decision', 'steps': []})])
def test_invalid_or_human_only_guidance_never_grants_a_retry(tmp_path, answer):
    c, f = setup(tmp_path, answer=answer)
    c.tick()
    assert c.store.task('game', 'T1')['state'] == S.FAILED
    assert len(f.guidance_calls) == 1
    assert c.retry_guidance.allowance(c.store.task('game', 'T1')) == 0


def test_disabled_policy_preserves_original_stop(tmp_path):
    c, f = setup(tmp_path, enabled=False)
    c.tick()
    assert c.store.task('game', 'T1')['state'] == S.FAILED and not f.guidance_calls


def test_unavailable_quota_does_not_bypass_gate(tmp_path):
    c, f = setup(tmp_path)
    f.admission['Foundry-Kilo-Opus'] = {'admit': False, 'reason': 'weekly 75% used'}
    c.tick()
    t = c.store.task('game', 'T1')
    assert t['state'] == S.FAILED and not f.guidance_calls
    assert '75%' in t['data']['retry_guidance']['reason']


def test_transient_usage_429_retries_adviser_without_spending_attempt(tmp_path):
    c, f = setup(tmp_path)
    admit = f.helper_admission
    calls = 0

    def temporarily_unavailable(persona):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise FoundryError('anthropic usage HTTP 429')
        return admit(persona)

    f.helper_admission = temporarily_unavailable
    c.tick()
    t = c.store.task('game', 'T1')
    assert t['state'] == S.CHANGES_REQUIRED and t['attempt'] == 3
    assert t['data']['retry_guidance']['status'] == 'deferred'
    assert not f.guidance_calls
    c.tick()
    assert calls == 1  # bounded backoff, not a tight polling loop
    c.store.merge_data('game', 'T1', retry_guidance={**t['data']['retry_guidance'],
                                                     'retry_at': 0})
    assert run_until(c, lambda: c.store.task('game', 'T1')['state'] == S.COMPLETE)
    assert len(f.guidance_calls) == 1


def test_restart_during_consultation_is_not_replayed(tmp_path):
    c, f = setup(tmp_path)
    c.store.merge_data('game', 'T1', retry_guidance={'status': 'calling', 'extra_attempts': 0})
    c.tick()
    assert c.store.task('game', 'T1')['state'] == S.FAILED and not f.guidance_calls
    assert c.store.task('game', 'T1')['data']['retry_guidance']['status'] == 'unknown'


def test_hard_session_limit_is_not_overridden(tmp_path):
    c, f = setup(tmp_path)
    c.store.update_task('game', 'T1', sessions=8)
    c.tick()
    assert c.store.task('game', 'T1')['state'] == S.FAILED and not f.guidance_calls


def test_user_pause_does_not_launch_guidance(tmp_path):
    c, f = setup(tmp_path)
    c.pause('game', 'T1', 'Victor is updating the service')
    c.tick()
    assert c.store.task('game', 'T1')['state'] == S.PAUSED
    assert not f.guidance_calls


def test_stale_revision_does_not_grant_an_extra_attempt(tmp_path, monkeypatch):
    c, f = setup(tmp_path)
    chat = f.chat
    def moving_source(*args, **kwargs):
        result = chat(*args, **kwargs)
        monkeypatch.setattr(c.projects['game'].ws, 'head', lambda: 'b' * 40)
        return result
    f.chat = moving_source
    c.tick()
    task = c.store.task('game', 'T1')
    assert task['state'] == S.FAILED
    assert 'stale' in task['data']['retry_guidance']['reason']
    assert c.retry_guidance.allowance(task) == 0


def test_consultation_without_extra_allowance_reports_then_stops(tmp_path):
    c, f = setup(tmp_path, extra=0)
    c.tick()
    assert c.store.task('game', 'T1')['state'] == S.FAILED
    assert len(f.guidance_calls) == 1
