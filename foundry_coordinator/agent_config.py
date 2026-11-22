"""The first `opencode.jsonc` of an OpenCode project folder (v0.181.247).

    foundry-coordinator agent-config PROJECT                      dry run: print the file
    foundry-coordinator agent-config PROJECT --apply              write it (never overwrites)
    foundry-coordinator agent-config PROJECT --provider-from FILE take provider.foundry from
                                                                  FILE (a kilo.jsonc, an
                                                                  opencode.jsonc or the router's
                                                                  provider export)

After this file exists, everything the coordinator does to Kilo's file it does to this one
with the same code (kilo_config.regenerate, the per-track files, skills allows): OpenCode
reads the same `agent.<name>.permission`, `mcp`, `instructions` and `snapshot` keys, and
OpenCode 2.x still reads those 1.x forms (opencode.ai/v2/migrate-v1).

What the file always holds, and doctor / preflight check:
  share "disabled"          sessions are never uploaded
  autoupdate false          (2.x also: update "disable") the version only changes by hand
  enabled_providers         only "foundry": no OpenCode Zen or other provider fallback
  provider.foundry          the router's OpenAI-compatible endpoint; the key is read from the
                            server's environment ({env:FOUNDRY_KEY_OPENCODE}), never written
  subagent_depth 1          (2.x: experimental.subagent_depth) helpers start no helpers
  snapshot false            no snapshot question to a person
  agent stubs               every agent the role rules name, so regenerate can fill their
                            permission blocks (it never creates agents)
Nothing Kilo-only is written (OpenCode rejects unknown top-level keys).
"""

from __future__ import annotations

import copy
import json
import os
from typing import Optional

from foundry_automation import agent_backends as AB
from foundry_automation import jsonc
from foundry_automation import role_rules as RR


class AgentConfigError(RuntimeError):
    pass


def provider_block(source_text: str = "", source_path: str = "",
                   key_env: str = AB.OPENCODE.key_env) -> dict:
    """provider.foundry from a Kilo / OpenCode config or the router's export, with the API
    key pointed at OpenCode's own variable. Only an {env:...} reference is ever kept: a
    literal key in the source is dropped, not copied."""
    if not source_text and source_path:
        with open(source_path, encoding="utf-8") as f:
            source_text = f.read()
    if not source_text:
        raise AgentConfigError("no provider source: pass --provider-from (a kilo.jsonc or the "
                               "router's Kilo provider config export)")
    data = jsonc.parse(source_text)
    prov = ((data or {}).get("provider") or (data or {}).get("providers") or {}).get("foundry")
    if not isinstance(prov, dict):
        raise AgentConfigError("the source has no provider.foundry block")
    prov = copy.deepcopy(prov)
    opts = dict(prov.get("options") or {})
    opts["apiKey"] = "{env:" + key_env + "}"
    prov["options"] = opts
    prov.setdefault("npm", "@ai-sdk/openai-compatible")
    prov.setdefault("name", "Foundry Router")
    # Kilo-only model keys OpenCode's schema does not know are left as they are: model
    # entries are open objects in both 1.x and 2.x (2.x ignores attachment/reasoning and
    # reads image input from modalities, which the router export always writes)
    prov.pop("whitelist", None)
    prov.pop("blacklist", None)
    return prov


def _agent_names(role_rules: Optional[dict]) -> list[str]:
    out = []
    for role in RR.RULE_ROLES:
        name = RR.normalize(role, (role_rules or {}).get(role)).get("agent")
        if name and name not in out:
            out.append(name)
    return out


def opencode_config(*, provider: dict, implementer: str, dialect: str = "oc2",
                    role_rules: Optional[dict] = None, default_agent: str = "build",
                    subagents: tuple = (), instructions: tuple = ()) -> dict:
    """The config object (see the module docstring for why each key is there)."""
    if dialect not in AB.DIALECTS:
        raise AgentConfigError(f"dialect must be one of {AB.DIALECTS}")
    model = f"foundry/{implementer}" if implementer else ""
    cfg: dict = {"$schema": AB.OPENCODE.schema_url, "share": "disabled", "autoupdate": False}
    if dialect == "oc2":
        cfg["update"] = "disable"
    cfg.update({"enabled_providers": ["foundry"], "provider": {"foundry": provider}})
    if model:
        cfg["model"] = model
        cfg["small_model"] = model
    cfg["default_agent"] = default_agent
    cfg["snapshot"] = False
    if dialect == "oc2":
        cfg["experimental"] = {"subagent_depth": 1}
    else:
        cfg["subagent_depth"] = 1
    agents: dict = {default_agent: {"mode": "primary"}}
    for name in _agent_names(role_rules):
        if name in agents:
            continue
        agents[name] = {"mode": "subagent" if name in subagents else "primary"}
    cfg["agent"] = agents
    cfg["mcp"] = {}
    if instructions:
        cfg["instructions"] = list(instructions)
    return cfg


def target_path(profile) -> str:
    from .kilo_config import kilo_path
    return kilo_path(profile)


def write(profile, *, provider_from: str = "", dialect: str = "", apply: bool = False,
          role_rules: Optional[dict] = None) -> dict:
    """Build (and with apply, write) the project's opencode.jsonc. Refuses to overwrite an
    existing file: after the first write the coordinator edits it in place."""
    client = getattr(profile, "client", None) or {}
    if client.get("kind") != "opencode":
        raise AgentConfigError(f"project {profile.id}: [client] kind is not opencode")
    path = target_path(profile)
    if dialect not in AB.DIALECTS:
        dialect = str(client.get("api") or "")
        if dialect not in AB.DIALECTS:
            dialect = "oc2"                    # the current OpenCode line
    src = provider_from
    if not src:
        raise AgentConfigError("pass --provider-from: a kilo.jsonc (its provider.foundry is "
                               "copied with the key reference changed) or the router's export")
    prov = provider_block(source_path=src)
    rules = role_rules if role_rules is not None else (
        (getattr(profile, "raw", None) or {}).get("role_rules"))
    impl = ""
    try:
        impl = profile.persona("implementer", "")
    except Exception:                                            # noqa: BLE001
        impl = ""
    from . import skills_install
    helpers = (getattr(profile, "raw", None) or {}).get("helpers") or {}
    subs = tuple(x for x in (helpers.get("agent"),) if x)
    cfg = opencode_config(provider=prov, implementer=impl, dialect=dialect, role_rules=rules,
                          default_agent=str(client.get("agent") or AB.OPENCODE.default_agent),
                          subagents=subs)
    text = "// written by foundry-coordinator agent-config; the coordinator edits it in place\n" \
           + json.dumps(cfg, indent=2) + "\n"
    out = {"path": path, "dialect": dialect, "agents": list(cfg["agent"]), "applied": False,
           "skills_target": skills_install.settings(profile)["target"],
           "models": sorted((prov.get("models") or {}).keys())}
    if not apply:
        out["text"] = text                     # holds only the {env:...} key reference
        return out
    if os.path.exists(path):
        raise AgentConfigError(f"{path} exists: the coordinator edits it in place "
                               f"(kilo-config); move it aside to write a new one")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    jsonc.parse(open(tmp, encoding="utf-8").read())
    os.replace(tmp, path)
    out["applied"] = True
    return out


__all__ = ["AgentConfigError", "provider_block", "opencode_config", "write", "target_path"]
