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
