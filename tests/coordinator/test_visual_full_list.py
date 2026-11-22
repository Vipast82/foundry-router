"""After two visual fix rounds every batch is reviewed before anything goes back, and the
one list tells the fix session to re-capture and check every frame (visual_approval.py;
item 11 of COORDINATOR_AUTOMATION_NEEDS). POL-7 went through 3+ rounds a day, each fixing
some overlaps and adding others, because the first batch with a finding ended the review."""

from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace

from foundry_coordinator.visual_approval import FULL_LIST_NOTE, step

PNG = b"\x89PNG\r\n\x1a\n" + b"frame"


class Store:
    def __init__(self, rounds=0):
        self.ops, self.appr, self.events = {}, [], []
        self.data = {"visual_changes_rounds": rounds}

    def task(self, pid, tid):
        return {"task_id": tid, "data": dict(self.data)}

    def merge_data(self, pid, tid, **kw):
        self.data.update(kw)

    def update_task(self, pid, tid, **k):
        self.note = k.get("next_action", "")

    def op_begin(self, key, *a):
        if key in self.ops:
            return self.ops[key]
        self.ops[key] = {"status": "started"}
        return None

    def op_update(self, key, status, result):
        self.ops[key] = {"status": status, "result": result}

    def session(self, sid):
        return None

    def add_session(self, *a, **k):
        pass

    def update_session(self, *a, **k):
        pass

    def event(self, pid, tid, kind, **k):
        self.events.append((kind, k.get("detail") or {}))

    def approvals(self, pid, tid, kind=None):
        return []

    def add_approval(self, *a):
        self.appr.append(a)


def env(tmp_path, rounds, bad, n=5, cfg=None):
    """`n` frames in batches of 2; the frames in `bad` have a finding."""
    rels = [f"docs/evidence/POL-7/f{i}.png" for i in range(n)]
    arts = {}
    for i, rel in enumerate(rels):
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        blob = PNG + bytes([i])
        p.write_bytes(blob)
        arts[rel] = hashlib.sha256(blob).hexdigest()
    raw = {"approval": {"visual_model": True, "visual_batch_frames": 2,
                        "visual_rounds_max": 8, "visual_rounds_before_judge": 8, **(cfg or {})}}
    profile = SimpleNamespace(workspace=str(tmp_path), in_workspace=lambda p: True, raw=raw,
                              persona=lambda role, task: "vision")
    P = SimpleNamespace(profile=profile, id="animaluprising",
                        ws=SimpleNamespace(head=lambda: "rev", status=lambda: []),
                        adapter=SimpleNamespace(approval_artifacts=lambda *_: dict(arts)))
    spec = SimpleNamespace(task_id="POL-7", scope=list(rels), spec="no overlaps")
    rule = SimpleNamespace(artifacts=["docs/evidence/{task}/*.png"], kind="visual")
    sent = []

    def chat(persona, messages, **kw):
        paths = [p["text"].split(" ")[2].rstrip(",") for p in messages[-1]["content"]
                 if p.get("type") == "text" and p["text"].startswith("Required frame ")]
        sent.append(paths)
        frames = [{"path": r, "sha256": arts[r], "image_seen": True, "subject_present": True,
                   "validity": "ok", "observation": "seen",
                   "findings": [f"{r}: the timer overlaps the wave label"] if r in bad else []}
                  for r in paths]
        verdict = "changes_required" if any(f["findings"] for f in frames) else "approve"
        return json.dumps({"verdict": verdict, "frames": frames}), {"served_model": "vm"}
    P.foundry = SimpleNamespace(
        persona_info=lambda _: {"exists": True, "enabled": True, "strict": True,
                                "client_allowed": True, "routes": [{"model": "vm", "vision": True}]},
        bind=lambda *a, **k: None, chat=chat)
    store = Store(rounds)
    back = []
    c = SimpleNamespace(store=store, clock=lambda: 100.0, _valid_approval=lambda *a: None,
                        reject_human=lambda pid, tid, findings, actor: back.append(list(findings)))
    task = {"task_id": "POL-7", "run_id": "run", "session_id": "s", "attempt": 1}
    return SimpleNamespace(c=c, P=P, spec=spec, rule=rule, task=task, store=store, sent=sent,
                           back=back, rels=rels)


def kinds(e):
    return [k for k, _ in e.store.events]


def test_before_two_fix_rounds_a_batch_with_a_finding_goes_back_at_once(tmp_path):
    e = env(tmp_path, rounds=1, bad={"docs/evidence/POL-7/f0.png", "docs/evidence/POL-7/f4.png"})
    step(e.c, e.P, e.task, e.spec, e.rule, "rev")
    assert len(e.sent) == 1 and len(e.back) == 1               # the old behaviour
    assert len(e.back[0]) == 1 and "f0.png" in e.back[0][0]
    assert "visual_findings_held" not in kinds(e)


def test_replay_after_two_fix_rounds_every_batch_is_reviewed_and_one_list_goes_back(tmp_path):
    e = env(tmp_path, rounds=2, bad={"docs/evidence/POL-7/f0.png", "docs/evidence/POL-7/f4.png"})
    step(e.c, e.P, e.task, e.spec, e.rule, "rev")              # batch 1: f0 has a finding
    assert e.back == [] and kinds(e).count("visual_findings_held") == 1
    assert "1 finding(s) held; 3 frame(s) left" in e.store.note
    step(e.c, e.P, e.task, e.spec, e.rule, "rev")              # batch 2: clean
    assert e.back == []
    step(e.c, e.P, e.task, e.spec, e.rule, "rev")              # batch 3: f4 has a finding
    assert [len(b) for b in e.sent] == [2, 2, 1]
    assert sorted(sum(e.sent, [])) == sorted(e.rels)           # every frame looked at once
    [lst] = e.back
    assert lst[0] == FULL_LIST_NOTE.format(n=2, round=3)
    assert "re-capture EVERY frame named below" in lst[0]
    assert [x.split(":")[0] for x in lst[1:]] == ["docs/evidence/POL-7/f0.png",
                                                 "docs/evidence/POL-7/f4.png"]
    assert e.store.data["visual_changes_rounds"] == 3 and e.store.data["visual_round"] is None
    full = [d for k, d in e.store.events if k == "visual_approval_changes"]
    assert full[0]["complete_list"] is True and full[0]["round"] == 3
    assert e.store.appr == []                                  # nothing was approved


def test_the_last_batch_clean_still_sends_the_held_findings(tmp_path):
    e = env(tmp_path, rounds=2, bad={"docs/evidence/POL-7/f1.png"})
    for _ in range(3):
        step(e.c, e.P, e.task, e.spec, e.rule, "rev")
    assert len(e.sent) == 3 and e.store.appr == []
    [lst] = e.back
    assert lst[0] == FULL_LIST_NOTE.format(n=1, round=3) and "f1.png" in lst[1]
    assert "visual_full_list" in kinds(e)


def test_a_set_that_fits_one_batch_gets_the_recapture_instruction_too(tmp_path):
    e = env(tmp_path, rounds=2, bad={"docs/evidence/POL-7/f0.png"}, n=2)
    step(e.c, e.P, e.task, e.spec, e.rule, "rev")
    [lst] = e.back
    assert lst[0] == FULL_LIST_NOTE.format(n=1, round=3) and len(lst) == 2
    # before the limit there is no such line
    e2 = env(tmp_path / "b", rounds=0, bad={"docs/evidence/POL-7/f0.png"}, n=2)
    step(e2.c, e2.P, e2.task, e2.spec, e2.rule, "rev")
    assert len(e2.back[0]) == 1


def test_the_number_of_rounds_is_a_setting_and_zero_means_from_the_first_review(tmp_path):
    bad = {"docs/evidence/POL-7/f0.png"}
    e = env(tmp_path, rounds=0, bad=bad, cfg={"visual_full_list_after_rounds": 0})
    step(e.c, e.P, e.task, e.spec, e.rule, "rev")
    assert e.back == [] and "visual_findings_held" in kinds(e)
    e2 = env(tmp_path / "b", rounds=4, bad=bad, cfg={"visual_full_list_after_rounds": 9})
    step(e2.c, e2.P, e2.task, e2.spec, e2.rule, "rev")
    assert len(e2.back) == 1 and len(e2.back[0]) == 1          # not yet: at once, as before
    from foundry_automation import schema
    f = schema.find_field("approval.visual_full_list_after_rounds")
    assert f is not None and f.default == 2


def test_a_new_revision_starts_a_new_round_and_forgets_what_was_held(tmp_path):
    e = env(tmp_path, rounds=2, bad={"docs/evidence/POL-7/f0.png"})
    step(e.c, e.P, e.task, e.spec, e.rule, "rev")
    assert e.store.data["visual_round"]["revision"] == "rev"
    # the sprint committed a fix: another revision; what was held for "rev" is not sent
    e.P.ws = SimpleNamespace(head=lambda: "rev2", status=lambda: [])
    step(e.c, e.P, e.task, e.spec, e.rule, "rev2")
    # reviewed again from the frame that had the finding; f1 was clean at these bytes and
    # is not sent twice
    assert e.sent[-1] == ["docs/evidence/POL-7/f0.png", "docs/evidence/POL-7/f2.png"]
    assert e.store.data["visual_round"]["revision"] == "rev2"
    assert len(e.store.data["visual_round"]["findings"]) == 1 and e.back == []
