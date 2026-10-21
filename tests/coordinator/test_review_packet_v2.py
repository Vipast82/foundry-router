"""Review packet v2 (v0.121.8): explicit review-phase contract, packets bound
to the reviewed commit, per-file diffs in priority order with honest coverage,
cited evidence, earlier findings, persona-sized budgets, "too big to inspect"
blocks, and packet-format versioning of review records and op keys.

Real git repositories, the generic profile, a scripted coding client and a fake
Foundry; adapters are small test subclasses implementing the adapter contract
(review_contract, describe_at, review_evidence, diff_priority)."""

import json

import pytest

from foundry_coordinator import review as R
from foundry_coordinator import states as S
from foundry_coordinator.adapters.base import TaskSpec
from foundry_coordinator.adapters.project import GenericAdapter
from foundry_coordinator.adapters.scripted import ScriptedClient
from tests.coordinator.helpers import (FakeFoundry, coordinator, git, make_repo, review_json,
                                       run_until, write_profile)

T1 = {"id": "T1", "title": "Make the thing", "spec": "Write src/a.txt containing OK.",
      "scope": ["src/a.txt", "src/tests/test_a.py", "docs/evidence/A_RAW.log"]}

DEFERRED = ["SUMMARY.md entry", "STATE.md update", "MASTER_ORDER tick", "push to origin"]


def _state(c, tid="T1"):
    return c.store.task("game", tid)["state"]


class BuildAdapter(GenericAdapter):
    """A two-phase (BUILD then CLOSE) project that reads its task and evidence
    from the reviewed commit."""

    def review_contract(self, spec):
        return {"phase": "build", "reviewed": ["implementation in src/", "BUILD gate evidence"],
                "deferred": list(DEFERRED)}

    def describe_at(self, task_id, revision, ws):
        items = json.loads(ws.show_at(revision, "tasks.json"))
        item = next(t for t in items if t["id"] == task_id)
        return TaskSpec(project_id=self.p.id, task_id=task_id, title=item["title"],
                        spec=item["spec"], scope=list(item["scope"]), data=dict(item))

    def review_evidence(self, spec, revision, ws):
        text = ws.show_at(revision, "docs/plan.md")
        return [{"title": "BUILD plan", "path": "docs/plan.md", "revision": revision,
                 "blob": ws.blob_at(revision, "docs/plan.md"), "lines": "",
                 "text": text or "", "status": "ok" if text is not None else "missing",
                 "truncated": False, "note": "", "priority": 0}]

    def diff_priority(self, path, spec):
        return R.default_diff_priority(path, spec)        # src/a.txt is source here


class LegacyAdapter(GenericAdapter):
    """Only the old review_extra hook (even if GenericAdapter gains the new ones)."""
    review_contract = None
    describe_at = None
    review_evidence = None
    diff_priority = None

    def review_extra(self, spec, revision):
        return "gate summary: ALL PASS (from the working tree)"


class LimitsClient(ScriptedClient):
    """A coding client whose effective config advertises persona limits."""
    provider = "foundry"

    def __init__(self, script, project_id, limits):
        super().__init__(script, project_id=project_id)
        self.limits = limits

    def effective_config(self, directory):
        return {"provider": {"foundry": {"models": {
            p: {"limit": lim} for p, lim in self.limits.items()}}}}


class RecordingFoundry(FakeFoundry):
    def __init__(self, reviewer=None):
        super().__init__(reviewer)
        self.max_tokens = []

    def chat(self, persona, messages, *, session_id="", max_tokens=4000, schema=None):
        self.max_tokens.append(max_tokens)
        return super().chat(persona, messages, session_id=session_id, max_tokens=max_tokens,
                            schema=schema)


def _work(log_lines=0, code="OK"):
    def script(ctx):
        writes = {"src/a.txt": code, "src/tests/test_a.py": "OK = True\n"}
        if log_lines:
            writes["docs/evidence/A_RAW.log"] = "".join(
                f"OK raw evidence line {i:06d} " + "x" * 60 + "\n" for i in range(log_lines))
        return {"writes": writes, "commit": "T1: work",
                "final": '```coordinator-report\n{"status": "DONE", "summary": "did it"}\n```'}
    return script


@pytest.fixture()
def game(tmp_path):
    root, remote = make_repo(tmp_path, "game", [T1],
                             extra_files={"docs/plan.md": "PLAN: committed plan, step 1 OK\n"})
    prof = write_profile(tmp_path, "game", root)
    return tmp_path, root, prof


def _build(game, script=None, ff=None, adapter=BuildAdapter, client=None):
    tmp, root, prof = game
    ff = ff or RecordingFoundry()
    c = coordinator(tmp, [prof], {"game": client or script or _work()}, foundry={"game": ff})
    P = c.projects["game"]
    if adapter is not None:
        P.adapter = adapter(P.profile, str(tmp / "state"))
    return c, ff, P


# -- 1. review-phase contract -------------------------------------------------------------

def test_build_phase_packet_names_the_deferred_close_duties(game):
    c, ff, _ = _build(game)
    c.approve("game", "T1", [], "victor")
    assert run_until(c, lambda: _state(c) == S.COMPLETE)
    pk = ff.chats[0]["packet"]
    assert "## Review phase contract" in pk and "Phase: BUILD" in pk
    for d in DEFERRED:
        assert f"- {d}" in pk
    assert "never in missing_evidence or required_repairs" in pk
    assert "Real missing BUILD evidence" in pk and "still blocks" in pk
    assert "complete the BUILD phase" in pk
    assert "Review phase contract" in R.SYSTEM and "residual_risks" in R.SYSTEM
    rev = c.store.reviews("game", "T1")[0]
    assert rev["result"]["packet"]["version"] == R.PACKET_VERSION
    assert rev["result"]["packet"]["phase"] == "build"


def test_one_phase_task_keeps_the_full_task_contract(game):
    c, ff, _ = _build(game, adapter=LegacyAdapter)
    c.approve("game", "T1", [], "victor")
    assert run_until(c, lambda: _state(c) == S.COMPLETE)
    pk = ff.chats[0]["packet"]
    assert "Phase: FULL" in pk and "Phase: BUILD" not in pk
    assert "Does this commit complete the task correctly, within scope?" in pk
    assert "Deferred to the CLOSE session" not in pk
    # legacy evidence is shown, but never as bound to the reviewed commit
    assert "[UNVERIFIED] Project evidence (legacy review_extra)" in pk
    assert "not bound to the reviewed revision" in pk
    assert "task spec and scope read from: working tree" in pk


def test_real_missing_build_evidence_still_blocks_approval(game):
    seen = []

    def reviewer(packet):
        seen.append(packet)
        if len(seen) == 1:      # "approves" but lists missing BUILD evidence
            return review_json(packet, "APPROVED", missing=["S28 suite verdicts at the head"])
        return review_json(packet)
    c, ff, _ = _build(game, ff=RecordingFoundry(reviewer),
                      script=lambda ctx: {**_work()(ctx), "writes": {
                          "src/a.txt": f"OK {ctx['attempt']}", "src/tests/test_a.py": "OK\n"}})
    c.approve("game", "T1", [], "victor")
    assert run_until(c, lambda: _state(c) == S.CHANGES_REQUIRED)
    first = c.store.reviews("game", "T1")[0]
    assert first["verdict"] == "CHANGES_REQUIRED"
    assert "not an approval" in " ".join(first["result"]["validator_notes"])
    t = c.store.task("game", "T1")
    assert t["reviewed_sha"] in (None, "")
    assert any("S28 suite verdicts" in f for f in t["data"]["findings"])


# -- 2. diff priority and coverage --------------------------------------------------------

def test_huge_evidence_diff_cannot_displace_source_and_test_hunks(game):
    c, ff, P = _build(game, script=_work(log_lines=6000))     # ~500 KB raw log
    P.profile.review["max_diff_bytes"] = 80000
    c.approve("game", "T1", [], "victor")
    assert run_until(c, lambda: len(ff.chats) == 1)
    pk = ff.chats[0]["packet"]
    diff = pk.split("## Diff (per file, priority order)", 1)[1]
    # path order would put docs/evidence/ first; priority order puts code first
    assert diff.index("### src/a.txt (source, included)") < \
        diff.index("### docs/evidence/A_RAW.log (evidence/log, partial)")
    assert "+OK" in diff and "+OK = True" in diff
    cov = pk.split("## Coverage", 1)[1].split("## Diff", 1)[0]
    assert "- src/a.txt — source (p0)" in cov and ": included" in cov
    assert "- src/tests/test_a.py — test (p1)" in cov
    assert "docs/evidence/A_RAW.log — evidence/log (p8)" in cov and ": partial" in cov
    assert "all source/test changes included (2 file(s)" in cov
    assert "partial (cut for the budget): docs/evidence/A_RAW.log" in cov
    log_patch = diff.split("### docs/evidence/A_RAW.log", 1)[1]
    assert len(log_patch) < 82000                              # the diff cap held
    meta = c.store.reviews("game", "T1")[0]["result"]["packet"]
    assert meta["partial"] == ["docs/evidence/A_RAW.log"] and meta["omitted"] == []
    assert len(pk) <= R.DEFAULT_PACKET_CHARS and meta["chars"] == len(pk)


def test_default_priority_heuristic():
    p = R.default_diff_priority
    assert p("src/ui/ComponentGallery.lua") == 0 and p("lib/x.py") == 0
    assert p("tests/test_x.py") == 1 and p("src/tests/RegressionIndex.spec.lua") == 1
    assert p("docs/SUMMARY.md") == 5
    assert p("docs/evidence/UI-01_EVIDENCE_RAW.log") == 8 and p("out.log") == 8
    assert p("art/x.png", binary=True) == 9
    assert p("project.toml") == 2


# -- 3. revision-bound packets --------------------------------------------------------------

def test_working_tree_mutation_does_not_alter_the_packet_for_a_commit(game):
    tmp, root, _ = game
    c, ff, P = _build(game)
    c.approve("game", "T1", [], "victor")
    assert run_until(c, lambda: _state(c) == S.REVIEWING)
    head = c.store.task("game", "T1")["head_sha"]
    # after the commit, someone edits the plan, the task text and evidence on disk
    (root / "docs" / "plan.md").write_text("PLAN: MUTATED after the commit\n")
    tasks = json.loads((root / "tasks.json").read_text())
    tasks[0]["spec"] = "MUTATED task text"
    tasks[0]["scope"].append("src/sneaky.txt")
    (root / "tasks.json").write_text(json.dumps(tasks))
    c.tick()
    pk = ff.chats[0]["packet"]
    assert "MUTATED" not in pk and "src/sneaky.txt" not in pk
    assert "PLAN: committed plan, step 1 OK" in pk
    assert "Write src/a.txt containing OK." in pk
    assert f"task spec and scope read from: revision {head}" in pk
    blob = P.ws.blob_at(head, "docs/plan.md")
    assert f"`docs/plan.md` @ {head[:12]} · blob {blob[:12]}" in pk
    assert "[OK] BUILD plan" in pk


def test_missing_stale_and_truncated_evidence_is_explicit():
    ev = [{"title": "suite S28", "path": "docs/evidence/S28.log", "revision": "a" * 40,
           "blob": "b" * 40, "lines": "8600-8640", "text": "PASS\n" * 10000, "status": "ok",
           "truncated": False, "note": "", "priority": 0},
          {"title": "capture 1366x768", "path": "docs/evidence/cap.png", "revision": "a" * 40,
           "blob": None, "lines": "", "text": "", "status": "missing", "truncated": False,
           "note": "not in the reviewed commit", "priority": 0},
          {"title": "gate summary", "path": "docs/gate.md", "revision": "c" * 40, "blob": "d" * 40,
           "lines": "1-20", "text": "gate ran at an older commit", "status": "stale",
           "truncated": False, "note": "from c" * 1 + "ccccccccccc, not the head", "priority": 1},
          {"title": "adapter excerpt", "path": "docs/x.log", "revision": "a" * 40, "blob": None,
           "lines": "", "text": "already cut", "status": "ok", "truncated": True, "note": "",
           "priority": 2}]
    pk, info = R.build_packet(
        project_id="p", task_id="T1", title="t", spec="do it", scope=["src/a.lua"],
        base="0" * 40, head="a" * 40, commits=[], verification=[], files=[],
        patch=lambda rel: "", priority=lambda f: 0, evidence=ev,
        contract={"phase": "build", "deferred": ["push"]},
        provenance={"spec_from": "revision " + "a" * 40}, budget=R.packet_budget({}))
    assert info["insufficient"] is None
    assert "### E1 [OK] suite S28" in pk and "lines 8600-8640" in pk
    assert "(truncated: excerpt" in pk                         # 50 KB cut to the item cap
    assert pk.count("PASS") < 10000
    assert "### E2 [MISSING] capture 1366x768" in pk and "(MISSING: no content" in pk
    assert "### E3 [STALE] gate summary" in pk and f"@ {'c' * 12}" in pk
    assert "### E4 [OK] adapter excerpt" in pk and "truncated: yes" in pk.split("### E4", 1)[1]
    cov = pk.split("## Coverage", 1)[1]
    assert "evidence missing: E2" in cov and "evidence stale: E3" in cov
    assert "evidence truncated: E1, E4" in cov
    assert "no source/test files changed" in cov


# -- 4. budget ----------------------------------------------------------------------------

def test_budget_comes_from_the_reviewer_personas_advertised_limits(game):
    tmp, root, prof = game
    ff = RecordingFoundry()
    client = LimitsClient(_work(log_lines=6000), "game",
                          {"Foundry-Kilo-Sonnet": {"context": 30000, "output": 2000}})
    c, ff, P = _build(game, ff=ff, client=client)
    c.approve("game", "T1", [], "victor")
    assert run_until(c, lambda: len(ff.chats) == 1)
    pk = ff.chats[0]["packet"]
    b = R.packet_budget(P.profile.review, {"context": 30000, "output": 2000}, "Foundry-Kilo-Sonnet")
    assert b["packet_chars"] == int((30000 - 2000) * 3.5 * 0.9) - len(R.SYSTEM) - 2000
    assert len(pk) <= b["packet_chars"]
    assert R.est_tokens(len(pk) + len(R.SYSTEM)) + 2000 <= 30000
    assert "Kilo effective config provider.foundry.models.Foundry-Kilo-Sonnet.limit" in pk
    assert ff.max_tokens == [2000]                     # min(profile 4000, advertised output)


def test_budget_without_persona_limits_uses_profile_or_default():
    assert R.packet_budget({})["packet_chars"] == R.DEFAULT_PACKET_CHARS
    b = R.packet_budget({"max_packet_chars": 50000, "max_tokens": 3000})
    assert b["packet_chars"] == 50000 and b["max_tokens"] == 3000
    assert "max_packet_chars" in b["source"]
    capped = R.packet_budget({"max_packet_chars": 50000}, {"context": 200000})
    assert capped["packet_chars"] == 50000


def test_code_too_big_blocks_as_insufficiently_inspected_without_a_review_call(game):
    c, ff, P = _build(game, script=_work(code="OK " + "y" * 9000))
    P.profile.review["max_diff_bytes"] = 4000
    c.approve("game", "T1", [], "victor")
    assert run_until(c, lambda: _state(c) == S.BLOCKED)
    t = c.store.task("game", "T1")
    assert t["blocker"].startswith("insufficiently inspected: source/test diff ")
    assert "exceeds the review packet budget" in t["blocker"]
    assert "split the review or raise the budget" in t["blocker"]
    assert ff.chats == [] and c.store.reviews("game", "T1") == []
    assert c.store.q("SELECT * FROM ops WHERE kind='review'") == []
    ev = [e for e in c.store.q("SELECT * FROM events WHERE kind='review_insufficient'")]
    assert len(ev) == 1 and json.loads(ev[0]["detail"])["code_files"] == 2


# -- 5. earlier findings through the normal lifecycle ----------------------------------

def test_repaired_head_is_reviewed_with_the_earlier_findings_and_fresh_verification(game):
    tmp, root, _ = game
    seen = []

    def reviewer(packet):
        seen.append(packet)
        if len(seen) == 1:
            return review_json(packet, "CHANGES_REQUIRED", findings=[
                {"severity": "major", "file": "src/a.txt", "line": 1, "message": "say OK twice"}])
        return review_json(packet)

    def agent(ctx):
        if "say OK twice" in ctx["prompt"]:
            return {"writes": {"src/a.txt": "OK OK"}, "commit": "T1: per review",
                    "final": '```coordinator-report\n{"status": "DONE", "summary": "x"}\n```'}
        return _work()(ctx)
    c, ff, _ = _build(game, script=agent, ff=RecordingFoundry(reviewer))
    c.approve("game", "T1", [], "victor")
    assert run_until(c, lambda: _state(c) == S.COMPLETE, limit=60)
    revs = c.store.reviews("game", "T1")
    assert [r["verdict"] for r in revs] == ["CHANGES_REQUIRED", "APPROVED"]
    assert revs[0]["reviewed_sha"] != revs[1]["reviewed_sha"]
    assert "Earlier review findings" not in seen[0]
    earlier = seen[1].split("## Earlier review findings — verify whether the new commit "
                            "resolves them", 1)[1].split("\n## ", 1)[0]
    assert revs[0]["review_id"] in earlier and revs[0]["reviewed_sha"][:12] in earlier
    assert "say OK twice" in earlier
    # the repaired head was verified again before its review (verification not bypassed)
    ver = c.store.q("SELECT DISTINCT revision FROM verifications WHERE task_id='T1' "
                    "AND phase='verify'")
    assert {v["revision"] for v in ver} == {r["reviewed_sha"] for r in revs}
    # the historical rejection stays in the audit trail untouched
    assert c.store.review(revs[0]["review_id"], "game", "T1")["verdict"] == "CHANGES_REQUIRED"


def test_re_review_sends_only_the_diff_since_the_last_review(game):
    """Token cost (2026-10-21): every re-review re-sent the whole task diff. After a
    CHANGES_REQUIRED review at an ancestor, only files changed since it are sent."""
    tmp, root, _ = game
    seen = []

    def reviewer(packet):
        seen.append(packet)
        if len(seen) == 1:
            return review_json(packet, "CHANGES_REQUIRED", findings=[
                {"severity": "major", "file": "src/a.txt", "line": 1, "message": "say OK twice"}])
        return review_json(packet)

    def agent(ctx):
        if "say OK twice" in ctx["prompt"]:
            return {"writes": {"src/a.txt": "OK OK"}, "commit": "T1: per review",
                    "final": '```coordinator-report\n{"status": "DONE", "summary": "x"}\n```'}
        return _work()(ctx)
    c, ff, _ = _build(game, script=agent, ff=RecordingFoundry(reviewer))
    c.approve("game", "T1", [], "victor")
    assert run_until(c, lambda: _state(c) == S.COMPLETE, limit=60)
    revs = c.store.reviews("game", "T1")
    assert "src/tests/test_a.py" in seen[0].split("## Diff", 1)[1]
    second = seen[1].split("## Diff", 1)[1]
    assert "src/a.txt" in second and "src/tests/test_a.py" not in second
    assert "incremental re-review" in seen[1] and revs[0]["reviewed_sha"][:12] in seen[1]
    assert "say OK twice" in seen[1]


# -- 6. packet format version vs stored state ----------------------------------------------

def test_old_format_approval_and_ops_are_not_reused_or_looped_on(game):
    def reviewer(packet):
        return review_json(packet, "CHANGES_REQUIRED", findings=[
            {"severity": "major", "file": "src/a.txt", "line": 1, "message": "v2 sees a gap"}])
    c, ff, _ = _build(game, ff=RecordingFoundry(reviewer))
    c.approve("game", "T1", [], "victor")
    assert run_until(c, lambda: _state(c) == S.REVIEWING)
    head = c.store.task("game", "T1")["head_sha"]
    # state left by the v1 coordinator: an approval made from an old (truncated)
    # packet at this head, its "done" op, and a later call left "started" by a crash
    old = c.store.save_review("game", "T1", head, "reviewer", "Foundry-Kilo-Sonnet", "APPROVED",
                              True, "", {"verdict": "APPROVED", "findings": []}, "{}")
    for key, st in ((f"review:game:T1:{head}:reviewer:0", "done"),
                    (f"review:game:T1:{head}:reviewer:1", "started")):
        c.store.op_begin(key, "game", "T1", "review")
        c.store.op_update(key, st, {"review_id": old} if st == "done" else {})
    assert R.packet_version(c.store.review(old, "game", "T1")) == 1
    c.tick()
    t = c.store.task("game", "T1")
    assert t["state"] == S.CHANGES_REQUIRED and t["review_id"] != old   # old approval unused
    assert len(ff.chats) == 1
    revs = c.store.reviews("game", "T1")
    assert [r["review_id"] for r in revs][0] == old                     # kept in the audit trail
    assert R.packet_version(revs[-1]) == R.PACKET_VERSION
    assert c.store.op(f"review:game:T1:{head}:reviewer:1")["result"]["uncertain"] is True
    new_key = f"review:game:T1:{head}:reviewer:1:p{R.PACKET_VERSION}"
    assert c.store.op(new_key)["status"] == "done"
    kinds = [e["kind"] for e in c.store.q("SELECT kind FROM events WHERE task_id='T1'")]
    assert "review_not_reused" in kinds and "review_call_uncertain" in kinds


def test_current_format_review_at_the_same_head_is_reused_without_a_call(game):
    c, ff, _ = _build(game)
    c.approve("game", "T1", [], "victor")
    assert run_until(c, lambda: _state(c) == S.REVIEWING)
    head = c.store.task("game", "T1")["head_sha"]
    rid = c.store.save_review("game", "T1", head, "reviewer", "Foundry-Kilo-Sonnet", "APPROVED",
                              True, "", {"verdict": "APPROVED", "findings": [],
                                         "packet": {"version": R.PACKET_VERSION}}, "{}")
    c.tick()
    t = c.store.task("game", "T1")
    assert t["review_id"] == rid and t["reviewed_sha"] == head and ff.chats == []


def test_essential_evidence_that_cannot_fit_is_insufficient_not_hidden():
    files = [{"path": "src/a.lua", "added": 1, "deleted": 0, "binary": False}]
    ev = [{"title": "gate log", "path": "docs/evidence/gate.log", "revision": "a" * 40,
           "blob": "b" * 40, "lines": "", "text": "PASS\n" * 2000, "status": "ok",
           "truncated": False, "note": "", "priority": 0}]
    kw = dict(project_id="p", task_id="T1", title="t", spec="do it", scope=["src/a.lua"],
              base="0" * 40, head="a" * 40, commits=[], verification=[], files=files,
              priority=lambda f: 0, evidence=ev, contract={},
              provenance={"spec_from": "revision"})
    # whatever the code size, the packet is either sent WITH the essential evidence
    # (whole or as a marked excerpt) or not sent at all
    reasons = set()
    for n in range(100, 2000, 25):
        pk, info = R.build_packet(patch=lambda rel, n=n: "+x\n" * n,
                                  budget=R.packet_budget({"max_packet_chars": 9000}), **kw)
        if info["insufficient"]:
            assert pk == ""
            reasons.add(info["insufficient"].split(":")[1].strip().split(" ")[0])
        else:
            assert "PASS\nPASS" in pk.split("### E1 [OK] gate log", 1)[1]
            assert len(pk) <= 9000
    assert reasons == {"essential", "source/test"}       # both limits were exercised
    # with room it is sent as a cited, bounded excerpt
    pk, info = R.build_packet(patch=lambda rel: "+x\n", budget=R.packet_budget({}), **kw)
    assert info["insufficient"] is None and "### E1 [OK] gate log" in pk


def test_plain_generic_adapter_keeps_a_full_contract(game):
    c, ff, _ = _build(game, adapter=None)
    c.approve("game", "T1", [], "victor")
    assert run_until(c, lambda: len(ff.chats) == 1)
    pk = ff.chats[0]["packet"]
    assert "Phase: FULL" in pk and "Deferred to the CLOSE session" not in pk
    assert "- packet format: v2" in pk
