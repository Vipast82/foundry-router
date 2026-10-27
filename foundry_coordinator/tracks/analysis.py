"""Sprint analysis (Team Lead piece 1): which sprints can run side by side.

Reads every sprint plan of a project (`docs/progress/<ID>_PLAN.md`, the Animal
Uprising format; or the generic task file) and works out, deterministically and
without a model call:

  * what each sprint NEEDS: `studio`, `blender`, `audio`, `images`, `gpu`,
    `backend` or `none`. Sources, in order: an optional `## Needs` block in the
    plan, the existing `**NEEDS (MCP):**` line (every spelling the AU plans
    use), else an inference from the plan: UI stories or a gate file -> studio;
    `assets/**` with .blend/.fbx -> blender; `audio/**` -> audio; else backend.
    audio / images imply `gpu` (one card for ACE-Step, Comfy and the local model).
  * the dependency graph: explicit "Depends on" plus file overlap (two sprints
    that may change the same file are serial), except files on the
    `append_safe` list (shared ledgers the coordinator merges row by row).
  * ordered tracks: each track is a chain of sprints that must stay serial,
    with the union of their needs; plus every serial pair with its reason.

`analyze()` is pure (plans in, dict out); `collect_plans()` reads a project;
`propose()` cuts the tracks to an agent count; `render()` prints for the CLI.
"""

from __future__ import annotations

import fnmatch
import os
import re
from dataclasses import dataclass, field
from typing import Iterable, Optional

from ..profile import matches_any

NEEDS = ("studio", "blender", "audio", "images", "gpu", "backend", "none")
# resources with a capacity of one that two parallel tracks would fight over
CAPACITY_ONE = ("blender", "gpu")
# shared ledgers every AU sprint appends a row to: a rebase merges them (and a
# conflict goes to the Lead repair), so they never make two sprints serial.
# RegressionIndex.lua, Strings.lua, Ui.lua, default.project.json and GateRunner.lua
# stay conflicts until the coordinator merges them from per-sprint fragments.
DEFAULT_APPEND_SAFE = ["docs/progress/STATE.md", "docs/progress/state/**",
                       "docs/progress/STATE_ARCHIVE*.md", "docs/MASTER_ORDER.md",
                       "docs/design/ASSET_LEDGER.md", "docs/memory/**"]
PLAN_GLOB = "docs/progress/*_PLAN.md"
ORDER_FILE = "docs/MASTER_ORDER.md"
# a sprint id: UI-03a, AS-C-IMP4b, S51B-R, GF-FX1, T1, HK-COORD1
ID_RE = re.compile(r"\b([A-Z]{1,5}[0-9]?-?[A-Za-z0-9.-]*[0-9][A-Za-z0-9]*)\b")
# a MASTER_ORDER phase or lane label (A0, A1, B2, D1, C3a): plans cite them in
# "Depends on" prose ("the A0 entry gate"), but they are not sprints
PHASE_LABEL_RE = re.compile(r"^[A-F][0-9]{0,2}[a-z]?$")


def drop_phase_labels(ids: list[str], has_plan) -> list[str]:
    """`ids` without phase labels, unless a sprint plan by that name exists
    (`has_plan(id) -> bool`), so a real sprint id is never dropped."""
    return [d for d in ids if not PHASE_LABEL_RE.match(d) or has_plan(d)]
_FILES_HEADING = re.compile(r"^#{2,4}\s+Files this sprint may change", re.I)
_NEEDS_HEADING = re.compile(r"^#{2,4}\s+Needs\s*$", re.I)
_MCP_LINE = re.compile(r"NEEDS\s*\(?MCP\)?\s*:?\**\s*(.*)$", re.I)
_SERVER_NEED = {
    "robloxstudio-mcp": "studio", "roblox_studio": "studio", "roblox studio": "studio",
    "blender-mcp": "blender", "blender": "blender",
    "comfy-mcp": "images", "comfy": "images",
    "stable-audio": "audio", "acestep-music": "audio", "ace-step": "audio", "kokoro": "audio",
    "kokoro-tts": "audio", "chatterbox-tts": "audio", "chatterbox": "audio",
}
_STUDIO_IDLE = re.compile(r"roblox_studio\s*\(?[^.;)]*\b(idle|no studio work)\b", re.I)


@dataclass
class PlanInfo:
    task_id: str
    path: str = ""
    title: str = ""
    tier: str = ""
    files: list = field(default_factory=list)
    suites: list = field(default_factory=list)
    stories: list = field(default_factory=list)
    deps: list = field(default_factory=list)             # ids named by the plan
    needs: list = field(default_factory=list)
    needs_source: str = "inferred"                       # block | mcp_line | inferred | task
    done: bool = False

    def to_json(self) -> dict:
        return {"task_id": self.task_id, "path": self.path, "title": self.title,
                "tier": self.tier, "files": list(self.files), "suites": list(self.suites),
                "stories": list(self.stories), "deps": list(self.deps),
                "needs": list(self.needs), "needs_source": self.needs_source,
                "done": self.done}


# ---------------------------------------------------------------------------- parsing

def _lines(text: str) -> list[str]:
    return text.replace("\r\n", "\n").replace("\r", "\n").split("\n")


def _section(text: str, heading_re: re.Pattern) -> Optional[str]:
    """The body of the first '##..####' section whose heading matches, or None."""
    out, on, level = [], False, 0
    for line in _lines(text):
        s = line.strip()
        m = re.match(r"^(#{2,4})\s+(.*)$", s)
        if m and not on and heading_re.match(s):
            on, level = True, len(m.group(1))
            continue
        if on and m and len(m.group(1)) <= level:
            break
        if on:
            out.append(line)
    return "\n".join(out) if on else None


def plan_files(text: str) -> list[str]:
    """The 'Files this sprint may change' entries (done_check.plan_files rule:
    bullets whose first backtick span is the path)."""
    body = _section(text, _FILES_HEADING)
    if body is None:
        return []
    out = []
    for line in _lines(body):
        m = re.match(r"^[-*]\s+`([^`]+)`", line.strip())
        if m:
            p = m.group(1).strip().replace("\\", "/")
            if p and p not in out:
                out.append(p)
    return out


def plan_tier(text: str) -> str:
    m = re.search(r"\*\*Tier:\*\*\s*([ABC])\b", text) or re.search(r"\bTier[:\s]+\**([ABC])\b", text)
    return m.group(1) if m else ""


def plan_title(text: str, task_id: str) -> str:
    for line in _lines(text):
        if line.startswith("# "):
            return line[2:].strip()[:200]
    return task_id


def plan_stories(text: str) -> list[str]:
    body = _section(text, re.compile(r"^#{2,4}\s+Stories\b", re.I))
    if not body:
        return []
    ids = re.findall(r"`([a-z0-9][a-z0-9_-]*\.[a-z0-9][a-z0-9_.-]*)`", body)
    out: list[str] = []
    for i in ids:
        if i not in out:
            out.append(i)
    return out


def plan_suites(text: str) -> list[str]:
    body = _section(text, re.compile(r"^#{2,4}\s+Affected suites\b", re.I))
    if not body:
        return []
    out: list[str] = []
    for s in re.findall(r"\b(S\d+[A-Za-z]?|[A-Z]{2,}_[A-Z0-9][A-Za-z0-9_]*)\b", body):
        if s not in out:
            out.append(s)
    return out


def plan_deps(text: str, task_id: str, has_plan=lambda _id: False) -> list[str]:
    """Ids named by '**Depends on:**' lines and 'Depends on X' prose, without
    MASTER_ORDER phase labels (see drop_phase_labels)."""
    out: list[str] = []
    # prose wraps: fold each paragraph (lines up to a blank line or heading) first
    folded, para = [], []
    for line in _lines(text):
        if not line.strip() or line.lstrip().startswith("#"):
            folded.append(" ".join(para))
            folded.append(line)
            para = []
        else:
            para.append(line.strip())
    folded.append(" ".join(para))
    for m in re.finditer(r"\bDepends on:?\**\s*([^\n]+)", "\n".join(folded)):
        seg = m.group(1).split("**")[0]
        seg = re.split(r"[.;]\s|\s·\s", seg)[0]
        for d in ID_RE.findall(seg):
            if d != task_id and d not in out:
                out.append(d)
    return drop_phase_labels(out, has_plan)


def parse_needs_block(text: str) -> Optional[list[str]]:
    """`## Needs` block: 'studio, blender' or one need per bullet. None when absent."""
    body = _section(text, _NEEDS_HEADING)
    if body is None:
        return None
    words = re.findall(r"[a-z]+", body.lower())
    found = [w for w in words if w in NEEDS]
    return _normalize(found) if found else ["none"]


def parse_mcp_line(text: str) -> Optional[list[str]]:
    """The `**NEEDS (MCP):**` line (any of the plan spellings) -> needs, or None."""
    for line in _lines(text):
        if not re.search(r"NEEDS\s*\(?MCP\)?", line, re.I):
            continue
        m = _MCP_LINE.search(line)
        if not m:
            continue
        body = m.group(1)
        # only the servers that are ON count: cut at OFF / No / Do NOT segments
        pos = re.split(r"\bOFF\b|\bNo\b(?=\s+[a-z])|\bDo NOT\b|\bnever\b", body, maxsplit=1,
                       flags=re.I)[0]
        low = pos.lower()
        needs: list[str] = []
        for server, need in _SERVER_NEED.items():
            if server in low:
                if need == "studio" and server in ("roblox_studio", "roblox studio") \
                        and "robloxstudio-mcp" not in low and _STUDIO_IDLE.search(pos):
                    continue                      # "Roblox_Studio idle (no Studio work)"
                if need == "blender" and server == "blender" and "blender-mcp" not in low:
                    continue                      # a bare mention, no Blender run
                needs.append(need)
        if re.search(r"^\s*yes\b", pos.strip(), re.I) and "studio" in low:
            needs.append("studio")
        # Blender run from PowerShell in background mode is still Blender (the one
        # install), even when blender-mcp itself is OFF on that line
        if re.search(r"blender(\.exe)?\s+-b\b|blender[^.;]*background mode", body, re.I):
            needs.append("blender")
        return _normalize(needs) if needs else ["backend"]
    return None


def infer_needs(files: Iterable[str], stories: Iterable[str], tier: str = "") -> list[str]:
    """The design's fallback: UI stories or a gate file -> studio; assets/** with
    .blend/.fbx -> blender; audio/** -> audio; otherwise backend."""
    files = [f.replace("\\", "/") for f in files]
    needs: list[str] = []
    if list(stories) or any(re.search(r"(^|/)docs/progress/[^/]*_gate\.json$", f) for f in files) \
            or any(f.lower().endswith((".story.luau", ".story.lua")) for f in files):
        needs.append("studio")
    if any(f.startswith("assets/") and re.search(r"\.(blend|fbx)$|/\*\*$|\*\.(blend|fbx)$", f)
           for f in files):
        needs.append("blender")
    if any(f.startswith("audio/") for f in files):
        needs.append("audio")
    return _normalize(needs) if needs else ["backend"]


def _normalize(needs: Iterable[str]) -> list[str]:
    out: list[str] = []
    for n in needs:
        if n in NEEDS and n not in out:
            out.append(n)
    if "none" in out and len(out) > 1:
        out.remove("none")
    if ("audio" in out or "images" in out) and "gpu" not in out:
        out.append("gpu")
    if "backend" in out and any(n in out for n in ("studio", "blender", "audio", "images")):
        out.remove("backend")
    return sorted(out, key=NEEDS.index)


def parse_plan(task_id: str, text: str, path: str = "", done: bool = False) -> PlanInfo:
    files = plan_files(text)
    stories = plan_stories(text)
    tier = plan_tier(text)
    needs, src = parse_needs_block(text), "block"
    if needs is None:
        needs, src = parse_mcp_line(text), "mcp_line"
    if needs is None:
        needs, src = infer_needs(files, stories, tier), "inferred"
    plan_dir = os.path.dirname(path) if path else ""
    return PlanInfo(task_id=task_id, path=path, title=plan_title(text, task_id), tier=tier,
                    files=files, suites=plan_suites(text), stories=stories,
                    deps=plan_deps(text, task_id, has_plan=lambda d: bool(plan_dir) and
                                   os.path.isfile(os.path.join(plan_dir, f"{d}_PLAN.md"))),
                    needs=needs, needs_source=src, done=done)


def plan_from_spec(spec, done: bool = False) -> PlanInfo:
    """A PlanInfo from a TaskSpec (generic task files): scope = files, deps,
    `needs` from the task item or tags `needs:<x>`, else inferred."""
    d = spec.data or {}
    needs = d.get("needs")
    if isinstance(needs, str):
        needs = re.findall(r"[a-z]+", needs.lower())
    tagged = [t.split(":", 1)[1] for t in (spec.tags or []) if str(t).startswith("needs:")]
    src = "task"
    if isinstance(needs, list) and needs:
        needs = _normalize(needs)
    elif tagged:
        needs = _normalize(tagged)
    else:
        needs, src = infer_needs(spec.scope, []), "inferred"
    return PlanInfo(task_id=spec.task_id, path=str(d.get("plan") or ""), title=spec.title,
                    tier=str(d.get("tier") or ""), files=list(spec.scope), deps=list(spec.deps),
                    needs=needs, needs_source=src, done=done)


# ---------------------------------------------------------------------------- overlap

_GLOB = re.compile(r"[*?\[]")


def _prefix(p: str) -> str:
    m = _GLOB.search(p)
    return p if not m else p[:m.start()]


def files_overlap(a: str, b: str) -> bool:
    """Two 'may change' entries can touch the same file."""
    a, b = a.replace("\\", "/").strip("/"), b.replace("\\", "/").strip("/")
    if not a or not b:
        return False
    ga, gb = bool(_GLOB.search(a)), bool(_GLOB.search(b))
    if not ga and not gb:
        return a == b or a.startswith(b + "/") or b.startswith(a + "/")
    if not ga:
        return fnmatch.fnmatchcase(a, b) or a.startswith(_prefix(b)) or matches_any(a, [b])
    if not gb:
        return files_overlap(b, a)
    pa, pb = _prefix(a), _prefix(b)
    return pa.startswith(pb) or pb.startswith(pa)


def shared_files(a: PlanInfo, b: PlanInfo, append_safe: list[str]) -> list[str]:
    """Entries of `a` that overlap an entry of `b`, minus the append-safe ledgers."""
    out: list[str] = []
    for fa in a.files:
        if matches_any(fa, append_safe):
            continue
        for fb in b.files:
            if matches_any(fb, append_safe):
                continue
            if files_overlap(fa, fb):
                out.append(fa if fa == fb else f"{fa} ~ {fb}")
                break
    return out


# ---------------------------------------------------------------------------- analysis

class _UF:
    def __init__(self, keys):
        self.p = {k: k for k in keys}

    def find(self, k):
        while self.p[k] != k:
            self.p[k] = self.p[self.p[k]]
            k = self.p[k]
        return k

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[rb] = ra


def _topo(ids: list[str], deps: dict[str, list[str]], order: dict[str, int]) -> list[str]:
    """Dependencies first; ties by MASTER_ORDER position, then id. Cycles are
    broken by order (never raises)."""
    ids = sorted(ids, key=lambda i: (order.get(i, 10 ** 6), i))
    out: list[str] = []
    pending = list(ids)
    while pending:
        pick = next((i for i in pending if all(d in out or d not in ids for d in deps.get(i, []))),
                    pending[0])
        out.append(pick)
        pending.remove(pick)
    return out


def analyze(plans: list[PlanInfo], append_safe: Optional[list[str]] = None,
            main_sprint: str = "", order: Optional[list[str]] = None) -> dict:
    """Ordered tracks and the serial pairs. Done sprints only satisfy
    dependencies; they are never put on a track."""
    safe = list(DEFAULT_APPEND_SAFE if append_safe is None else append_safe)
    pos = {tid: i for i, tid in enumerate(order or [])}
    by_id = {p.task_id: p for p in plans}
    open_ids = [p.task_id for p in plans if not p.done]
    deps = {p.task_id: [d for d in p.deps if d in by_id and not by_id[d].done]
            for p in plans}
    external = {p.task_id: [d for d in p.deps if d not in by_id] for p in plans}
    serial: list[dict] = []
    uf = _UF(open_ids)
    for tid in open_ids:
        for d in deps[tid]:
            if d in uf.p:
                uf.union(d, tid)
                serial.append({"a": tid, "b": d, "reason": f"{tid} depends on {d}"})
    for i, a in enumerate(open_ids):
        for b in open_ids[i + 1:]:
            if any(s["a"] in (a, b) and s["b"] in (a, b) for s in serial):
                continue
            shared = shared_files(by_id[a], by_id[b], safe)
            if shared:
                uf.union(a, b)
                serial.append({"a": a, "b": b, "files": shared[:12],
                               "reason": f"{a} and {b} both change " + ", ".join(shared[:4])
                               + (f" (+{len(shared) - 4})" if len(shared) > 4 else "")})
    groups: dict[str, list[str]] = {}
    for tid in open_ids:
        groups.setdefault(uf.find(tid), []).append(tid)
    tracks = []
    for members in groups.values():
        chain = _topo(members, deps, pos)
        needs = _normalize(n for t in chain for n in by_id[t].needs)
        tracks.append({"name": _track_name(chain[0]), "sprints": chain, "needs": needs or ["none"],
                       "studio_at_merge": "studio" in needs,
                       "first_order": min(pos.get(t, 10 ** 6) for t in chain)})
    tracks.sort(key=lambda t: (t["first_order"], t["sprints"][0]))
    mt = next((t for t in tracks if main_sprint and main_sprint in t["sprints"]), None)
    if mt is not None:
        tracks.remove(mt)
        tracks.insert(0, mt)
    for t in tracks:
        t.pop("first_order", None)
    if tracks:
        tracks[0]["kind"] = "main"
        for t in tracks[1:]:
            t["kind"] = "parallel"
    warnings = []
    for tid in open_ids:
        if external[tid]:
            warnings.append(f"{tid} depends on {', '.join(external[tid])}, which has no plan here")
        if not by_id[tid].files:
            warnings.append(f"{tid} lists no files it may change")
    return {"tracks": tracks, "serial": serial, "append_safe": safe,
            "sprints": {p.task_id: p.to_json() for p in plans},
            "open": open_ids, "done": [p.task_id for p in plans if p.done],
            "warnings": warnings}


def _track_name(first_sprint: str) -> str:
    return re.sub(r"[^a-z0-9._-]+", "-", first_sprint.lower()).strip("-") or "track"


def propose(analysis: dict, max_agents: int, running: Optional[Iterable[str]] = None) -> dict:
    """Cut the analysis to `max_agents`: the main track always counts first; up
    to max_agents-1 parallel tracks, never two that share a capacity-one
    resource (blender, gpu), longest chains first. Deterministic."""
    max_agents = max(1, int(max_agents or 1))
    tracks = list(analysis.get("tracks") or [])
    running = set(running or [])
    if not tracks:
        return {"max_agents": max_agents, "agents": 0, "main": None, "tracks": [], "notes": []}
    main, rest = tracks[0], tracks[1:]
    chosen: list[dict] = []
    notes: list[str] = []
    taken: set = set()
    cands = sorted(rest, key=lambda t: (-len(t["sprints"]), t["name"]))
    for t in cands:
        if any(s in running for s in t["sprints"]):
            notes.append(f"{t['name']}: already running")
            continue
        if len(chosen) >= max_agents - 1:
            notes.append(f"{t['name']}: over max_agents ({max_agents})")
            continue
        clash = [n for n in t["needs"] if n in CAPACITY_ONE and n in taken]
        if clash:
            notes.append(f"{t['name']}: would share {', '.join(clash)} with a chosen track")
            continue
        taken.update(n for n in t["needs"] if n in CAPACITY_ONE)
        chosen.append(t)
    chosen.sort(key=lambda t: tracks.index(t))
    for t in chosen:
        if t.get("studio_at_merge"):
            notes.append(f"{t['name']}: needs Studio; its Studio work runs at merge time in the "
                         f"main checkout")
    return {"max_agents": max_agents, "agents": 1 + len(chosen), "main": main,
            "tracks": chosen, "notes": notes}


# ---------------------------------------------------------------------------- a project

def read_order(workspace: str) -> list[str]:
    """Sprint ids in docs/MASTER_ORDER.md order (first mention wins); [] when absent."""
    try:
        with open(os.path.join(workspace, ORDER_FILE), encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        return []
    out: list[str] = []
    for m in ID_RE.finditer(text):
        if m.group(1) not in out:
            out.append(m.group(1))
    return out


def collect_plans(P, done: Optional[Iterable[str]] = None, plan_glob: str = PLAN_GLOB) -> list[PlanInfo]:
    """Every plan of a project: the `docs/progress/*_PLAN.md` files of the
    workspace, else the generic task file through the adapter. `done` = ids the
    coordinator already finished (adapter.dependency_done is asked for the rest)."""
    import glob
    ws = P.profile.workspace
    done_ids = set(done or [])
    dep_done = getattr(P.adapter, "dependency_done", None)

    def is_done(tid: str) -> bool:
        if tid in done_ids:
            return True
        try:
            return bool(dep_done(tid)) if callable(dep_done) else False
        except Exception:                                         # noqa: BLE001
            return False
    plans: list[PlanInfo] = []
    paths = sorted(glob.glob(os.path.join(ws, *plan_glob.split("/"))))
    for path in paths:
        name = os.path.basename(path)
        tid = name[:-len("_PLAN.md")] if name.endswith("_PLAN.md") else os.path.splitext(name)[0]
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                text = f.read()
        except OSError:
            continue
        rel = os.path.relpath(path, ws).replace("\\", "/")
        plans.append(parse_plan(tid, text, rel, done=is_done(tid)))
    if not plans:
        items = []
        tf = getattr(P.adapter, "_task_file", None)
        if callable(tf):
            try:
                items = tf()
            except Exception:                                     # noqa: BLE001
                items = []
        for it in items:
            tid = str(it.get("id") or "")
            if not tid:
                continue
            try:
                spec = P.adapter.describe(tid)
            except Exception:                                     # noqa: BLE001
                continue
            plans.append(plan_from_spec(spec, done=is_done(tid)))
    return plans


def analyze_project(P, done: Optional[Iterable[str]] = None, append_safe=None,
                    main_sprint: str = "") -> dict:
    plans = collect_plans(P, done)
    return analyze(plans, append_safe=append_safe, main_sprint=main_sprint,
                   order=read_order(P.profile.workspace))


def render(analysis: dict) -> str:
    lines = []
    for i, t in enumerate(analysis.get("tracks") or []):
        tag = "main" if t.get("kind") == "main" else f"track {i}"
        lines.append(f"{tag:<8} {t['name']:<14} needs {', '.join(t['needs'])}"
                     + ("  (Studio at merge)" if t.get("studio_at_merge") and i else ""))
        for s in t["sprints"]:
            p = analysis["sprints"].get(s) or {}
            lines.append(f"           {s:<14} {', '.join(p.get('needs') or [])} "
                         f"[{p.get('needs_source', '')}] {len(p.get('files') or [])} files"
                         + (f", deps {', '.join(p['deps'])}" if p.get("deps") else ""))
    if analysis.get("serial"):
        lines.append("serial:")
        for s in analysis["serial"]:
            lines.append(f"  {s['reason']}")
    if analysis.get("done"):
        lines.append("done: " + ", ".join(analysis["done"]))
    for w in analysis.get("warnings") or []:
        lines.append(f"warning: {w}")
    return "\n".join(lines) or "no plans"
