"""Doctor reports persona validation honestly ("passed" is a pass, unknown
statuses are shown verbatim as WARN) and diagnoses the image persona through
Foundry's read-only media catalog, not the chat catalog, never generating."""
import pytest

from foundry_coordinator.cli import doctor
from tests.coordinator.helpers import coordinator, make_repo, write_profile, write_ok


def _setup(tmp_path, validation=None, media=None, image=False):
    root, _ = make_repo(tmp_path, 'game', [dict(id='T1', title='test', spec='test', scope=['src/a.txt'])])
    profile = write_profile(tmp_path, 'game', root)
    if image:
        text = profile.read_text().replace('deep_reviewer = "Foundry-Kilo-Opus"',
                                           'deep_reviewer = "Foundry-Kilo-Opus"\n'
                                           'image_generator = "Foundry-Kilo-Images"', 1)
        profile.write_text(text)
    c = coordinator(tmp_path, [profile], {'game': write_ok('src/a.txt')})
    p = c.projects['game']
    fm = {'backend': 'claude', 'served_model': 'sonnet', 'routing': 'strict'}
    if validation is not None:
        fm['validation'] = validation
    calls = []

    def req(method, path, *a, **k):
        calls.append((method, path))
        if path == '/media/models':
            if isinstance(media, Exception):
                raise media
            return (media if media is not None else {}), {}
        return {'data': [{'id': 'Foundry-Kilo-Sonnet', 'foundry': fm}]}, {}
    p.foundry._req = req
    p.client.inference_models = lambda _: {'Foundry-Kilo-Local': {'id': 'Foundry-Kilo-Local'}}
    return c, calls


def _row(out, label):
    return next(line for line in out.splitlines() if label in line)


@pytest.mark.parametrize('val', ['pass', 'passed', 'PASSED', 'ok', 'Valid', {'status': 'passed'}])
def test_passing_validation_statuses_are_pass(tmp_path, capsys, val):
    c, _ = _setup(tmp_path, validation=val)
    assert doctor(c) == 0
    assert _row(capsys.readouterr().out, 'persona reviewer').split()[0] == 'PASS'


@pytest.mark.parametrize('val', ['degraded-route', {'status': 'weird_status'}])
def test_unknown_validation_status_is_warn_and_shown_verbatim(tmp_path, capsys, val):
    c, _ = _setup(tmp_path, validation=val)
    assert doctor(c) == 0
    row = _row(capsys.readouterr().out, 'persona reviewer')
    assert row.split()[0] == 'WARN'
    assert (val if isinstance(val, str) else val['status']) in row


def test_unvalidated_persona_is_warn(tmp_path, capsys):
    c, _ = _setup(tmp_path)
    assert doctor(c) == 0
    row = _row(capsys.readouterr().out, 'persona reviewer')
    assert row.split()[0] == 'WARN' and 'not validated' in row


def test_image_persona_is_checked_against_the_media_catalog_not_chat(tmp_path, capsys):
    media = {'enabled': True, 'backend': 'cliproxyapi', 'backend_error': None,
             'models': [{'id': 'gpt-image-2.5-sunburst', 'role': 'final'}]}
    c, calls = _setup(tmp_path, validation='passed', media=media, image=True)
    assert doctor(c) == 0
    row = _row(capsys.readouterr().out, 'persona image_generator')
    assert row.split()[0] == 'PASS'
    assert 'media adapter' in row and 'gpt-image-2.5-sunburst' in row
    assert 'not offered' not in row
    # read-only: only GETs, and nothing that submits an image job
    assert all(m == 'GET' for m, _ in calls)
    assert not any('/media/jobs' in p or '/v1/images' in p for _, p in calls)


@pytest.mark.parametrize('media', [RuntimeError('403 missing media scope'), {'data': []}])
def test_unreachable_media_catalog_is_not_a_failure(tmp_path, capsys, media):
    c, calls = _setup(tmp_path, validation='passed', media=media, image=True)
    assert doctor(c) == 0
    row = _row(capsys.readouterr().out, 'persona image_generator')
    assert row.split()[0] in ('INFO', 'WARN')
    assert ('media: not checked by doctor (media personas are served by the media adapter, '
            'not the chat catalog)') in row
    assert not any('/media/jobs' in p or '/v1/images' in p for _, p in calls)


def test_disabled_media_is_warn_not_fail(tmp_path, capsys):
    c, _ = _setup(tmp_path, validation='passed', image=True,
                  media={'enabled': False, 'backend': None, 'models': []})
    assert doctor(c) == 0
    row = _row(capsys.readouterr().out, 'persona image_generator')
    assert row.split()[0] == 'WARN' and 'disabled' in row


def test_doctor_reports_lead_readiness_only_when_enabled(tmp_path, capsys):
    from types import SimpleNamespace
    c, _ = _setup(tmp_path, validation="passed")
    c.lead = SimpleNamespace(enabled=lambda P: False, readiness=lambda P, fresh=False: [])
    doctor(c)
    assert "lead " not in capsys.readouterr().out
    c.lead = SimpleNamespace(enabled=lambda P: True, readiness=lambda P, fresh=False: [
        {"name": "route_chain", "ok": False, "blocking": True,
         "detail": "routes [astra] != expected [opus, astra]"},
        {"name": "persona_strict", "ok": True, "blocking": True, "detail": "strict"}])
    assert doctor(c) == 1
    out = capsys.readouterr().out
    assert _row(out, "lead route_chain").strip().startswith("FAIL")
    assert _row(out, "lead persona_strict").strip().startswith("PASS")
