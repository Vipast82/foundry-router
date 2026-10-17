"""Doctor must test each role with the client key that actually runs it."""
from foundry_coordinator.cli import doctor
from tests.coordinator.helpers import coordinator, make_repo, write_profile, write_ok


def setup(tmp_path, worker=True, reviewer=True):
    root, _ = make_repo(tmp_path, 'game', [dict(id='T1', title='test', spec='test', scope=['src/a.txt'])])
    profile = write_profile(tmp_path, 'game', root)
    c = coordinator(tmp_path, [profile], {'game': write_ok('src/a.txt')})
    p = c.projects['game']
    direct = ([{'id': 'Foundry-Kilo-Sonnet'}] if reviewer else [])
    p.foundry._req = lambda *a, **k: ({'data': direct}, {})
    p.client.inference_models = lambda _: ({'Foundry-Kilo-Local': {'id': 'Foundry-Kilo-Local'}} if worker else {})
    return c


def test_local_worker_does_not_need_coordinator_key_access(tmp_path, capsys):
    assert doctor(setup(tmp_path)) == 0
    output = capsys.readouterr().out
    assert 'worker inference key' in output
    assert 'coordinator key' in output


def test_missing_direct_reviewer_still_fails(tmp_path, capsys):
    assert doctor(setup(tmp_path, reviewer=False)) == 1
    assert 'Foundry-Kilo-Sonnet not offered to coordinator key' in capsys.readouterr().out


def test_missing_worker_model_still_fails(tmp_path, capsys):
    assert doctor(setup(tmp_path, worker=False)) == 1
    assert 'Foundry-Kilo-Local not offered to worker inference key' in capsys.readouterr().out


def _canonical(tmp_path, coord_ids, kilo_ids):
    """A profile whose reviewer is the canonical Foundry-Kilo-Review."""
    root, _ = make_repo(tmp_path, 'game', [dict(id='T1', title='test', spec='test', scope=['src/a.txt'])])
    profile = write_profile(tmp_path, 'game', root)
    text = profile.read_text().replace('reviewer = "Foundry-Kilo-Sonnet"',
                                       'reviewer = "Foundry-Kilo-Review"', 1)
    profile.write_text(text)
    c = coordinator(tmp_path, [profile], {'game': write_ok('src/a.txt')})
    p = c.projects['game']
    p.foundry._req = lambda *a, **k: ({'data': [{'id': i} for i in coord_ids]}, {})
    p.client.inference_models = lambda _: {i: {'id': i} for i in kilo_ids}
    return c


def test_coordinator_key_with_only_a_legacy_alias_gets_an_actionable_hint(tmp_path, capsys):
    c = _canonical(tmp_path, ['Foundry-Kilo-Sonnet'],
                   ['Foundry-Kilo-Local', 'Foundry-Kilo-Review'])
    assert doctor(c) == 1
    out = capsys.readouterr().out
    assert "Foundry-Kilo-Review not offered to coordinator key" in out
    assert "Kilo's key can use it, so add Foundry-Kilo-Review to the coordinator" in out
    assert "legacy aliases don't authorize Foundry-Kilo-Review" in out


def test_frontier_only_coordinator_key_with_canonical_reviewer_passes(tmp_path, capsys):
    # the coordinator key legitimately omits Local; Kilo's key carries the worker
    c = _canonical(tmp_path, ['Foundry-Kilo-Review', 'Foundry-Kilo-Opus'],
                   ['Foundry-Kilo-Local', 'Foundry-Kilo-Review'])
    assert doctor(c) == 0
    out = capsys.readouterr().out
    assert 'Foundry-Kilo-Review' in out and 'coordinator key' in out
