"""Coordinator side of the C1 config store: pull the deployed revision, apply
it at a safe point onto the local TOML (local keys stay local), reject an
invalid one, keep the last applied revision when the router can't be reached,
and regenerate kilo.jsonc agent blocks from role rules."""

from __future__ import annotations

import copy
import json
import os
import time

import pytest

from foundry_automation import FORMAT
from foundry_automation import document as D
from foundry_automation import jsonc
from foundry_automation import role_rules as RR
from foundry_coordinator.adapters.foundry import FoundryError
from foundry_coordinator.config_pull import ConfigPuller
from foundry_coordinator.kilo_config import KiloConfigError, regenerate
from foundry_coordinator.lead import kilo_decision, lead_write_permissions

from .helpers import FakeFoundry, coordinator, write_profile


class ConfigFoundry(FakeFoundry):
    def __init__(self):
        super().__init__()
        self.body = None              # the deployed revision (None = nothing deployed)
        self.down = False
        self.reports: list[dict] = []
        self.gets = 0

    def get_config(self, project_id, etag=""):
        self.gets += 1
        if self.down:
            raise FoundryError("GET /v1/foundry/control/config: connection refused")
        if self.body is None:
            return "none", None
        if etag and etag == self.body["hash"]:
            return "unchanged", None
        return "ok", copy.deepcopy(self.body)

    def config_applied(self, payload):
        if self.down:
            raise FoundryError("down")
        self.reports.append(payload)
        return {"ok": True}

    def deploy(self, doc, rev):
        self.body = {"project": doc["project"], "rev": rev, "hash": D.doc_hash(doc),
                     "ts": time.time(), "actor": "victor", "doc": doc}


def _setup(tmp_path, foundry=None):
    ws = tmp_path / "ws"
    ws.mkdir()
    prof = write_profile(tmp_path, "game", ws)
    f = foundry or ConfigFoundry()
    c = coordinator(tmp_path, [prof], {"game": lambda ctx: None}, foundry={"game": f})
    return c, f, prof


def _doc(c, **limits):
    import tomllib
    raw = tomllib.loads(open(c.projects["game"].profile.path, encoding="utf-8").read())
    doc, _, _ = D.import_profile(raw, "game")
    doc["settings"]["limits"].update(limits)
    return doc


def _live_session(c):
    c.store.add_session("ses_live", "game", "T1", "run-1", "implementer", "implement", 1,
                        "fake", "P")
    c.store.update_session("ses_live", prompt_ts=time.time())


def test_nothing_deployed_keeps_the_toml(tmp_path):
    c, f, _ = _setup(tmp_path)
    cp = ConfigPuller(c)
    assert cp.sync("game", force=True) == "current"
    assert c.projects["game"].profile.limits["max_attempts"] == 3 and f.reports == []


def test_applies_at_a_safe_point_and_keeps_local_keys(tmp_path):
    c, f, _ = _setup(tmp_path)
    P = c.projects["game"]
    prof_obj = P.profile
    doc = _doc(c, max_attempts=7)
    doc["settings"]["foundry"] = {"url": "http://evil:1"}      # never taken from the router
    doc["role_rules"]["implementer"]["edit_paths"]["allow"] = ["src/**"]
    f.deploy(doc, 1)
    cp = ConfigPuller(c)
    _live_session(c)
    assert cp.sync("game", force=True) == "pending"
    assert P.profile.limits["max_attempts"] == 3
    assert [r["status"] for r in f.reports] == ["pending"]
    assert cp.sync("game", force=True) == "pending" and len(f.reports) == 1   # reported once
    c.store.update_session("ses_live", ended_ts=time.time())
    # the router refuses the local key, so the document is rejected as a whole
    assert cp.sync("game", force=True) == "rejected"
    assert f.reports[-1]["status"] == "rejected" and "foundry.url" in f.reports[-1]["detail"]
    assert P.profile.limits["max_attempts"] == 3
    del doc["settings"]["foundry"]
    f.deploy(doc, 2)
    assert cp.sync("game", force=True) == "applied"
    assert P.profile is prof_obj and P.profile.limits["max_attempts"] == 7
    assert P.profile.foundry["url"] == "http://foundry.invalid"
    assert f.reports[-1] == {"project": "game", "rev": 2, "hash": f.body["hash"],
                             "status": "applied", "detail": "", "restart_needed": [],
                             "source_id": cp.source_id}
    kinds = [e["kind"] for e in c.store.events("game", 20)]
    assert "config_applied" in kinds
    cache = json.load(open(os.path.join(c.state_dir, "config", "game.json"), encoding="utf-8"))
    assert cache["rev"] == 2
    # the session rules now carry the deployed role rules, hard denies included
    rules = P.adapter.session_permissions(None, "implement")
    assert kilo_decision(rules, "edit", "src/a.lua") == "allow"
    assert RR.decide(rules, "bash", "git push --force")[0] == "deny"
    helper = P.adapter.helper_permissions(None)
    assert kilo_decision(helper, "edit", "src/a.lua") == "deny"
    # unchanged on the router: nothing new
    assert cp.sync("game", force=True) == "current"


def test_invalid_revision_is_rejected_and_the_last_one_stays(tmp_path):
    c, f, _ = _setup(tmp_path)
    cp = ConfigPuller(c)
    f.deploy(_doc(c, max_attempts=5), 1)
    assert cp.sync("game", force=True) == "applied"
    bad = _doc(c, max_attempts=0)
    f.deploy(bad, 2)
    assert cp.sync("game", force=True) == "rejected"
    assert c.projects["game"].profile.limits["max_attempts"] == 5
    assert cp.applied("game")["rev"] == 1
    # a document that passes the shared schema but not the profile loader
    worse = _doc(c)
    worse["settings"]["workspace"]["allowed_dirs"] = ["../escape"]
    f.deploy(worse, 3)
    assert cp.sync("game", force=True) == "rejected"
    assert "leaves the workspace" in f.reports[-1]["detail"]


def test_unreachable_router_keeps_the_cached_revision(tmp_path):
    c, f, _ = _setup(tmp_path)
    f.deploy(_doc(c, max_attempts=6), 4)
    assert ConfigPuller(c).sync("game", force=True) == "applied"
    # a new process: the router is down; the cache still applies
    f.down = True
    c.projects["game"].profile.limits["max_attempts"] = 3     # as if freshly loaded from TOML
    cp = ConfigPuller(c)
    assert cp.sync("game", force=True) == "applied"
    assert c.projects["game"].profile.limits["max_attempts"] == 6
    assert cp.applied("game")["rev"] == 4
    # still down later: nothing changes, nothing raises
    assert cp.sync("game", force=True) == "current"


def test_no_cache_and_unreachable_uses_the_toml(tmp_path):
    c, f, _ = _setup(tmp_path)
    f.down = True
    assert ConfigPuller(c).sync("game", force=True) == "current"
    assert c.projects["game"].profile.limits["max_attempts"] == 3


def test_config_pull_can_be_switched_off(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    prof = write_profile(tmp_path, "game", ws, extra="[config_pull]\nenabled = false\n")
    f = ConfigFoundry()
    c = coordinator(tmp_path, [prof], {"game": lambda ctx: None}, foundry={"game": f})
    cp = ConfigPuller(c)
    assert not cp.enabled("game")
    cp.poll()
    assert f.gets == 0


def test_lead_write_access_keeps_the_hard_denies(tmp_path):
    c, _, _ = _setup(tmp_path)
    rules = lead_write_permissions(c.projects["game"], None)
    assert RR.decide(rules, "bash", "git push --force origin master")[0] == "deny"
    assert RR.decide(rules, "bash", "git status")[0] == "allow"


# --------------------------------------------------------------------------- kilo.jsonc

KILO = """{
  // AU kilo config (hand-written comments must survive)
  "model": "foundry/Foundry-Kilo-Local",
  "agent": {
    "code": {
      "model": "foundry/Foundry-Kilo-Local",
      "permission": {
        "task": {"*": "deny", "au-review": "allow"},
        "skill": {"*": "deny", "au-fast-lane": "allow"}  // kept: not generated
      }
    },
    "au-review": {
      "model": "foundry/Foundry-Kilo-Review",
      "permission": {"*": "deny", "read": {"*": "allow"}}
    }
  }
}
"""


def test_kilo_config_dry_run_then_apply(tmp_path):
    c, _, _ = _setup(tmp_path)
    P = c.projects["game"]
    path = os.path.join(P.profile.workspace, "kilo.jsonc")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(KILO)
    with pytest.raises(KiloConfigError, match="no role rules"):
        regenerate(P.profile)
    rules = RR.all_defaults()
    dry = regenerate(P.profile, rules)
    assert set(dry["changed"]) == {"code", "au-review"} and not dry["applied"]
    assert "lead" in dry["missing"]                       # never created
    assert open(path, encoding="utf-8").read() == KILO
    out = regenerate(P.profile, rules, apply=True, store=c.store, clock=lambda: 0)
    assert out["applied"] and os.path.exists(out["backup"])
    assert open(out["backup"], encoding="utf-8").read() == KILO
    text = open(path, encoding="utf-8").read()
    assert "// AU kilo config" in text        # comments outside the replaced blocks survive
    data = jsonc.parse(text)
    code = data["agent"]["code"]["permission"]
    assert list(code)[0] == "*"                            # catch-all first
    assert code["skill"] == {"*": "deny", "au-fast-lane": "allow"}   # untouched key kept
    assert code["task"] == {"*": "deny", "explore": "allow"}
    assert code["bash"]["git push*--force*"] == "deny"
    assert data["agent"]["au-review"]["permission"]["edit"]["*"] == "deny"
    assert data["agent"]["au-review"]["permission"]["edit"]["*.env"] == "deny"
    assert "kilo_config_applied" in [e["kind"] for e in c.store.events("game", 10)]
    # applying again changes nothing
    assert regenerate(P.profile, rules, apply=True)["changed"] == []


def test_kilo_config_refuses_broken_jsonc(tmp_path):
    c, _, _ = _setup(tmp_path)
    P = c.projects["game"]
    path = os.path.join(P.profile.workspace, "kilo.jsonc")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write('{"agent": {')
    with pytest.raises(KiloConfigError, match="not valid JSONC"):
        regenerate(P.profile, RR.all_defaults(), apply=True)
    assert open(path, encoding="utf-8").read() == '{"agent": {'


def test_document_format_constant():
    assert D.empty("x")["format"] == FORMAT
