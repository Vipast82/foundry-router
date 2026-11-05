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


def test_frame_validity_and_verdict_synonyms_are_understood():
    """UI-D1 (2026-10-20): the prompt never named the allowed values, a model answered
    validity "valid" (and a verdict "approved"), and every good frame became an
    "invalid, occluded or wrong UI subject" CHANGES finding."""
    from foundry_coordinator import visual_approval as VA
    frames = [("a.png", "image/png", b"x"), ("b.png", "image/png", b"y")]
    arts = {"a.png": "1" * 64, "b.png": "2" * 64}

    def row(path, validity="ok", findings=()):
        return {"path": path, "sha256": arts[path], "image_seen": True, "subject_present": True,
                "validity": validity, "observation": "HUD visible", "findings": list(findings)}
    for verdict, validity in (("approve", "ok"), ("approved", "valid"), ("Approve", "OK"),
                              ("approve", True)):
        ans = {"verdict": verdict, "frames": [row("a.png", validity), row("b.png", validity)]}
        assert VA._valid_verdict(ans, frames, arts) == ("approve", []), (verdict, validity)
    ans = {"verdict": "changes requested", "frames": [row("a.png"), row("b.png", "ok", ["clipped"])]}
    assert VA._valid_verdict(ans, frames, arts) == ("changes_required", ["b.png: clipped"])
    ans = {"verdict": "approve", "frames": [row("a.png"), row("b.png", "occluded")]}
    got, findings = VA._valid_verdict(ans, frames, arts)
    assert got == "changes_required" and "invalid, occluded or wrong UI subject" in findings[0]
    assert '"ok", "invalid", "occluded", "missing_subject"' in VA.FRAME_FIELDS


def test_uncertain_word_over_clean_verified_frames_is_an_approval():
    """UI-02a (2026-10-21): the model returned verdict "uncertain" while every frame was
    seen, matched, valid and had no finding. Real gaps stay uncertain."""
    from foundry_coordinator import visual_approval as VA
    frames = [("a.png", "image/png", b"x"), ("b.png", "image/png", b"y")]
    arts = {"a.png": "1" * 64, "b.png": "2" * 64}

    def row(path, **kw):
        r = {"path": path, "sha256": arts[path], "image_seen": True, "subject_present": True,
             "validity": "ok", "observation": "HUD visible", "findings": []}
        r.update(kw)
        return r
    assert VA._valid_verdict({"verdict": "uncertain", "frames": [row("a.png"), row("b.png")]},
                             frames, arts) == ("approve", [])
    got, _ = VA._valid_verdict({"verdict": "uncertain",
                                "frames": [row("a.png"), row("b.png", image_seen=False)]}, frames, arts)
    assert got == "uncertain"                                    # an unseen frame: still not a pass
    got, f = VA._valid_verdict({"verdict": "uncertain",
                                "frames": [row("a.png"), row("b.png", findings=["clipped"])]}, frames, arts)
    assert got == "uncertain" and f == ["b.png: clipped"]        # a finding: not an approval


def test_frames_unchanged_since_closeout_lets_a_later_head_be_reviewed(tmp_path):
    from foundry_coordinator.visual_approval import _frames_unchanged_since
    project, spec, rule, rel, _ = _review_setup(tmp_path)
    changed = {"files": ["docs/plans/AS-D19_PLAN.md"]}
    project.ws = SimpleNamespace(head=lambda: "later", is_ancestor=lambda a, b: True,
                                 changed_files=lambda a, b: changed["files"])
    assert _frames_unchanged_since(project, spec, rule, "closeout")
    changed["files"] = ["docs/plans/x.md", rel]          # a named frame changed since
    assert not _frames_unchanged_since(project, spec, rule, "closeout")
    changed["files"] = ["docs/evidence/UI-02/new.png"]   # a new frame under the rule
    assert not _frames_unchanged_since(project, spec, rule, "closeout")
    project.ws.is_ancestor = lambda a, b: False          # history rewritten: fail closed
    changed["files"] = []
    assert not _frames_unchanged_since(project, spec, rule, "closeout")


def test_a_glob_scope_entry_requires_the_frames_it_matches(tmp_path):
    """2026-11-02 AS-P10: its plan's scope said `docs/evidence/AS-P10/*.png`; _required took
    the glob as a literal frame path, found it "missing" and the visual approval sent the
    approved sprint back four times, until the review budget (5/5) stopped it."""
    rels = ["docs/evidence/AS-P10/kit_front.png", "docs/evidence/AS-P10/kit_side.png"]
    arts = {}
    for rel in rels:
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(PNG + rel.encode())
        arts[rel] = hashlib.sha256(PNG + rel.encode()).hexdigest()
    arts["docs/evidence/AS-P10/notes.txt"] = "x"
    profile = SimpleNamespace(workspace=str(tmp_path), in_workspace=lambda p: True)
    project = SimpleNamespace(profile=profile)
    spec = SimpleNamespace(task_id="AS-P10", scope=["docs/evidence/AS-P10/*.png"])
    rule = SimpleNamespace(artifacts=["docs/evidence/{task}/*.png"])
    frames, problem = _required(project, spec, rule, arts)
    assert not problem
    assert [f[0] for f in frames] == rels
    # a glob that matches no frame is still a missing requirement
    assert "no frame matches" in _required(project, spec, rule, {})[1]


def test_approval_artifacts_list_the_frames_of_the_plans_own_subfolder_globs(tmp_path):
    """2026-11-02 AS-P10 (after v0.181.3): its plan names `docs/evidence/AS-P10/cand_A/*.png`;
    the approval rule's own pattern `docs/evidence/{task}/*.png` does not reach into
    sub-folders, so the 16 committed frames were never listed and the visual approval
    answered "no frame matches docs/evidence/AS-P10/cand_A/*.png"."""
    from foundry_coordinator.adapters.project import GenericAdapter
    for rel in ("docs/evidence/AS-P10/compare_A.png", "docs/evidence/AS-P10/cand_A/front.png",
                "docs/evidence/AS-P10/cand_B/front.png", "docs/evidence/AS-P10/notes/x.txt"):
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(PNG + rel.encode())
    profile = SimpleNamespace(workspace=str(tmp_path), in_workspace=lambda p: True)
    spec = SimpleNamespace(task_id="AS-P10", scope=[
        "docs/evidence/AS-P10/cand_A/*.png", "docs/evidence/AS-P10/cand_B/*.png",
        "docs/evidence/AS-P10/**", "src/x.lua"])
    arts = GenericAdapter(profile, "").approval_artifacts(spec, ["docs/evidence/{task}/*.png"])
    assert sorted(arts) == ["docs/evidence/AS-P10/cand_A/front.png",
                            "docs/evidence/AS-P10/cand_B/front.png",
                            "docs/evidence/AS-P10/compare_A.png"]
    # without such scope entries: the rule's pattern alone, as before
    spec.scope = ["docs/evidence/AS-P10/*.png"]
    assert sorted(GenericAdapter(profile, "").approval_artifacts(
        spec, ["docs/evidence/{task}/*.png"])) == ["docs/evidence/AS-P10/compare_A.png"]


def test_a_large_frame_set_is_reviewed_in_batches_and_approved_once_all_are_clean(tmp_path):
    """2026-11-04 POL: the polish re-audit makes ~200 captures and one request with all of
    them would pass the model's context and the API's request size. Frames go in batches
    ([approval] visual_batch_frames); a batch's clean frames join the task's memory, and
    the set's approval is recorded only once every frame is covered."""
    rels = [f"docs/evidence/UI-02/f{i}.png" for i in range(5)]
    arts = {}
    for i, rel in enumerate(rels):
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        blob = PNG + bytes([i])
        p.write_bytes(blob)
        arts[rel] = hashlib.sha256(blob).hexdigest()
    profile = SimpleNamespace(workspace=str(tmp_path), in_workspace=lambda p: True,
                              raw={"approval": {"visual_model": True,
                                                "visual_batch_frames": 2}},
                              persona=lambda role, task: "vision")
    project = SimpleNamespace(profile=profile, id="animaluprising",
                              ws=SimpleNamespace(head=lambda: "rev", status=lambda: []),
                              adapter=SimpleNamespace(approval_artifacts=lambda *_: dict(arts)))
    spec = SimpleNamespace(task_id="UI-02", scope=list(rels), spec="every frame clean")
    rule = SimpleNamespace(artifacts=["docs/evidence/{task}/*.png"], kind="visual")
    sent = []

    def chat(persona, messages, **kw):
        imgs = [p for p in messages[-1]["content"] if p.get("type") == "text"
                and p["text"].startswith("Required frame ")]
        paths = [p["text"].split(" ")[2].rstrip(",") for p in imgs]
        sent.append(paths)
        return json.dumps({"verdict": "approve", "frames": [
            {"path": r, "sha256": arts[r], "image_seen": True, "subject_present": True,
             "validity": "ok", "observation": "fine", "findings": []} for r in paths]}), \
            {"served_model": "vision-model"}
    project.foundry = SimpleNamespace(
        persona_info=lambda _: {"exists": True, "enabled": True, "strict": True,
                                "client_allowed": True,
                                "routes": [{"model": "vision-model", "vision": True}]},
        bind=lambda *a, **k: None, chat=chat)

    class Store:
        def __init__(self):
            self.ops, self.appr, self.data, self.events = {}, [], {}, []

        def task(self, pid, tid):
            return {"task_id": tid, "data": dict(self.data)}

        def merge_data(self, pid, tid, **kw):
            self.data.update(kw)

        def update_task(self, *a, **k):
            pass

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
            self.events.append(kind)

        def approvals(self, pid, tid, kind=None):
            return []

        def add_approval(self, *a):
            self.appr.append(a)

    store = Store()
    c = SimpleNamespace(store=store, clock=lambda: 100.0, _valid_approval=lambda *a: None)
    task = {"task_id": "UI-02", "run_id": "run", "session_id": "s", "attempt": 1}
    for _ in range(3):
        step(c, project, task, spec, rule, "rev")
    assert [len(b) for b in sent] == [2, 2, 1]
    assert sorted(sum(sent, [])) == sorted(rels)          # every frame seen exactly once
    assert store.events.count("visual_approval_batch") == 2
    assert len(store.appr) == 1 and store.appr[0][4] == arts   # one approval, whole set


def test_a_whole_evidence_folder_in_scope_requires_every_image_in_it(tmp_path):
    """2026-11-04 POL: plans listed docs/evidence/<id>/** (no image extension), so their
    captures were never required or reviewed. A whole-folder entry now requires all of them."""
    rels = ["docs/evidence/POL-9/a.png", "docs/evidence/POL-9/world/b.png"]
    arts = {}
    for i, rel in enumerate(rels):
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(PNG + bytes([i]))
        arts[rel] = hashlib.sha256(PNG + bytes([i])).hexdigest()
    project = SimpleNamespace(profile=SimpleNamespace(workspace=str(tmp_path),
                                                      in_workspace=lambda p: True))
    spec = SimpleNamespace(task_id="POL-9", scope=["docs/evidence/POL-9/**", "src/client/**"])
    rule = SimpleNamespace(artifacts=["docs/evidence/{task}/**/*.png"])
    frames, problem = _required(project, spec, rule, arts)
    assert not problem and sorted(f[0] for f in frames) == sorted(rels)
    spec.scope = ["src/client/**"]
    assert "names no required" in _required(project, spec, rule, arts)[1]


def _judge_env(rounds, cfg, rulings):
    from foundry_coordinator import visual_approval as VA
    events, rejected, approvals = [], [], []

    class Store:
        data = {"visual_changes_rounds": rounds}

        def task(self, pid, tid):
            return {"task_id": tid, "data": dict(self.data)}

        def merge_data(self, pid, tid, **kw):
            self.data.update(kw)

        def event(self, pid, tid, kind, **kw):
            events.append((kind, kw.get("detail")))

        def add_approval(self, *a):
            approvals.append(a)

    calls = []

    def chat(persona, messages, **kw):
        calls.append(persona)
        return json.dumps({"rulings": rulings}), {"served_model": "judge-model"}
    P = SimpleNamespace(id="animaluprising",
                        profile=SimpleNamespace(raw={"approval": cfg}, workspace=".",
                                                persona=lambda role, tid: "judge"),
                        foundry=SimpleNamespace(
                            persona_info=lambda _: {"routes": [{"model": "j", "vision": True}]},
                            bind=lambda *a, **k: None, chat=chat))
    c = SimpleNamespace(store=Store(), reject_human=lambda pid, tid, f, actor: rejected.append(f))
    t = {"task_id": "POL-9", "run_id": "r", "session_id": "s"}
    return VA, P, c, t, events, rejected, approvals, calls


def test_visual_fixes_loop_with_a_judge_until_clean_or_the_cap():
    """Victor 2026-11-04: as many review and fix rounds as needed, with a judge. Before
    visual_rounds_before_judge a finding goes straight back; after it a judge rules on each
    finding; a real one goes back with the judge's instruction, none real is accepted, and
    visual_rounds_max ends the loop with an owner flag."""
    cfg = {"visual_rounds_before_judge": 2, "visual_rounds_max": 4}
    f = ["docs/evidence/POL-9/a.png: HUD banner cropped"]
    # round 1: straight back, no judge
    VA, P, c, t, ev, rej, appr, calls = _judge_env(0, cfg, [])
    VA._changes(c, P, t, "rev", {"docs/evidence/POL-9/a.png": "x"}, "model:r", f)
    assert rej == [f] and not calls and c.store.data["visual_changes_rounds"] == 1
    # round 3: the judge says it is real -> back with the instruction
    VA, P, c, t, ev, rej, appr, calls = _judge_env(2, cfg, [
        {"finding": f[0], "ruling": "real", "instruction": "anchor the banner to the safe area"}])
    VA._changes(c, P, t, "rev", {"docs/evidence/POL-9/a.png": "x"}, "model:r", f)
    assert calls == ["judge"] and "anchor the banner" in rej[0][0]
    # the judge says not a defect -> accepted with notes, no further round
    VA, P, c, t, ev, rej, appr, calls = _judge_env(2, cfg, [
        {"finding": f[0], "ruling": "not_a_defect", "instruction": ""}])
    VA._changes(c, P, t, "rev", {"docs/evidence/POL-9/a.png": "x"}, "model:r", f)
    assert not rej and len(appr) == 1
    # at the cap -> accepted with notes and flagged for the owner
    VA, P, c, t, ev, rej, appr, calls = _judge_env(4, cfg, [])
    VA._changes(c, P, t, "rev", {"docs/evidence/POL-9/a.png": "x"}, "model:r", f)
    assert not rej and len(appr) == 1 and any(k == "visual_rounds_exhausted" for k, _ in ev)


def test_the_judge_binds_a_role_the_router_accepts():
    """v0.181.55: the judge bound its router session as role "visual_judge" and the router
    answered HTTP 400 invalid_work_binding, so VR-MAP1 looped on findings no judge ruled on
    until its review budget ran out (2026-11-05 01:13)."""
    from foundry_router.orchestration import ROLES
    VA, P, c, t, ev, rej, appr, calls = _judge_env(2, {"visual_rounds_before_judge": 2,
                                                       "visual_rounds_max": 8}, [
        {"finding": "docs/evidence/POL-9/a.png: x", "ruling": "owner", "instruction": ""}])
    bound = []
    P.foundry.bind = lambda *a, **k: bound.append(a[4])
    out = VA._judge(c, P, t, "rev", {"docs/evidence/POL-9/a.png": "x"},
                    ["docs/evidence/POL-9/a.png: x"])
    assert out and bound and all(r in ROLES for r in bound)


def test_a_refused_judge_persona_falls_back_to_the_visual_reviewer():
    """v0.181.56: the router refused the judge persona for the coordinator's key ("client
    'coordinator' may not use model 'Foundry-Kilo-Vision-Deep'"), so no judge ruled and
    VR-MAP1 kept looping on new-art findings. The visual reviewer's persona judges then."""
    f = ["docs/evidence/POL-9/a.png: placeholder prop"]
    VA, P, c, t, ev, rej, appr, calls = _judge_env(2, {}, [
        {"finding": f[0], "ruling": "owner", "instruction": ""}])
    P.profile.persona = lambda role, tid: {"visual_judge": "Deep",
                                           "visual_reviewer": "Review"}[role]
    used = []

    def chat(persona, messages, **kw):
        used.append(persona)
        if persona == "Deep":
            raise RuntimeError("Foundry refused (config/model_not_allowed): client "
                               "'coordinator' may not use model 'Deep'")
        return json.dumps({"rulings": [{"finding": f[0], "ruling": "owner",
                                        "instruction": ""}]}), {"served_model": "m"}
    P.foundry.chat = chat
    out = VA._judge(c, P, t, "rev", {"docs/evidence/POL-9/a.png": "x"}, f)
    assert used == ["Deep", "Review"] and out[0]["ruling"] == "owner"
    assert any(k == "visual_judge_unavailable" for k, _ in ev)
    assert any(k == "visual_judge" and d["persona"] == "Review" for k, d in ev)


def test_visual_judge_is_a_known_profile_role():
    """v0.181.54: a profile naming [roles] visual_judge failed to load ("unknown roles") and
    the coordinator exited with code 2 on restart (2026-11-04 23:29)."""
    from foundry_automation.schema import PROFILE_ROLES, roles_table
    assert "visual_judge" in PROFILE_ROLES
    roles = {"implementer": "I", "visual_judge": "Foundry-Kilo-Visual-Review"}
    assert roles_table("p.toml", roles) == roles


def test_an_inconclusive_review_waits_and_retries_instead_of_accepting():
    """v0.181.59: VR-AS01 (2026-11-05) was accepted unreviewed. The Claude route was down
    and the local fallback twice returned no valid verdict. In review-until-clean mode
    (visual_rounds_max) an inconclusive review now waits [approval]
    visual_inconclusive_wait_s and tries again, up to visual_inconclusive_retries times;
    only then is it accepted, with an owner item."""
    cfg = {"visual_rounds_max": 8, "visual_inconclusive_retries": 2,
           "visual_inconclusive_wait_s": 600}
    arts = {"docs/evidence/VR-AS01/a.png": "x"}
    now = [1000.0]
    VA, P, c, t, ev, rej, appr, calls = _judge_env(0, cfg, [])
    c.clock = lambda: now[0]
    c.store.update_task = lambda *a, **k: None
    # the first inconclusive answer: the one immediate structured retry (unchanged)
    VA._uncertain(c, P, t, "rev", arts, "model:r/qwen", "f" * 64, 0, ["no valid verdict"])
    assert not appr and c.store.data["visual_uncertain"]["f" * 16] == 1
    # the structured retry also inconclusive: deferred, not accepted
    VA._uncertain(c, P, t, "rev", arts, "model:r/qwen", "f" * 64, 1, ["no valid verdict"])
    assert not appr and c.store.data["visual_retry_after"] == 1600.0
    assert VA._inconclusive_waiting(c, P, t) is True
    now[0] = 1700.0
    assert VA._inconclusive_waiting(c, P, t) is False
    VA._uncertain(c, P, t, "rev", arts, "model:r/qwen", "f" * 64, 2, ["no valid verdict"])
    assert not appr and c.store.data["visual_uncertain"]["f" * 16] == 3
    # out of waits: accepted with notes and an owner item
    VA._uncertain(c, P, t, "rev", arts, "model:r/qwen", "f" * 64, 3, ["no valid verdict"])
    assert len(appr) == 1 and any(k == "visual_owner_items" for k, _ in ev)
    # without visual_rounds_max the old rule holds
    VA, P, c, t, ev, rej, appr, calls = _judge_env(0, {}, [])
    c.clock = lambda: 0.0
    VA._uncertain(c, P, t, "rev", arts, "model:r", "f" * 64, 1, ["x"])
    assert len(appr) == 1
