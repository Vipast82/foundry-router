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

Team Lead piece 5: `track_kilo_config(track)` writes a kilo.jsonc into a track's
worktree (kilo.jsonc is untracked, so a fresh worktree has none). It starts from
the main file's text (comments, providers and models survive), turns every MCP
server a need controls on or off by the track's needs, regenerates the agent
blocks from the role rules for that worktree (Studio tools denied without the
`studio` need, Blender / audio / image tools denied without theirs), sets
`"snapshot": false`, and validates the result by parsing it back. Servers no need
controls (git, context7 ...) stay as the source has them. A stub
`{"enabled": true}` for a server the main file only knows from the global Kilo
config relies on Kilo merging project over global config.
"""

from __future__ import annotations

import copy
import os
import shutil
import time
from typing import Optional

from foundry_automation import jsonc
from foundry_automation import role_rules as RR
from foundry_automation import service_map as SM


class KiloConfigError(RuntimeError):
    pass


# 2026-10-27 rules streamline: instruction files no session may load any more.
# KILO_MCP_PROFILES.md told every session to run a profile switcher that rewrites the
# shared kilo.jsonc and named five retired MCP servers; the coordinator sets the MCP set
# per sprint from the plan's `## Needs`.
RETIRED_INSTRUCTIONS = ("KILO_MCP_PROFILES.md",)


# the Kilo-native session core (rules streamline phase 2, AU repo); once the workspace
# has it, it replaces the Cline-era core in `instructions`
SESSION_CORE = "docs/rules/SESSION_CORE.md"
OLD_CORE = "CLINERULES_CORE.md"


def drop_retired_instructions(text: str, data: Optional[dict] = None,
                              workspace: str = "") -> tuple[str, list]:
    """(new text, removed entries): `instructions` without the retired files, and, when
    `workspace` holds docs/rules/SESSION_CORE.md, with the session core in place of the
    old CLINERULES_CORE.md entry (added when neither is listed)."""
    data = jsonc.parse(text) if data is None else data
    ins = (data or {}).get("instructions") if isinstance(data, dict) else None
    if not isinstance(ins, list):
        return text, []

    def norm(x):
        return x.replace("\\", "/").lower() if isinstance(x, str) else ""
    gone = [x for x in ins if any(r.lower() in norm(x) for r in RETIRED_INSTRUCTIONS)]
    keep = [x for x in ins if x not in gone]
    if workspace and os.path.isfile(os.path.join(workspace, *SESSION_CORE.split("/"))):
        old = [x for x in keep if norm(x).endswith(OLD_CORE.lower())]
        gone += old
        have = any(norm(x).endswith(SESSION_CORE.lower()) for x in keep)
        out = []
        for x in keep:
            if x in old:
                if not have:
                    out.append(SESSION_CORE)
                    have = True
                continue
            out.append(x)
        if not have:
            out.append(SESSION_CORE)
        if out != keep:
            keep = out
    if keep == ins:
        return text, []
    return jsonc.replace_value(text, ("instructions",), keep), gone


def _skill_map(agents: dict, name: str) -> Optional[dict]:
    perm = (agents.get(name) or {}).get("permission") if isinstance(agents.get(name), dict) \
        else None
    sk = perm.get("skill") if isinstance(perm, dict) else None
    return dict(sk) if isinstance(sk, dict) else None


def with_skill_maps(blocks: dict, agents: dict, role_rules: Optional[dict]) -> dict:
    """Generated blocks with the skill maps the source file holds: each agent keeps its
    own `skill` map (generated skill rules, e.g. the foundry-images deny, win), and the
    lead agent gets the implementer's map, so it can load the Luau / Roblox baseline
    skills its rules require (AU audit H13: it inherited the global map, which allows
    neither)."""
    impl = RR.normalize("implementer", (role_rules or {}).get("implementer")).get("agent")
    lead = RR.normalize("lead", (role_rules or {}).get("lead")).get("agent")
    out = {}
    for agent, gen in blocks.items():
        base = _skill_map(agents, agent)
        if agent == lead and impl:
            base = {**(_skill_map(agents, impl) or {}), **(base or {})} or None
        gen = dict(gen)
        if base is not None:
            mine = gen.get("skill")
            mine = mine if isinstance(mine, dict) else ({"*": mine} if isinstance(mine, str)
                                                        else {})
            merged = {k: v for k, v in base.items() if k not in mine}
            merged.update(mine)
            gen["skill"] = merged
        out[agent] = gen
    return out


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
    blocks = with_skill_maps(RR.agent_blocks(role_rules, workspace), agents, role_rules)
    out = {"changed": [], "unchanged": [], "missing": [], "blocks": {}}
    new, out["instructions_removed"] = drop_retired_instructions(text, data, workspace)
    out["instructions_changed"] = new != text
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
              "snapshot": "set false" if p.get("snapshot_changed") else "already false",
              "instructions_removed": p.get("instructions_removed") or []}
    if not apply or not (p["changed"] or p.get("snapshot_changed")
                         or p.get("instructions_changed")):
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


# --------------------------------------------------------------------------- per-track file

def _attr(track, name: str, default=None):
    if isinstance(track, dict):
        return track.get(name, default)
    return getattr(track, name, default)


def track_rules(role_rules: Optional[dict], needs: list) -> Optional[dict]:
    """The role rules narrowed to a track's needs: Studio tools denied without `studio`,
    each other tool family denied without its need. None stays None (no rules)."""
    if not role_rules:
        return None
    out: dict = {}
    for role in RR.RULE_ROLES:
        r = copy.deepcopy(RR.normalize(role, role_rules.get(role)))
        if "studio" not in needs:
            r["permissions"]["studio_read"] = "deny"
            r["permissions"]["studio_write"] = "deny"
        deny = list(r.get("deny_tools") or [])
        for need, tools in SM.NEEDS_TOOLS.items():
            if need not in needs:
                deny += [t for t in tools if t not in deny]
        r["deny_tools"] = deny
        if "images" not in needs:
            # the foundry-images skill drives foundry-media: off with the image tools
            skill = {"permission": "skill", "pattern": SM.FOUNDRY_IMAGES_SKILL, "action": "deny"}
            r["extra"] = list(r.get("extra") or []) + [skill]
        out[role] = r
    return out


def track_text(text: str, needs, role_rules: Optional[dict], workspace: str) -> dict:
    """Pure part of `track_kilo_config`: {"text", "mcp": {name: enabled}, "blocks": {agent: block},
    "missing": [agents]}. Raises JsoncError on an unparseable source."""
    needs = SM.normalize_needs(needs)
    data = jsonc.parse(text)
    if not isinstance(data, dict):
        raise jsonc.JsoncError("kilo.jsonc is not an object")
    new = text
    if data.get("snapshot") is not False:
        new = jsonc.replace_value(new, ("snapshot",), False)
    new, _ = drop_retired_instructions(new, workspace=workspace)
    want = set(SM.mcp_for(needs))
    mcp = data.get("mcp") if isinstance(data.get("mcp"), dict) else None
    flags: dict = {}
    for name in SM.CONTROLLED_MCP:
        on = name in want
        cur = (mcp or {}).get(name)
        if isinstance(cur, dict):
            if cur.get("enabled") is not on:
                new = jsonc.replace_value(new, ("mcp", name, "enabled"), on)
            flags[name] = on
        elif on:                                   # the server lives in the global config only
            if "mcp" in jsonc.parse(new):
                new = jsonc.replace_value(new, ("mcp", name), {"enabled": True})
            else:
                new = jsonc.replace_value(new, ("mcp",), {name: {"enabled": True}})
            flags[name] = True
    out = {"mcp": flags, "blocks": {}, "missing": []}
    rules = track_rules(role_rules, needs)
    if rules:
        agents = data.get("agent") or {}
        for agent, gen in with_skill_maps(RR.agent_blocks(rules, workspace), agents,
                                          role_rules).items():
            if agent not in agents or not isinstance(agents[agent], dict):
                out["missing"].append(agent)
                continue
            # a track's block is the generated one outright: no stale key of the main file survives
            if agents[agent].get("permission") != gen:
                new = jsonc.replace_value(new, ("agent", agent, "permission"), gen)
            out["blocks"][agent] = gen
    out["text"] = new
    return out


def track_kilo_config(track, *, profile=None, role_rules: Optional[dict] = None, source: str = "",
                      workspace: str = "", clock=time.time) -> str:
    """Write `<track worktree>/kilo.jsonc` for the track's needs and return its path.

    track: object or dict with `.worktree`, `.needs`, optionally `.name`.
    profile: a coordinator Profile; supplies `source` (kilo_path) and `role_rules` when those
    are not given. Raises KiloConfigError when the source is unreadable or invalid, or the
    written file reads back differently (an existing target is restored from its backup)."""
    wt = str(_attr(track, "worktree") or "")
    if not wt:
        raise KiloConfigError("the track has no worktree")
    needs = SM.normalize_needs(_attr(track, "needs") or [])
    source = source or (kilo_path(profile) if profile is not None else "")
    if not source:
        raise KiloConfigError("no source kilo.jsonc: pass source= or profile=")
    if role_rules is None and profile is not None:
        role_rules = (getattr(profile, "raw", None) or {}).get("role_rules")
    try:
        with open(source, encoding="utf-8") as f:
            text = f.read()
    except OSError as e:
        raise KiloConfigError(f"cannot read {source}: {e}") from None
    try:
        p = track_text(text, needs, role_rules, workspace or wt)
    except (jsonc.JsoncError, RR.RuleError) as e:
        raise KiloConfigError(f"{source}: {e}") from None
    target = os.path.join(wt, "kilo.jsonc")
    os.makedirs(wt, exist_ok=True)
    backup = ""
    if os.path.exists(target):
        with open(target, encoding="utf-8") as f:
            if f.read() == p["text"]:
                return target                        # idempotent: nothing to rewrite
        backup = f"{target}.bak-{time.strftime('%Y%m%d-%H%M%S', time.localtime(clock()))}"
        shutil.copy2(target, backup)
    tmp = target + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8", newline="") as f:
            f.write(p["text"])
        os.replace(tmp, target)
        with open(target, encoding="utf-8") as f:
            back = jsonc.parse(f.read())
        if back.get("snapshot") is not False:
            raise KiloConfigError("snapshot: the written value reads back differently")
        for name, on in p["mcp"].items():
            if ((back.get("mcp") or {}).get(name) or {}).get("enabled") is not on:
                raise KiloConfigError(f"mcp {name}: the written value reads back differently")
        for agent, gen in p["blocks"].items():
            if back["agent"][agent].get("permission") != gen:
                raise KiloConfigError(f"agent {agent}: the written block reads back differently")
    except Exception as e:                                       # noqa: BLE001
        if backup:
            shutil.copy2(backup, target)
        else:
            try:
                os.remove(target)
            except OSError:
                pass
        raise KiloConfigError(f"track kilo.jsonc not written: {type(e).__name__}: {e}"[:500]
                              ) from None
    return target
