"""The AnimalUprising BUILD handoff reads the committed plan, not model prose."""

from types import SimpleNamespace

from foundry_coordinator.adapters.base import PASS, FAIL
from foundry_coordinator.projects.animaluprising.adapter import AnimalUprisingAdapter


def test_open_build_rows_and_final_gate_are_separate_from_close(monkeypatch):
    plan = ("## Checklist\n- [x] 0 setup\n- [ ] 1 build screens\n"
            "- [ ] 2 docs, gate PASS, done_check PASS, pushed\n")
    ws = SimpleNamespace(show_at=lambda revision, path: plan)
    spec = SimpleNamespace(task_id="UI-02", data={"plan": "docs/progress/UI-02_PLAN.md",
                                                    "tier": "B"})
    adapter = AnimalUprisingAdapter.__new__(AnimalUprisingAdapter)
    monkeypatch.setattr(adapter, "_gate_binding",
                        lambda *_: SimpleNamespace(result=FAIL))
    pending = adapter.pending_build_steps(spec, "abc123", ws)
    assert len(pending) == 2 and "build screens" in pending[0]
    assert "final BUILD gate" in pending[1]

    monkeypatch.setattr(adapter, "_gate_binding",
                        lambda *_: SimpleNamespace(result=PASS))
    assert adapter.pending_build_steps(spec, "abc123", ws) == pending[:1]


def test_no_structured_committed_checklist_uses_normal_verification():
    ws = SimpleNamespace(show_at=lambda revision, path: "# Plan\n- [ ] work\n")
    spec = SimpleNamespace(task_id="UI-02", data={"tier": "B"})
    adapter = AnimalUprisingAdapter.__new__(AnimalUprisingAdapter)
    assert adapter.pending_build_steps(spec, "abc123", ws) is None
