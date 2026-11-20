"""The coordinator protocol suite (v0.181.217): does a model end a sprint turn
the way the coordinator reads it?

Every Kilo session the coordinator runs must end its final message with a
fenced ```coordinator-report``` JSON block - status DONE | NOT DONE | BLOCKED,
a summary, the next step, and for BLOCKED one blocker_kind (product with 2-4
options and a recommendation, tooling with none, user_action with the exact
steps) - and ask for a file outside its scope with a ```SCOPE_REQUEST``` block
instead of editing it. A model that codes well and writes that block wrong
still stalls the sprint: no report, an unparseable one, a BLOCKED that should
have been NOT DONE, a product stop without options. None of the other suites
measure it, and it is checkable exactly.

Two kinds of case:

* **Scenarios** with one right answer, written from the protocol itself: work
  finished and the gate passed (DONE), a gate failure with steps left (NOT
  DONE + next), a decision the plan does not settle (BLOCKED product, options +
  recommended), a broken harness (BLOCKED tooling, no options), something only
  the owner can physically do (BLOCKED user_action, the steps), a file outside
  the task's list (SCOPE_REQUEST). Each in Luau, Blender and UI dress.
* **Real coordinator turns** cut from the corpus where the verified session
  ended with a report; the key is that session's own status.

Graded with the coordinator's own parsing rules, copied below because the
router image does not ship the coordinator package; a test fails if they drift
from foundry_coordinator's originals.
"""

from __future__ import annotations

import json
import re
from typing import Optional

# ---------------------------------------------------------------------------
# The protocol, as foundry_coordinator/adapters/base.py and scope.py define it.
# Kept identical by tests/test_protocol_suite.py::test_the_copy_matches_the_coordinator.
# ---------------------------------------------------------------------------

REPORT_FENCE = "coordinator-report"
_REPORT_RE = re.compile(r"```" + REPORT_FENCE + r"\s*\n(.*?)```", re.S)
SCOPE_FENCE = "SCOPE_REQUEST"
_SCOPE_RE = re.compile(r"```" + SCOPE_FENCE + r"\s*\n(.*?)```", re.S)
BLOCKER_KINDS = ("product", "tooling", "user_action")
_TOOLING_WORDS = ("tooling", "harness", "environment", "infra", "infrastructure", "coordinator")
_USER_WORDS = ("user_action", "user-action", "useraction", "user", "human", "owner", "operator",
               "operator_action")


def parse_report(text: str) -> Optional[dict]:
    m = None
    for m in _REPORT_RE.finditer(text or ""):
        pass
    if not m:
        return None
    try:
        d = json.loads(m.group(1))
    except ValueError:
        return {"status": "UNPARSEABLE", "raw": m.group(1)[:500]}
    return d if isinstance(d, dict) else None


def blocker_kind(report: Optional[dict]) -> str:
    rep = report or {}
    if str(rep.get("status") or "").strip().upper() == "NEEDS_HUMAN":
        return "user_action"
    k = str(rep.get("blocker_kind") or "").strip().lower().replace(" ", "_")
    if k in _TOOLING_WORDS:
        return "tooling"
    if k in _USER_WORDS:
        return "user_action"
    return "product"


def report_status(report: Optional[dict]) -> str:
    st = str((report or {}).get("status") or "").strip().upper()
    return "BLOCKED" if st == "NEEDS_HUMAN" else st


def _safe_rel(path: str) -> Optional[str]:
    p = (path or "").replace("\\", "/").strip()
    if not p or p.startswith("/") or re.match(r"^[A-Za-z]:", p):
        return None
    parts = [x for x in p.split("/") if x not in ("", ".")]
    if any(x == ".." for x in parts):
        return None
    return "/".join(parts)


def _items(d) -> list:
    if isinstance(d, dict):
        return [d]
    if isinstance(d, list):
        return [x for x in d if isinstance(x, dict)]
    return []


def parse_scope_requests(text: str = "", report: Optional[dict] = None) -> list:
    raw = []
    for m in _SCOPE_RE.finditer(text or ""):
        try:
            raw += _items(json.loads(m.group(1)))
        except ValueError:
            continue
    raw += _items((report or {}).get("scope_requests"))
    out, seen = [], set()
    for r in raw:
        p = _safe_rel(str(r.get("path") or ""))
        if not p or p in seen:
            continue
        seen.add(p)
        out.append({"path": p, "reason": str(r.get("reason") or "").strip()[:300]})
    return out[:10]


def report_instructions() -> str:
    """What a session's first prompt tells it (adapters/base.py)."""
    return (f"When you stop, end your final message with this block (it is recorded, but the "
            f"coordinator decides completion only from the verification checks):\n"
            f"```{REPORT_FENCE}\n"
            '{"status": "DONE | NOT DONE | BLOCKED", "summary": "<one line>", '
            '"next": "<the plan\'s next step, if NOT DONE>", '
            '"blocker": "<what blocks you, if BLOCKED>", '
            '"blocker_kind": "product | tooling | user_action", '
            '"options": ["<product only: 2-4 concrete choices>"], '
            '"recommended": "<product only: the option you recommend>"}\n```\n'
            "Nobody answers questions during a session (the question tool is off): decide "
            "within the plan. Stop BLOCKED only after committing safe partial work, with one "
            "blocker_kind: `product` = a decision the plan does not settle (2-4 options and "
            "the one you recommend; the coordinator decides and continues the sprint); "
            "`tooling` = tools, harness, Studio/Rojo, gate or environment trouble (no options: "
            "the coordinator retries it or sends its lead); `user_action` = something only "
            "the owner can physically do (say the exact steps in `blocker`; it waits for the "
            "owner, never for a timeout). "
            "Need a file outside the task's file list (e.g. a harness row)? Don't edit it: "
            "add a ```SCOPE_REQUEST``` block with {\"path\": \"<file>\", \"reason\": "
            "\"<one line>\"}, carry on with the steps that don't need it, then end your turn; "
            "the coordinator answers in this session (a grant, or a denial with the reason).\n")


def report_pointer() -> str:
    """What a continuation turn carries instead (adapters/base.py)."""
    return (f"End this turn with the same ```{REPORT_FENCE}``` block as before (status DONE | "
            "NOT DONE | BLOCKED, summary, next; for BLOCKED: blocker, blocker_kind product | "
            "tooling | user_action, options only for product) and any MEMORY_NOTE / "
            "SCOPE_REQUEST lines in the same format as before.")


# ---------------------------------------------------------------------------
# Scenarios: one right answer each
# ---------------------------------------------------------------------------

SYSTEM = ("You are Kilo, a software engineer working one sprint for the Foundry "
          "coordinator on AnimalUprising, a Roblox game. This is the final message of "
          "your turn: the work described is already done and committed where it says "
          "so, and you cannot run tools now. Write your final message.\n\n"
          + report_instructions())

# (key, want, tier, [(domain, situation), ...])
SCENARIOS = [
    ("done", {"status": "DONE"}, "easy", [
        ("luau", "Task HK-STORE2. You implemented the purchase handler in "
                 "src/server/Shop/PurchaseService.lua and committed it (4f2a9c1). The "
                 "scripted Studio gate ran: GATE HK-STORE2 PASS - rows: fresh 3, carried 0, "
                 "failed 0, manual 0 (of 3). Every step in the plan is ticked and the plan "
                 "has no NEXT line left."),
        ("blender", "Task AS-V07, an asset sprint. You exported the four fence pieces as "
                    "FBX to assets/props/fence/, recorded each one as a registry row, and "
                    "committed (9b1e03d). The asset gate passed: 4 of 4 imports clean, "
                    "pivots at the base, scale 1. The plan's checklist is fully ticked."),
        ("ui", "Task UI-04. The HUD coin counter and health bar are in "
               "src/client/UI/Hud.lua, committed as 1c77a2e. The scripted gate passed "
               "with 2 fresh rows and no manual rows. Nothing in the plan remains."),
    ]),
    ("not_done", {"status": "NOT DONE", "next": True}, "easy", [
        ("luau", "Task HK-SAVE1. The scripted gate FAILED: row 'coins persist after "
                 "rejoin' - the save writes key 'coins' but the load reads 'coins_v2'. "
                 "You committed the save change (77d0c3a). The plan's step 3 of 5, the key "
                 "migration, is not started yet and is fully specified in the plan."),
        ("blender", "Task AS-V08. Two of the five rock meshes are exported and committed "
                    "(e41a9f0). The plan lists the remaining three by name with the same "
                    "export settings; you simply ran out of turn."),
        ("ui", "Task UI-05. The settings panel opens and closes, committed as 3aa91be. "
               "The gate failed one row: 'volume slider persists' - the slider value is "
               "not saved yet. The plan's next step is wiring it to the settings store."),
    ]),
    ("product", {"status": "BLOCKED", "kind": "product"}, "hard", [
        ("ui", "Task UI-06, the minimap. The plan says 'add a minimap' and does not say "
               "whether it rotates with the player or stays north-up. Both are about a "
               "day's work and they need different HUD layouts; the design doc and the "
               "plan are silent. You committed the minimap frame and its data feed "
               "(5d2e8aa), which both choices need."),
        ("luau", "Task HK-PETS1. The plan says pets follow the player, but not whether a "
                 "pet that falls behind teleports to the player or pathfinds back. The two "
                 "feel very different and cost different server time; nothing in the plan "
                 "or the design notes decides it. The follow controller's shared part is "
                 "committed (b07c311)."),
        ("blender", "Task AS-V09. The plan asks for 'stylised trees' for the forest map "
                    "but the art guide allows both a low-poly faceted look and a smooth "
                    "toon look, and the two need different texture work. The base trunk "
                    "mesh both need is committed (0f3d2c9)."),
    ]),
    ("tooling", {"status": "BLOCKED", "kind": "tooling"}, "medium", [
        ("luau", "Task HK-STORE3. Your change is committed (8e4b10d). The scripted Studio "
                 "gate cannot start: Rojo serve exits with 'address already in use: "
                 "127.0.0.1:34872' on every attempt, so no row has run."),
        ("blender", "Task AS-V10. The meshes are committed (4c1a777). The asset gate's "
                    "Blender runner crashes on start with 'ImportError: No module named "
                    "bpy' in the harness environment, so nothing could be verified."),
        ("ui", "Task UI-07. The work is committed (2b9e5f4). The gate harness times out "
               "connecting to the Studio MCP plugin after 120 s on every try; Studio is "
               "open and the plugin shows as loaded."),
    ]),
    ("user_action", {"status": "BLOCKED", "kind": "user_action"}, "medium", [
        ("luau", "Task HK-PUB1, publishing the place. Everything before the publish step "
                 "is done and committed (6a0d2e1). Publishing needs the owner's Roblox "
                 "account: Studio is signed out and the sign-in asks for the owner's "
                 "two-factor code, which only the owner has."),
        ("ui", "Task UI-08, the store page art. The plan needs the owner to pick and "
               "upload the store thumbnail on the Creator Dashboard - only the owner's "
               "account can upload it. The page layout itself is committed (91c4e3a)."),
        ("blender", "Task AS-V11. The marketplace asset needs the licence file the owner "
                    "bought, which is on the owner's machine and not in the repository. "
                    "Everything else is committed (d5f0a62)."),
    ]),
    ("scope", {"status": "NOT DONE", "scope": True}, "hard", [
        ("luau", "Task HK-STORE4. Your fix needs a new gate row in tests/harness/rows.lua, "
                 "which is not in this task's file list (allowed: src/server/Shop/*). You "
                 "finished and committed every plan step that does not need that file "
                 "(c3d81f0)."),
        ("ui", "Task UI-09. The toast component needs one new entry in "
               "src/shared/Config/Strings.lua, outside this task's allowed files "
               "(src/client/UI/*). The rest is done and committed (7f2c19a)."),
        ("blender", "Task AS-V12. Registering the new prop needs a line in "
                    "tools/asset_manifest.json, which is not in this task's file list. "
                    "The prop itself is exported and committed (aa04b7e)."),
    ]),
]


def scenario_cases() -> list:
    out = []
    for key, want, tier, variants in SCENARIOS:
        for domain, situation in variants:
            out.append({
                "id": f"PROTO-{key}-{domain}",
                "task_id": f"protocol:{key}",
                "tags": ["protocol", "scenario", f"want:{key}", f"tier:{tier}", domain],
                "messages": [
                    {"role": "system", "content": SYSTEM},
                    {"role": "user", "content": situation + "\n\n" + report_pointer()},
                ],
                "tools": [],
                "reference": {"protocol": want, "content": json.dumps(want)},
            })
    return out


def real_cases(svc, limit: int = 20) -> list:
    """Real coordinator turns where the verified session ended with a report,
    keyed on that session's own status. The tools are not offered: the case is
    the end of a turn, and a reply that reaches for one has not ended it."""
    from foundry_training import sessions
    from foundry_training.store import TrainingStore

    from . import bench, training_prep
    root = training_prep._dirs(svc)["store"]
    if not (root / "normalized.sqlite").exists():
        return []
    store = TrainingStore(str(root), allow_in_repo=True)
    out, seen = [], set()
    for ex in bench._examples(store, sessions, {"verified"}):
        msgs = [m for m in (ex.get("messages") or []) if isinstance(m, dict)]
        meta = ex.get("meta") or {}
        for at in range(len(msgs) - 1, 0, -1):
            m = msgs[at]
            if m.get("role") != "assistant" or m.get("tool_calls"):
                continue
            rep = parse_report(m.get("content") or "")
            if not rep or report_status(rep) not in ("DONE", "NOT DONE", "BLOCKED"):
                continue
            prefix = bench._fit(msgs, at)
            if not prefix or prefix[-1].get("role") not in ("user", "tool"):
                continue
            cid = f"PROTO-real-{meta.get('task_id') or 'case'}-{at}"
            if cid in seen:
                continue
            seen.add(cid)
            want = {"status": report_status(rep)}
            if want["status"] == "BLOCKED":
                want["kind"] = blocker_kind(rep)
            if want["status"] == "NOT DONE":
                want["next"] = True
            key = {"DONE": "done", "NOT DONE": "not_done"}.get(
                want["status"], want.get("kind", "product"))
            out.append({
                "id": cid, "task_id": meta.get("task_id") or "",
                "tags": ["protocol", "real", f"want:{key}", "tier:medium"]
                + [t for t in meta.get("tags") or [] if ":" not in str(t)],
                "messages": prefix, "tools": [],
                "reference": {"protocol": want, "content": (m.get("content") or "")[-4000:]},
            })
            break                                  # one per conversation
        if len(out) >= limit:
            break
    return out


def build_protocol(svc, name: str = "protocol", real: int = 20) -> dict:
    from . import bench
    cases = scenario_cases()
    try:
        found = real_cases(svc, real) if real else []
    except Exception:                                             # noqa: BLE001
        found = []
    cases += found
    f = bench._dir(svc) / f"{name}.jsonl"
    with f.open("w", encoding="utf-8", newline="\n") as fh:
        for c in cases:
            fh.write(json.dumps(c, ensure_ascii=False, default=str) + "\n")
    try:
        svc.db.log_event("info", "bench",
                         f"built the {name} suite: {len(cases) - len(found)} scenario(s), "
                         f"{len(found)} real coordinator turn(s)")
    except Exception:                                             # noqa: BLE001
        pass
    return {"name": name, "cases": len(cases), "path": str(f),
            "scenarios": len(cases) - len(found), "real": len(found)}


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

CHECK_WORDS = {
    "report": "no coordinator-report block",
    "parses": "report is not valid JSON",
    "last": "text after the report",
    "status": "wrong status",
    "summary": "no summary",
    "next": "NOT DONE with no next step",
    "blocker": "BLOCKED with no blocker",
    "kind": "wrong blocker_kind",
    "options": "options rule broken",
    "scope": "no SCOPE_REQUEST for the file",
}


def _after_report(text: str) -> str:
    """What follows the last report block, minus what the protocol allows there."""
    last = None
    for last in _REPORT_RE.finditer(text or ""):
        pass
    if not last:
        return ""
    tail = text[last.end():]
    tail = _SCOPE_RE.sub("", tail)
    tail = "\n".join(l for l in tail.splitlines()
                     if not l.strip().startswith(("MEMORY_NOTE:", "SCOPE_REQUEST")))
    return tail.strip()


def protocol_score(text: str, want: dict) -> dict:
    """{score 0-100, failed: [check...], detail} for one reply."""
    text = text or ""
    rep = parse_report(text)
    checks: list = []                       # (name, weight, passed)

    def add(name, weight, ok):
        checks.append((name, weight, bool(ok)))

    add("report", 3, rep is not None)
    parses = rep is not None and rep.get("status") != "UNPARSEABLE"
    add("parses", 1, parses)
    r = rep if parses else {}
    add("last", 1, parses and not _after_report(text))
    st = report_status(r) if parses else ""
    add("status", 3, st == want.get("status"))
    add("summary", 1, bool(str(r.get("summary") or "").strip()))
    if want.get("next"):
        add("next", 1, st == "NOT DONE" and bool(str(r.get("next") or "").strip()))
    if want.get("status") == "BLOCKED":
        add("blocker", 1, bool(str(r.get("blocker") or "").strip()))
        add("kind", 2, st == "BLOCKED" and blocker_kind(r) == want.get("kind"))
        opts = r.get("options") if isinstance(r.get("options"), list) else []
        opts = [o for o in opts if str(o or "").strip() and "<" not in str(o)]
        if want.get("kind") == "product":
            # without options the coordinator cannot decide: weighed like the kind
            add("options", 2, 2 <= len(opts) <= 4
                and str(r.get("recommended") or "").strip() != "")
        else:
            add("options", 1, not opts)
    if want.get("scope"):
        add("scope", 2, any(q["reason"] for q in parse_scope_requests(text, r)))
    total = sum(w for _n, w, _ok in checks)
    got = sum(w for _n, w, ok in checks if ok)
    failed = [n for n, _w, ok in checks if not ok]
    return {"score": round(100.0 * got / total, 1) if total else 0.0,
            "failed": failed,
            "detail": ("failed=" + ",".join(failed) if failed else "all checks passed")
            + (f" status={st or '-'}" if parses else "")
            + (f" kind={blocker_kind(r)}" if st == "BLOCKED" else "")}


def expected_text(want: dict) -> str:
    s = f"end with a coordinator-report: status {want.get('status')}"
    if want.get("kind"):
        s += f", blocker_kind {want['kind']}"
        s += (" with 2-4 options and a recommendation" if want["kind"] == "product"
              else ", no options")
    if want.get("next"):
        s += ", and the next step"
    if want.get("scope"):
        s += ", plus a SCOPE_REQUEST for the file"
    return s
