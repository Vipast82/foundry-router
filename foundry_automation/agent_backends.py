"""The agent CLIs the stack can drive: Kilo Code and OpenCode.

One `BackendSpec` per product holds what differs between them: binary names, the
`serve` command line and its port, the server's auth variables, config file names
and folders, the skills and rules folders, protected paths, default agents, the
tools only that product has, the router client key and the training source name.
The Foundry Client, the coordinator, the automation schema and training all read
these, so a literal like "kilo.jsonc" or "KILO_SERVER_PASSWORD" lives here once.

The Kilo spec holds exactly the values the stack used before OpenCode support
(v0.181.246): reading it changes nothing for Kilo.

A backend speaks an HTTP *dialect*:
  oc1   the opencode 1.x server (`/session`, `/session/{id}/prompt_async`, ...).
        Kilo CLI 7.x is oc1 plus extras (`/config/effective`, ...); OpenCode 1.19 is oc1.
  oc2   the OpenCode 2.x server (`/api/*`). A future Kilo on the 2.x base would be oc2.
`dialect = "auto"` means: ask the server (`GET /api/info` answers on oc2 only).

No imports from the coordinator or the client: this module is a leaf.
"""

from __future__ import annotations

from dataclasses import dataclass, field

DIALECTS = ("oc1", "oc2")
KINDS = ("kilo", "opencode")


@dataclass(frozen=True)
class BackendSpec:
    kind: str                          # "kilo" | "opencode"
    label: str                         # shown in the UI
    binaries: tuple                    # names find_binary looks up on PATH
    npm_fallbacks: tuple               # where npm -g puts them
    default_port: int
    password_env: str                  # the server's Basic-auth password variable
    username_env: str
    default_username: str
    serve_env: dict = field(default_factory=dict)   # always set on the server process
    config_names: tuple = ()           # project config files, the first is the one written
    global_config_dir: str = ""        # ~-relative
    schema_url: str = ""
    skills_dir: str = ""               # workspace-relative
    rules_dir: str = ""
    protected: tuple = ()              # globs no session may edit (config files, own folder)
    scratch: tuple = ()                # globs a recovery may delete (the CLI's own folder)
    default_agent: str = ""            # the primary agent a session runs as
    helper_subagent: str = "explore"   # the built-in read-only subagent
    only_tools: tuple = ()             # tools only this product has (session denies)
    client_key: str = ""               # router client-key name of its worker traffic
    key_env: str = ""                  # env var its config reads the router key from
    training_source: str = ""
    dialect: str = "oc1"               # default; "auto" = detect
    npm_package: str = ""

    @property
    def config_name(self) -> str:
        return self.config_names[0] if self.config_names else ""

    def global_config_paths(self) -> list[str]:
        if not self.global_config_dir:
            return []
        return [f"{self.global_config_dir}/{n}" for n in self.config_names]


# Kilo-only built-in tools that sessions deny (role_rules.UNUSED_SESSION_TOOLS keeps its own
# list for Kilo, unchanged; this copy is what an OpenCode session must NOT be sent, since
# OpenCode has none of them).
KILO = BackendSpec(
    kind="kilo", label="Kilo Code",
    binaries=("kilo", "kilo.cmd"),
    npm_fallbacks=("~/AppData/Roaming/npm/kilo.cmd", "~/.npm-global/bin/kilo"),
    default_port=4096,
    password_env="KILO_SERVER_PASSWORD", username_env="KILO_SERVER_USERNAME",
    default_username="kilo",
    serve_env={"KILO_DISABLE_EXTERNAL_SKILLS": "true"},
    config_names=("kilo.jsonc", "kilo.json"),
    global_config_dir="~/.config/kilo",
    schema_url="https://app.kilo.ai/config.json",
    skills_dir=".kilo/skills", rules_dir=".kilo/rules",
    protected=("kilo.jsonc", "kilo.json", ".kilo/*"),
    scratch=(".kilo/*", ".kilo/**"),
    default_agent="code", helper_subagent="explore",
    client_key="kilo", key_env="FOUNDRY_KEY_KILO",
    training_source="kilo", dialect="oc1", npm_package="@kilocode/cli",
)

# OpenCode (anomalyco/opencode, formerly sst/opencode). 2.x is the current line (2.3.2,
# 2026-11-18, npm @opencode/cli); 1.19.x (npm opencode-ai) speaks oc1 like Kilo. The two
# cannot be installed side by side (both are `opencode`), so the dialect is detected.
OPENCODE = BackendSpec(
    kind="opencode", label="OpenCode",
    binaries=("opencode", "opencode.cmd", "opencode.exe"),
    npm_fallbacks=("~/AppData/Roaming/npm/opencode.cmd", "~/.npm-global/bin/opencode",
                   "~/.opencode/bin/opencode"),
    default_port=4097,
    # 2.x reads OPENCODE_PASSWORD and still accepts the 1.x name; we set both (see serve_env_for)
    password_env="OPENCODE_SERVER_PASSWORD", username_env="OPENCODE_SERVER_USERNAME",
    default_username="opencode",
    serve_env={
        # never read ~/.claude/skills, .claude/skills, .agents/skills or CLAUDE.md: the
        # workstation and the game repo have .claude folders for Claude Code itself
        # (skill discovery: OPENCODE_DISABLE_EXTERNAL_SKILLS / _CLAUDE_CODE_SKILLS; rules:
        # OPENCODE_DISABLE_CLAUDE_CODE also skips every CLAUDE.md). Sharing has no env switch:
        # the config renderer always writes "share": "disabled".
        "OPENCODE_DISABLE_EXTERNAL_SKILLS": "true",
        "OPENCODE_DISABLE_CLAUDE_CODE_SKILLS": "true",
        "OPENCODE_DISABLE_CLAUDE_CODE": "true",
        "OPENCODE_DISABLE_AUTOUPDATE": "true",
        # no free OpenCode Zen models next to the Foundry provider (2.x)
        "OPENCODE_DISABLE_FREE_MODELS": "true",
    },
    config_names=("opencode.jsonc", "opencode.json"),
    global_config_dir="~/.config/opencode",
    schema_url="https://opencode.ai/config.json",
    skills_dir=".opencode/skills", rules_dir=".opencode/rules",
    protected=("opencode.jsonc", "opencode.json", ".opencode/*"),
    scratch=(".opencode/*", ".opencode/**"),
    default_agent="build", helper_subagent="explore",
    client_key="opencode", key_env="FOUNDRY_KEY_OPENCODE",
    training_source="opencode", dialect="auto", npm_package="@opencode/cli",
)

SPECS = {"kilo": KILO, "opencode": OPENCODE}


def spec(kind: str) -> BackendSpec:
    """The spec of an agent client kind; anything else (fake, scripted, "") is Kilo, which
    is what the stack assumed before there was a choice."""
    return SPECS.get(str(kind or "kilo").strip().lower(), KILO)


def is_agent_kind(kind: str) -> bool:
    return str(kind or "").strip().lower() in SPECS


def serve_env_for(s: BackendSpec, password: str = "") -> dict:
    """Environment for the server process: the spec's fixed variables plus the password
    under every name that product reads (OpenCode 2.x: OPENCODE_PASSWORD)."""
    env = dict(s.serve_env)
    if password:
        env[s.password_env] = password
        if s.kind == "opencode":
            env["OPENCODE_PASSWORD"] = password
    return env


def all_protected() -> tuple:
    """Config files and folders of every backend: a session of either may edit none."""
    out: list = []
    for s in SPECS.values():
        for p in s.protected:
            if p not in out:
                out.append(p)
    return tuple(out)


def all_config_names() -> tuple:
    out: list = []
    for s in SPECS.values():
        for n in s.config_names:
            if n not in out:
                out.append(n)
    return tuple(out)


# -- permission dialects -------------------------------------------------------------------
# The coordinator writes Kilo / opencode 1.x rules: [{permission, pattern, action}], last
# match wins. OpenCode 2.x takes [{action, resource, effect}], also last match wins, with
# some tools renamed (docs: opencode.ai/docs/permissions, migrate-v1 "Permissions and tools").
OC2_ACTION = {"bash": "shell", "task": "subagent", "write": "edit", "patch": "edit",
              "multiedit": "edit", "apply_patch": "edit"}


# What every OpenCode session is denied on top of the coordinator's rules (the role rules
# already deny Kilo's own config; these are OpenCode's): its config files and folder, and
# shell commands that would print its server password. Appended last, so they win.
OPENCODE_GUARD = (
    [{"permission": p, "pattern": f, "action": "deny"}
     for p in ("edit", "write", "patch", "multiedit", "apply_patch")
     for f in ("opencode.jsonc", "*/opencode.jsonc", "opencode.json", "*/opencode.json",
               ".opencode/*", "*/.opencode/*")]
    + [{"permission": "bash", "pattern": p, "action": "deny"}
       for p in ("*OPENCODE_SERVER_PASSWORD*", "*OPENCODE_PASSWORD*", "*FOUNDRY_KEY_OPENCODE*")])


def oc2_rules(rules) -> list[dict]:
    """1.x session rules -> 2.x rules, order kept (so the same rule still wins)."""
    out = []
    for r in rules or []:
        if not isinstance(r, dict):
            continue
        perm = str(r.get("permission") or "*")
        out.append({"action": OC2_ACTION.get(perm, perm),
                    "resource": str(r.get("pattern") or "*"),
                    "effect": str(r.get("action") or "ask")})
    return out


def oc1_rules(rules) -> list[dict]:
    """2.x rules -> 1.x shape (for code that reads rules back); 2.x names kept as they are
    except shell/subagent, which map back to bash/task."""
    back = {"shell": "bash", "subagent": "task"}
    out = []
    for r in rules or []:
        if isinstance(r, dict):
            a = str(r.get("action") or "*")
            out.append({"permission": back.get(a, a), "pattern": str(r.get("resource") or "*"),
                        "action": str(r.get("effect") or "ask")})
    return out


__all__ = ["OPENCODE_GUARD", "oc2_rules", "oc1_rules", "OC2_ACTION", "BackendSpec", "KILO", "OPENCODE", "SPECS", "DIALECTS", "KINDS", "spec",
           "is_agent_kind", "serve_env_for", "all_protected", "all_config_names"]
