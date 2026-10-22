"""Regenerate kilo.jsonc agent permission blocks from the deployed role rules.

    foundry-coordinator kilo-config PROJECT            dry run: what would change
    foundry-coordinator kilo-config PROJECT --apply    back up, write, verify

Only `agent.<name>.permission` of agents named by role rules is touched (the
value is replaced in place, so comments elsewhere survive). Generated keys
replace existing keys where they stand; new keys are added after them, except
a generated "*" catch-all, which goes first (Kilo: the last match wins). An
agent missing from kilo.jsonc is reported and never created. After writing,
the file is parsed again and every block compared; on any mismatch the backup
is restored.

Kilo caches its config per workspace instance: the new blocks take effect for
sessions created after Kilo reloads (it reloads on its own instance dispose,
which the coordinator never forces mid-session).
"""

from __future__ import annotations

import os
import shutil
import time
from typing import Optional

from foundry_automation import jsonc
from foundry_automation import role_rules as RR


class KiloConfigError(RuntimeError):
    pass


def kilo_path(profile) -> str:
    cp = (getattr(profile, "raw", None) or {}).get("config_pull") or {}
    p = str(cp.get("kilo_config") or "")
    return p if p else os.path.join(profile.workspace, "kilo.jsonc")


def merge_block(old: Optional[dict], generated: dict) -> dict:
    out: dict = {}
    if "*" in generated and not (isinstance(old, dict) and "*" in old):
        out["*"] = generated["*"]
    for k, v in (old or {}).items() if isinstance(old, dict) else ():
        out[k] = generated[k] if k in generated else v
    for k, v in generated.items():
        if k not in out:
            out[k] = v
    return out


def plan(text: str, role_rules: Optional[dict], workspace: str) -> dict:
    """{"text": new text, "changed": [agents], "unchanged": [...], "missing": [...],
    "blocks": {agent: merged block}}"""
    data = jsonc.parse(text)
    agents = (data or {}).get("agent") or {}
    blocks = RR.agent_blocks(role_rules, workspace)
    out = {"changed": [], "unchanged": [], "missing": [], "blocks": {}}
    new = text
    # W1.1: Kilo's snapshot question is a question to a person; the top-level
    # `snapshot: false` turns it off (the key is absent from the hand-written file)
    out["snapshot_changed"] = False
    if isinstance(data, dict) and data.get("snapshot") is not False:
        new = jsonc.replace_value(new, ("snapshot",), False)
        out["snapshot_changed"] = True
    for agent, gen in blocks.items():
        if agent not in agents or not isinstance(agents[agent], dict):
            out["missing"].append(agent)
            continue
        old = agents[agent].get("permission")
        merged = merge_block(old if isinstance(old, dict) else None, gen)
        out["blocks"][agent] = merged
        if old == merged and list((old or {}).keys()) == list(merged.keys()):
            out["unchanged"].append(agent)
            continue
        new = jsonc.replace_value(new, ("agent", agent, "permission"), merged)
        out["changed"].append(agent)
    out["text"] = new
    return out


def regenerate(profile, role_rules: Optional[dict] = None, apply: bool = False,
               path: str = "", store=None, clock=time.time) -> dict:
    """Dry run (default) or apply. Returns a JSON-able result."""
    path = path or kilo_path(profile)
    rules = role_rules if role_rules is not None else (
        (getattr(profile, "raw", None) or {}).get("role_rules"))
    if not rules:
        raise KiloConfigError("no role rules: deploy role rules from the router GUI first "
                              "(or put [role_rules] in the profile)")
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except OSError as e:
        raise KiloConfigError(f"cannot read {path}: {e}") from None
    try:
        p = plan(text, rules, profile.workspace)
    except jsonc.JsoncError as e:
        raise KiloConfigError(f"{path} is not valid JSONC: {e}") from None
    result = {"path": path, "changed": p["changed"], "unchanged": p["unchanged"],
              "missing": p["missing"], "applied": False, "backup": "",
              "snapshot": "set false" if p.get("snapshot_changed") else "already false"}
    if not apply or not (p["changed"] or p.get("snapshot_changed")):
        return result
    backup = f"{path}.bak-{time.strftime('%Y%m%d-%H%M%S', time.localtime(clock()))}"
    shutil.copy2(path, backup)
    result["backup"] = backup
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8", newline="") as f:
            f.write(p["text"])
        os.replace(tmp, path)
        with open(path, encoding="utf-8") as f:
            back = jsonc.parse(f.read())
        for agent in p["changed"]:
            if back["agent"][agent].get("permission") != p["blocks"][agent]:
                raise KiloConfigError(f"agent {agent}: the written block reads back differently")
        if p.get("snapshot_changed") and back.get("snapshot") is not False:
            raise KiloConfigError("snapshot: the written value reads back differently")
    except Exception as e:                                       # noqa: BLE001
        shutil.copy2(backup, path)
        result["error"] = f"{type(e).__name__}: {e}"[:500]
        result["restored"] = True
        if store is not None:
            store.event(profile.id, "", "kilo_config_failed", detail=result)
        raise KiloConfigError(f"kilo.jsonc not changed (backup restored): {result['error']}"
                              ) from None
    result["applied"] = True
    if store is not None:
        store.event(profile.id, "", "kilo_config_applied", detail=result)
    return result
