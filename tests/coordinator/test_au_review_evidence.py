"""AnimalUprising BUILD review evidence is read from the reviewed commit only.

Synthetic AU-like repositories (never the real game repository): a plan, a raw
evidence log, evidence captures and a done_check.py stub providing the plan
parsing the adapter imports."""

import pytest

from foundry_coordinator.adapters.base import TaskSpec
from foundry_coordinator.adapters.git import Workspace
from foundry_coordinator.adapters.project import AdapterError
from foundry_coordinator.profile import Profile
from foundry_coordinator.projects.animaluprising.adapter import AnimalUprisingAdapter
from tests.coordinator.helpers import git

T = "UX-01"
PLAN_REL = f"docs/progress/{T}_PLAN.md"
LOG_REL = f"docs/progress/{T}_EVIDENCE_RAW.log"

DONE_CHECK_STUB = '''\
import re


def plan_files(plan_text):
    entries, on = [], False
    for line in plan_text.splitlines():
        s = line.strip()
        if re.match(r"^#{2,4}\\s+Files this sprint may change", s, re.I):
            on = True
            continue
        if on and s.startswith("#"):
            break
        if on:
            m = re.match(r"^[-*]\\s+`([^`]+)`", s)
            if m:
                entries.append(m.group(1).strip())
    return entries


def plan_tier(plan_text):
    m = re.search(r"\\*\\*Tier:\\*\\*\\s*([ABC])\\b", plan_text)
    return m.group(1) if m else None
'''

PLAN = f"""# {T} — Synthetic UI kit — PLAN

**Tier:** B. **Depends on:** AS-C16.

## Goal
Build the synthetic kit.

## Files this sprint may change
- `src/client/Kit.lua`
- `tests/UX_01_check.lua`
- `docs/evidence/{T}/gallery_desktop.png`
- `docs/evidence/{T}/gallery_phone.png`
- `docs/progress/{T}_PLAN.md`

## Checklist (tick after each step)
- [x] 1 Kit module
- [x] 2 Captures desktop + phone
- [ ] 3 Regression subset + gate
- [ ] 4 Docs + gate -Finish + done_check + commit + push (NO tags)

## Acceptance lines
Gate PASS; captures `docs/evidence/{T}/size_1366x768.png` reviewed; COMPLETE-PENDING-VISUAL-REVIEW.
"""


def _log(gate_head, extra_block="", suites=None):
    lines = ["SESSION 1 (2026-10-14)", "boot 36/0"]
    lines += suites if suites is not None else [
        "SESSION 37 (2026-10-17)",
        "PASS S28 c1 open", "PASS S28 c2 close", "PASS S28 c3 focus", "PASS S28 c4 scroll",
        "PASS S28 c5 audit",
        "PASS S39 c1", "PASS S39 c2", "PASS S39 c3", "PASS S39 c4", "PASS S39 c5",
        "PASS S39 c6", "PASS S39 c7",
        "S45 PASS 12/12", "LUNE 73/73 PASS", "unrelated chatter line"]
    lines += [f"PASS: GATE HEAD {gate_head}", "=== GATE FINISH ===", "boot 36/0 + 21/0"]
    if extra_block:
        lines.append(extra_block)
    lines += [f"GATE {T} PASS", "trailing note after the gate"]
    return "\n".join(lines) + "\n"


def _profile(ws):
    return Profile(id="animaluprising", name="Animal Uprising", version="1", path="fixture.toml",
                   hash="fixture", workspace=str(ws), allowed_dirs=["src", "tests", "docs"],
                   branch="master", remote="origin", raw={},
                   closeout={"mode": "session",
                             "exempt_paths": ["docs/progress/{task}_*", "docs/progress/STATE.md",
                                              "docs/MASTER_ORDER.md"]})


def _write(ws, rel, content, binary=False):
    p = ws / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    if binary:
        p.write_bytes(content)
    else:
        p.write_text(content, encoding="utf-8", newline="\n")


def _commit(ws, msg):
    git(ws, "add", "-A")
    git(ws, "commit", "-q", "--no-verify", "-m", msg)
    return git(ws, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path):
    """A synthetic AU-like repo: code commit, then a gate run bound to it."""
    ws = tmp_path / "au-fake"
    ws.mkdir()
    git(ws, "init", "-q", "-b", "master")
    _write(ws, "AGENT_RULES.md", "rules\nCOORDINATOR-ADVANCEMENT\n")
    _write(ws, "tools/guard/done_check.py", DONE_CHECK_STUB)
    _write(ws, PLAN_REL, PLAN)
    _write(ws, "src/client/Kit.lua", "return {}\n")
    _write(ws, "tests/UX_01_check.lua", "return true\n")
    base = _commit(ws, "base")
    _write(ws, "src/client/Kit.lua", "return {ok = true}\n")
    _write(ws, f"docs/evidence/{T}/gallery_desktop.png", b"\x89PNG desktop", binary=True)
    _write(ws, f"docs/evidence/{T}/gallery_phone.png", b"\x89PNG phone", binary=True)
    code = _commit(ws, f"{T}: kit + captures")
    _write(ws, LOG_REL, _log(code[:10]))
    head = _commit(ws, f"{T}: gate evidence")
    a = AnimalUprisingAdapter(_profile(ws), str(tmp_path / "state"))
    return {"ws": ws, "w": Workspace(str(ws)), "a": a, "base": base, "code": code, "head": head}


def _by_title(items, title):
    return next(i for i in items if i["title"] == title)


def test_describe_at_reads_the_plan_at_the_revision_with_provenance(repo):
    a, w, head = repo["a"], repo["w"], repo["head"]
    spec = a.describe_at(T, head, w)
    assert spec.data["spec_source"] == "revision" and spec.data["spec_revision"] == head
    assert spec.data["plan"] == PLAN_REL
    assert spec.data["plan_blob"] == git(repo["ws"], "rev-parse", f"{head}:{PLAN_REL}")
    assert "src/client/Kit.lua" in spec.scope and "tier:B" in spec.tags and "visual" in spec.tags
    assert spec.deps == ["AS-C16"]
    # same parser as describe(): identical spec text/scope for an unchanged plan
    wt = a.describe(T)
    assert (wt.spec, wt.scope, wt.tags) == (spec.spec, spec.scope, spec.tags)
    assert wt.data["spec_source"] == "working tree"
    assert spec.data["close_step"].startswith("4 Docs + gate -Finish")


def test_working_tree_mutation_does_not_change_the_reviewed_packet(repo):
    a, w, head, ws = repo["a"], repo["w"], repo["head"], repo["ws"]
    spec0 = a.describe_at(T, head, w)
    ev0 = a.review_evidence(spec0, head, w)
    _write(ws, PLAN_REL, PLAN.replace("`src/client/Kit.lua`", "`src/client/Other.lua`")
           .replace("- [ ] 3", "- [x] 3"))
    _write(ws, LOG_REL, "GATE UX-01 PASS\nPASS S28 suite verdict PASS (forged)\n")
    (ws / f"docs/evidence/{T}/gallery_phone.png").unlink()
    _write(ws, f"docs/evidence/{T}/size_1366x768.png", b"\x89PNG new", binary=True)
    spec1 = a.describe_at(T, head, w)
    assert (spec1.spec, spec1.scope, spec1.data) == (spec0.spec, spec0.scope, spec0.data)
    assert a.review_evidence(spec1, head, w) == ev0
    assert "src/client/Other.lua" in a.describe(T).scope       # the working tree did change


def test_describe_at_without_the_plan_at_that_revision_raises(repo):
    with pytest.raises(AdapterError, match="does not exist at"):
        repo["a"].describe_at(T, repo["base"] + "~0", repo["w"]) if False else \
            repo["a"].describe_at("UX-02", repo["head"], repo["w"])
    with pytest.raises(AdapterError, match="not a sprint id"):
        repo["a"].describe_at("../etc", repo["head"], repo["w"])


def test_excerpts_are_raw_numbered_and_carry_path_revision_blob(repo):
    a, w, head = repo["a"], repo["w"], repo["head"]
    items = a.review_evidence(a.describe_at(T, head, w), head, w)
    blob = git(repo["ws"], "rev-parse", f"{head}:{LOG_REL}")
    raw = _log(repo["code"][:10]).split("\n")
    gate = _by_title(items, f"{T} last gate block")
    assert gate["status"] == "ok" and gate["path"] == LOG_REL and gate["revision"] == head
    assert gate["blob"] == blob and f"{LOG_REL}@{head}" in gate["text"]
    first = raw.index("=== GATE FINISH ===") + 1
    last = raw.index(f"GATE {T} PASS") + 1
    assert gate["lines"] == f"{first}-{last}"
    assert f"{first}: === GATE FINISH ===" in gate["text"]
    assert f"{last}: GATE {T} PASS" in gate["text"]
    assert "trailing note" not in gate["text"]
    assert "bound to" in gate["note"] and gate["priority"] == 0
    # every quoted line is the raw line at that number
    suites = _by_title(items, f"{T} suite verdict lines")
    for ln in suites["text"].splitlines()[1:]:
        n, _, body = ln.partition(": ")
        assert raw[int(n) - 1] == body
    assert "unrelated chatter line" not in suites["text"]
    assert suites["blob"] == blob and suites["status"] == "ok"


def test_suite_lines_without_a_verdict_are_shown_raw_not_summarised(repo):
    a, w, head = repo["a"], repo["w"], repo["head"]
    items = a.review_evidence(a.describe_at(T, head, w), head, w)
    text = _by_title(items, f"{T} suite verdict lines")["text"]
    raw = _log(repo["code"][:10]).split("\n")
    assert sum(1 for l in text.splitlines() if " PASS S28 c" in l) == 5
    assert sum(1 for l in text.splitlines() if " PASS S39 c" in l) == 7
    session = raw.index("SESSION 37 (2026-10-17)") + 1
    assert f"{session}: SESSION 37 (2026-10-17)" in text
    # nothing invented: no line that isn't in the raw log, no synthesised suite verdict
    assert "S28 PASS" not in text and "S39 PASS" not in text
    joined = "\n".join(i["text"] + i["note"] for i in items)
    assert "S28 PASS" not in joined and "S39 PASS" not in joined
    assert "no verdict is inferred" in _by_title(items, f"{T} suite verdict lines")["note"]


def test_required_and_missing_captures_are_explicit(repo):
    a, w, head, ws = repo["a"], repo["w"], repo["head"], repo["ws"]
    items = a.review_evidence(a.describe_at(T, head, w), head, w)
    caps = _by_title(items, f"{T} evidence captures")
    for name in ("gallery_desktop.png", "gallery_phone.png"):
        rel = f"docs/evidence/{T}/{name}"
        assert f"{rel}  blob {git(ws, 'rev-parse', f'{head}:{rel}')}  (required)" in caps["text"]
    missing = [i for i in items if i["status"] == "missing"]
    assert [i["path"] for i in missing] == [f"docs/evidence/{T}/size_1366x768.png"]
    assert missing[0]["priority"] == 0 and "required by the plan" in missing[0]["note"]
    assert "1 missing" in caps["note"]


def test_missing_log_and_captures_at_an_earlier_commit_are_explicit(repo):
    a, w, code = repo["a"], repo["w"], repo["base"]
    items = a.review_evidence(a.describe_at(T, code, w), code, w)
    gate = _by_title(items, f"{T} last gate block")
    suites = _by_title(items, f"{T} suite verdict lines")
    assert gate["status"] == suites["status"] == "missing" and "does not exist at" in gate["note"]
    caps = _by_title(items, f"{T} evidence captures")
    assert caps["status"] == "missing" and "(no files)" in caps["text"]
    assert {i["path"] for i in items if i["title"].startswith("Required capture")} == {
        f"docs/evidence/{T}/gallery_desktop.png", f"docs/evidence/{T}/gallery_phone.png",
        f"docs/evidence/{T}/size_1366x768.png"}


def test_a_gate_run_before_a_code_change_is_stale(repo):
    a, w, ws = repo["a"], repo["w"], repo["ws"]
    _write(ws, "src/client/Kit.lua", "return {ok = false}\n")
    later = _commit(ws, f"{T}: change after the gate")
    gate = _by_title(a.review_evidence(a.describe_at(T, later, w), later, w), f"{T} last gate block")
    assert gate["status"] == "stale" and "src/client/Kit.lua" in gate["note"]


def test_a_measured_baseline_that_is_not_an_ancestor_is_stale(repo):
    a, w, ws, head = repo["a"], repo["w"], repo["ws"], repo["head"]
    git(ws, "checkout", "-q", "-b", "side", repo["base"])
    _write(ws, "src/client/Kit.lua", "return {side = true}\n")
    side = _commit(ws, "side work")
    git(ws, "checkout", "-q", "master")
    _write(ws, LOG_REL, _log(repo["code"][:10], extra_block=f"measured source baseline {side[:7]}"))
    rev = _commit(ws, f"{T}: gate with baseline")
    gate = _by_title(a.review_evidence(a.describe_at(T, rev, w), rev, w), f"{T} last gate block")
    assert gate["status"] == "stale"
    assert f"{side[:7]} is not the reviewed commit" in gate["note"]
    # an unknown sha is stale too, never silently trusted
    _write(ws, LOG_REL, _log(repo["code"][:10], extra_block="baseline 1234567abc"))
    rev2 = _commit(ws, f"{T}: gate with bogus baseline")
    gate2 = _by_title(a.review_evidence(a.describe_at(T, rev2, w), rev2, w),
                      f"{T} last gate block")
    assert gate2["status"] == "stale" and "not a commit" in gate2["note"]
    assert head != rev2


def test_a_gate_without_a_bound_head_is_unverified(repo):
    a, w, ws = repo["a"], repo["w"], repo["ws"]
    _write(ws, LOG_REL, _log("x").replace("PASS: GATE HEAD x\n", ""))
    rev = _commit(ws, f"{T}: unbound gate")
    gate = _by_title(a.review_evidence(a.describe_at(T, rev, w), rev, w), f"{T} last gate block")
    assert gate["status"] == "unverified" and "not bound" in gate["note"]


def test_finish_includes_its_bound_inline_studio_session_before_final_verdict(repo):
    a, w, ws, code = repo["a"], repo["w"], repo["ws"], repo["code"]
    studio = (f"SESSION 38 studio lines (coordinator run {T}-a3-4559aa attempt 3), "
              f"HEAD {code[:7]}; src/tests byte-identical.\n"
              "--- SESSION 36 part 2 / SESSION 37 verdict lines (VMs at baseline) ---\n"
              "SUITE UX_01 pass=193 fail=0 expect=193 designedFails=0 VERDICT PASS")
    raw = _log(code[:10], extra_block=studio)
    raw = raw.replace("=== GATE FINISH ===", "=== GATE lune ===\n"
                      "SESSION 37 gate preparation\n=== GATE FINISH ===")
    _write(ws, LOG_REL, raw)
    rev = _commit(ws, f"{T}: inline Studio gate evidence")
    gate = _by_title(a.review_evidence(a.describe_at(T, rev, w), rev, w),
                     f"{T} last gate block")
    assert gate["status"] == "ok" and not gate["truncated"]
    assert studio.splitlines()[0] in gate["text"]
    verdict_line = raw.splitlines().index(f"GATE {T} PASS") + 1
    assert f"{verdict_line}: GATE {T} PASS" in gate["text"]
    assert "no 'GATE" not in gate["note"]


@pytest.mark.parametrize("boundary", [
    "SESSION 39 (new unrelated run)",
    "SESSION 39 studio lines (coordinator run OTHER-a3-4559aa attempt 3), HEAD {head}",
    "SESSION 39 studio lines (coordinator run UX-01-a3-4559aa attempt 3), HEAD 1234567",
    "PASS: GATE HEAD {head}",
    "=== GATE START ===",
    "GATE RUN sprint=UX-01",
    "GATE OTHER PASS",
])
def test_incomplete_finish_never_borrows_later_runs_verdict(repo, boundary):
    a, w, ws, code = repo["a"], repo["w"], repo["ws"], repo["code"]
    raw = _log(code[:10], extra_block=boundary.format(head=code[:10]))
    _write(ws, LOG_REL, raw)
    rev = _commit(ws, f"{T}: incomplete finish before another run")
    gate = _by_title(a.review_evidence(a.describe_at(T, rev, w), rev, w),
                     f"{T} last gate block")
    assert gate["status"] == "unverified"
    assert "no 'GATE" in gate["note"]
    assert f": GATE {T} PASS" not in gate["text"]


def test_inline_studio_does_not_admit_a_second_session(repo):
    a, w, ws, code = repo["a"], repo["w"], repo["ws"], repo["code"]
    header = (f"SESSION 38 studio lines (coordinator run {T}-a3-4559aa attempt 3), "
              f"HEAD {code[:7]}")
    raw = _log(code[:10], extra_block=header + "\n" + header.replace("38", "39"))
    _write(ws, LOG_REL, raw)
    rev = _commit(ws, f"{T}: incomplete inline Studio gate")
    gate = _by_title(a.review_evidence(a.describe_at(T, rev, w), rev, w),
                     f"{T} last gate block")
    assert gate["status"] == "unverified" and "no 'GATE" in gate["note"]
    assert f": GATE {T} PASS" not in gate["text"]


def test_bound_inline_studio_fail_verdict_is_preserved(repo):
    a, w, ws, code = repo["a"], repo["w"], repo["ws"], repo["code"]
    header = (f"SESSION 38 studio lines (coordinator run {T}-a3-4559aa attempt 3), "
              f"HEAD {code[:7]}")
    raw = _log(code[:10], extra_block=header).replace(f"GATE {T} PASS", f"GATE {T} FAIL")
    raw += f"SESSION 39 later run\nGATE {T} PASS\n"
    _write(ws, LOG_REL, raw)
    rev = _commit(ws, f"{T}: failing bound gate before later success")
    gate = _by_title(a.review_evidence(a.describe_at(T, rev, w), rev, w),
                     f"{T} last gate block")
    assert f": GATE {T} FAIL" in gate["text"]
    assert f": GATE {T} PASS" not in gate["text"]


def test_inline_studio_cannot_borrow_previous_finish_head(repo):
    a, w, ws, code = repo["a"], repo["w"], repo["ws"], repo["code"]
    raw = _log(code[:10]) + (
        "=== GATE FINISH ===\nVERDICT PASS: GATE lune\n"
        f"SESSION 39 studio lines (coordinator run {T}-a3-4559aa attempt 3), HEAD {code[:7]}\n"
        f"GATE {T} PASS\n")
    _write(ws, LOG_REL, raw)
    rev = _commit(ws, f"{T}: unbound current finish after old success")
    gate = _by_title(a.review_evidence(a.describe_at(T, rev, w), rev, w),
                     f"{T} last gate block")
    assert gate["status"] == "unverified" and "no 'GATE" in gate["note"]
    assert f": GATE {T} PASS" not in gate["text"]


def test_new_run_without_head_cannot_reuse_incomplete_previous_run_head(repo):
    a, w, ws, code = repo["a"], repo["w"], repo["ws"], repo["code"]
    raw = (f"PASS: GATE HEAD {code[:10]}\nGATE RUN sprint={T}\n"
           "=== GATE FINISH ===\n"
           f"SESSION 39 studio lines (coordinator run {T}-a3-4559aa attempt 3), HEAD {code[:7]}\n"
           f"GATE {T} PASS\n")
    _write(ws, LOG_REL, raw)
    rev = _commit(ws, f"{T}: new run without a bound HEAD")
    gate = _by_title(a.review_evidence(a.describe_at(T, rev, w), rev, w),
                     f"{T} last gate block")
    assert gate["status"] == "unverified" and "no 'GATE" in gate["note"]
    assert f": GATE {T} PASS" not in gate["text"]


def test_truncation_is_flagged_and_keeps_raw_lines(repo):
    a, w, ws, code = repo["a"], repo["w"], repo["ws"], repo["code"]
    many = ["SESSION 50 (2026-10-18)"] + [f"PASS S46 c{i} leg {'x' * 40}" for i in range(2000)]
    _write(ws, LOG_REL, _log(code[:10], suites=many))
    rev = _commit(ws, f"{T}: long log")
    a.cfg["review_evidence_item_chars"] = 4000
    suites = _by_title(a.review_evidence(a.describe_at(T, rev, w), rev, w),
                       f"{T} suite verdict lines")
    assert suites["truncated"] is True and "only the last" in suites["note"]
    assert len(suites["text"]) <= 4000 + 200
    assert f"GATE {T} PASS" in suites["text"]                 # the tail (latest) is kept
    raw = _log(code[:10], suites=many).split("\n")
    for ln in suites["text"].splitlines()[1:]:
        n, _, body = ln.partition(": ")
        assert raw[int(n) - 1] == body


def test_checklist_state_is_quoted_with_line_numbers_and_close_step_reserved(repo):
    a, w, head = repo["a"], repo["w"], repo["head"]
    items = a.review_evidence(a.describe_at(T, head, w), head, w)
    ck = _by_title(items, f"{T} plan BUILD checklist")
    raw = PLAN.split("\n")
    n1 = raw.index("- [x] 1 Kit module") + 1
    n4 = next(i for i, l in enumerate(raw) if l.startswith("- [ ] 4 Docs")) + 1
    assert ck["lines"] == f"{n1}-{n4}" and f"{n1}: - [x] 1 Kit module" in ck["text"]
    assert "2 of 4 items ticked" in ck["note"] and "CLOSE step" in ck["note"]
    assert ck["blob"] == git(repo["ws"], "rev-parse", f"{head}:{PLAN_REL}")


def test_review_extra_still_works(repo):
    a = repo["a"]
    extra = a.review_extra(a.describe(T), repo["head"])
    assert "=== GATE FINISH ===" in extra


def test_build_contract_defers_exactly_the_close_duties(repo):
    a, w, head = repo["a"], repo["w"], repo["head"]
    spec = a.describe_at(T, head, w)
    c = a.review_contract(spec)
    assert c["phase"] == "build"
    assert c["deferred"] == [d["duty"] for d in a._close_duties(spec)]
    joined = "\n".join(c["deferred"])
    for need in (f"{T}_SUMMARY.md", "STATE.md ledger row", "API facts", "MEMORY_NOTE",
                 "MASTER_ORDER", "final CLOSE step (4 Docs + gate -Finish", f"DONE-CHECK {T} PASS",
                 f'"{T}: closeout" commit'):
        assert need in joined, need
    # 2026-10-27: the coordinator pushes master after closeout verification; the CLOSE
    # session has no push duty
    assert "git push" not in joined and "the coordinator pushes it" in joined
    # one memory store (2026-10-27): docs/progress/state/* is read-only history
    assert "docs/progress/state/" not in joined
    reviewed = "\n".join(c["reviewed"])
    assert "BUILD implementation" in reviewed and f"GATE {T} PASS" in reviewed
    assert "captures" in reviewed and "Scope" in reviewed
    assert not any("SUMMARY.md" in r or "git push" in r or "STATE.md" in r
                   for r in c["reviewed"])
    # the CLOSE prompt is rendered from the same duties
    prompt = a.closeout_prompt(spec, {"base": head})
    for d in a._close_duties(spec):
        if d["step"]:
            assert d["step"] in prompt
    assert "1. docs/progress/UX-01_SUMMARY.md" in prompt and "5. Commit only those" in prompt
    assert "Never push" in prompt and "git push origin" not in prompt


def test_diff_priority_ordering(repo):
    a = repo["a"]
    spec = a.describe(T)
    spec.scope.append("default.project.json")
    p = a.diff_priority
    assert p("src/client/Kit.lua", spec) == 0
    assert p("tests/UX_01_check.lua", spec) == 1
    assert p(PLAN_REL, spec) == 2 and p("default.project.json", spec) == 2
    assert p("docs/design/notes.md", spec) == 5
    assert p(LOG_REL, spec) == 8 and p(f"docs/evidence/{T}/notes.txt", spec) == 8
    assert p(f"docs/evidence/{T}/gallery_phone.png", spec) == 9
    order = sorted([f"docs/evidence/{T}/a.png", LOG_REL, "docs/x.md", PLAN_REL,
                    "tests/UX_01_check.lua", "src/client/Kit.lua"], key=lambda x: p(x, spec))
    assert order[:2] == ["src/client/Kit.lua", "tests/UX_01_check.lua"]
    assert order[-1] == f"docs/evidence/{T}/a.png"


def test_contract_for_a_hand_built_spec_without_plan_data():
    """Callers with an older TaskSpec (no close_step) still get the contract."""
    profile = Profile(id="animaluprising", name="AU", version="1", path="x", hash="x",
                      workspace=".", allowed_dirs=["src"], branch="master", remote="origin",
                      raw={})
    a = AnimalUprisingAdapter(profile, ".")
    c = a.review_contract(TaskSpec(project_id="animaluprising", task_id="UI-01", title="t", spec="",
                             scope=["src/a.lua"], data={"tier": "B"}))
    assert c["phase"] == "build" and len(c["deferred"]) == 6
    assert "final CLOSE step with its evidence line" in "\n".join(c["deferred"])



def test_depends_on_drops_master_order_phase_labels(repo):
    """'Depends on: the A0 entry gate' names a MASTER_ORDER phase, not a sprint
    (GF-GATE1 was blocked on 2026-10-27 waiting for a sprint called A0). A
    phase-shaped id is still a dependency when a sprint plan by that name exists."""
    ws, a = repo["ws"], repo["a"]
    _write(ws, PLAN_REL, PLAN.replace(
        "**Depends on:** AS-C16.",
        "**Depends on:** the A0 entry gate in `docs/MASTER_ORDER.md` §5, AS-C16 and D1."))
    assert a.describe(T).deps == ["AS-C16"]
    _write(ws, "docs/progress/D1_PLAN.md", "# D1 — a real sprint that looks like a phase\n")
    assert a.describe(T).deps == ["AS-C16", "D1"]


def _scope_row(files):
    from foundry_coordinator.adapters.base import FAIL, VerificationResult
    return VerificationResult("au.done_check.SCOPE", FAIL, True, "",
                              f"9 files changed by {T} commits; outside the plan list: "
                              + ", ".join(files), "rev")


def test_scope_forgives_only_memory_files(repo):
    """The coordinator's own docs/memory note commits carry the sprint prefix, so
    done_check's SCOPE counted them (GF-GATE1, 2026-10-27). docs/memory files alone
    pass (0.145.1: also a session's own memory commit, reported advisory by the
    engine); any other file, or a list done_check may have cut at 6, still fails."""
    from foundry_coordinator.adapters.base import FAIL, PASS
    ws, a = repo["ws"], repo["a"]
    spec = a.describe(T)
    _write(ws, "docs/memory/CONVENTIONS.md", "# conventions\n- a note\n")
    git(ws, "add", "-A")
    git(ws, "commit", "-q", "--no-verify", "-m", f"{T}: memory notes from session ses_x: "
        "CONVENTIONS.md", "-m", "Coordinator memory commit (docs/memory). Scope override "
        "AU_ALLOW_SCOPE set to exactly these paths by the coordinator.")
    row = _scope_row(["docs/memory/CONVENTIONS.md"])
    assert a._forgive_memory_scope(spec, [row]) and row.result == PASS
    row = _scope_row(["docs/memory/CONVENTIONS.md", "src/server/Other.lua"])
    assert not a._forgive_memory_scope(spec, [row]) and row.result == FAIL
    _write(ws, "docs/memory/KNOWN_ISSUES.md", "# issues\n- by the session itself\n")
    _commit(ws, f"{T}: session edits memory directly")
    row = _scope_row(["docs/memory/CONVENTIONS.md", "docs/memory/KNOWN_ISSUES.md"])
    assert a._forgive_memory_scope(spec, [row]) and row.result == PASS
    row = _scope_row([f"docs/memory/f{i}.md" for i in range(6)])
    assert not a._forgive_memory_scope(spec, [row]) and row.result == FAIL
