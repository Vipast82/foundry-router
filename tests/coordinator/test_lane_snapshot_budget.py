"""A production-sized task needs a bounded snapshot, without its raw logs."""
from types import SimpleNamespace

import pytest

from foundry_coordinator.artifacts import ArtifactStore
from foundry_coordinator.proactive import Proactive
from foundry_coordinator.store import Store
from tests.coordinator.test_snapshot_scope import FakeWS


@pytest.mark.parametrize('budget,expected', [(None, 'too_large'), (2_000_000, 'consistent')])
def test_realistic_source_snapshot_uses_project_budget_and_excludes_evidence(tmp_path, budget, expected):
    workspace = tmp_path / 'game'
    workspace.mkdir()
    (workspace / 'main.luau').write_bytes(b'-- source\n' * 60_000)
    (workspace / 'UI-01_EVIDENCE_RAW.log').write_bytes(b'raw' * 1_000_000)
    (workspace / 'unapproved.luau').write_text('must not enter snapshot')
    raw = {'lanes': {}}
    if budget is not None:
        raw['lanes']['snapshot_max_bytes'] = budget
    project = SimpleNamespace(id='game', ws=FakeWS(),
        profile=SimpleNamespace(workspace=str(workspace), raw=raw))
    artifacts = ArtifactStore(Store(str(tmp_path / 'state.sqlite')), str(tmp_path / 'state'))
    lane = SimpleNamespace(artifacts=artifacts)
    spec = SimpleNamespace(scope=['main.luau', 'UI-01_EVIDENCE_RAW.log'])
    result = Proactive._snap(lane, project, {'task_id': 'T1', 'run_id': 'run'}, spec)
    assert result['status'] == expected
    if expected == 'consistent':
        assert set(result['manifest']) == {'main.luau'}
        assert len(artifacts.snapshot_file(result['snapshot_id'], 'main.luau', 'game', 'T1')) == 600_000
