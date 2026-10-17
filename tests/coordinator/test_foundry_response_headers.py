"""Real serving identity must survive ordinary HTTP header normalization."""
import pytest

from foundry_coordinator.adapters.foundry import FoundryClient


@pytest.mark.parametrize('header', [
    'x-foundry-served-model', 'X-Foundry-Served-Model', 'X-FOUNDRY-SERVED-MODEL',
])
def test_chat_serving_identity_is_case_insensitive(header):
    client = FoundryClient('http://unused.invalid')
    client._req = lambda *a, **kw: ({
        'model': 'Foundry-Kilo-Visual-Review',
        'choices': [{'message': {'content': 'observed'}, 'finish_reason': 'stop'}],
    }, {header: 'claude-sonnet-5-5', 'x-foundry-served-backend': 'meridian'})
    text, meta = client.chat('Foundry-Kilo-Visual-Review', [])
    assert text == 'observed'
    assert meta['served_model'] == 'claude-sonnet-5-5'
    assert meta['served_backend'] == 'meridian'


def test_chat_without_serving_header_preserves_response_model():
    client = FoundryClient('http://unused.invalid')
    client._req = lambda *a, **kw: ({'model': 'response-model', 'choices': []}, {})
    _, meta = client.chat('persona', [])
    assert meta['served_model'] == 'response-model'
    assert meta['served_backend'] is None
