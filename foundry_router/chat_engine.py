"""Operator chat (W3, 2026-10-22): plain-language control of the whole stack
from the Automation page, multi-turn, with the model proposing and the router
deciding.

How a turn works
  1. The operator's text and a compact CONTEXT (hashed blocks: project index
     from the coordinator's bridge data, deployed settings summary, usage,
     stack status from the Foundry Client heartbeat, open decisions) go to the
     persona `Foundry-Operator-Chat` with a fixed TOOL CATALOG. Only blocks
     whose hash changed since this thread's last turn are sent again.
  2. The model answers JSON `{say, ask, calls[]}`. Every call is validated
     here, deterministically: known tool, schema fields and types, project
     allow-list, locked / local settings keys, size caps. An invalid call is
     dropped with the reason shown.
  3. Calls of an AUTO class (reads, drafts, previews) run at once and their
     results go back to the model for another round (at most 4 model calls and
     8 tool calls per turn). Calls of a CONFIRM class (deploy, cutoffs,
     persona, sprint, decision, restart) become one confirm card the operator
     ticks and confirms; each class can be switched to auto in the GUI
     (chat_auto_classes). `ask` ends the turn with a question.
  4. Everything is persisted (chat_threads, chat_turns, chat_confirms) and
     audited in chat_audit_log (cc_action_log style rows). Mutations that go
     to the coordinator or the Foundry Client are ordinary cc_actions carrying
     {chat: {thread, turn, call}} in their payload.

The model never executes anything and never sees a secret: settings reach it
through the config store's documents (which refuse local keys and secrets),
and every write goes through the same store, persona, cutoff and action code
the GUI buttons use, under the signed-in operator's name.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

from foundry_automation import ROUTER_DOC
from foundry_automation import document as D
from foundry_automation import role_rules as RR
from foundry_automation import schema as SCH

from .automation_store import StoreError
from .control_center import ControlError, _hash, _j, _s, _uj

log = logging.getLogger(__name__)

PERSONA = "Foundry-Operator-Chat"      # older versions routed the chat through it
# The built-in chain when operator_chat.routes is empty: Claude Sonnet, then
# GPT-6.1 Sol, then the local Qwen, each on the first enabled backend serving it
PERSONA_CHAIN = ["claude-sonnet-5-5", "gpt-6.1-sol", "qwen3.8-flash-next-iq3_s"]
MAX_MODEL_CALLS = 4
MAX_TOOL_CALLS = 8
MAX_TEXT = 4000
MAX_TOKENS = 1400
HISTORY_TURNS = 6                      # verbatim; older turns are summarised
RESULT_CHARS = 3000                    # a tool result shown to the model, at most
CONTEXT_CHARS = 6000
CONFIRM_TTL_S = 24 * 3600
AUTO_CLASSES = ("read", "draft", "preview", "ask")
CONFIRM_CLASSES = ("deploy", "cutoffs", "persona", "sprint", "decision", "restart", "tracks",
                   "memory", "backend", "router_config", "admin")
CLASS_LABEL = {"deploy": "Deploy or roll back settings", "cutoffs": "Change usage cutoffs",
               "persona": "Change personas (routes, settings, new personas, client-key grants, "
               "eval runs)", "sprint": "Start, queue, pause, resume or "
               "cancel sprints (and pause/resume a project)", "decision": "Answer decisions "
               "and questions", "restart": "Restart stack components / reload kilo.jsonc",
               "tracks": "Start, stop or merge parallel tracks; change max_agents",
               "memory": "Add to or edit project memory (committed to the game repo)",
               "backend": "Turn a whole backend on or off",
               "router_config": "Change router settings in config.yaml (and the chat's own backend)",
               "admin": "Other router admin actions (model research, toggles, pricing, MCP "
               "servers...); deletes and clears always ask"}
# the task states the queue tools accept, mirrored from the coordinator
_TERMINAL = ("COMPLETE", "CANCELLED")

SCHEMA = [
    """CREATE TABLE IF NOT EXISTS chat_threads (
      thread_id TEXT PRIMARY KEY, actor TEXT, title TEXT, project TEXT, status TEXT,
      created_ts REAL, updated_ts REAL, turns INTEGER, summary TEXT, context_hashes TEXT)""",
    "CREATE INDEX IF NOT EXISTS idx_chat_threads_actor ON chat_threads(actor, updated_ts)",
    """CREATE TABLE IF NOT EXISTS chat_turns (
      turn_id TEXT PRIMARY KEY, thread_id TEXT, seq INTEGER, ts REAL, actor TEXT, role TEXT,
      text TEXT, say TEXT, ask TEXT, calls TEXT, results TEXT, refused TEXT, context_sent TEXT,
      model_calls INTEGER, tool_calls INTEGER, served_model TEXT, prompt_tokens INTEGER,
      completion_tokens INTEGER, status TEXT, error TEXT, confirm_id TEXT, changes TEXT)""",
    "CREATE INDEX IF NOT EXISTS idx_chat_turns_thread ON chat_turns(thread_id, seq)",
    """CREATE TABLE IF NOT EXISTS chat_confirms (
      confirm_id TEXT PRIMARY KEY, thread_id TEXT, turn_id TEXT, actor TEXT, calls TEXT,
      status TEXT, created_ts REAL, expires_ts REAL, decided_ts REAL, decided_by TEXT,
      picked TEXT, results TEXT)""",
    """CREATE TABLE IF NOT EXISTS chat_auto_classes (
      cls TEXT PRIMARY KEY, auto INTEGER, updated_by TEXT, updated_ts REAL)""",
    """CREATE TABLE IF NOT EXISTS chat_audit_log (
      id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, thread_id TEXT, turn_id TEXT, status TEXT,
      actor TEXT, detail TEXT)""",
    "CREATE INDEX IF NOT EXISTS idx_chat_audit_thread ON chat_audit_log(thread_id, id)",
]

OUTPUT_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["say", "ask", "calls"],
    "properties": {
        "say": {"type": "string"},
        "ask": {"type": ["object", "null"], "additionalProperties": False,
                "required": ["text", "options"],
                "properties": {"text": {"type": "string"},
                               "options": {"type": "array", "items": {"type": "string"}}}},
        "calls": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["id", "tool", "args"],
            "properties": {"id": {"type": "string"}, "tool": {"type": "string"},
                           "args": {"type": "object"}}}},
    },
}

SYSTEM = (
    "You are the operator chat of Foundry, a router that runs an autonomous coding stack: a "
    "coordinator that runs sprints (tasks) on projects through Kilo coding sessions, personas "
    "that route model calls (Claude, GPT, local), usage cutoffs, versioned automation settings "
    "per project (draft -> validate -> deploy -> rollback, with role rules), and a workstation "
    "Foundry Client that runs the Kilo server, the coordinator and Rojo. The operator talks to "
    "you in plain language; you do the work with the tools below and answer briefly.\n"
    "Rules:\n"
    "- Reply with JSON only: {\"say\": string, \"ask\": null | {\"text\", \"options\": []}, "
    "\"calls\": [{\"id\", \"tool\", \"args\"}]}. `say` is your message to the operator (plain "
    "text, short). `ask` ends the turn with one question (give options when they exist). `calls` "
    "are tool calls to make now; use the exact tool names and argument names given.\n"
    "- Read before you change: look at the current state with the read tools, then propose. "
    "Reads, drafts and previews run at once and their results come back to you in this same "
    "turn. Changes of class CONFIRM are shown to the operator as a card they confirm; you do "
    "not need to ask for permission in words, just make the call and say what it will do.\n"
    "- Never invent task ids, project ids, persona names, model names or settings paths: take "
    "them from the context or from a read tool. If something is ambiguous, use `ask`.\n"
    "- Settings edits go to the project's draft (set_settings / set_role_rules) and only "
    "deploy_settings makes them live; say so. Secrets, local paths and locked keys cannot be "
    "changed here; say where they live (the Foundry Client's Settings page, config.yaml).\n"
    "- Sprints: plan_sprint_steps asks the coordinator for checked queue steps; run_sprint_steps "
    "runs the ones the operator confirms. Pause, resume, cancel and unblock one sprint with "
    "task_action; project_pause / project_resume stop or start a whole project.\n"
    "- Decisions and questions are answered with operator authority only after the confirm card; "
    "you may recommend an option and say why.\n"
    "- Stack: restart_kilo / restart_coordinator / restart_rojo / reload_kilo_config go to the "
    "Foundry Client; if no client is attached, say so and give the command "
    "`foundry-client restart kilo --wait`.\n"
    "- Parallel tracks (a dev-team lead): analyze_tracks reads every sprint plan and says which "
    "sprints can run side by side (needs: studio, blender, audio, backend; file overlap and "
    "dependencies make sprints serial). propose_tracks(project, max_agents) returns a plan "
    "card (plan_id, tracks, agent count); track_start(plan_id) starts the proposed tracks after "
    "the operator confirms. Each track is a git worktree + branch with its own Kilo session; "
    "Studio stays with the main checkout and a track's Studio work runs at merge time. "
    "track_status shows them; track_stop / track_merge act on one; set_max_agents changes "
    "the live cap (the main sprint always counts first). Nothing starts without the card.\n"
    "- Services: the `services` context block lists each service's status and the sprints waiting "
    "for one (a sprint declares needs: studio, blender, audio, images, gpu, backend; a missing "
    "service makes it wait, never fail). services_status shows detail; service_restart and "
    "service_enable only work for services a Foundry Client component runs.\n"
    "- Project memory (docs/memory in the game repo: decisions, conventions, known issues, the "
    "asset registry, per-sprint and per-track notes) is what every session reads: memory_search "
    "and memory_summary read it; memory_add appends a fact under a heading and memory_edit "
    "replaces a heading's body, both committed by the coordinator after the confirm card. Record "
    "the operator's standing decisions and rules there when asked to remember something.\n"
    "- The playbook is the cross-project knowledge base (templates, how-tos, fixes: error "
    "signature -> verified fix, scripts, formats) every project's sessions also read: kb_search "
    "reads it; kb_add / kb_edit change it (coordinator commits, confirm card). Project facts go "
    "to memory, reusable org-wide knowledge to the playbook.\n"
    "- Installing the Foundry Client on a workstation: read_install_guide reads the install "
    "guide (docs/CLIENT_MANAGER.md) section by section and client_install_status says whether "
    "installers for this router's version are downloadable (with links), plus every connected "
    "client's version and heartbeat. Walk the operator through it one step at a time, give the "
    "download link for their OS, and check client_install_status at the end to confirm the "
    "client registered. The dashboard's Client tab shows the same guide and downloads.\n"
    "- The router itself: you can see and change all of it. Personas: list_personas / get_persona, "
    "then set_persona_routes (family words local / Claude / ChatGPT keep that family's current "
    "routes), set_persona_fields, create_persona (starts disabled), grant_persona_to_client; "
    "after any persona change offer run_persona_evals. Backends: set_backend_enabled. Settings: "
    "get_router_config / set_router_config (config.yaml, versioned, restart-needed shown). "
    "Anything else: list_admin_endpoints, then admin_get to read and admin_call to change. "
    "Model research: research_status, research_models. 'How is performance?': "
    "performance_report, then give findings and concrete recommendations (which setting, which "
    "value, why). The chat's own model: get_chat_backend / set_chat_backend.\n"
    "- At most 8 tool calls per turn. Keep `say` under 120 words. No markdown tables.")


# ---------------------------------------------------------------------------- helpers

def _now() -> float:
    return time.time()


def _compact(v: Any, n: int) -> str:
    s = json.dumps(v, separators=(",", ":"), default=str, sort_keys=True)
    return s if len(s) <= n else s[:n - 20] + '..."[truncated]"'


def _hash_block(v: Any) -> str:
    return hashlib.sha256(_compact(v, 10 ** 7).encode()).hexdigest()[:12]


def _json_block(text: str) -> str:
    t = (text or "").strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else t
        t = t.rsplit("```", 1)[0]
    i, j = t.find("{"), t.rfind("}")
    return t[i:j + 1] if i >= 0 and j > i else t


def _settings_first(changes: list, cap: int = 60) -> list:
    """Diff rows for a card: settings before role rules (a first deploy adds
    dozens of default role-rule rows), at most `cap`."""
    rows = sorted(changes, key=lambda c: (str(c.get("path", "")).startswith("role_rules."),
                                          str(c.get("path", ""))))
    out = rows[:cap]
    if len(rows) > cap:
        out.append({"path": f"... {len(rows) - cap} more", "op": "more", "from": None,
                    "to": None, "apply": "live"})
    return out


def parse_reply(raw: str) -> tuple[Optional[dict], str]:
    """(reply, error). A reply has say (str), ask (None | {text, options}),
    calls (list of {id, tool, args})."""
    try:
        out = json.loads(_json_block(raw))
    except (ValueError, TypeError) as e:
        return None, f"not valid JSON: {str(e)[:120]}"
    if not isinstance(out, dict):
        return None, "the reply must be a JSON object"
    say = out.get("say")
    if say is None:
        say = ""
    if not isinstance(say, str):
        return None, "say must be a string"
    ask = out.get("ask")
    if ask not in (None, "", {}):
        if isinstance(ask, str):
            ask = {"text": ask, "options": []}
        if not isinstance(ask, dict) or not isinstance(ask.get("text"), str):
            return None, "ask must be null or {text, options}"
        ask = {"text": ask["text"][:1000],
               "options": [str(o)[:200] for o in (ask.get("options") or [])
                           if isinstance(o, (str, int, float))][:8]}
    else:
        ask = None
    calls = out.get("calls") or []
    if not isinstance(calls, list):
        return None, "calls must be a list"
    clean = []
    for i, c in enumerate(calls[:MAX_TOOL_CALLS * 2]):
        if not isinstance(c, dict):
            continue
        clean.append({"id": _s(c.get("id") or f"c{i + 1}", 40), "tool": _s(c.get("tool"), 60),
                      "args": c.get("args") if isinstance(c.get("args"), dict) else {}})
    return {"say": say[:4000], "ask": ask, "calls": clean}, ""


class ChatError(ValueError):
    def __init__(self, message: str, code: str = "invalid", status: int = 400, **extra):
        super().__init__(message)
        self.code, self.status, self.extra = code, status, extra


# ---------------------------------------------------------------------------- tool catalog

@dataclass
class Tool:
    name: str
    description: str
    cls: str                                  # read | draft | preview | ask | a CONFIRM class
    args: dict = field(default_factory=dict)  # name -> {type, enum?, items?, desc?}
    required: tuple = ()
    project_arg: str = ""                     # the argument that names the project
    run: Optional[Callable[..., Awaitable[dict]]] = None
    summary: Optional[Callable[[dict], str]] = None
    # (args, ctx) -> reason to refuse the call, or "" (runs at proposal AND at confirm)
    check: Optional[Callable[[dict, Any], str]] = None
    # args -> True when this call must show a card even if its class is on auto
    force_confirm: Optional[Callable[[dict], bool]] = None
    # (args, ctx) -> what the confirm card shows under the call
    preview: Optional[Callable[[dict, Any], dict]] = None

    def to_json(self) -> dict:
        return {"name": self.name, "description": self.description, "class": self.cls,
                "args": self.args, "required": list(self.required)}


@dataclass
class Ctx:
    engine: "ChatEngine"
    actor: str
    projects: list                            # the operator's project allow-list ([] = all)
    scopes: list
    thread_id: str
    turn_id: str
    call_id: str = ""

    @property
    def svc(self):
        return self.engine.svc

    @property
    def store_actor(self) -> str:
        return f"{self.actor} via operator chat"[:80]


def _type_ok(v: Any, t: str) -> bool:
    return {"string": lambda x: isinstance(x, str),
            "boolean": lambda x: isinstance(x, bool),
            "integer": lambda x: isinstance(x, int) and not isinstance(x, bool),
            "number": lambda x: isinstance(x, (int, float)) and not isinstance(x, bool),
            "array": lambda x: isinstance(x, list),
            "object": lambda x: isinstance(x, dict),
            "any": lambda x: True}.get(t, lambda x: False)(v)


def validate_args(tool: Tool, args: dict) -> tuple[dict, list[str]]:
    """(clean args, problems). Unknown arguments are dropped with a note;
    a missing required or mistyped argument is a problem."""
    probs, clean = [], {}
    for k in tool.required:
        if k not in args or args[k] is None:
            probs.append(f"missing argument {k!r}")
    for k, v in args.items():
        spec = tool.args.get(k)
        if spec is None:
            probs.append(f"unknown argument {k!r} ignored")
            continue
        if v is None and k not in tool.required:
            continue
        if not _type_ok(v, spec.get("type", "any")):
            probs.append(f"{k} must be {spec.get('type')}")
            continue
        if spec.get("enum") is not None and v not in spec["enum"]:
            probs.append(f"{k} must be one of {spec['enum']}")
            continue
        if isinstance(v, str) and len(v) > spec.get("max", 4000):
            probs.append(f"{k} is over {spec.get('max', 4000)} characters")
            continue
        if isinstance(v, list) and len(v) > spec.get("max_items", 50):
            probs.append(f"{k} has more than {spec.get('max_items', 50)} items")
            continue
        clean[k] = v
    hard = [p for p in probs if not p.endswith("ignored")]
    return clean, (hard or probs)


# ---------------------------------------------------------------------------- engine

class ChatEngine:
    def __init__(self, svc, model_fn: Optional[Callable] = None, clock: Callable[[], float] = _now):
        self.svc = svc
        self.db = svc.db
        self.clock = clock
        self.app = None                       # set by main.py; needed for the in-process model call
        self._model_fn = model_fn
        self._locks: dict[str, asyncio.Lock] = {}
        self._usage_cache: tuple[float, Any] = (0.0, None)
        for sql in SCHEMA:
            self.db.execute(sql)
        self.tools: dict[str, Tool] = {}
        self._build_catalog()
        self._catalog_text = self._render_catalog()

    # ------------------------------------------------------------------ settings (classes)

    def auto_classes(self) -> dict:
        out = {c: False for c in CONFIRM_CLASSES}
        for r in self.db.query("SELECT cls, auto FROM chat_auto_classes"):
            if r["cls"] in out:
                out[r["cls"]] = bool(r["auto"])
        return out

    def set_auto_classes(self, patch: dict, actor: str) -> dict:
        cur = self.auto_classes()
        changed = []
        for k, v in (patch or {}).items():
            if k not in CONFIRM_CLASSES:
                raise ChatError(f"unknown class {k!r}; classes: {', '.join(CONFIRM_CLASSES)}")
            v = bool(v)
            if cur[k] != v:
                self.db.execute("INSERT INTO chat_auto_classes (cls, auto, updated_by, updated_ts) "
                                "VALUES (?,?,?,?) ON CONFLICT(cls) DO UPDATE SET auto=excluded.auto, "
                                "updated_by=excluded.updated_by, updated_ts=excluded.updated_ts",
                                (k, int(v), actor, self.clock()))
                changed.append(f"{k}={'auto' if v else 'confirm'}")
        if changed:
            self._audit("", "", "auto_classes", actor, ", ".join(changed))
            self.db.log_event("info", "chat", f"chat auto-run classes changed by {actor}: "
                              + ", ".join(changed))
        return self.auto_classes()

    def class_of(self, tool: Tool) -> str:
        """'auto' or 'confirm' for this tool right now."""
        if tool.cls in AUTO_CLASSES:
            return "auto"
        return "auto" if self.auto_classes().get(tool.cls) else "confirm"

    # ------------------------------------------------------------------ audit

    def _audit(self, thread_id: str, turn_id: str, status: str, actor: str, detail: str) -> None:
        self.db.execute("INSERT INTO chat_audit_log (ts, thread_id, turn_id, status, actor, detail) "
                        "VALUES (?,?,?,?,?,?)", (self.clock(), thread_id, turn_id, status, actor,
                                                 (detail or "")[:1000]))

    def audit(self, thread_id: str = "", limit: int = 200) -> list[dict]:
        if thread_id:
            return self.db.query("SELECT * FROM chat_audit_log WHERE thread_id=? ORDER BY id DESC "
                                 "LIMIT ?", (thread_id, max(1, min(1000, limit))))
        return self.db.query("SELECT * FROM chat_audit_log ORDER BY id DESC LIMIT ?",
                             (max(1, min(1000, limit)),))

    # ------------------------------------------------------------------ threads

    def threads(self, actor: str, limit: int = 50) -> list[dict]:
        rows = self.db.query("SELECT thread_id, title, project, status, created_ts, updated_ts, turns "
                             "FROM chat_threads WHERE actor=? AND status != 'deleted' ORDER BY "
                             "updated_ts DESC LIMIT ?", (actor, max(1, min(200, limit))))
        return rows

    def create_thread(self, actor: str, title: str = "", project: str = "") -> dict:
        tid = "cht-" + uuid.uuid4().hex[:12]
        now = self.clock()
        self.db.execute("INSERT INTO chat_threads (thread_id, actor, title, project, status, "
                        "created_ts, updated_ts, turns, summary, context_hashes) VALUES "
                        "(?,?,?,?,?,?,?,?,?,?)", (tid, actor, _s(title, 120) or "New chat",
                                                   _s(project, 120), "open", now, now, 0, "", "{}"))
        self._audit(tid, "", "thread_created", actor, _s(title, 120))
        return self.thread(tid, actor)

    def _thread_row(self, thread_id: str, actor: str) -> dict:
        r = self.db.query_one("SELECT * FROM chat_threads WHERE thread_id=? AND status != 'deleted'",
                              (thread_id,))
        if not r or r["actor"] != actor:
            raise ChatError("no such chat", "not_found", 404)
        return r

    def thread(self, thread_id: str, actor: str) -> dict:
        r = self._thread_row(thread_id, actor)
        turns = [self._turn_out(t) for t in self.db.query(
            "SELECT * FROM chat_turns WHERE thread_id=? ORDER BY seq", (thread_id,))]
        confirms = [self._confirm_out(c) for c in self.db.query(
            "SELECT * FROM chat_confirms WHERE thread_id=? ORDER BY created_ts", (thread_id,))]
        return {"thread_id": r["thread_id"], "title": r["title"], "project": r["project"],
                "status": r["status"], "created_ts": r["created_ts"], "updated_ts": r["updated_ts"],
                "turns": turns, "confirms": confirms,
                "pending_confirms": [c for c in confirms if c["status"] == "pending"]}

    def rename_thread(self, thread_id: str, actor: str, title: str) -> dict:
        self._thread_row(thread_id, actor)
        self.db.execute("UPDATE chat_threads SET title=? WHERE thread_id=?",
                        (_s(title, 120) or "Chat", thread_id))
        return self.thread(thread_id, actor)

    def delete_thread(self, thread_id: str, actor: str) -> None:
        self._thread_row(thread_id, actor)
        self.db.execute("UPDATE chat_threads SET status='deleted', updated_ts=? WHERE thread_id=?",
                        (self.clock(), thread_id))
        self.db.execute("UPDATE chat_confirms SET status='expired' WHERE thread_id=? AND "
                        "status='pending'", (thread_id,))
        self._audit(thread_id, "", "thread_deleted", actor, "")

    @staticmethod
    def _turn_out(t: dict) -> dict:
        out = dict(t)
        for k in ("ask", "calls", "results", "refused", "context_sent", "changes"):
            out[k] = _uj(t.get(k), None)
        return out

    @staticmethod
    def _confirm_out(c: dict) -> dict:
        out = dict(c)
        for k in ("calls", "picked", "results"):
            out[k] = _uj(c.get(k), None)
        return out

    def _lock(self, thread_id: str) -> asyncio.Lock:
        lk = self._locks.get(thread_id)
        if lk is None:
            lk = self._locks[thread_id] = asyncio.Lock()
        return lk

    # ------------------------------------------------------------------ context blocks

    def allowed_projects(self, projects: list) -> list[str]:
        """Project ids the operator may touch: the session's allow-list, else
        every project known to the config store or reported by the bridge."""
        known = set()
        try:
            known |= {p["project"] for p in self.svc.automation.projects()
                      if p.get("project") and p["project"] != ROUTER_DOC}
        except Exception:                                        # noqa: BLE001
            pass
        try:
            known |= set(self.svc.control.overview([], hours=24 * 30).get("projects") or [])
        except Exception:                                        # noqa: BLE001
            pass
        if projects:
            return sorted(set(projects))
        return sorted(known)

    def _block_projects(self, projects: list) -> dict:
        cc = self.svc.control
        ov = cc.overview(projects, hours=24 * 30)
        out: dict = {}
        for t in ov.get("tasks") or []:
            pid = t.get("project_id")
            if not pid:
                continue
            p = out.setdefault(pid, {"paused": bool((ov.get("project_status") or {}).get(pid, {})
                                                    .get("paused")), "tasks": []})
            tv = t.get("task_view") or {}
            cl = tv.get("checklist") or {}
            row = {"task": t["task_id"], "state": t.get("state")}
            if cl.get("total"):
                row["checklist"] = f"{cl['total'] - cl.get('open', 0)}/{cl['total']}"
            if cl.get("next_action"):
                row["next"] = _s(cl["next_action"], 80)
            n_dec = len((tv.get("decisions") or {}).get("open") or [])
            if n_dec:
                row["open_decisions"] = n_dec
            if t.get("data", {}).get("deps"):
                row["deps"] = list(t["data"]["deps"])[:6]
            if len(p["tasks"]) < 40:
                p["tasks"].append(row)
        for pid in self.allowed_projects(projects):
            out.setdefault(pid, {"paused": False, "tasks": []})
        src = [s for s in cc.sources(projects) if s["kind"] == "coordinator"]
        return {"projects": out,
                "coordinator": ("online" if any(s["fresh"] for s in src) else
                                "silent" if src else "never reported")}

    def _block_settings(self, projects: list) -> dict:
        st = self.svc.automation
        out: dict = {}
        for pid in self.allowed_projects(projects)[:10]:
            try:
                dep = st.deployed(pid)
                dr = st.draft(pid)
            except Exception:                                    # noqa: BLE001
                continue
            row: dict = {"deployed_rev": dep["rev"] if dep else None,
                         "draft_dirty": bool(dr.get("dirty"))}
            doc = (dep or dr or {}).get("doc") or {}
            s = doc.get("settings") or {}
            if s.get("roles"):
                row["roles"] = s["roles"]
            for tbl in ("lifecycle", "lead_manager", "fast_lane", "automation"):
                if isinstance(s.get(tbl), dict) and "enabled" in s[tbl]:
                    row[f"{tbl}.enabled"] = s[tbl]["enabled"]
            out[pid] = row
        try:
            rd = st.deployed(ROUTER_DOC)
            out["_router"] = {"deployed_rev": rd["rev"] if rd else None,
                              "cutoffs": ((rd or {}).get("doc") or {}).get("settings", {})
                              .get("backends") or {}}
        except Exception:                                        # noqa: BLE001
            pass
        return out

    async def _block_usage(self) -> dict:
        now = self.clock()
        if now - self._usage_cache[0] < 60 and self._usage_cache[1] is not None:
            return self._usage_cache[1]
        try:
            from .ui.automation_routes import live_usage
            u = await asyncio.wait_for(live_usage(self.svc), timeout=20)
        except Exception as e:                                   # noqa: BLE001
            u = {"error": f"usage not readable: {str(e)[:120]}"}
        out: dict = {}
        for name, row in (u or {}).items():
            if not isinstance(row, dict):
                out[name] = row
                continue
            if not row.get("subscription"):
                out[name] = {"family": row.get("family"), "enabled": row.get("enabled"),
                             "healthy": row.get("healthy")}
                continue
            w = {k: (v.get("used_pct") if isinstance(v, dict) else None)
                 for k, v in (row.get("windows") or {}).items()}
            out[name] = {"family": row.get("family"), "enabled": row.get("enabled"),
                         "healthy": row.get("healthy"), "cutoff_pct": row.get("cutoff_pct"),
                         "used_pct": w, "note": _s(row.get("note"), 80)}
        try:
            from . import cutoffs
            v = cutoffs.view(self.svc)
            out["_cutoffs"] = {"claude": v["claude"], "gpt": v["gpt"]}
        except Exception:                                        # noqa: BLE001
            pass
        self._usage_cache = (now, out)
        return out

    def _block_stack(self) -> dict:
        clients = self.svc.control.client_sources()
        if not clients:
            return {"client": "none attached", "components": []}
        c = clients[0]
        d = c.get("detail") or {}
        return {"client": c["source_id"], "fresh": c["fresh"], "age_s": c["age_s"],
                "version": c.get("version"), "overall": d.get("overall"),
                "components": [{k: x.get(k) for k in ("name", "state", "detail", "enabled",
                                                       "pending_restart") if k in x}
                               for x in d.get("components") or []],
                "config_problems": d.get("config_problems") or []}

    def _client_detail(self) -> dict:
        clients = self.svc.control.client_sources()
        return (clients[0].get("detail") or {}) if clients else {}

    def services_view(self, projects: Optional[list] = None, full: bool = False) -> dict:
        """The service registry as the Foundry Client reports it (status, provider, needed-by),
        plus the sprints waiting for a service. `full` adds the MCP probes and detail text;
        the small form (the context block) carries no timestamps, so its hash only changes when
        a service does."""
        d = self._client_detail()
        rows = d.get("services") or []
        out_rows = []
        for r in rows:
            row = {"name": r.get("name"), "status": r.get("status"), "provider": r.get("provider"),
                   "needed_by": r.get("needed_by") or []}
            if r.get("provider") == "client":
                row["component"] = r.get("component")
                row["enabled"] = r.get("enabled")
            if full:
                row.update(label=r.get("label"), kind=r.get("kind"), detail=r.get("detail"),
                           note=r.get("note"), component_state=r.get("component_state"))
            elif r.get("status") == "down":
                row["detail"] = _s(r.get("detail"), 80)
            out_rows.append(row)
        waiting = []
        try:
            rs = self.db.query("SELECT project_id, task_id, state, data FROM cc_tasks WHERE data "
                               "LIKE ? LIMIT 30", ('%"service_wait": {%',))
        except Exception:                                        # noqa: BLE001
            rs = []
        for r in rs:
            if projects and r["project_id"] not in projects:
                continue
            if r["state"] in _TERMINAL:
                continue
            w = (_uj(r.get("data"), {}) or {}).get("service_wait") or {}
            waiting.append({"project": r["project_id"], "task": r["task_id"],
                            "waiting_for": list(w.get("missing") or [])[:6],
                            "needs": list(w.get("needs") or [])[:6]})
        out = {"client": "attached" if rows or d else "none attached", "services": out_rows,
               "waiting_sprints": waiting[:10]}
        if full:
            out["mcp"] = [{k: m.get(k) for k in ("name", "type", "enabled", "available", "detail")}
                          for m in d.get("mcp") or []]
        return out

    def _service_component(self, name: str) -> tuple:
        """(component, registry row) for a service or component name; ChatError when the
        service is external (no client component runs it)."""
        name = (name or "").strip()
        rows = self._client_detail().get("services") or []
        row = next((r for r in rows if r.get("name") == name), None) or next(
            (r for r in rows if r.get("component") == name), None)
        if row is None:
            known = ", ".join(r.get("name") for r in rows) or "none reported (is the client attached?)"
            raise ChatError(f"no service {name!r}; the client reports: {known}", "unknown_service", 404)
        if row.get("provider") != "client" or not row.get("component"):
            raise ChatError(f"{row['name']} is {row.get('provider')}: no Foundry Client component "
                            f"runs it, so there is nothing to restart or switch here"
                            + (f" ({row.get('note')})" if row.get("note") else ""),
                            "external_service", 409)
        return row["component"], row

    def _block_pending(self, projects: list) -> dict:
        cc = self.svc.control
        ov = cc.overview(projects, hours=24 * 30)
        decisions = []
        for t in ov.get("tasks") or []:
            for d in ((t.get("task_view") or {}).get("decisions") or {}).get("open") or []:
                decisions.append({"project": t["project_id"], "task": t["task_id"],
                                  "decision_id": d.get("decision_id"), "kind": d.get("kind"),
                                  "question": _s(d.get("question"), 240),
                                  "options": [str(o) for o in (d.get("options") or [])][:8],
                                  "recommended": _s(d.get("recommended") or d.get("default_option"),
                                                    120),
                                  "deadline_ts": d.get("deadline_ts") or d.get("timeout_ts"),
                                  "answer_in_flight": bool(d.get("answer_action"))})
        questions = [{"project": p.get("project_id"), "task": p.get("task_id"),
                      "request_id": p.get("request_id"), "kind": p.get("kind"),
                      "questions": [_s(q.get("question") if isinstance(q, dict) else q, 200)
                                    for q in ((p.get("data") or {}).get("questions") or [])][:4]}
                     for p in (ov.get("pending") or [])[:10]]
        alerts = [{"kind": a.get("kind"), "severity": a.get("severity"),
                   "title": _s(a.get("title"), 120)} for a in cc.alerts(projects, "open", 6)]
        return {"decisions": decisions[:10], "questions": questions, "alerts": alerts}

    async def context_blocks(self, projects: list) -> dict:
        blocks = {"projects": self._block_projects(projects),
                  "settings": self._block_settings(projects),
                  "usage": await self._block_usage(),
                  "stack": self._block_stack(),
                  "services": self.services_view(projects),
                  "pending": self._block_pending(projects),
                  "memory": self._block_memory(projects)}
        return blocks

    def _memory_snapshot(self, project: str) -> Optional[dict]:
        try:
            return self.svc.control.snapshot("memory", project)
        except Exception:                                        # noqa: BLE001
            return None

    def _block_memory(self, projects: list) -> dict:
        """Project memory, small and hashed: files with counts, the latest
        decisions, live slices (memory_view.summary) per project."""
        from .memory_view import summary
        out: dict = {}
        for pid in self.allowed_projects(projects)[:6]:
            snap = self._memory_snapshot(pid)
            if snap is None:
                continue
            out[pid] = summary(snap)
        return out or {"state": "no project has reported memory"}

    # ------------------------------------------------------------------ prompt

    def _render_catalog(self) -> str:
        lines = ["Tools (name(args) [class]: what it does):"]
        for t in self.tools.values():
            a = ", ".join(f"{k}{'' if k in t.required else '?'}: {v.get('type', 'any')}"
                          + (f" in {v['enum']}" if v.get("enum") else "")
                          for k, v in t.args.items())
            lines.append(f"- {t.name}({a}) [{t.cls}]: {t.description}")
        lines.append("Classes: read/draft/preview/ask run at once; " + ", ".join(CONFIRM_CLASSES)
                     + " need the operator's confirmation unless switched to auto (irreversible "
                     "admin calls always need it).")
        return "\n".join(lines)

    def _history(self, thread_id: str) -> list[dict]:
        rows = self.db.query("SELECT * FROM chat_turns WHERE thread_id=? ORDER BY seq", (thread_id,))
        msgs: list[dict] = []
        older, recent = rows[:-HISTORY_TURNS], rows[-HISTORY_TURNS:]
        if older:
            summ = []
            for t in older:
                if t["role"] == "operator":
                    summ.append(f"operator: {_s(t.get('text'), 160)} -> assistant: "
                                f"{_s(t.get('say'), 160)}"
                                + (f" (calls: {len(_uj(t.get('calls'), []) or [])})"
                                   if t.get("calls") else ""))
                else:
                    summ.append(f"system: {_s(t.get('text'), 160)}")
            msgs.append({"role": "user", "content": "Earlier in this chat (summary):\n"
                         + "\n".join(summ[-20:])})
            msgs.append({"role": "assistant", "content": _compact({"say": "Noted.", "ask": None,
                                                                   "calls": []}, 200)})
        for t in recent:
            if t["role"] == "operator":
                ctx = _uj(t.get("context_sent"), None)
                content = "Operator: " + (t.get("text") or "")
                if ctx:
                    content += "\nContext (changed blocks): " + _compact(ctx, CONTEXT_CHARS)
                msgs.append({"role": "user", "content": content})
                reply = {"say": t.get("say") or "", "ask": _uj(t.get("ask"), None),
                         "calls": [{"id": c.get("id"), "tool": c.get("tool"), "args": c.get("args")}
                                   for c in (_uj(t.get("calls"), []) or [])]}
                msgs.append({"role": "assistant", "content": _compact(reply, 3000)})
                res = _uj(t.get("results"), []) or []
                if res:
                    msgs.append({"role": "user", "content": "Tool results: " + _compact(
                        [{"id": r.get("id"), "tool": r.get("tool"), "ok": r.get("ok"),
                          "result": r.get("result")} for r in res], RESULT_CHARS)})
                    msgs.append({"role": "assistant", "content": _compact(
                        {"say": t.get("say") or "", "ask": None, "calls": []}, 2000)})
            else:
                msgs.append({"role": "user", "content": "System: " + (t.get("text") or "")})
                msgs.append({"role": "assistant", "content": _compact(
                    {"say": "Understood.", "ask": None, "calls": []}, 200)})
        return msgs

    def _system(self) -> str:
        classes = self.auto_classes()
        auto = [c for c, v in classes.items() if v]
        note = ("Classes switched to auto by the operator (they run without a card): "
                + ", ".join(auto)) if auto else "No confirm class is on auto."
        return SYSTEM + "\n\n" + self._catalog_text + "\n" + note

    # ------------------------------------------------------------------ model call

    async def _call_model(self, messages: list[dict]) -> tuple[str, dict]:
        """One JSON-schema completion on the chat's own chain (chat_routes):
        each route pinned to its backend, through the pool (budget gate and
        ledger apply), the next route on failure. Returns (text, meta)."""
        if self._model_fn is not None:
            out = self._model_fn(messages)
            if asyncio.iscoroutine(out):
                out = await out
            return out
        from . import request_context
        routes = self.chat_routes()
        if not routes:
            raise ChatError("no chat model is available: no enabled backend serves the built-in "
                            "chain; set the chat backend (Automation -> Chat -> settings)",
                            "no_model", 503)
        errors = []
        for backend, model in routes:
            tok = request_context.pin_backend(backend)
            from . import capture as _capture
            try:
                # captured only with training_capture.operator_chat: true
                with _capture.purpose("operator_chat"):
                    res, served = await self.svc.pool.chat(
                        model, messages, options={"temperature": 0}, max_tokens=MAX_TOKENS,
                        fmt=OUTPUT_SCHEMA)
            except Exception as e:                               # noqa: BLE001
                errors.append(f"{model}@{backend}: {str(e)[:160]}")
                continue
            finally:
                request_context._backend_pin.reset(tok)
            return res.content or "", {"served_model": model, "backend": served or backend,
                                       "prompt_tokens": int(res.prompt_tokens or 0),
                                       "completion_tokens": int(res.completion_tokens or 0)}
        raise ChatError("the chat model is not available: " + " | ".join(errors)[:400],
                        "model_failed", 502)

    def chat_routes(self) -> list[tuple[str, str]]:
        """The chat's own model chain, [(backend, model)]: operator_chat.routes,
        or the built-in chain on the first enabled backend serving each model.
        Never a persona, so persona edits can't break the chat."""
        cfg = getattr(self.svc.config_store.config, "operator_chat", None)
        if cfg is not None and cfg.routes:
            return [(r.backend, r.model) for r in cfg.routes]
        from . import route_resolve
        bmap = route_resolve.backends(self.svc)
        out = []
        for m in PERSONA_CHAIN:
            hosts = [h for h in route_resolve.served_by(bmap, m) if bmap[h]["enabled"]]
            if hosts:
                out.append((hosts[0], m))
        return out

    # ------------------------------------------------------------------ validation

    def validate_call(self, call: dict, ctx: Ctx) -> tuple[Optional[dict], str]:
        """(clean call, reason-if-dropped). Deterministic: tool exists, args fit
        the schema, project allowed, settings keys allowed."""
        tool = self.tools.get(call.get("tool") or "")
        if tool is None:
            return None, f"unknown tool {call.get('tool')!r}"
        args, probs = validate_args(tool, call.get("args") or {})
        hard = [p for p in probs if not p.endswith("ignored")]
        if hard:
            return None, "; ".join(hard)[:300]
        if tool.project_arg:
            pid = str(args.get(tool.project_arg) or "")
            if pid != ROUTER_DOC:
                allowed = self.allowed_projects(ctx.projects)
                if pid not in allowed:
                    return None, (f"project {pid!r} is not one you may act on"
                                  + (f" (known: {', '.join(allowed[:8])})" if allowed else ""))
        if tool.name in ("set_settings",):
            for path in (args.get("changes") or {}):
                why = self._settings_path_problem(path)
                if why:
                    return None, f"{path}: {why}"
        if len(_compact(args, 10 ** 6)) > 8000:
            return None, "arguments are too large"
        if tool.check is not None:
            try:
                why = tool.check(args, ctx)
            except Exception as e:                               # noqa: BLE001
                why = f"{type(e).__name__}: {str(e)[:200]}"
            if why:
                return None, why[:600]
        mode = self.class_of(tool)
        if tool.force_confirm is not None and tool.force_confirm(args):
            mode = "confirm"
        return {"id": call.get("id"), "tool": tool.name, "args": args,
                "class": tool.cls, "mode": mode,
                "summary": (tool.summary(args) if tool.summary else tool.name)[:300],
                "notes": [p for p in probs if p.endswith("ignored")]}, ""

    @staticmethod
    def _settings_path_problem(path: str) -> str:
        if not isinstance(path, str) or not re.fullmatch(r"[A-Za-z0-9_.*-]{1,120}", path):
            return "not a settings path"
        if path.startswith("role_rules"):
            return "role rules are edited with set_role_rules"
        if SCH.is_local_key(path):
            return "a local/secret setting; it lives on the workstation, not in the router"
        f = SCH.find_field(path)
        if f is None:
            return "not a known setting (use search_settings)"
        if f.locked:
            return "locked; it cannot be changed from here"
        return ""

    # ------------------------------------------------------------------ the turn

    async def turn(self, thread_id: str, text: str, actor: str, projects: list,
                   scopes: Optional[list] = None) -> dict:
        text = (text or "").strip()
        if not text:
            raise ChatError("say something")
        if len(text) > MAX_TEXT:
            raise ChatError(f"message is over {MAX_TEXT} characters")
        self._thread_row(thread_id, actor)
        async with self._lock(thread_id):
            return await self._turn(thread_id, text, actor, projects, scopes or ["operator"])

    async def _turn(self, thread_id, text, actor, projects, scopes) -> dict:
        th = self._thread_row(thread_id, actor)
        turn_id = "chu-" + uuid.uuid4().hex[:12]
        seq = int(th.get("turns") or 0) + 1
        ctx = Ctx(self, actor, list(projects or []), list(scopes or []), thread_id, turn_id)
        self._audit(thread_id, turn_id, "turn", actor, f"operator message, {len(text)} chars")
        # context: only blocks whose hash changed since this thread's last turn
        try:
            blocks = await self.context_blocks(ctx.projects)
        except Exception as e:                                   # noqa: BLE001
            log.exception("chat context failed")
            blocks = {"error": f"context unavailable: {str(e)[:120]}"}
        sent_hashes = _uj(th.get("context_hashes"), {}) or {}
        hashes = {k: _hash_block(v) for k, v in blocks.items()}
        delta = {k: v for k, v in blocks.items() if sent_hashes.get(k) != hashes[k]}
        pending = self.db.query("SELECT confirm_id, calls FROM chat_confirms WHERE thread_id=? AND "
                                "status='pending'", (thread_id,))
        user = "Operator: " + text
        if delta:
            user += "\nContext (changed blocks; unchanged: " + ", ".join(
                sorted(set(blocks) - set(delta))) + "): " + _compact(delta, CONTEXT_CHARS)
        if pending:
            user += "\nA confirm card from an earlier turn is still waiting for the operator: " \
                    + _compact([{"confirm_id": p["confirm_id"],
                                 "calls": [c.get("summary") for c in _uj(p["calls"], []) or []]}
                                for p in pending], 1000) \
                    + ". Do not repeat those calls; the operator confirms or declines the card."
        messages = [{"role": "system", "content": self._system()}, *self._history(thread_id),
                    {"role": "user", "content": user}]
        executed: list[dict] = []
        confirm_calls: list[dict] = []
        refused: list[dict] = []
        changes: list[str] = []
        model_calls = tool_calls = ptok = ctok = 0
        say, ask, served, error = "", None, "", ""
        repaired = False
        final_calls: list[dict] = []
        while model_calls < MAX_MODEL_CALLS:
            try:
                raw, meta = await self._call_model(messages)
            except ChatError as e:
                error = str(e)
                break
            except Exception as e:                               # noqa: BLE001
                log.exception("chat model call failed")
                error = f"model call failed: {type(e).__name__}: {str(e)[:200]}"
                break
            model_calls += 1
            served = meta.get("served_model") or served
            ptok += int(meta.get("prompt_tokens") or 0)
            ctok += int(meta.get("completion_tokens") or 0)
            reply, perr = parse_reply(raw)
            if reply is None:
                if repaired:
                    error = f"the model's answer was not valid ({perr})"
                    break
                repaired = True
                messages += [{"role": "assistant", "content": (raw or "")[:4000]},
                             {"role": "user", "content": f"That was not valid: {perr}. Reply again "
                              "with JSON only, exactly {\"say\", \"ask\", \"calls\"}."}]
                continue
            say, ask = reply["say"], reply["ask"]
            auto_now: list[dict] = []
            for c in reply["calls"]:
                if c["tool"] == "ask_operator":
                    a = c.get("args") or {}
                    ask = ask or {"text": _s(a.get("text"), 1000),
                                  "options": [str(o)[:200] for o in (a.get("options") or [])][:8]}
                    continue
                if tool_calls + len(auto_now) + len(confirm_calls) >= MAX_TOOL_CALLS:
                    refused.append({"id": c["id"], "tool": c["tool"],
                                    "reason": f"over the {MAX_TOOL_CALLS}-call limit for one turn"})
                    continue
                clean, why = self.validate_call(c, ctx)
                if clean is None:
                    refused.append({"id": c["id"], "tool": c["tool"], "reason": why})
                    continue
                (auto_now if clean["mode"] == "auto" else confirm_calls).append(clean)
            final_calls = reply["calls"]
            if not auto_now:
                break
            results = []
            for c in auto_now:
                tool_calls += 1
                r = await self._execute(c, ctx)
                results.append(r)
                executed.append(r)
                changes += r.get("changed") or []
            if model_calls >= MAX_MODEL_CALLS:
                break
            fed = [{"id": r["id"], "tool": r["tool"], "ok": r["ok"],
                    "result": r.get("result") if r["ok"] else r.get("error")} for r in results]
            if refused:
                fed.append({"refused": refused[-len(reply["calls"]):]})
            messages += [{"role": "assistant", "content": _compact(reply, 4000)},
                         {"role": "user", "content": "Tool results: " + _compact(fed, RESULT_CHARS)
                          + "\nNow answer the operator (say), ask, or make more calls. Calls that "
                          "need confirmation were queued for the operator's card: do not repeat "
                          "them."}]
        confirm_id = ""
        if confirm_calls and not error:
            confirm_id = self._make_confirm(ctx, confirm_calls)
        if not say and not error:
            say = ("I made the calls above." if executed or confirm_calls else
                   "I have nothing to add.")
        if error and not say:
            say = "I could not finish this turn: " + error
        now = self.clock()
        self.db.execute(
            "INSERT INTO chat_turns (turn_id, thread_id, seq, ts, actor, role, text, say, ask, calls, "
            "results, refused, context_sent, model_calls, tool_calls, served_model, prompt_tokens, "
            "completion_tokens, status, error, confirm_id, changes) VALUES "
            "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (turn_id, thread_id, seq, now, actor, "operator", text, say, _j(ask) if ask else None,
             _j([*[{**c, "mode": "auto"} for c in [r["call"] for r in executed]],
                 *[{**c, "mode": "confirm"} for c in confirm_calls]]),
             _j([{k: v for k, v in r.items() if k != "call"} for r in executed]),
             _j(refused), _j(delta) if delta else None, model_calls, tool_calls, served, ptok, ctok,
             "error" if error else "ok", error, confirm_id, _j(changes)))
        title_sql = ", title=?" if seq == 1 and (th.get("title") in ("", "New chat", None)) else ""
        args: list = [now, seq, _j(hashes)]
        if title_sql:
            args.append(_s(text, 60))
        args.append(thread_id)
        self.db.execute(f"UPDATE chat_threads SET updated_ts=?, turns=?, context_hashes=?{title_sql} "
                        "WHERE thread_id=?", tuple(args))
        self._audit(thread_id, turn_id, "model", actor,
                    f"{model_calls} model call(s) on {served or '?'}, {ptok}+{ctok} tokens, "
                    f"{tool_calls} tool call(s), {len(confirm_calls)} queued for confirmation, "
                    f"{len(refused)} refused" + (f"; error: {error}" if error else ""))
        for r in refused:
            self._audit(thread_id, turn_id, "refused", actor, f"{r['tool']}: {r['reason']}")
        return {**self._turn_out(self.db.query_one("SELECT * FROM chat_turns WHERE turn_id=?",
                                                   (turn_id,))),
                "confirm": self._confirm_out(self.db.query_one(
                    "SELECT * FROM chat_confirms WHERE confirm_id=?", (confirm_id,)))
                if confirm_id else None,
                "model_reply_calls": final_calls}

    async def _execute(self, call: dict, ctx: Ctx) -> dict:
        tool = self.tools[call["tool"]]
        ctx.call_id = call["id"]
        t0 = self.clock()
        try:
            out = await tool.run(ctx, call["args"])
            if not isinstance(out, dict):
                out = {"result": out}
            res = {"id": call["id"], "tool": tool.name, "ok": True, "result": out.get("result", out),
                   "changed": list(out.get("changed") or []), "ms": round((self.clock() - t0) * 1000),
                   "call": call}
            self._audit(ctx.thread_id, ctx.turn_id, "applied" if call["class"] not in AUTO_CLASSES
                        else "read", ctx.actor, f"{tool.name} ({call['class']}): {call['summary']}"
                        + (f" -> {'; '.join(res['changed'][:4])}" if res["changed"] else ""))
            return res
        except (ChatError, ControlError, StoreError) as e:
            msg = str(e)
            val = (getattr(e, "extra", None) or {}).get("validation") or {}
            if val.get("errors"):
                msg += ": " + "; ".join(f"{x.get('path')}: {x.get('message')}"
                                        for x in val["errors"][:5])
            self._audit(ctx.thread_id, ctx.turn_id, "failed", ctx.actor, f"{tool.name}: {msg}")
            return {"id": call["id"], "tool": tool.name, "ok": False, "error": msg[:600],
                    "code": getattr(e, "code", "invalid"), "changed": [], "call": call}
        except Exception as e:                                   # noqa: BLE001
            log.exception("chat tool %s failed", tool.name)
            self._audit(ctx.thread_id, ctx.turn_id, "failed", ctx.actor,
                        f"{tool.name}: {type(e).__name__}: {e}")
            return {"id": call["id"], "tool": tool.name, "ok": False,
                    "error": f"{type(e).__name__}: {str(e)[:300]}", "changed": [], "call": call}

    # ------------------------------------------------------------------ confirm cards

    def _make_confirm(self, ctx: Ctx, calls: list[dict]) -> str:
        cid = "chc-" + uuid.uuid4().hex[:12]
        now = self.clock()
        for c in calls:
            c["preview"] = self._preview(c, ctx)
        self.db.execute("INSERT INTO chat_confirms (confirm_id, thread_id, turn_id, actor, calls, "
                        "status, created_ts, expires_ts) VALUES (?,?,?,?,?,?,?,?)",
                        (cid, ctx.thread_id, ctx.turn_id, ctx.actor, _j(calls), "pending", now,
                         now + CONFIRM_TTL_S))
        self._audit(ctx.thread_id, ctx.turn_id, "proposed", ctx.actor,
                    f"confirm card {cid}: " + "; ".join(f"{c['tool']} [{c['class']}]" for c in calls))
        return cid

    def _preview(self, call: dict, ctx: Ctx) -> dict:
        """What the card shows under the call: a diff, a question, a safe-point
        note. Read-only; never fails the card."""
        try:
            a = call["args"]
            tool = self.tools.get(call["tool"])
            if tool is not None and tool.preview is not None:
                return tool.preview(a, ctx) or {}
            if call["tool"] == "deploy_settings":
                st = self.svc.automation
                d = st.diff(a["project"])
                v = st.validate(a["project"])
                return {"changes": _settings_first(d["changes"]),
                        "restart_needed": v.get("restart_needed") or [], "valid": v.get("ok")}
            if call["tool"] == "rollback_settings":
                st = self.svc.automation
                return {"changes": _settings_first(D.diff(
                    st._resolve(a["project"], "deployed"),
                    st.revision(a["project"], int(a["rev"]))["doc"]))}
            if call["tool"] == "answer_decision":
                d = self._find_decision(a["project"], a["task_id"], a["decision_id"])
                return {"question": d.get("question"), "options": d.get("options")}
            if call["tool"].startswith("restart_") or call["tool"] == "reload_kilo_config":
                return {"stack": self._block_stack()}
            if call["tool"] in ("service_restart", "service_enable"):
                return {"services": self.services_view(ctx.projects)}
            if call["tool"] == "set_cutoffs":
                from . import cutoffs
                return {"current": cutoffs.view(self.svc)}
            if call["tool"] == "track_start" and a.get("plan_id"):
                # the plan card: the proposal the coordinator returned for this plan_id
                for r in self.db.query("SELECT result FROM cc_actions WHERE kind='tracks_propose' "
                                       "AND status='applied' AND project_id=? ORDER BY "
                                       "created_ts DESC LIMIT 20", (a["project"],)):
                    plan = _uj(r.get("result"), {}) or {}
                    if plan.get("plan_id") == a["plan_id"]:
                        return {"plan": {k: plan.get(k) for k in ("plan_id", "agents", "max_agents",
                                                                  "main", "tracks", "notes")}}
                return {"note": f"no proposal {a['plan_id']} found; propose_tracks again"}
            if call["tool"] in ("track_stop", "track_merge", "set_max_agents"):
                v = self.svc.control.tracks_view([], a["project"])
                st = (v.get("tracks") or {}).get(a["project"]) or {}
                return {"max_agents": st.get("max_agents"),
                        "tracks": [{k: t.get(k) for k in ("name", "state", "current_sprint")}
                                   for t in st.get("tracks") or []][:12]}
        except Exception as e:                                   # noqa: BLE001
            return {"note": f"no preview: {str(e)[:120]}"}
        return {}

    async def decide(self, confirm_id: str, actor: str, projects: list, picked: Optional[list],
                     decision: str, scopes: Optional[list] = None) -> dict:
        c = self.db.query_one("SELECT * FROM chat_confirms WHERE confirm_id=?", (confirm_id,))
        if not c or c["actor"] != actor:
            raise ChatError("no such confirm card", "not_found", 404)
        if c["status"] != "pending":
            raise ChatError(f"this card was already {c['status']}", "stale", 409)
        if c["expires_ts"] and c["expires_ts"] < self.clock():
            self.db.execute("UPDATE chat_confirms SET status='expired' WHERE confirm_id=?", (confirm_id,))
            raise ChatError("this card expired; ask again", "stale", 409)
        if decision not in ("confirm", "decline"):
            raise ChatError("decision must be confirm or decline")
        async with self._lock(c["thread_id"]):
            calls = _uj(c["calls"], []) or []
            now = self.clock()
            if decision == "decline" or not picked:
                self.db.execute("UPDATE chat_confirms SET status='declined', decided_ts=?, decided_by=?, "
                                "picked=? WHERE confirm_id=?", (now, actor, _j([]), confirm_id))
                self._audit(c["thread_id"], c["turn_id"], "declined", actor, f"card {confirm_id}")
                self._system_turn(c["thread_id"], actor, f"The operator declined confirm card "
                                  f"{confirm_id} (" + "; ".join(x["summary"] for x in calls) + ").",
                                  [], [])
                return self._confirm_out(self.db.query_one(
                    "SELECT * FROM chat_confirms WHERE confirm_id=?", (confirm_id,)))
            want = {str(p) for p in picked}
            run = [x for x in calls if x["id"] in want]
            if not run:
                raise ChatError("none of the picked calls is on this card")
            ctx = Ctx(self, actor, list(projects or []), list(scopes or ["operator"]),
                      c["thread_id"], c["turn_id"])
            results, changes = [], []
            for x in run:
                clean, why = self.validate_call(x, ctx)          # the world may have moved
                if clean is None:
                    results.append({"id": x["id"], "tool": x["tool"], "ok": False, "error": why,
                                    "changed": []})
                    continue
                clean["id"] = x["id"]
                r = await self._execute(clean, ctx)
                results.append({k: v for k, v in r.items() if k != "call"})
                changes += r.get("changed") or []
            self.db.execute("UPDATE chat_confirms SET status='confirmed', decided_ts=?, decided_by=?, "
                            "picked=?, results=? WHERE confirm_id=?",
                            (now, actor, _j(sorted(want)), _j(results), confirm_id))
            self._audit(c["thread_id"], c["turn_id"], "confirmed", actor,
                        f"card {confirm_id}: {len(run)} of {len(calls)} call(s) run, "
                        f"{sum(1 for r in results if r['ok'])} ok")
            text = (f"The operator confirmed card {confirm_id}: " + "; ".join(
                f"{r['tool']} -> {'ok' if r['ok'] else 'failed: ' + str(r.get('error'))[:120]}"
                for r in results) + (". Skipped: " + "; ".join(
                    x["summary"] for x in calls if x["id"] not in want) if len(run) < len(calls)
                    else ""))
            self._system_turn(c["thread_id"], actor, text, results, changes)
            return self._confirm_out(self.db.query_one(
                "SELECT * FROM chat_confirms WHERE confirm_id=?", (confirm_id,)))

    def _system_turn(self, thread_id: str, actor: str, text: str, results: list,
                     changes: list) -> None:
        th = self.db.query_one("SELECT turns FROM chat_threads WHERE thread_id=?", (thread_id,))
        seq = int((th or {}).get("turns") or 0) + 1
        now = self.clock()
        self.db.execute(
            "INSERT INTO chat_turns (turn_id, thread_id, seq, ts, actor, role, text, say, results, "
            "changes, model_calls, tool_calls, status) VALUES (?,?,?,?,?,?,?,?,?,?,0,?,'ok')",
            ("chu-" + uuid.uuid4().hex[:12], thread_id, seq, now, actor, "system", text, "",
             _j(results), _j(changes), len(results)))
        self.db.execute("UPDATE chat_threads SET updated_ts=?, turns=? WHERE thread_id=?",
                        (now, seq, thread_id))

    # ------------------------------------------------------------------ catalog

    def catalog(self) -> list[dict]:
        classes = self.auto_classes()
        return [{**t.to_json(), "mode": "auto" if t.cls in AUTO_CLASSES or classes.get(t.cls)
                 else "confirm"} for t in self.tools.values()]

    def _add(self, name, description, cls, args=None, required=(), project_arg="", summary=None,
             check=None, force_confirm=None, preview=None):
        def deco(fn):
            self.tools[name] = Tool(name, description, cls, args or {}, tuple(required), project_arg,
                                    fn, summary, check, force_confirm, preview)
            return fn
        return deco

    def _build_catalog(self) -> None:          # noqa: C901 - one place for the whole catalog
        eng = self
        P = {"type": "string", "max": 120}

        # ---- reads ------------------------------------------------------------
        @self._add("get_overview", "Every project with its sprints (task id, state, checklist "
                   "progress, next action, open decisions) and whether the coordinator is online.",
                   "read")
        async def get_overview(ctx: Ctx, a: dict):
            return eng._block_projects(ctx.projects)

        @self._add("get_queue", "The sprint queue of one project: tasks with state, dependencies, "
                   "who approved, note. Pass include_done to see COMPLETE/CANCELLED too.", "read",
                   {"project": P, "include_done": {"type": "boolean"}}, ("project",), "project")
        async def get_queue(ctx: Ctx, a: dict):
            ov = eng.svc.control.overview(ctx.projects, hours=24 * 60)
            rows = []
            for t in ov.get("tasks") or []:
                if t.get("project_id") != a["project"]:
                    continue
                if not a.get("include_done") and t.get("state") in _TERMINAL:
                    continue
                d = t.get("data") or {}
                tv = t.get("task_view") or {}
                rows.append({"task": t["task_id"], "state": t.get("state"),
                             "deps": list(d.get("deps") or [])[:8], "approved_by": d.get("approved_by"),
                             "phase": d.get("phase"), "attempt": d.get("attempt"),
                             "next": _s((tv.get("checklist") or {}).get("next_action"), 160),
                             "blocker": _s(d.get("blocker") or d.get("blocked_reason"), 160)})
            return {"project": a["project"], "paused": bool((ov.get("project_status") or {})
                                                            .get(a["project"], {}).get("paused")),
                    "tasks": rows[:60]}

        @self._add("list_pending", "What waits on the operator: open decisions (with ids, question, "
                   "options, recommended default), open Kilo questions, open alerts.", "read")
        async def list_pending(ctx: Ctx, a: dict):
            return eng._block_pending(ctx.projects)

        @self._add("get_usage", "Live subscription usage per backend (5-hour and weekly percent "
                   "used), the cutoff in force and the headline Claude/GPT cutoffs.", "read")
        async def get_usage(ctx: Ctx, a: dict):
            eng._usage_cache = (0.0, None)
            return await eng._block_usage()

        @self._add("get_stack", "The workstation stack as the Foundry Client reports it: each "
                   "component's state and detail, pending restarts, config problems.", "read")
        async def get_stack(ctx: Ctx, a: dict):
            return eng._block_stack()

        @self._add("services_status", "The service registry: each service (Rojo, Kilo, the "
                   "Studio / Blender / audio / image MCP servers, the local model) with status "
                   "up|down|unknown, who provides it (client component, backend, external), which "
                   "sprint needs it, the MCP probes, and the sprints waiting for a service.",
                   "read")
        async def services_status(ctx: Ctx, a: dict):
            return eng.services_view(ctx.projects, full=True)

        @self._add("client_install_status", "Install state of the Foundry Client: this router's "
                   "version, whether installers for that version can be downloaded (GitHub "
                   "release state and links), and every connected client with its version, "
                   "heartbeat freshness and router-version match.", "read")
        async def client_install_status(ctx: Ctx, a: dict):
            from . import __version__
            from .client_guide import install_status
            return await install_status(eng.svc, __version__)

        @self._add("read_install_guide", "The Foundry Client install guide, from "
                   "docs/CLIENT_MANAGER.md. Without section: the list of sections. With "
                   "section (its id, e.g. install, first-run, router-key-for-the-relay, "
                   "troubleshooting): that section's text.", "read",
                   {"section": {"type": "string", "max": 60}})
        async def read_install_guide(ctx: Ctx, a: dict):
            from .client_guide import load_guide
            g = load_guide()
            if g["missing"]:
                return {"error": "docs/CLIENT_MANAGER.md was not found next to this checkout"}
            if not a.get("section"):
                return {"sections": [{"id": s["id"], "title": s["title"]} for s in g["sections"]],
                        "hint": "pass section=<id> to read one"}
            want = str(a["section"]).strip().lower().replace(" ", "-")
            for s in g["sections"]:
                if s["id"] == want or s["id"].startswith(want):
                    return {"section": s["id"], "title": s["title"],
                            "text": _s(s["text"], RESULT_CHARS - 200)}
            return {"error": f"no section {a['section']!r}",
                    "sections": [s["id"] for s in g["sections"]]}

        @self._add("service_restart", "Restart a service that a Foundry Client component runs "
                   "(kilo, rojo, blender and the audio servers when managed). External services "
                   "cannot be restarted here. force=true skips the safe-point wait.", "restart",
                   {"name": {"type": "string", "max": 80}, "force": {"type": "boolean"}}, ("name",),
                   summary=lambda a: f"restart service {a.get('name')} "
                   f"({'now' if a.get('force') else 'at a safe point'})")
        async def service_restart(ctx: Ctx, a: dict):
            comp, _row = eng._service_component(a["name"])
            return await eng._client_action(ctx, "client_restart",
                                            {"component": comp, "force": bool(a.get("force"))})

        @self._add("service_enable", "Switch a managed service component on or off in the Foundry "
                   "Client's settings (rojo, blender, acestep-music, stable-audio, kokoro-tts, "
                   "chatterbox-tts); on starts it, off stops it.", "restart",
                   {"name": {"type": "string", "max": 80}, "on": {"type": "boolean"}},
                   ("name", "on"),
                   summary=lambda a: f"{'enable' if a.get('on') else 'disable'} service "
                   f"{a.get('name')}")
        async def service_enable(ctx: Ctx, a: dict):
            comp, _row = eng._service_component(a["name"])
            return await eng._client_action(ctx, "client_service_enable",
                                            {"component": comp, "enabled": bool(a["on"])})

        @self._add("get_actions", "Recent operator actions (queue, decisions, stack) with status, "
                   "so you can tell what the coordinator or client did with them.", "read",
                   {"project": P, "limit": {"type": "integer"}})
        async def get_actions(ctx: Ctx, a: dict):
            rows = eng.svc.control.actions(ctx.projects, a.get("project") or "", "", "",
                                           min(int(a.get("limit") or 15), 50))
            return [{k: r.get(k) for k in ("action_id", "kind", "project_id", "task_id", "summary",
                                            "status", "status_detail", "created_ts")} for r in rows]

        # ---- settings ----------------------------------------------------------
        @self._add("search_settings", "Find settings by words in their path or description. "
                   "Returns path, type, default, description, whether a change needs a restart.",
                   "read", {"text": {"type": "string", "max": 120}}, ("text",))
        async def search_settings(ctx: Ctx, a: dict):
            words = [w for w in re.split(r"\W+", a["text"].lower()) if w]
            hits = []
            for f in SCH.FIELDS:
                if f.scope == "local":
                    continue
                hay = f"{f.key} {f.description} {f.label}".lower()
                score = sum(1 for w in words if w in hay)
                if score:
                    hits.append((score, {"path": f.key, "type": f.type, "default": f.default,
                                         "description": f.description, "apply": f.apply,
                                         "scope": f.scope, "locked": f.locked,
                                         **({"enum": f.enum} if f.enum else {}),
                                         **({"min": f.min} if f.min is not None else {}),
                                         **({"max": f.max} if f.max is not None else {})}))
            hits.sort(key=lambda x: -x[0])
            return [h for _, h in hits[:25]]

        @self._add("get_settings", "Current settings of a project (or '_router' for router-wide "
                   "cutoffs): deployed and draft values. Pass paths (dotted, prefix ok) to read "
                   "only those; without paths you get the tables and the roles.", "read",
                   {"project": P, "paths": {"type": "array", "max_items": 20}}, ("project",),
                   "project")
        async def get_settings(ctx: Ctx, a: dict):
            st = eng.svc.automation
            dep = st.deployed(a["project"])
            dr = st.draft(a["project"], ctx.store_actor)
            ddoc = (dep or {}).get("doc") or {}
            paths = [str(p) for p in (a.get("paths") or []) if isinstance(p, str)]

            def pick(doc):
                flat = D.flatten(doc.get("settings") or {})
                flat.update({f"role_rules.{k}": v for k, v in D.flatten(doc.get("role_rules") or {})
                             .items()})
                if not paths:
                    return {"tables": sorted({k.split(".", 1)[0] for k in flat}),
                            "roles": (doc.get("settings") or {}).get("roles") or {}}
                return {k: v for k, v in flat.items()
                        if any(k == p or k.startswith(p.rstrip("*").rstrip(".") + ".") for p in paths)}
            return {"project": a["project"], "deployed_rev": dep["rev"] if dep else None,
                    "draft_dirty": dr.get("dirty"), "deployed": pick(ddoc), "draft": pick(dr["doc"])}

        @self._add("set_settings", "Change settings in the project's DRAFT (not live until "
                   "deploy_settings): changes = {\"table.key\": value}. Values are validated against "
                   "the schema; local, secret and locked keys are refused.", "draft",
                   {"project": P, "changes": {"type": "object"}}, ("project", "changes"), "project",
                   lambda a: f"draft {a.get('project')}: set " + ", ".join(
                       f"{k}={_compact(v, 60)}" for k, v in (a.get('changes') or {}).items()))
        async def set_settings(ctx: Ctx, a: dict):
            st = eng.svc.automation
            dr = st.draft(a["project"], ctx.store_actor)
            doc = copy.deepcopy(dr["doc"])
            changed = []
            for path, value in (a["changes"] or {}).items():
                f = SCH.find_field(path)
                why = SCH.check_value(f, value) if f else "unknown setting"
                if why:
                    raise ChatError(f"{path}: {why}")
                D._set_path(doc.setdefault("settings", {}), path, value)
                changed.append(f"{path} -> {_compact(value, 60)} (draft)")
            out = st.save_draft(a["project"], doc, dr["etag"], ctx.store_actor)
            v = out["validation"]
            return {"result": {"valid": v["ok"], "errors": v["errors"][:10],
                               "warnings": v.get("warnings", [])[:10],
                               "restart_needed": v.get("restart_needed") or [],
                               "note": "saved to the draft; deploy_settings makes it live"},
                    "changed": changed}

        @self._add("set_role_rules", "Replace one role's rules in the project's DRAFT (edit/write "
                   "scopes, bash, tools). rules follow the role_rules schema (use "
                   "preview_role_rules to see the current ones).", "draft",
                   {"project": P, "role": {"type": "string", "enum": list(RR.RULE_ROLES)},
                    "rules": {"type": "object"}}, ("project", "role", "rules"), "project",
                   lambda a: f"draft {a.get('project')}: role rules for {a.get('role')}")
        async def set_role_rules(ctx: Ctx, a: dict):
            errs, _ = RR.validate({a["role"]: a["rules"]})
            if errs:
                raise ChatError("the role rules do not validate: " + "; ".join(
                    f"{e.get('path')}: {e.get('message')}" for e in errs[:5]))
            st = eng.svc.automation
            dr = st.draft(a["project"], ctx.store_actor)
            doc = copy.deepcopy(dr["doc"])
            doc.setdefault("role_rules", {})[a["role"]] = RR.normalize(a["role"], a["rules"])
            out = st.save_draft(a["project"], doc, dr["etag"], ctx.store_actor)
            return {"result": {"valid": out["validation"]["ok"],
                               "errors": out["validation"]["errors"][:10]},
                    "changed": [f"role_rules.{a['role']} (draft)"]}

        @self._add("preview_role_rules", "The effective rules of a role (draft) and the Kilo "
                   "permission block they produce.", "preview",
                   {"project": P, "role": {"type": "string", "enum": list(RR.RULE_ROLES)}},
                   ("project", "role"), "project")
        async def preview_role_rules(ctx: Ctx, a: dict):
            doc = eng.svc.automation.draft(a["project"])["doc"]
            rules = RR.normalize(a["role"], (doc.get("role_rules") or {}).get(a["role"]))
            return {"rules": rules, "agent_permission": RR.agent_block(a["role"], rules, "")}

        @self._add("test_role_path", "Would this role be allowed to edit/read/write a path or run a "
                   "command under the draft rules?", "preview",
                   {"project": P, "role": {"type": "string", "enum": list(RR.RULE_ROLES)},
                    "op": {"type": "string", "enum": ["read", "edit", "write", "bash"]},
                    "value": {"type": "string", "max": 500}}, ("project", "role", "op", "value"),
                   "project")
        async def test_role_path(ctx: Ctx, a: dict):
            doc = eng.svc.automation.draft(a["project"])["doc"]
            rules = RR.normalize(a["role"], (doc.get("role_rules") or {}).get(a["role"]))
            return RR.test(a["role"], rules, a["op"], a["value"], "")

        @self._add("preview_settings", "What the draft would change against the deployed revision "
                   "(diff), whether it validates and which changes need a coordinator restart.",
                   "preview", {"project": P}, ("project",), "project")
        async def preview_settings(ctx: Ctx, a: dict):
            st = eng.svc.automation
            d = st.diff(a["project"])
            v = st.validate(a["project"])
            return {"changes": d["changes"][:60], "valid": v["ok"], "errors": v["errors"][:10],
                    "restart_needed": v.get("restart_needed") or []}

        @self._add("discard_draft", "Throw the project's draft away (back to the deployed "
                   "revision).", "draft", {"project": P}, ("project",), "project",
                   lambda a: f"discard the draft of {a.get('project')}")
        async def discard_draft(ctx: Ctx, a: dict):
            eng.svc.automation.reset_draft(a["project"], None, ctx.store_actor)
            return {"result": "draft reset", "changed": [f"{a['project']}: draft discarded"]}

        @self._add("get_revisions", "Deployed revisions of a project (number, time, actor, note) "
                   "and its recent audit rows.", "read", {"project": P, "limit": {"type": "integer"}},
                   ("project",), "project")
        async def get_revisions(ctx: Ctx, a: dict):
            st = eng.svc.automation
            n = min(int(a.get("limit") or 10), 50)
            return {"revisions": st.revisions(a["project"], n), "audit": st.audit(a["project"], n)}

        @self._add("deploy_settings", "Deploy the project's draft as a new revision (the coordinator "
                   "pulls it; '_router' deploys cutoffs into the router).", "deploy",
                   {"project": P, "note": {"type": "string", "max": 300}}, ("project",), "project",
                   lambda a: f"deploy the draft of {a.get('project')}"
                   + (f": {a['note']}" if a.get("note") else ""))
        async def deploy_settings(ctx: Ctx, a: dict):
            st = eng.svc.automation
            dr = st.draft(a["project"], ctx.store_actor)
            out = st.deploy(a["project"], dr["etag"], a.get("note") or "operator chat",
                            ctx.store_actor)
            notes = []
            if a["project"] == ROUTER_DOC and not out.get("unchanged"):
                notes = await eng._apply_router_doc(st.deployed(a["project"])["doc"])
            return {"result": {**out, "router_notes": notes},
                    "changed": ([] if out.get("unchanged") else
                                [f"{a['project']}: revision {out['rev']} deployed"
                                 + (f"; restart the coordinator for {', '.join(out['restart_needed'])}"
                                    if out.get("restart_needed") else "")])}

        @self._add("rollback_settings", "Roll a project back to an earlier revision (appends a new "
                   "revision that copies it).", "deploy",
                   {"project": P, "rev": {"type": "integer"}, "note": {"type": "string", "max": 300}},
                   ("project", "rev"), "project",
                   lambda a: f"roll {a.get('project')} back to revision {a.get('rev')}")
        async def rollback_settings(ctx: Ctx, a: dict):
            st = eng.svc.automation
            out = st.rollback(a["project"], int(a["rev"]), a.get("note") or "operator chat",
                              ctx.store_actor)
            notes = []
            if a["project"] == ROUTER_DOC:
                notes = await eng._apply_router_doc(st.deployed(a["project"])["doc"])
            return {"result": {**out, "router_notes": notes},
                    "changed": [f"{a['project']}: rolled back to {a['rev']} as revision {out['rev']}"]}

        # ---- personas / cutoffs ----------------------------------------------
        @self._add("list_personas", "Every persona with its routes (ordered models, which are on), "
                   "backend, routing mode and whether it is enabled.", "read")
        async def list_personas(ctx: Ctx, a: dict):
            out = []
            for p in eng.svc.personas.list():
                allow = _uj(p.get("model_allowlist"), []) or []
                pins = _uj(p.get("pinned_models"), []) or []
                out.append({"name": p["virtual_name"], "enabled": bool(p.get("enabled")),
                            "routing": p.get("routing"), "backend": p.get("backend"),
                            "routes": [{"model": m, "enabled": m in allow} for m in pins]
                            + [{"model": m, "enabled": True} for m in allow if m not in pins],
                            "kind": p.get("kind")})
            return out[:60]

        @self._add("get_persona", "One persona in detail (routes, backend, limits, description).",
                   "read", {"name": {"type": "string", "max": 120}}, ("name",))
        async def get_persona(ctx: Ctx, a: dict):
            p = eng.svc.personas.get(a["name"])
            if not p:
                raise ChatError(f"no persona {a['name']!r}", "not_found", 404)
            keep = ("virtual_name", "description", "enabled", "routing", "backend", "model_allowlist",
                    "pinned_models", "fallback_policy", "max_output_tokens", "context_window",
                    "reasoning_effort", "kind", "execution_mode", "capabilities")
            return {k: (_uj(p.get(k), p.get(k)) if k in ("model_allowlist", "pinned_models",
                                                            "capabilities") else p.get(k))
                    for k in keep}

        @self._add("get_cutoffs", "The usage cutoffs in force (percent used at which a "
                   "subscription backend stops taking optional work): Claude, GPT, per backend.",
                   "read")
        async def get_cutoffs(ctx: Ctx, a: dict):
            from . import cutoffs
            return cutoffs.view(eng.svc)

        @self._add("set_cutoffs", "Change usage cutoffs: claude (all Meridian backends), gpt (the "
                   "CLIProxyAPI default) and/or backends {name: pct|null}. 1-100; null clears an "
                   "override. Written to config.yaml and live at once.", "cutoffs",
                   {"claude": {"type": "number"}, "gpt": {"type": "number"},
                    "backends": {"type": "object"}}, (),
                   summary=lambda a: "cutoffs: " + ", ".join(
                       f"{k}={v}" for k, v in a.items() if k in ("claude", "gpt"))
                   + ("; " + ", ".join(f"{k}={v}" for k, v in (a.get("backends") or {}).items())
                      if a.get("backends") else ""))
        async def set_cutoffs(ctx: Ctx, a: dict):
            from . import cutoffs
            body = {k: v for k, v in a.items() if k in ("claude", "gpt", "backends")}
            if not body:
                raise ChatError("give claude, gpt and/or backends")
            try:
                out = cutoffs.apply(eng.svc, body)
            except cutoffs.CutoffError as e:
                raise ChatError(str(e)) from None
            eng._usage_cache = (0.0, None)
            eng.svc.db.log_event("info", "chat", f"cutoffs changed by {ctx.actor} via operator chat: "
                                 + _compact(body, 200))
            return {"result": {"claude": out["claude"], "gpt": out["gpt"]},
                    "changed": [f"cutoffs: {_compact(body, 120)}"]}

        # ---- sprints / queue (through the coordinator's typed actions) --------
        @self._add("plan_sprint_steps", "Ask the coordinator (which reads every sprint plan) for "
                   "checked queue steps that do what `request` says, e.g. 'queue T3 after T2' or "
                   "'start from the next sprint that is not done'. Returns a plan_id, the steps and "
                   "warnings; nothing runs. Takes up to a minute.", "draft",
                   {"project": P, "request": {"type": "string", "max": 2000}},
                   ("project", "request"), "project",
                   lambda a: f"plan sprint steps for {a.get('project')}")
        async def plan_sprint_steps(ctx: Ctx, a: dict):
            act = eng.svc.control.create_action(ctx.actor, ctx.projects, {
                "kind": "assistant_plan", "project_id": a["project"], "text": a["request"],
                "idempotency_key": f"chat:{ctx.thread_id}:{ctx.turn_id}:{ctx.call_id}"},
                scopes=ctx.scopes)
            done = await eng._wait_action(act["action_id"], 90)
            if done.get("status") == "applied":
                plan = done.get("result") or {}
                return {"plan_id": plan.get("plan_id"), "summary": plan.get("summary"),
                        "steps": plan.get("steps"), "warnings": plan.get("warnings"),
                        "questions": plan.get("questions"), "action_id": act["action_id"]}
            return {"action_id": act["action_id"], "status": done.get("status"),
                    "detail": done.get("status_detail"),
                    "note": "the coordinator has not answered yet (is it running with --control?); "
                            "check get_actions later"}

        @self._add("run_sprint_steps", "Run the steps of a plan from plan_sprint_steps (all, or the "
                   "given step indexes). The coordinator re-checks each step against the live queue.",
                   "sprint", {"project": P, "plan_id": {"type": "string", "max": 40},
                              "steps": {"type": "array", "max_items": 20}},
                   ("project", "plan_id"), "project",
                   lambda a: f"run plan {a.get('plan_id')} on {a.get('project')}"
                   + (f" (steps {a['steps']})" if a.get("steps") is not None else ""))
        async def run_sprint_steps(ctx: Ctx, a: dict):
            body = {"kind": "assistant_run", "project_id": a["project"], "plan_id": a["plan_id"],
                    "idempotency_key": f"chat:{ctx.thread_id}:{ctx.turn_id}:{ctx.call_id}:"
                                       f"{uuid.uuid4().hex[:6]}"}
            if a.get("steps") is not None:
                body["steps"] = [int(i) for i in a["steps"]]
            act = eng.svc.control.create_action(ctx.actor, ctx.projects, body, scopes=ctx.scopes)
            done = await eng._wait_action(act["action_id"], 45)
            res = done.get("result") or {}
            return {"result": {"action_id": act["action_id"], "status": done.get("status"),
                               "detail": done.get("status_detail"), "results": res.get("results"),
                               "warnings": res.get("warnings")},
                    "changed": [f"{a['project']}: {r.get('kind')} {r.get('task_id') or ''} "
                                f"{r.get('result')}" for r in (res.get("results") or [])]
                    or [f"{a['project']}: plan {a['plan_id']} sent to the coordinator "
                        f"({done.get('status')})"]}

        @self._add("task_action", "Pause, resume, cancel or unblock one sprint. note = the reason "
                   "(pause/cancel) or what was fixed (unblock). reset_attempts: fresh attempt budget "
                   "on unblock.", "sprint",
                   {"project": P, "task_id": {"type": "string", "max": 120},
                    "kind": {"type": "string", "enum": ["pause", "resume", "cancel", "unblock"]},
                    "note": {"type": "string", "max": 500}, "reset_attempts": {"type": "boolean"}},
                   ("project", "task_id", "kind"), "project",
                   lambda a: f"{a.get('kind')} {a.get('task_id')} ({a.get('project')})"
                   + (f": {a['note']}" if a.get("note") else ""))
        async def task_action(ctx: Ctx, a: dict):
            cc = eng.svc.control
            task = eng.db.query_one("SELECT state FROM cc_tasks WHERE project_id=? AND task_id=?",
                                    (a["project"], a["task_id"]))
            if not task:
                raise ChatError(f"{a['project']}/{a['task_id']} has not been reported by the "
                                "coordinator", "unknown_task", 404)
            note = (a.get("note") or "").strip() or "operator chat"
            body = {"kind": a["kind"], "project_id": a["project"], "task_id": a["task_id"],
                    "idempotency_key": f"chat:{ctx.thread_id}:{ctx.turn_id}:{ctx.call_id}",
                    "expected": {"task_state": task["state"]},
                    "chat": {"thread": ctx.thread_id, "turn": ctx.turn_id, "call": ctx.call_id}}
            if a["kind"] in ("pause", "cancel"):
                body["reason"] = note
            if a["kind"] == "cancel":
                body["confirm"] = f"cancel {a['task_id']}"
            if a["kind"] == "unblock":
                body["resolution"] = note
                body["reset_attempts"] = bool(a.get("reset_attempts"))
            act = cc.create_action(ctx.actor, ctx.projects, body, scopes=ctx.scopes)
            return {"result": {"action_id": act["action_id"], "status": act["status"],
                               "detail": act["status_detail"]},
                    "changed": [f"{a['project']}/{a['task_id']}: {a['kind']} requested "
                                f"(action {act['action_id']})"]}

        @self._add("project_pause", "Pause a whole project: mode safe (finish the current step, "
                   "start nothing new) or now (abort running sessions). A note is required.",
                   "sprint", {"project": P, "mode": {"type": "string", "enum": ["safe", "now"]},
                              "note": {"type": "string", "max": 500},
                              "resume_after_upgrade": {"type": "boolean"}}, ("project", "note"),
                   "project", lambda a: f"pause project {a.get('project')} "
                   f"({a.get('mode') or 'safe'}): {a.get('note')}")
        async def project_pause(ctx: Ctx, a: dict):
            act = eng.svc.control.create_action(ctx.actor, ctx.projects, {
                "kind": "project_pause", "project_id": a["project"], "mode": a.get("mode") or "safe",
                "note": a["note"], "resume_after_upgrade": bool(a.get("resume_after_upgrade")),
                "idempotency_key": f"chat:{ctx.thread_id}:{ctx.turn_id}:{ctx.call_id}"},
                scopes=ctx.scopes)
            return {"result": {"action_id": act["action_id"], "status": act["status"]},
                    "changed": [f"project {a['project']}: pause requested (action {act['action_id']})"]}

        @self._add("project_resume", "Resume a paused project.", "sprint",
                   {"project": P, "note": {"type": "string", "max": 500}}, ("project",), "project",
                   lambda a: f"resume project {a.get('project')}")
        async def project_resume(ctx: Ctx, a: dict):
            act = eng.svc.control.create_action(ctx.actor, ctx.projects, {
                "kind": "project_resume", "project_id": a["project"], "note": a.get("note") or "",
                "idempotency_key": f"chat:{ctx.thread_id}:{ctx.turn_id}:{ctx.call_id}"},
                scopes=ctx.scopes)
            return {"result": {"action_id": act["action_id"], "status": act["status"]},
                    "changed": [f"project {a['project']}: resume requested (action {act['action_id']})"]}

        # ---- decisions ---------------------------------------------------------
        @self._add("answer_decision", "Answer an open coordinator decision with operator authority: "
                   "option (one of its options) and/or a free-text answer, plus an optional note.",
                   "decision", {"project": P, "task_id": {"type": "string", "max": 120},
                                "decision_id": {"type": "string", "max": 120},
                                "option": {"type": "string", "max": 300},
                                "answer": {"type": "string", "max": 4000},
                                "note": {"type": "string", "max": 2000}},
                   ("project", "task_id", "decision_id"), "project",
                   lambda a: f"answer decision {a.get('decision_id')} on {a.get('task_id')}: "
                   f"{a.get('option') or a.get('answer') or ''}")
        async def answer_decision(ctx: Ctx, a: dict):
            d = eng._find_decision(a["project"], a["task_id"], a["decision_id"])
            act = eng.svc.control.create_action(ctx.actor, ctx.projects, {
                "kind": "decision_answer", "project_id": a["project"], "task_id": a["task_id"],
                "decision_id": a["decision_id"], "option": a.get("option") or "",
                "answer": a.get("answer") or a.get("option") or "", "note": a.get("note") or "",
                "question_hash": _hash(d.get("question") or ""), "authority": "operator",
                "idempotency_key": f"chat:{ctx.thread_id}:{ctx.turn_id}:{ctx.call_id}",
                "chat": {"thread": ctx.thread_id, "turn": ctx.turn_id, "call": ctx.call_id}},
                scopes=ctx.scopes)
            return {"result": {"action_id": act["action_id"], "status": act["status"]},
                    "changed": [f"decision {a['decision_id']} ({a['task_id']}): answer "
                                f"'{a.get('option') or a.get('answer')}' sent (action {act['action_id']})"]}

        @self._add("answer_question", "Answer an open Kilo question (request_id from list_pending) "
                   "with one answer per question asked.", "decision",
                   {"project": P, "task_id": {"type": "string", "max": 120},
                    "request_id": {"type": "string", "max": 160},
                    "answers": {"type": "array", "max_items": 10}},
                   ("project", "task_id", "request_id", "answers"), "project",
                   lambda a: f"answer question {a.get('request_id')} on {a.get('task_id')}")
        async def answer_question(ctx: Ctx, a: dict):
            ans = [[str(x)] if not isinstance(x, list) else [str(y) for y in x] for x in a["answers"]]
            act = eng.svc.control.create_action(ctx.actor, ctx.projects, {
                "kind": "answer_question", "project_id": a["project"], "task_id": a["task_id"],
                "request_id": a["request_id"], "answers": ans,
                "idempotency_key": f"chat:{ctx.thread_id}:{ctx.turn_id}:{ctx.call_id}"},
                scopes=ctx.scopes)
            return {"result": {"action_id": act["action_id"], "status": act["status"]},
                    "changed": [f"question {a['request_id']}: answered (action {act['action_id']})"]}

        # ---- stack (Foundry Client) --------------------------------------------
        def stack_tool(name, component, desc):
            @self._add(name, desc + " force=true restarts at once instead of waiting for a safe "
                       "point (no coding session working).", "restart",
                       {"force": {"type": "boolean"}}, (),
                       summary=lambda a: f"restart {component} "
                       f"({'now' if a.get('force') else 'at a safe point'})")
            async def _restart(ctx: Ctx, a: dict):
                return await eng._client_action(ctx, "client_restart",
                                                {"component": component,
                                                 "force": bool(a.get("force"))})
        stack_tool("restart_kilo", "kilo", "Restart the Kilo server through the Foundry Client.")
        stack_tool("restart_coordinator", "coordinator",
                   "Restart the coordinator through the Foundry Client.")
        stack_tool("restart_rojo", "rojo", "Restart Rojo through the Foundry Client (Studio's "
                   "plugin must reconnect afterwards).")

        @self._add("reload_kilo_config", "Make the Kilo server re-read kilo.jsonc (after role-rule "
                   "deploys); the client disposes Kilo's instance or restarts it at a safe point.",
                   "restart", {}, (), summary=lambda a: "reload kilo.jsonc into the Kilo server")
        async def reload_kilo_config(ctx: Ctx, a: dict):
            return await eng._client_action(ctx, "client_reload_kilo_config", {})

        # ---- parallel tracks (Team Lead; typed actions the coordinator claims) ----
        @self._add("analyze_tracks", "Read every sprint plan of a project and return the "
                   "deterministic track analysis: ordered tracks (chains of sprints with their "
                   "needs: studio, blender, audio, images, gpu, backend), the serial pairs with "
                   "the reason (dependency or shared file), warnings, and the current "
                   "max_agents. No model call; takes a few seconds.", "read",
                   {"project": P}, ("project",), "project")
        async def analyze_tracks(ctx: Ctx, a: dict):
            return await eng._track_action(ctx, "tracks_analyze", a["project"], {}, 60)

        @self._add("propose_tracks", "A track plan for an agent count: the main track plus up "
                   "to max_agents-1 parallel tracks (never two on one blender/gpu), with notes. "
                   "Returns plan_id, tracks[{name, sprints, needs}], agents. Nothing starts: "
                   "track_start(plan_id) does, after the operator's card.", "draft",
                   {"project": P, "max_agents": {"type": "integer"}}, ("project",), "project",
                   lambda a: f"propose tracks for {a.get('project')}"
                   + (f" ({a['max_agents']} agents)" if a.get("max_agents") else ""))
        async def propose_tracks(ctx: Ctx, a: dict):
            body = {}
            if a.get("max_agents") is not None:
                body["max_agents"] = int(a["max_agents"])
            return await eng._track_action(ctx, "tracks_propose", a["project"], body, 60)

        @self._add("track_status", "The tracks of a project as the coordinator last reported "
                   "them: each with state, sprints and their states, worktree, needs, merge "
                   "stage; max_agents and running agents; the Studio lease and timeline.",
                   "read", {"project": P}, ("project",), "project")
        async def track_status(ctx: Ctx, a: dict):
            v = eng.svc.control.tracks_view(ctx.projects, a["project"])
            st = (v.get("tracks") or {}).get(a["project"])
            if not st:
                return {"project": a["project"], "tracks": [], "note": "the coordinator has not "
                        "reported tracks yet (is it running with --control on a build with "
                        "tracks?)", "source": v.get("source")}
            studio = dict(st.get("studio") or {})
            studio["journal"] = (studio.get("journal") or [])[-12:]
            return {"project": a["project"], "max_agents": st.get("max_agents"),
                    "agents_running": st.get("agents_running"), "main": st.get("main"),
                    "tracks": [{k: t.get(k) for k in ("name", "state", "current_sprint",
                                                      "sprints", "needs", "branch", "worktree",
                                                      "merge", "lease")}
                               for t in st.get("tracks") or []][:20],
                    "studio": {k: studio.get(k) for k in ("in_use", "pending", "lease",
                                                          "last_main_call_age_s", "journal")},
                    "reported_s_ago": v.get("age_s")}

        @self._add("track_start", "Start parallel tracks: plan_id from propose_tracks (all its "
                   "tracks, or `names`), or one track by name with its sprints (and needs). "
                   "Creates the git worktree and branch, queues the sprints on it.", "tracks",
                   {"project": P, "plan_id": {"type": "string", "max": 40},
                    "names": {"type": "array", "max_items": 8},
                    "name": {"type": "string", "max": 40},
                    "sprints": {"type": "array", "max_items": 20},
                    "needs": {"type": "array", "max_items": 8}}, ("project",), "project",
                   lambda a: (f"start the tracks of {a['plan_id']} on {a.get('project')}"
                              + (f" ({', '.join(map(str, a['names']))})" if a.get("names") else "")
                              if a.get("plan_id") else
                              f"start track {a.get('name')} on {a.get('project')}: "
                              + " -> ".join(map(str, a.get("sprints") or []))))
        async def track_start(ctx: Ctx, a: dict):
            body = {k: a[k] for k in ("plan_id", "names", "name", "sprints", "needs") if a.get(k)}
            if not body.get("plan_id") and not (body.get("name") and body.get("sprints")):
                raise ChatError("give plan_id (from propose_tracks) or name + sprints")
            return await eng._track_action(ctx, "track_start", a["project"], body, 60,
                                           changed=True)

        @self._add("track_stop", "Stop a track: its unfinished sprints are cancelled (sessions "
                   "aborted); the worktree and branch are kept for inspection.", "tracks",
                   {"project": P, "name": {"type": "string", "max": 40},
                    "reason": {"type": "string", "max": 500}}, ("project", "name"), "project",
                   lambda a: f"stop track {a.get('name')} on {a.get('project')}"
                   + (f": {a['reason']}" if a.get("reason") else ""))
        async def track_stop(ctx: Ctx, a: dict):
            return await eng._track_action(ctx, "track_stop", a["project"],
                                           {"name": a["name"], "reason": a.get("reason") or ""},
                                           30, changed=True)

        @self._add("track_merge", "Merge a finished track into master at the main sprint's next "
                   "quiet point (rebase, fast-forward, the gate on the merged files in the main "
                   "checkout). With `option`, answer the track's open conflict decision: "
                   "'retry the merge after the Lead repair' (the default while a Lead repair of "
                   "the conflict runs), 'stop this track' (the default otherwise), 'retry the "
                   "merge', 'keep the branch, no merge'.",
                   "tracks", {"project": P, "name": {"type": "string", "max": 40},
                              "option": {"type": "string", "max": 60}}, ("project", "name"),
                   "project", lambda a: (f"track {a.get('name')}: {a['option']}" if a.get("option")
                                         else f"merge track {a.get('name')} on {a.get('project')}"))
        async def track_merge(ctx: Ctx, a: dict):
            body = {"name": a["name"]}
            if a.get("option"):
                body["option"] = a["option"]
            return await eng._track_action(ctx, "track_merge", a["project"], body, 30,
                                           changed=True)

        @self._add("set_max_agents", "Change the live cap on sprints running at once across the "
                   "main checkout and the tracks (1 = no parallel tracks; the main sprint always "
                   "counts first). Applies at once; survives a coordinator restart.", "tracks",
                   {"project": P, "max_agents": {"type": "integer"}}, ("project", "max_agents"),
                   "project", lambda a: f"set max_agents to {a.get('max_agents')} "
                   f"({a.get('project')})")
        async def set_max_agents(ctx: Ctx, a: dict):
            n = a["max_agents"]
            if isinstance(n, bool) or not isinstance(n, int) or not 1 <= n <= 16:
                raise ChatError("max_agents must be a whole number from 1 to 16")
            return await eng._track_action(ctx, "set_max_agents", a["project"],
                                           {"max_agents": n}, 30, changed=True)
        # ---- project memory (docs/memory, Team Lead piece 4) --------------------
        MF = {"type": "string", "max": 120, "desc": "DECISIONS.md | CONVENTIONS.md | KNOWN_ISSUES.md "
                                                   "| ASSETS.md | sprints/<ID>.md | tracks/<name>.md"}

        @self._add("memory_search", "Search the project's memory (docs/memory: decisions, "
                   "conventions, known issues, the asset registry, per-sprint and per-track notes) "
                   "as the coordinator indexed it. Returns file, heading and text of the best "
                   "matches; file narrows to one file.", "read",
                   {"project": P, "query": {"type": "string", "max": 200}, "file": MF},
                   ("project", "query"), "project")
        async def memory_search(ctx: Ctx, a: dict):
            from .memory_view import search
            snap = eng._memory_snapshot(a["project"])
            if not snap:
                raise ChatError(f"the coordinator has not reported memory for {a['project']} "
                                "(docs/memory missing, [automation] project_memory off, or the "
                                "coordinator is offline)", "no_memory", 404)
            ents = (snap.get("data") or {}).get("entries") or []
            hits = search(ents, a["query"], 10, a.get("file") or "")
            return {"project": a["project"], "entries_indexed": len(ents),
                    "results": [{**h, "text": _s(h["text"], 500)} for h in hits]}

        @self._add("memory_summary", "The project's memory at a glance: files with entry counts, "
                   "the latest decisions, the memory slice each running session got, recent notes "
                   "sessions recorded.", "read", {"project": P}, ("project",), "project")
        async def memory_summary(ctx: Ctx, a: dict):
            from .memory_view import summary
            snap = eng._memory_snapshot(a["project"])
            out = summary(snap, decisions=8)
            if snap:
                d = snap.get("data") or {}
                out["notes"] = [{k: n.get(k) for k in ("task_id", "file", "heading", "status",
                                                       "detail")} for n in (d.get("notes") or [])[:10]]
                out["slices"] = [{"session": (s.get("session_id") or "")[:12], "task": s.get("task_id"),
                                  "role": s.get("role"), "track": s.get("track"), "live": s.get("live"),
                                  "entries": len(s.get("entries") or []), "tokens": s.get("tokens"),
                                  "budget": s.get("budget"), "prompts": s.get("prompts")}
                                 for s in (d.get("slices") or [])[:10]]
            return out

        @self._add("memory_add", "Append a fact under a heading of a memory file (a new heading is "
                   "created at the end of the file; use file=sprints/<ID>.md for one sprint, "
                   "tracks/<name>.md for a track, ASSETS.md heading Registry with a table row for "
                   "an asset). The coordinator validates it (no secrets, size) and commits it to the "
                   "game repo.", "memory",
                   {"project": P, "file": MF, "heading": {"type": "string", "max": 120},
                    "text": {"type": "string", "max": 4000}, "task_id": {"type": "string", "max": 120}},
                   ("project", "file", "heading", "text"), "project",
                   lambda a: f"memory: add to {a.get('file')} > {a.get('heading')}: "
                   f"{_compact(a.get('text'), 100)}")
        async def memory_add(ctx: Ctx, a: dict):
            return await eng._memory_action(ctx, "memory_add", a)

        @self._add("memory_edit", "Replace the whole body under one heading of a memory file with "
                   "new text (memory_search first to see the current body). The coordinator "
                   "validates and commits it.", "memory",
                   {"project": P, "file": MF, "heading": {"type": "string", "max": 120},
                    "text": {"type": "string", "max": 4000}, "task_id": {"type": "string", "max": 120}},
                   ("project", "file", "heading", "text"), "project",
                   lambda a: f"memory: replace {a.get('file')} > {a.get('heading')} with "
                   f"{_compact(a.get('text'), 100)}")
        async def memory_edit(ctx: Ctx, a: dict):
            return await eng._memory_action(ctx, "memory_edit", a)

        # ---- cross-project playbook (A1) ----------------------------------------
        KF = {"type": "string", "max": 120,
              "desc": "templates/<name>.md | howto/<name>.md | fixes/<name>.md | "
                      "scripts/<name>.md | formats/<name>.md"}

        @self._add("kb_search", "Search the cross-project playbook (templates, how-tos, "
                   "fixes: error signature -> verified fix, scripts, formats) as the "
                   "coordinator indexed it. The playbook is shared by every project.", "read",
                   {"query": {"type": "string", "max": 200}, "file": KF}, ("query",))
        async def kb_search(ctx: Ctx, a: dict):
            from .memory_view import search
            snap = eng._memory_snapshot("@playbook")
            if not snap:
                raise ChatError("the coordinator has not reported a playbook (no --playbook "
                                "path configured, or the coordinator is offline)",
                                "no_playbook", 404)
            d = snap.get("data") or {}
            ents = d.get("entries") or []
            hits = search(ents, a["query"], 10, a.get("file") or "")
            return {"entries_indexed": len(ents),
                    "results": [{**h, "text": _s(h["text"], 500)} for h in hits],
                    "candidates_pending": sum(1 for cd in (d.get("candidates") or [])
                                              if cd.get("status") == "proposed")}

        @self._add("kb_add", "Append an entry under a heading of a playbook file (cross-project; "
                   "a new heading is created at the end of the file). Use fixes/<name>.md for "
                   "an error-signature -> fix entry. The coordinator validates (no secrets, "
                   "size) and commits it to the playbook repo.", "memory",
                   {"project": P, "file": KF, "heading": {"type": "string", "max": 120},
                    "text": {"type": "string", "max": 4000}},
                   ("project", "file", "heading", "text"), "project",
                   lambda a: f"playbook: add to {a.get('file')} > {a.get('heading')}: "
                   f"{_compact(a.get('text'), 100)}")
        async def kb_add(ctx: Ctx, a: dict):
            return await eng._memory_action(ctx, "playbook_add", a)

        @self._add("kb_edit", "Replace the whole body under one heading of a playbook file "
                   "(kb_search first to see the current body). The coordinator validates and "
                   "commits it.", "memory",
                   {"project": P, "file": KF, "heading": {"type": "string", "max": 120},
                    "text": {"type": "string", "max": 4000}},
                   ("project", "file", "heading", "text"), "project",
                   lambda a: f"playbook: replace {a.get('file')} > {a.get('heading')} with "
                   f"{_compact(a.get('text'), 100)}")
        async def kb_edit(ctx: Ctx, a: dict):
            return await eng._memory_action(ctx, "playbook_edit", a)

        # ---- personas, backends, the whole router (chat_router_tools.py) ----
        from . import chat_router_tools
        chat_router_tools.register(self)

        @self._add("ask_operator", "Ask the operator one question and end the turn; options are "
                   "shown as buttons.", "ask", {"text": {"type": "string", "max": 1000},
                                                "options": {"type": "array", "max_items": 8}},
                   ("text",))
        async def ask_operator(ctx: Ctx, a: dict):          # handled in the loop, never run
            return {"result": "asked"}

    # ------------------------------------------------------------------ executors' helpers

    def _find_decision(self, project: str, task_id: str, decision_id: str) -> dict:
        t = self.db.query_one("SELECT data FROM cc_tasks WHERE project_id=? AND task_id=?",
                              (project, task_id))
        decs = (_uj((t or {}).get("data"), {}) or {}).get("decisions") or []
        d = next((x for x in decs if isinstance(x, dict) and x.get("decision_id") == decision_id), None)
        if not d:
            raise ChatError(f"no decision {decision_id!r} on {task_id}", "unknown_decision", 404)
        if d.get("status") != "open":
            raise ChatError(f"decision {decision_id} is {d.get('status')}, not open", "stale", 409)
        return d

    async def _track_action(self, ctx: Ctx, kind: str, project: str, payload: dict,
                            wait_s: float, changed: bool = False) -> dict:
        """File a track action for the coordinator and wait briefly for its answer
        (the coordinator claims it on its next tick; reads come back with the
        result, changes with the status)."""
        act = self.svc.control.create_action(ctx.actor, ctx.projects, {
            "kind": kind, "project_id": project, **payload,
            "idempotency_key": f"chat:{ctx.thread_id}:{ctx.turn_id}:{ctx.call_id}",
            "chat": {"thread": ctx.thread_id, "turn": ctx.turn_id, "call": ctx.call_id}},
            scopes=ctx.scopes)
        done = await self._wait_action(act["action_id"], wait_s)
        status = done.get("status")
        res = done.get("result") or {}
        if status == "applied" and not changed:
            return {**res, "action_id": act["action_id"]}
        if status in ("rejected", "failed"):
            raise ChatError(f"the coordinator {status} it: {done.get('status_detail')}",
                            "coordinator_" + status, 409)
        note = ("" if status == "applied" else
                "the coordinator has not answered yet (is it running with --control on a build "
                "with tracks?); check get_actions or track_status later")
        out = {"action_id": act["action_id"], "status": status,
               "detail": done.get("status_detail"), **({"result": res} if res else {}),
               **({"note": note} if note else {})}
        if changed:
            return {"result": out, "changed": [f"{project}: {act['summary']} ({status})"]}
        return out
    async def _memory_action(self, ctx: Ctx, kind: str, a: dict) -> dict:
        body = {"kind": kind, "project_id": a["project"], "file": a["file"], "heading": a["heading"],
                "text": a["text"], "idempotency_key": f"chat:{ctx.thread_id}:{ctx.turn_id}:{ctx.call_id}",
                "chat": {"thread": ctx.thread_id, "turn": ctx.turn_id, "call": ctx.call_id}}
        if a.get("task_id"):
            body["task_id"] = a["task_id"]
        act = self.svc.control.create_action(ctx.actor, ctx.projects, body, scopes=ctx.scopes)
        done = await self._wait_action(act["action_id"], 30)
        res = done.get("result") or {}
        status = done.get("status")
        return {"result": {"action_id": act["action_id"], "status": status,
                           "detail": done.get("status_detail"), "sha": res.get("sha"),
                           "path": res.get("path"),
                           "note": "" if status in ("applied", "rejected", "failed") else
                           "the coordinator has not applied it yet (is it running with --control?); "
                           "check get_actions later"},
                "changed": [f"{a['project']}: {act['summary']}: {status}"
                            + (f" ({res['sha'][:12]})" if res.get("sha") else "")
                            + (f" ({done.get('status_detail')})" if status == "rejected" else "")]}

    async def _client_action(self, ctx: Ctx, kind: str, payload: dict) -> dict:
        if not self.svc.control.client_sources():
            raise ChatError("no Foundry Client is attached to this router; on the workstation run "
                            "`foundry-client restart kilo --wait` (or start the client with a router "
                            "key)", "no_client", 409)
        act = self.svc.control.create_action(ctx.actor, ctx.projects, {
            "kind": kind, **payload,
            "idempotency_key": f"chat:{ctx.thread_id}:{ctx.turn_id}:{ctx.call_id}",
            "chat": {"thread": ctx.thread_id, "turn": ctx.turn_id, "call": ctx.call_id}},
            scopes=ctx.scopes)
        done = await self._wait_action(act["action_id"], 20)
        return {"result": {"action_id": act["action_id"], "status": done.get("status"),
                           "detail": done.get("status_detail"), "result": done.get("result")},
                "changed": [f"{act['summary']}: {done.get('status')}"
                            + (f" ({done.get('status_detail')})" if done.get("status_detail") else "")
                            + f" (action {act['action_id']})"]}

    async def _wait_action(self, action_id: str, seconds: float) -> dict:
        """Poll a cc_action until it is final or `seconds` pass (the coordinator
        or client claims and reports asynchronously)."""
        t0 = self.clock()
        while True:
            a = self.svc.control.action_view(action_id, with_log=False) or {}
            if a.get("status") in ("applied", "rejected", "failed", "expired", "cancelled"):
                return a
            if self.clock() - t0 >= seconds:
                return a
            await asyncio.sleep(min(1.5, max(0.05, seconds / 20)))

    async def _apply_router_doc(self, doc: dict) -> list[str]:
        """The _router document's per-backend cutoffs -> usage_limit_pct in
        config.yaml, then rebuild the pool (same as automation_api)."""
        want = ((doc or {}).get("settings") or {}).get("backends") or {}
        notes: list[str] = []

        def mutate(raw: dict) -> None:
            backends = (((raw.get("backend_pool") or {}).get("internal") or {}).get("backends")
                        or [])
            names = {b.get("name") for b in backends if isinstance(b, dict)}
            for name in want:
                if name not in names:
                    notes.append(f"backend {name!r} is not configured; its cutoff waits")
            for b in backends:
                if not isinstance(b, dict) or b.get("name") not in want:
                    continue
                pct = (want[b["name"]] or {}).get("cutoff_pct")
                if pct is None:
                    b.pop("usage_limit_pct", None)
                else:
                    b["usage_limit_pct"] = float(pct)
        self.svc.config_store.save(mutate)
        try:
            await self.svc.rebuild_pool()
        except Exception as e:                                   # noqa: BLE001
            notes.append(f"pool rebuild failed: {type(e).__name__}: {e}")
        self._usage_cache = (0.0, None)
        return notes
