"""The opt-in visual sign-off must be tied to actual, current frame bytes."""

import hashlib
import json
from types import SimpleNamespace

from foundry_coordinator.visual_approval import _required, _valid_verdict, step


PNG = b"\x89PNG\r\n\x1a\n" + b"test-frame-bytes"


def _review_setup(tmp_path):
    rel = "docs/evidence/UI-02/hud_phone.png"
    path = tmp_path / rel
    path.parent.mkdir(parents=True)
    path.write_bytes(PNG)
    digest = hashlib.sha256(PNG).hexdigest()
    profile = SimpleNamespace(workspace=str(tmp_path), in_workspace=lambda p: p == rel)
    project = SimpleNamespace(profile=profile)
    spec = SimpleNamespace(task_id="UI-02", scope=[rel])
    rule = SimpleNamespace(artifacts=["docs/evidence/{task}/*.png"])
    return project, spec, rule, rel, digest


def test_visual_review_requires_current_bytes_and_all_frame_observations(tmp_path):
    project, spec, rule, rel, digest = _review_setup(tmp_path)
    frames, problem = _required(project, spec, rule, {rel: digest})
    assert not problem
    assert frames == [(rel, "image/png", PNG)]
    answer = {"verdict": "approve", "frames": [{"path": rel, "sha256": digest,
              "image_seen": True, "subject_present": True, "validity": "ok",
              "observation": "HUD labels are legible", "findings": []}]}
    assert _valid_verdict(answer, frames, {rel: digest}) == ("approve", [])
    answer["frames"][0]["image_seen"] = False
    assert _valid_verdict(answer, frames, {rel: digest})[0] == "uncertain"
    answer["frames"][0]["image_seen"] = True
    answer["frames"][0]["findings"] = ["menu overlaps HUD"]
    assert _valid_verdict(answer, frames, {rel: digest})[0] == "changes_required"
    answer["frames"][0]["findings"] = []
    answer["frames"][0]["validity"] = "occluded"
    assert _valid_verdict(answer, frames, {rel: digest})[0] == "changes_required"


def test_visual_review_refuses_missing_or_changed_required_frame(tmp_path):
    project, spec, rule, rel, digest = _review_setup(tmp_path)
    assert "missing" in _required(project, spec, rule, {})[1]
    (tmp_path / rel).write_bytes(PNG + b"changed")
    assert "changed" in _required(project, spec, rule, {rel: digest})[1]


def test_visual_model_approval_sends_frame_and_records_bound_verdict(tmp_path):
    project, spec, rule, rel, digest = _review_setup(tmp_path)
    rule.kind = "visual"
    project.id = "animaluprising"
    project.profile.raw = {"approval": {"visual_model": True}}
    project.profile.persona = lambda role, task: "vision"
    project.ws = SimpleNamespace(head=lambda: "revision", status=lambda: [])
    project.adapter = SimpleNamespace(approval_artifacts=lambda *_: {rel: digest})
    packet = []
    foundry = SimpleNamespace(
        persona_info=lambda _: {"exists": True, "enabled": True, "strict": True,
                                "client_allowed": True,
                                "routes": [{"model": "vision-model", "vision": True}]},
        bind=lambda *args, **kwargs: None,
        chat=lambda persona, messages, **kwargs: (
            packet.append(messages[-1]["content"]) or json.dumps({
                "verdict": "approve", "frames": [{"path": rel, "sha256": digest,
                "image_seen": True, "subject_present": True, "validity": "ok",
                "observation": "HUD is visible and legible", "findings": []}]}),
            {"served_model": "vision-model"}))
    project.foundry = foundry

    class Store:
        def __init__(self):
            self.ops = {}
            self.approvals = []

        def update_task(self, *args, **kwargs):
            pass

        def op_begin(self, key, *args):
            if key in self.ops:
                return self.ops[key]
            self.ops[key] = {"status": "started"}
            return None

        def op_update(self, key, status, result):
            self.ops[key] = {"status": status, "result": result}

        def session(self, sid):
            return None

        def add_session(self, *args, **kwargs):
            pass

        def update_session(self, *args, **kwargs):
            pass

        def event(self, *args, **kwargs):
            pass

        def add_approval(self, *args):
            self.approvals.append(args)

    store = Store()
    coordinator = SimpleNamespace(store=store, clock=lambda: 100.0,
                                  _valid_approval=lambda *args: None)
    spec.spec = "HUD must be readable"
    task = {"task_id": "UI-02", "run_id": "run", "session_id": "primary",
            "attempt": 1}
    step(coordinator, project, task, spec, rule, "revision")
    assert packet and any(p.get("type") == "image_url" and
                          p["image_url"]["url"].startswith("data:image/png;base64,")
                          for p in packet[0])
    assert store.approvals[0][:5] == ("animaluprising", "UI-02", "visual",
                                    "revision", {rel: digest})
    step(coordinator, project, task, spec, rule, "revision")
    assert len(packet) == 1  # a completed call is never repeated
