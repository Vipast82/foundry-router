"""Automation gap audit 2026-10-28, section D (Foundry side): decisions never wait
forever on a tooling fault, and every coordinator commit is hook-safe.

  * review_access: a rebuilt packet, then the alternate reviewer, then a decision
    the lead takes first, with PARK after the timeout
  * only a `user_action` blocker cancels a timeout; a human-act or unmatched
    recommendation parks with the normal timeout
  * coordinator commits (bookkeeping/closeout, Lead, scope grant) carry the
    adapter's hook env (AU_ALLOW_SCOPE); a refused commit is unstaged again;
    the Lead's Luau is formatted before its commit
Temp git repos and scripted sessions only."""

import json
import os
import stat
from types import SimpleNamespace

import pytest

from foundry_coordinator import bookkeeping as BK
from foundry_coordinator import decision_policy as DP
from foundry_coordinator import scope as SC
from foundry_coordinator import states as S
from foundry_coordinator.adapters.git import GitError, Workspace
from foundry_coordinator.adapters.project import GenericAdapter
from foundry_coordinator.engine import Coordinator
from foundry_coordinator.projects.animaluprising.adapter import AnimalUprisingAdapter
from tests.coordinator.helpers import FakeFoundry, git, run_until
from tests.coordinator.test_lifecycle_e2e import green_t1
from tests.coordinator.test_lifecycle_engine import _access_denied, _game, _state

P0 = SimpleNamespace(profile=SimpleNamespace(raw={}))


# ------------------------------------------------------------------ decision policy

def test_review_access_is_a_decision_that_parks_after_the_timeout():
    assert DP.classify("review_access", {}) == "decision"
    m = DP.meta(P0, "review_access", {"recommended": DP.PARK},
                ["access fixed: review again", DP.PARK])
    assert m == {"decision_class": "decision", "recommended": json.dumps(DP.PARK),
                 "timeout_minutes": 30}


def test_an_unmatched_lead_recommendation_parks_with_the_normal_timeout():
    m = DP.meta(P0, "lead_escalation", {"reason": "lead_asks",
                                        "recommended": "something the options don't say"},
                ["layout A", "layout B"])
    assert m["recommended"] == json.dumps(DP.PARK) and m["timeout_minutes"] == 30
    # a kind with its own stop option parks on that option
    m = DP.meta(P0, "no_progress", {}, ["a", "stop the task"], recommendation="zzz")
    assert m["recommended"] == json.dumps("stop the task") and m["timeout_minutes"] == 30
    # a kind that understands no stop keeps waiting (as before)
    assert DP.meta(P0, "obligation_conflict", {}, ["a", "b"],
                   recommendation="neither")["recommended"] is None
    # no recommendation at all: the operator answers
    assert DP.meta(P0, "no_progress", {}, ["a", "b"])["recommended"] is None


def test_only_a_user_action_blocker_cancels_the_timeout():
    opts = ["Victor clicks Connect in Studio", "drop the live check"]
    # a human act under a product blocker is never applied: it parks
    m = DP.meta(P0, "implementer_blocked", {"blocker_kind": "product",
                                            "recommended": opts[0]}, opts)
    assert m["recommended"] == json.dumps(DP.PARK) and m["timeout_minutes"] == 30
    # "the operator" wording alone no longer cancels the timeout of a normal option
    m = DP.meta(P0, "implementer_blocked", {"blocker_kind": "tooling",
                                            "recommended": "drop the live check"}, opts)
    assert m["recommended"] == json.dumps("drop the live check")
    # user_action: waits for the owner, no timeout
    m = DP.meta(P0, "implementer_blocked", {"blocker_kind": "user_action",
                                            "recommended": "drop the live check"}, opts)
    assert m["recommended"] is None and m["timeout_minutes"] == 0


def test_lead_takes_review_access_and_scope_requests_first():
    assert "review_access" in Coordinator.LEAD_WAIT_KINDS
    assert "scope_request" in Coordinator.LEAD_WAIT_KINDS


# ------------------------------------------------------------------ review_access flow

def test_access_fault_retries_with_a_rebuilt_packet_then_the_alternate_reviewer(tmp_path):
    c, client, ff, _, _ = _game(tmp_path, green_t1, foundry=FakeFoundry(_access_denied))
    c.approve("game", "T1", [], "victor")
    assert run_until(c, lambda: _state(c) == S.BLOCKED, limit=60)
    personas = [x["persona"] for x in ff.chats]
    assert personas == ["Foundry-Kilo-Sonnet", "Foundry-Kilo-Sonnet", "Foundry-Kilo-Opus"]
    retries = [json.loads(e["detail"]) for e in c.store.q(
        "SELECT detail FROM events WHERE kind='review_access_retry' ORDER BY id")]
    assert [r["action"] for r in retries] == ["rebuild_packet", "alternate_reviewer"]
    t = c.store.task("game", "T1")
    did, = t["data"]["decision_wait"]["ids"]
    d = c.obligations.decision(did)
    assert d["kind"] == "review_access" and d["decision_class"] == "decision"
    assert json.loads(d["recommended"]) == DP.PARK and d["deadline_ts"]
    assert len(client.sessions) == 1                   # never a new implement session


def test_access_fault_cleared_by_the_rebuilt_packet_needs_no_decision(tmp_path):
    from tests.coordinator.helpers import review_json
    calls = []

    def reviewer(packet):
        calls.append(packet)
        return _access_denied(packet) if len(calls) == 1 else review_json(packet)
    c, client, ff, _, _ = _game(tmp_path, green_t1, foundry=FakeFoundry(reviewer))
    c.approve("game", "T1", [], "victor")
    assert run_until(c, lambda: _state(c) == S.COMPLETE, limit=60)
    assert len(ff.chats) == 2 and not c.store.q(
        "SELECT 1 FROM decisions WHERE kind='review_access'")
    # the retry is a full packet, not an incremental "same commit" re-review
    assert "incremental re-review of the SAME commit" not in json.dumps(calls[1])


# ------------------------------------------------------------------ hook-safe commits

HOOK = """#!/bin/sh
case ";$AU_ALLOW_SCOPE;" in
  *";$(git diff --cached --name-only | head -n 1);"*) exit 0 ;;
esac
echo "refused: AU_ALLOW_SCOPE=$AU_ALLOW_SCOPE" >&2
exit 1
"""


def _hooked_repo(tmp_path):
    root = tmp_path / "w"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.email", "t@t.invalid")
    git(root, "config", "user.name", "t")
    (root / "a.txt").write_text("a\n")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "init")
    hook = root / ".git" / "hooks" / "pre-commit"
    hook.write_text(HOOK, newline="\n")
    os.chmod(hook, os.stat(hook).st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return root, Workspace(str(root))


def test_a_refused_commit_is_unstaged_and_the_env_passes_the_hook(tmp_path):
    root, ws = _hooked_repo(tmp_path)
    (root / "docs").mkdir()
    (root / "docs" / "n.md").write_text("n\n")
    with pytest.raises(GitError) as e:
        ws.commit_paths(["docs/n.md"], "T1: note")
    assert "unstaged" in str(e.value)
    assert git(root, "diff", "--cached", "--name-only").strip() == ""      # nothing staged
    sha = ws.commit_paths(["docs/n.md"], "T1: note", env={"AU_ALLOW_SCOPE": "docs/n.md"})
    assert sha and git(root, "status", "--porcelain").strip() == ""


class _Store:
    def __init__(self, task):
        self.t, self.events = task, []

    def event(self, pid, tid, kind, session_id=None, detail=None, **kw):
        self.events.append((kind, detail))

    def task(self, pid, tid):
        return self.t

    def merge_data(self, pid, tid, **kw):
        self.t["data"].update(kw)


def _P(ws, root, adapter):
    return SimpleNamespace(id="game", ws=ws, adapter=adapter,
                           profile=SimpleNamespace(commit={"prefix": "{task}"},
                                                   workspace=str(root)))


class _EnvAdapter:
    def __init__(self):
        self.asked = []

    def commit_env(self, paths):
        self.asked.append(list(paths))
        return {"AU_ALLOW_SCOPE": ";".join(paths)}


def test_bookkeeping_and_closeout_commits_carry_the_adapter_env(tmp_path):
    root, ws = _hooked_repo(tmp_path)
    (root / "docs").mkdir()
    (root / "docs" / "T1_CLOSE.md").write_text("closed\n")
    t = {"task_id": "T1", "data": {}}
    c = SimpleNamespace(store=_Store(t))
    ad = _EnvAdapter()
    sha, err = BK.commit(c, _P(ws, root, ad), t, ["docs/T1_CLOSE.md"], "closeout", "closeout")
    assert sha and not err and ad.asked == [["docs/T1_CLOSE.md"]]
    # a generic adapter gives no env: the hook refuses, the paths are unstaged, an event says so
    (root / "docs" / "T1_MORE.md").write_text("more\n")
    sha, err = BK.commit(c, _P(ws, root, GenericAdapter.__new__(GenericAdapter)), t,
                         ["docs/T1_MORE.md"], "more", "closeout")
    assert not sha and "unstaged" in err
    assert c.store.events[-1][0] == "coordinator_commit_failed"
    assert git(root, "diff", "--cached", "--name-only").strip() == ""


def test_the_au_adapter_names_exactly_the_paths():
    a = AnimalUprisingAdapter.__new__(AnimalUprisingAdapter)
    assert a.commit_env(["src\\b.lua", "docs/a.md"]) == {"AU_ALLOW_SCOPE": "docs/a.md;src/b.lua"}
    assert a.commit_env([]) == {}
    assert GenericAdapter.commit_env(None, ["x"]) == {}


def test_a_scope_grant_commit_carries_the_env_and_is_unstaged_on_refusal(tmp_path):
    root, ws = _hooked_repo(tmp_path)
    (root / "plan.md").write_text("- src/a.txt\n")
    git(root, "add", "-A")
    git(root, "commit", "-q", "--no-verify", "-m", "plan")

    class Grant(_EnvAdapter):
        def grant_scope(self, spec, path, why):
            (root / "plan.md").write_text(f"- src/a.txt\n- {path}\n")
            return "plan.md"
    t = {"task_id": "T1", "data": {}, "session_id": ""}
    c = SimpleNamespace(store=_Store(t), clock=lambda: 1.0)
    sha = SC._grant(c, _P(ws, root, Grant()), t, None, "tests/x.lua", "r", "rule", {},
                    actor="policy")
    assert sha and git(root, "status", "--porcelain").strip() == ""

    class Refused(Grant):
        def commit_env(self, paths):
            return {}
    sha = SC._grant(c, _P(ws, root, Refused()), t, None, "tests/y.lua", "r", "rule", {},
                    actor="policy")
    assert sha == "" and c.store.events[-1][0] == "scope_grant_failed"
    assert git(root, "status", "--porcelain").strip() == ""      # unstaged and restored


def test_the_lead_commit_formats_luau_then_commits_with_the_env(tmp_path):
    root, ws = _hooked_repo(tmp_path)
    (root / "src").mkdir()
    (root / "src" / "m.lua").write_text("return {  }\n")
    formatted = []

    class Lead(_EnvAdapter):
        def describe(self, tid):
            return SimpleNamespace(scope=["src/m.lua"])

        def exempt_paths(self, spec):
            return []

        def format_for_commit(self, paths):
            formatted.append(list(paths))
            return "stylua formatted 1 file(s)"
    t = {"task_id": "T1", "data": {}}
    fake = SimpleNamespace(store=_Store(t), lead=SimpleNamespace(_act=lambda *a, **k: None))
    sha = Coordinator._lead_commit_pending(fake, _P(ws, root, Lead()), t,
                                           {"case_id": "rc1"}, {"hypothesis": "fix"})
    assert sha and formatted == [["src/m.lua"]]
    kinds = [k for k, _ in fake.store.events]
    assert "lead_commit_formatted" in kinds and "lead_commit" in kinds
