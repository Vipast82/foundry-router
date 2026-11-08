"""v0.167.0 asset judge: own checklist first (design only), then a blind score of the model's
images under shuffled letters; data-driven rule; paid once; bound to bytes."""

import hashlib
import json
from types import SimpleNamespace

import pytest

from foundry_coordinator import asset_judge as AJ
from foundry_coordinator.profile import ApprovalRule, ProfileError, load

from .helpers import write_profile

PNG = b"\x89PNG\r\n\x1a\n"


def _rule(**kw):
    base = dict(match="tier:*", kind="visual", stage="after_close", rubric="asset",
                artifacts=["docs/evidence/{task}/**/*.png"],
                scope_any=["assets/source/*.blend", "assets/exports/*.fbx"],
                asset_from=[r"^assets/source/[^/]+/([A-Z][A-Z0-9]*(?:-[A-Z0-9]+)+)/"],
                references=["assets/source/*/{asset}/ref/{asset}_views_v*.png",
                            "assets/source/*/{asset}/ref/{asset}_ref_v*.png"],
                notes=["assets/source/*/{asset}/checklist.md"], max_frames=0)
    base.update(kw)
    return ApprovalRule(**base)


def _write(root, rel, data):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return rel


def _project(tmp_path, judge=True, rules=None):
    profile = SimpleNamespace(workspace=str(tmp_path), in_workspace=lambda p: True,
                              raw={"approval": {"visual_model": True, "asset_judge": judge}},
                              approvals=rules or [], persona=lambda role, task: "vision")
    return SimpleNamespace(id="au", profile=profile)


def _spec(scope, tags=("tier:B",)):
    return SimpleNamespace(task_id="AS-X1", scope=list(scope), tags=list(tags),
                           spec="Build BS-KING per the concept.", data={})


SCOPE = ["tools/blender/build_bs_king.py", "assets/source/bosses/BS-KING/BS-KING.blend",
         "docs/evidence/AS-X1/**"]


# ------------------------------------------------------------------ profile

def _toml(tmp_path, rule_lines):
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    extra = "\n".join(["[approval]", "asset_judge = true", "[[approval.rules]]",
                       'match = "tier:*"', 'kind = "visual"', *rule_lines, ""])
    return str(write_profile(tmp_path, "p", ws, extra=extra))


def test_profile_parses_asset_rule_fields(tmp_path):
    prof = load(_toml(tmp_path, ['rubric = "asset"', 'references = ["a/{asset}/r.png"]',
                                 "asset_from = ['^x/([A-Z-]+)/']", "max_frames = 6",
                                 'scope_any = ["x/*.blend"]', 'notes = ["n/{asset}.md"]']))
    r = prof.approvals[0]
    assert (r.rubric, r.references, r.max_frames, r.scope_any) == (
        "asset", ["a/{asset}/r.png"], 6, ["x/*.blend"])


@pytest.mark.parametrize("line", ['rubric = "pretty"', "asset_from = ['^x/[A-Z]+/']",
                                  "max_frames = -1", 'references = "one"'])
def test_profile_refuses_bad_asset_rule(tmp_path, line):
    with pytest.raises(ProfileError, match="approval (rubric|asset_from|max_frames|references)"):
        load(_toml(tmp_path, [line]))


# ------------------------------------------------------------------ which rule applies

def test_applies_needs_switch_model_file_and_a_reference(tmp_path):
    P = _project(tmp_path)
    rule = _rule()
    assert not AJ.applies(P, _spec(SCOPE), rule)[0]                      # no reference yet
    _write(tmp_path, "assets/source/bosses/BS-KING/ref/BS-KING_ref_v1.png", PNG + b"1")
    _write(tmp_path, "assets/source/bosses/BS-KING/ref/BS-KING_ref_v2.png", PNG + b"2")
    ok, assets, refs, _ = AJ.applies(P, _spec(SCOPE), rule)
    assert ok and assets == ["BS-KING"]
    assert refs == ["assets/source/bosses/BS-KING/ref/BS-KING_ref_v2.png"]   # newest version
    _write(tmp_path, "assets/source/bosses/BS-KING/ref/BS-KING_views_v1.png", PNG + b"v")
    assert AJ.applies(P, _spec(SCOPE), rule)[2] == [
        "assets/source/bosses/BS-KING/ref/BS-KING_views_v1.png"]           # aligned pack first
    tools_only = ["tools/blender/build_en_rat.py", "docs/progress/AS-P9_PLAN.md"]
    assert "model file" in AJ.applies(P, _spec(tools_only), rule)[3]
    assert "asset_judge is off" in AJ.applies(_project(tmp_path, judge=False),
                                              _spec(SCOPE), rule)[3]


def test_asset_rule_replaces_the_screen_rule_only_when_it_applies(tmp_path):
    screen = ApprovalRule(match="visual", kind="visual", artifacts=["docs/evidence/{task}/*.png"])
    asset = _rule()
    P = _project(tmp_path, rules=[screen, asset])
    spec = _spec(SCOPE, tags=("tier:B", "visual"))
    assert AJ.select_rules(P, spec, [screen, asset]) == [screen]          # no reference
    assert AJ.rule_for(P, spec, "visual") is screen
    _write(tmp_path, "assets/source/bosses/BS-KING/ref/BS-KING_ref_v2.png", PNG)
    assert AJ.select_rules(P, spec, [screen, asset]) == [asset]
    assert AJ.rule_for(P, spec, "visual") is asset
    P.profile.raw["approval"]["asset_judge"] = False
    assert AJ.rule_for(P, spec, "visual") is screen


def test_letters_are_shuffled_stable_and_cover_every_image():
    rels = [f"docs/evidence/T/{v}.png" for v in ("front", "back", "left", "right")]
    a = AJ.letters("fp1", rels)
    assert sorted(a) == ["A", "B", "C", "D"] and sorted(a.values()) == sorted(rels)
    assert a == AJ.letters("fp1", rels)
    assert any(AJ.letters(f"fp{i}", rels) != a for i in range(2, 12))
    many = AJ.letters("x", [f"f{i}.png" for i in range(30)])
    assert "Z" in many and "AA" in many and "AD" in many


# ------------------------------------------------------------------ scoring

CHECK = [{"id": "C1", "part": "Monocle", "check": "monocle on the -X eye", "side": "-X",
          "kind": "side"},
         {"id": "C2", "part": "Cape", "check": "purple cape with gold trim", "side": "",
          "kind": "colour"},
         {"id": "C3", "part": "Tail", "check": "ringed tail at the back", "side": "",
          "kind": "presence"}]


def _frame(L, findings=()):
    return {"letter": L, "image_seen": True, "subject_present": True, "validity": "ok",
            "observation": "the raccoon boss is visible", "findings": list(findings)}


def _items(st=("pass", "pass", "pass"), letters=("A",)):
    return [{"id": c["id"], "status": s, "letters": list(letters), "note": "n"}
            for c, s in zip(CHECK, st)]


def test_valid_score_maps_letters_back_to_files():
    lettered = {"A": "e/front.png", "B": "e/back.png"}
    arts = {"e/front.png": "1", "e/back.png": "2"}
    ok = {"verdict": "approve", "frames": [_frame("A"), _frame("B")], "items": _items()}
    r = AJ.valid_score(ok, lettered, arts, CHECK)
    assert (r["verdict"], r["score"], sorted(r["clean"])) == ("approve", "3/3",
                                                               ["e/back.png", "e/front.png"])
    bad = dict(ok, items=_items(("pass", "fail", "pass"), letters=("B",)))
    r = AJ.valid_score(bad, lettered, arts, CHECK)
    assert r["verdict"] == "changes_required" and "e/back.png" in r["findings"][0]
    assert r["score"] == "2/3"
    seen = dict(ok, frames=[_frame("A", ["the monocle is on the +X eye"]), _frame("B")])
    assert AJ.valid_score(seen, lettered, arts, CHECK)["findings"][0].startswith("e/front.png:")
    vague = dict(ok, items=_items(("unclear", "unclear", "unclear")))
    # 2026-11-07 VR-AS41: a valid review that cannot see the items asks for better frames
    r = AJ.valid_score(vague, lettered, arts, CHECK)
    assert r["verdict"] == "changes_required"
    assert "could not be judged" in r["findings"][0] and len(r["findings"]) == 4
    assert r["findings"][1].startswith("checklist C1 Monocle") and "not visible" in r["findings"][1]
    wrong = dict(ok, frames=[_frame("A"), _frame("C")])
    assert AJ.valid_score(wrong, lettered, arts, CHECK)["verdict"] == "uncertain"
    missing = dict(ok, items=_items()[:2])
    assert AJ.valid_score(missing, lettered, arts, CHECK)["verdict"] == "uncertain"


def test_valid_checklist_needs_items_with_checks():
    assert AJ.valid_checklist({"checklist": CHECK})[0][0]["id"] == "C1"
    assert AJ.valid_checklist({"checklist": CHECK[:2]})[1]
    assert AJ.valid_checklist({"checklist": [*CHECK, {"id": "C1", "check": "dup"}]})[1]


# ------------------------------------------------------------------ the whole step

class Store:
    def __init__(self):
        self.ops, self.approvals_, self.events, self.data = {}, [], [], {}

    def op(self, key):
        return self.ops.get(key)

    def op_begin(self, key, *a):
        if key in self.ops:
            return self.ops[key]
        self.ops[key] = {"status": "started", "result": {}}
        return None

    def op_update(self, key, status, result=None):
        self.ops[key] = {"status": status, "result": result or {}}

    def task(self, pid, tid):
        return {"task_id": tid, "data": dict(self.data)}

    def merge_data(self, pid, tid, **kw):
        self.data.update(kw)

    def approvals(self, pid, tid, kind=None):
        return [{"artifacts": a[4], "revision": a[3]} for a in self.approvals_]

    def add_approval(self, *a):
        self.approvals_.append(a)

    def event(self, pid, tid, kind, **kw):
        self.events.append((kind, kw.get("detail")))

    session = lambda self, sid: None                                     # noqa: E731
    add_session = update_session = update_task = lambda self, *a, **k: None  # noqa: E731


def _env(tmp_path, answers):
    rule = _rule()
    P = _project(tmp_path, rules=[rule])
    ref = _write(tmp_path, "assets/source/bosses/BS-KING/ref/BS-KING_ref_v2.png", PNG + b"ref")
    _write(tmp_path, "assets/source/bosses/BS-KING/checklist.md",
           b"Must pass: monocle on his left eye (-X).")
    frames = [_write(tmp_path, f"docs/evidence/AS-X1/BS-KING_{v}.png", PNG + v.encode())
              for v in ("front", "back", "q34_front")]

    def arts(*_):
        return {r: hashlib.sha256((tmp_path / r).read_bytes()).hexdigest()
                for r in sorted(frames)}
    P.ws = SimpleNamespace(head=lambda: "rev1", status=lambda: [])
    P.adapter = SimpleNamespace(approval_artifacts=arts)
    calls = []

    def chat(persona, messages, **kw):
        parts = messages[-1]["content"]
        calls.append(parts)
        return json.dumps(answers(len(calls), parts)), {"served_model": "vision-model"}
    P.foundry = SimpleNamespace(
        persona_info=lambda _: {"exists": True, "enabled": True, "strict": True,
                                "routes": [{"model": "vision-model", "vision": True}]},
        bind=lambda *a, **k: None, chat=chat)
    store = Store()
    c = SimpleNamespace(store=store, clock=lambda: 100.0, _valid_approval=lambda *a: None,
                        reject_human=lambda *a, **k: store.events.append(("rejected", a)))
    t = {"task_id": "AS-X1", "run_id": "r", "session_id": "s", "attempt": 1}
    return P, rule, c, t, calls, ref, frames


def _letters_in(parts):
    return [p["text"].split()[-1] for p in parts
            if p.get("type") == "text" and p["text"].startswith("Model image ")]


def _answers(call, parts):
    if call == 1:
        return {"checklist": CHECK, "limitations": []}
    Ls = _letters_in(parts)
    return {"verdict": "approve", "frames": [_frame(L) for L in Ls], "items": _items(),
            "limitations": []}


def test_step_writes_checklist_from_design_then_scores_blind_and_records_once(tmp_path):
    P, rule, c, t, calls, ref, frames = _env(tmp_path, _answers)
    spec = _spec(SCOPE)
    AJ.step(c, P, t, spec, rule, "rev1")
    assert len(calls) == 2
    first = calls[0]
    imgs1 = [p for p in first if p.get("type") == "image_url"]
    assert len(imgs1) == 1                                   # the reference only, no model
    assert "NOT seen the model" in first[0]["text"] and "monocle on his left eye" in first[0]["text"]
    second = calls[1]
    text2 = " ".join(p["text"] for p in second if p.get("type") == "text")
    assert all(f not in text2 for f in frames)               # file names hidden
    assert sorted(_letters_in(second)) == ["A", "B", "C"]
    assert "C1 [side] Monocle (-X)" in text2
    assert len(c.store.approvals_) == 1 and "score 3/3" in c.store.approvals_[0][6]
    review = [d for k, d in c.store.events if k == "asset_judge_review"][0]
    assert sorted(review["letters"].values()) == sorted(frames)
    AJ.step(c, P, t, spec, rule, "rev1")                     # done: never paid again
    assert len(calls) == 2


def test_rework_pass_reuses_the_checklist_and_changes_go_back(tmp_path):
    def answers(call, parts):
        if call == 1:
            return {"checklist": CHECK}
        Ls = _letters_in(parts)
        return {"verdict": "changes_required", "frames": [_frame(L) for L in Ls],
                "items": _items(("fail", "pass", "pass"), letters=(Ls[0],))}
    P, rule, c, t, calls, ref, frames = _env(tmp_path, answers)
    AJ.step(c, P, t, _spec(SCOPE), rule, "rev1")
    assert len(calls) == 2 and not c.store.approvals_
    assert any(k == "rejected" for k, _ in c.store.events)
    (tmp_path / frames[0]).write_bytes(PNG + b"rebuilt")      # the rework pass
    P.ws.head = lambda: "rev2"
    AJ.step(c, P, t, _spec(SCOPE), rule, "rev2")
    assert len(calls) == 3                                    # score only; checklist reused
    assert "NOT seen the model" not in calls[2][0]["text"]


def test_unverified_serving_model_never_approves(tmp_path):
    P, rule, c, t, calls, ref, frames = _env(tmp_path, _answers)
    P.foundry.chat = (lambda persona, messages, **kw:
                      (calls.append(1) or json.dumps(_answers(len(calls), messages[-1]["content"])),
                       {"served_model": "text-only"}))
    AJ.step(c, P, t, _spec(SCOPE), rule, "rev1")
    assert not c.store.approvals_
    assert c.store.data.get("visual_uncertain")              # one structured retry scheduled


# ------------------------------------------------------------------ Phase C pilot: candidates

def _cand_env(tmp_path, answers, both=True):
    P, rule, c, t, calls, ref, _ = _env(tmp_path, answers)
    rule = _rule(artifacts=["docs/evidence/{task}/cand_*/*.png"],
                 candidates=["docs/evidence/{task}/cand_A/*.png",
                             "docs/evidence/{task}/cand_B/*.png"])
    frames = [_write(tmp_path, f"docs/evidence/AS-X1/cand_A/K_{v}.png", PNG + b"a" + v.encode())
              for v in ("front", "back")]
    if both:
        frames += [_write(tmp_path, f"docs/evidence/AS-X1/cand_B/K_{v}.png",
                          PNG + b"b" + v.encode()) for v in ("front", "back")]
    P.adapter.approval_artifacts = lambda *_: {
        r: hashlib.sha256((tmp_path / r).read_bytes()).hexdigest() for r in sorted(frames)}
    return P, rule, c, t, calls, frames


def test_profile_parses_candidates(tmp_path):
    path = _toml(tmp_path, ['rubric = "asset"', 'artifacts = ["docs/evidence/{task}/cand_*/*.png"]',
                            'candidates = ["docs/evidence/{task}/cand_A/*.png", '
                            '"docs/evidence/{task}/cand_B/*.png"]'])
    r = load(path).approvals[0]
    assert r.candidates == ["docs/evidence/{task}/cand_A/*.png",
                            "docs/evidence/{task}/cand_B/*.png"]


def test_candidates_are_scored_one_by_one_blind_and_the_best_decides(tmp_path):
    def answers(call, parts):
        if call == 1:
            return {"checklist": CHECK}
        Ls = _letters_in(parts)
        text = " ".join(p["text"] for p in parts if p.get("type") == "text")
        assert "ONE candidate model" in text and "cand_" not in text
        if call == 2:                                       # candidate A: one item fails
            return {"verdict": "changes_required", "frames": [_frame(L) for L in Ls],
                    "items": _items(("fail", "pass", "pass"), letters=(Ls[0],))}
        return {"verdict": "approve", "frames": [_frame(L) for L in Ls], "items": _items()}
    P, rule, c, t, calls, frames = _cand_env(tmp_path, answers)
    spec = _spec(SCOPE + ["docs/evidence/AS-X1/cand_A/**"])
    AJ.step(c, P, t, spec, rule, "rev1")
    assert len(calls) == 3                                  # checklist + one score per candidate
    for call in calls[1:]:
        assert sorted(_letters_in(call)) == ["A", "B"]      # each sees only its own two images
    ev = [d for k, d in c.store.events if k == "asset_judge_candidates"][0]
    assert ev["best"] == "B"
    assert [(x["candidate"], x["score"], x["verdict"]) for x in ev["candidates"]] == [
        ("A", "2/3", "changes_required"), ("B", "3/3", "approve")]
    assert len(c.store.approvals_) == 1 and "best B" in c.store.approvals_[0][6]
    AJ.step(c, P, t, spec, rule, "rev1")                    # recorded: never paid again
    assert len(calls) == 3
    assert len([k for k, _ in c.store.events if k == "asset_judge_candidates"]) == 1


def test_one_candidate_with_images_is_judged_as_a_single_model(tmp_path):
    P, rule, c, t, calls, frames = _cand_env(tmp_path, _answers, both=False)
    assert AJ.candidate_sets(_spec(SCOPE), rule, P.adapter.approval_artifacts()) == []
    AJ.step(c, P, t, _spec(SCOPE), rule, "rev1")
    assert len(calls) == 2 and len(c.store.approvals_) == 1
    assert not [k for k, _ in c.store.events if k == "asset_judge_candidates"]


def test_best_candidate_ranks_verdict_then_score_then_findings():
    r = [("A", {"verdict": "changes_required", "score": "9/10", "findings": ["x"]}),
         ("B", {"verdict": "approve", "score": "7/10", "findings": []}),
         ("C", {"verdict": "approve", "score": "8/10", "findings": []})]
    assert AJ.best_candidate(r)[0] == "C"
    assert AJ.best_candidate(r[:1] + [("D", {"verdict": "uncertain", "score": None})])[0] == "A"


def test_a_judge_call_cut_off_by_a_coordinator_restart_is_made_once_more(tmp_path, monkeypatch):
    """v0.181.68: the v0.181.67 deploy (2026-11-05 21:55) restarted the coordinator while
    VR-AS33's asset checklist call was in flight; the sprint sat 'outcome uncertain after
    restart; paid call not repeated' until a human cleared it. A judge call begun before this
    process started is made once more; a second cut-off call, or one this process is making,
    still waits."""
    import time as _t
    from types import SimpleNamespace
    from foundry_coordinator import asset_judge as AJ
    from foundry_coordinator import lead as L
    from foundry_coordinator.store import Store
    store = Store(str(tmp_path / "s.sqlite"))
    events = []
    store_event = store.event
    c = SimpleNamespace(store=store, clock=_t.time)
    key = "asset-checklist:game:T1:fp"
    assert AJ._op(c, key, "game", "T1", "asset_checklist") == (None, True)    # new: go
    store.op_update(key, "calling", {"session_id": "s1"})
    # the same process: in flight, wait
    monkeypatch.setattr(L, "PROCESS_START", _t.time() - 3600)
    prior, go = AJ._op(c, key, "game", "T1", "asset_checklist")
    assert not go and prior["status"] == "calling"
    # a restart since the call began: made once more
    monkeypatch.setattr(L, "PROCESS_START", _t.time() + 1)
    assert AJ._op(c, key, "game", "T1", "asset_checklist") == (None, True)
    assert store.op(key)["result"].get("restart_retry") is True
    assert store.q("SELECT 1 FROM events WHERE kind='asset_judge_restart_retry'")
    # cut off again: no third call
    monkeypatch.setattr(L, "PROCESS_START", _t.time() + 2)
    prior, go = AJ._op(c, key, "game", "T1", "asset_checklist")
    assert not go and prior["status"] == "calling"
