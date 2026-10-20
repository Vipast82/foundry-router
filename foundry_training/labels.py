"""UI v3 labels, tags and per-step authorship for episodes (AU_UI_REWORK_PLAN
section 14).

labels      what was measured at the episode's revisions, never text inside it:
            the studio_gate verdict JSON (check results, failed checks with
            details, captures), the vision verdict (APPROVED / CHANGES with
            numbered findings) and later operator / playtest critiques. A
            label belongs to an episode when its task matches and its head is
            one of the heads that episode's session produced.
tags        fast_lane (task listed in [fast_lane].tasks), ui_v3 (captures under
            docs/evidence/UI-v3 or a UI-v3 sprint plan), template:T1..T8 and
            widget:<Name> parsed from docs/progress/<ID>_PLAN.md, device:WxH
            from the captures. Used to export a Roblox-UI subset.
authorship  a step (one assistant turn) whose code came from a helper's
            prework patch (`helper:<unit>`) or from a harvested frontier
            pre-code model (`helper:precode-harvest`) is frontier-authored:
            its author becomes {"kind": "helper", ...}. Such turns stay in the
            conversation as context and are never training targets; the local
            primary's own turns stay eligible. Helper guidance that is not
            code (plans, reviews, findings) is not authorship.
inline      correction triples inside ONE fast-lane session: the local
corrections attempt up to a red gate / vision CHANGES, the findings the session
            received, and the corrected local code that followed.
"""

from __future__ import annotations

import json
import os
import re
from typing import Optional

from .schema import content_hash, stable_id

TEMPLATES = {"T1": "ModalFrame", "T2": "CatalogGrid", "T3": "Showcase", "T4": "TreeView",
             "T5": "ListRows", "T6": "RewardTrack", "T7": "ResultsSummary", "T8": "ConfirmDialog"}
KNOWN_WIDGETS = ("ModalFrame", "CatalogGrid", "DetailPane", "Showcase", "TreeView", "ListRows",
                 "RewardTrack", "ResultsSummary", "ConfirmDialog", "StatusPill", "CurrencyChip",
                 "VitalsBar", "AbilityButton", "AbilityArc", "AbilityRow", "Minimap", "MapOverlay",
                 "RadialRing", "InspectCard", "Toast", "AlertStrip", "TutorialSlip",
                 "DownedStrip", "Rail", "TabBar", "PromptFooter", "Button")
UI_V3_ROOT = "docs/evidence/UI-v3/"
_SIZE = re.compile(r"@(\d{2,5}x\d{2,5})\b")
_TEMPLATE = re.compile(r"\bT([1-8])\b")
CODE_TOOLS = ("write", "edit", "multiedit", "apply_patch", "patch", "write_to_file",
              "replace_in_file")
COPY_CMD = re.compile(r"\b(cp|mv|copy|move|git\s+mv|Copy-Item|Move-Item)\b", re.I)


# ---------------------------------------------------------------- labels

def heads_of_session(ev: list[dict], task_id: str, session_id: str) -> list[str]:
    """Every head this session handed to verification, in order."""
    out = []
    for e in ev:
        if e["event_type"] != "task_transition" or e["task_id"] != task_id:
            continue
        evd = e["facts"].get("evidence") or {}
        if evd.get("session_id") == session_id and e["facts"]["to"] == "VERIFYING" \
                and evd.get("head_sha") and evd["head_sha"] not in out:
            out.append(evd["head_sha"])
    return out


def _same(a: Optional[str], b: Optional[str]) -> bool:
    return bool(a and b) and (a.startswith(b) or b.startswith(a)) and min(len(a), len(b)) >= 7


def task_labels(ev: list[dict], task_id: Optional[str]) -> list[dict]:
    return [e for e in ev if e["event_type"] == "label" and task_id and e["task_id"] == task_id]


def summarize(e: dict) -> dict:
    f = e["facts"]
    base = {"source": f["label"], "head": e["source_revision"], "ts": e["timestamp"],
            "event_id": e["event_id"]}
    if f["label"] == "gate_verdict":
        # recompute for every label (also ones stored before the rule existed):
        # never trust the verdict file's own `result`
        from .adapters.gate import overall
        claimed = str(f.get("claimed_result") or f.get("result") or "UNKNOWN").upper()
        computed = overall(f.get("checks") or [], f.get("error"))
        bad = bool(f.get("inconsistent")) or claimed != computed
        return {**base, "result": "UNKNOWN" if bad else computed, "mode": f.get("mode"),
                "inconsistent": bad, "claimed_result": claimed,
                "summary_line": f.get("summary_line"), "checks": f.get("checks") or [],
                "failed_checks": f.get("failed") or [], "captures": f.get("captures") or [],
                "error": f.get("error")}
    if f["label"] == "vision_verdict":
        return {**base, "verdict": f["verdict"], "findings": f.get("findings") or [],
                "persona": f.get("persona"), "served_model": f.get("served_model"),
                "frames": f.get("frames") or []}
    return {**base, "kind": f.get("kind"), "by": f.get("by"), "findings": f.get("findings") or []}


def episode_labels(ev: list[dict], task_id: Optional[str], heads: list[str],
                   first_ts: float = 0) -> dict:
    """{gate, vision, critiques} bound to this episode's heads (critiques
    without a revision attach to every later episode of the task)."""
    out = {"gate": [], "vision": [], "critiques": []}
    for e in sorted(task_labels(ev, task_id), key=lambda e: e["timestamp"]):
        f = e["facts"]
        rev = e["source_revision"]
        if f["label"] == "critique":
            if (rev and any(_same(rev, h) for h in heads)) or (not rev and
                                                               e["timestamp"] >= first_ts):
                out["critiques"].append(summarize(e))
            continue
        if not any(_same(rev, h) for h in heads):
            continue
        out["gate" if f["label"] == "gate_verdict" else "vision"].append(summarize(e))
    for human in (e for e in ev if e["event_type"] == "human_correction" and task_id
                  and e["task_id"] == task_id and e["part"] == "human"
                  and not str(e["facts"].get("by") or "").startswith("model:")
                  and e["timestamp"] >= first_ts):
        out["critiques"].append({"source": "critique", "kind": "rejection",
                                 "by": human["facts"].get("by"), "ts": human["timestamp"],
                                 "event_id": human["event_id"], "head": None,
                                 "findings": [{"n": i + 1, "text": str(x)} for i, x in
                                              enumerate(human["facts"].get("findings") or [])]})
    return out


def label_artifacts(labels: dict) -> list[dict]:
    """Capture artifacts of the gate / vision labels (multimodal linking)."""
    out, seen = [], set()
    for g in labels.get("gate") or []:
        for c in g.get("captures") or []:
            key = (c.get("path"), c.get("sha256"))
            if key in seen:
                continue
            seen.add(key)
            out.append({"source": "gate", "head": g["head"], "path": c.get("path"),
                        "story": c.get("story"), "size": c.get("size"),
                        "status": c.get("status"), "sha256": c.get("sha256"),
                        "refs": [{k: c.get(k) for k in ("sha256", "mime", "width", "height")}]
                        if c.get("sha256") else []})
    for v in labels.get("vision") or []:
        for fr in v.get("frames") or []:
            key = (fr.get("path"), fr.get("sha256"))
            if key in seen:
                continue
            seen.add(key)
            out.append({"source": "vision", "head": v["head"], "path": fr.get("path"),
                        "status": fr.get("status"), "sha256": fr.get("sha256"),
                        "refs": [{"sha256": fr["sha256"]}] if fr.get("sha256") else []})
    return out


# ---------------------------------------------------------------- tags

def plan_tags(workspace: str, task_id: Optional[str], extra_widgets=()) -> list[str]:
    if not (workspace and task_id):
        return []
    path = os.path.join(workspace, "docs", "progress", f"{task_id}_PLAN.md")
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            text = f.read(200_000)
    except OSError:
        return []
    tags = {f"template:T{n}" for n in _TEMPLATE.findall(text)}
    for name in (*KNOWN_WIDGETS, *extra_widgets):
        if re.search(rf"(?<![\w]){re.escape(name)}(?![\w])", text):
            tags.add(f"widget:{name}")
            for t, tname in TEMPLATES.items():
                if tname == name:
                    tags.add(f"template:{t}")
    if re.search(r"UI-v3|ui_v3|au-ui-v3", text):
        tags.add("ui_v3")
    return sorted(tags)


def episode_tags(ep: dict, cfg, labels: dict, arts: list[dict]) -> list[str]:
    tags = set(plan_tags(cfg.workspace, ep.get("task_id"),
                         (getattr(cfg, "tags", None) or {}).get("widgets") or ()))
    if ep.get("task_id") and ep["task_id"] in (getattr(cfg, "fast_lane_tasks", None) or []):
        tags.add("fast_lane")
    paths = [a.get("path") or "" for a in arts]
    if any(p.replace("\\", "/").startswith(UI_V3_ROOT) for p in paths):
        tags.add("ui_v3")
    for a in arts:
        size = a.get("size") or next(iter(_SIZE.findall(a.get("path") or "")), None)
        if size:
            tags.add(f"device:{size}")
    if any(m.get("author", {}).get("kind") == "helper" for m in ep.get("messages") or []):
        tags.add("helper_steps")
    return sorted(tags)


# ---------------------------------------------------------------- authorship

def attributions(ev: list[dict], task_id: Optional[str], workspace: str = "") -> list[dict]:
    """Non-local code recorded for this task (prework patches) and for the
    project (helper-tagged commits, docs/progress/*_PRECODE files), with the
    block hashes captured at ingest for content matching. An attribution
    whose content is gone (`verifiable` False) makes the task's episodes
    unverifiable: they are excluded rather than trusted (fail closed)."""
    best: dict = {}
    for e in ev:
        if e["event_type"] != "authorship":
            continue
        f = e["facts"]
        if e["task_id"]:
            if e["task_id"] != task_id:
                continue
        elif not str(f.get("basis") or "").startswith(("helper-commit", "precode")):
            continue
        blocks = set(f.get("blocks") or [])
        small = [{"path": str(x.get("path") or ""), "lines": set(x.get("lines") or [])}
                 for x in f.get("small") or [] if isinstance(x, dict)]
        text = f.get("patch_text") or ""
        if not (blocks or small) and text:
            blocks, small = _hunk_index(text)
        if not (blocks or small) and not text and f.get("patch") and workspace:
            # last resort (older events): the tmp file, read whole, never truncated
            full = os.path.realpath(os.path.join(workspace, f["patch"]))
            if full.startswith(os.path.realpath(workspace) + os.sep) and os.path.isfile(full):
                with open(full, encoding="utf-8", errors="replace") as fh:
                    text = fh.read()
                blocks, small = _hunk_index(text)
        has_content = bool(blocks or small or text.strip())
        verifiable = has_content or not f.get("patch")
        a = {"author": f["author"], "basis": f["basis"], "patch": f.get("patch") or "",
             "paths": [p.replace("\\", "/") for p in f.get("paths") or []],
             "blocks": blocks, "small": small, "verifiable": verifiable,
             "event_id": e["event_id"]}
        key = (a["author"], a["basis"], a["patch"] or e["event_id"])
        if key not in best or (verifiable and not best[key]["verifiable"]):
            best[key] = a
    return list(best.values())


# ---- content blocks. A step that contains a contiguous block of a helper hunk
# (>= BLOCK_LINES non-trivial lines or >= BLOCK_CHARS characters, whitespace
# normalized) is helper-authored as a whole, whatever share of the step the
# block is (a whole-file write that embeds a helper patch included).

BLOCK_LINES = 3
BLOCK_CHARS = 200
_TRIVIAL = {"end", "else", "then", "do", "begin", "return", "break", "continue", "pass",
            "end)", "end,", "end;", "end),", "});", "})", "},", "];", "local"}
HELPER_TAG = re.compile(r"\(helper:([A-Za-z0-9_.\-]+)\)")


def _norm(line: str) -> str:
    return re.sub(r"\s+", " ", line).strip()


def _nontrivial(n: str) -> bool:
    return sum(ch.isalnum() for ch in n) >= 4 and n.lower() not in _TRIVIAL


def _h(lines) -> str:
    import hashlib
    return hashlib.sha256("\n".join(lines).encode("utf-8", "replace")).hexdigest()[:20]


def _windows(lines: list[str], keep=None) -> set:
    """Hashes of the minimal window starting at each line that reaches
    BLOCK_LINES lines or BLOCK_CHARS characters. The same lines in the same
    order give the same window, so a block is found anywhere inside a larger
    write. keep(i, j) filters windows (helper side: it must hold an added
    line, so a rewrite of untouched context is not a match)."""
    out = set()
    for i in range(len(lines)):
        chars = 0
        for j in range(i, len(lines)):
            chars += len(lines[j])
            if j - i + 1 >= BLOCK_LINES or chars >= BLOCK_CHARS:
                if keep is None or keep(i, j):
                    out.add(_h(lines[i:j + 1]))
                break
    return out


_DIFF = re.compile(r"^(diff --git |--- |\+\+\+ |@@ |\*\*\* (Begin Patch|Add File|Update File))",
                   re.M)


def _is_diff(text: str) -> bool:
    return bool(_DIFF.search(text))


def _hunks(text: str) -> list:
    """[(path, [(normalized new-side line, added?)])] per hunk. A text that is
    not a diff (a pre-code model file) is one hunk of added lines."""
    if not _is_diff(text):
        return [("", [(n, True) for n in (_norm(l) for l in text.splitlines())
                      if _nontrivial(n)])]
    out, state = [], {"path": "", "cur": []}

    def flush():
        if any(a for _, a in state["cur"]):
            out.append((state["path"], state["cur"]))
        state["cur"] = []
    for l in text.splitlines():
        if l.startswith(("+++ ", "*** Add File:", "*** Update File:")):
            flush()
            raw = (l.split(":", 1)[1] if l.startswith("***") else l[4:]).strip()
            state["path"] = (raw[2:] if raw.startswith(("a/", "b/")) else raw).replace("\\", "/")
            continue
        if l.startswith(("@@", "diff --git", "index ", "--- ", "***")):
            flush()
            continue
        if l.startswith(("+", " ")):
            n = _norm(l[1:])
            if _nontrivial(n):
                state["cur"].append((n, l.startswith("+")))
        elif l.startswith(("-", "\\")):
            continue
        else:                                   # a stray line ends the hunk
            flush()
    flush()
    return out


def _hunk_index(text: str) -> tuple:
    """(block hashes, small hunks) of a helper patch / file, over the whole
    text (never truncated). A hunk too small to form a block keeps its added
    lines' hashes and matches only a write to the same path."""
    blocks, small = set(), []
    for path, seq in _hunks(text):
        lines = [n for n, _ in seq]
        added = [a for _, a in seq]
        w = _windows(lines, keep=lambda i, j: any(added[i:j + 1]))
        if w:
            blocks |= w
        elif any(added):
            small.append({"path": path, "lines": {_h([n]) for n, a in seq if a}})
    return blocks, small


def index_facts(text: str) -> dict:
    """What the ingest stores for a helper patch: block hashes (any size),
    small hunks and whether content was present."""
    blocks, small = _hunk_index(text or "")
    return {"blocks": sorted(blocks),
            "small": [{"path": s["path"], "lines": sorted(s["lines"])} for s in small],
            "content": "present" if (text or "").strip() else "missing",
            "chars": len(text or "")}


def _strings(o):
    if isinstance(o, str):
        yield o
    elif isinstance(o, dict):
        for v in o.values():
            yield from _strings(v)
    elif isinstance(o, list):
        for v in o:
            yield from _strings(v)


def _step_lines(s: str) -> list:
    """Normalized non-trivial line runs of a string a step wrote: the new
    side of a diff, the REPLACE side of SEARCH/REPLACE blocks (Cline), else
    every line (a whole-file write, a shell heredoc, pasted text)."""
    if "<<<<<<< SEARCH" in s and ">>>>>>> REPLACE" in s:
        out, cur, on = [], [], False
        for l in s.splitlines():
            t = l.strip()
            if t.startswith("======="):
                on, cur = True, []
            elif t.startswith(">>>>>>> REPLACE"):
                on = False
                out.append(cur)
            elif on:
                n = _norm(l)
                if _nontrivial(n):
                    cur.append(n)
        return out
    if _is_diff(s):
        return [[n for n, _ in seq] for _, seq in _hunks(s)]
    return [[n for n in (_norm(l) for l in s.splitlines()) if _nontrivial(n)]]


def _path_of(args: dict) -> str:
    for k in ("filePath", "file_path", "path", "target_file"):
        if isinstance(args.get(k), str):
            return args[k].replace("\\", "/")
    return ""


def _content_hit(texts: list, target: str, a: dict) -> bool:
    if a["blocks"]:
        for s in texts:
            for seq in _step_lines(s):
                if _windows(seq) & a["blocks"]:
                    return True
    if a["small"] and target:
        mine = {_h([n]) for s in texts for seq in _step_lines(s) for n in seq}
        for sm in a["small"]:
            if sm["lines"] and sm["lines"] <= mine and sm["path"] and \
                    (target.endswith(sm["path"]) or sm["path"].endswith(target)):
                return True
    return False


def step_author(tool_calls: list[dict], attrs: list[dict], content: str = "") -> Optional[dict]:
    """The attribution a step (its tool calls and its text) carries out, if
    any. Conservative: applying or naming the helper patch, writing or
    copying a harvested model, a `(helper:<unit>)` tag, or any contiguous
    helper block anywhere in what the step wrote."""
    for tc in tool_calls or []:
        name = str(tc.get("name") or "").lower()
        args = tc.get("arguments") if isinstance(tc.get("arguments"), dict) else {}
        blob = json.dumps(args, ensure_ascii=False).replace("\\\\", "/")
        tag = HELPER_TAG.search(blob)
        if tag:
            return {"author": f"helper:{tag.group(1)}", "basis": "helper-tagged commit",
                    "ref": tag.group(0), "tool": name}
        for a in attrs:
            if a["patch"] and (a["patch"] in blob or os.path.basename(a["patch"]) in blob):
                return {"author": a["author"], "basis": "applied " + a["basis"],
                        "ref": a["patch"], "tool": name}
            if a["basis"].startswith("precode") and a["paths"]:
                target = _path_of(args)
                if name in ("write", "write_to_file", "create_file") and \
                        any(target.endswith(p) for p in a["paths"]):
                    return {"author": a["author"], "basis": "wrote a harvested model",
                            "ref": target, "tool": name}
                cmd = str(args.get("command") or "").replace("\\", "/")
                if cmd and COPY_CMD.search(cmd) and any(p in cmd for p in a["paths"]):
                    return {"author": a["author"], "basis": "copied a harvested model",
                            "ref": next(p for p in a["paths"] if p in cmd), "tool": name}
            if _content_hit(list(_strings(args)), _path_of(args), a):
                return {"author": a["author"], "basis": a["basis"] + " content",
                        "ref": a["patch"] or (a["paths"][0] if a["paths"] else ""),
                        "tool": name}
    if content:
        tag = HELPER_TAG.search(content)
        if tag:
            return {"author": f"helper:{tag.group(1)}", "basis": "helper-tagged text",
                    "ref": tag.group(0), "tool": ""}
        for a in attrs:
            if _content_hit([content], "", a):
                return {"author": a["author"], "basis": a["basis"] + " content (text)",
                        "ref": a["patch"], "tool": ""}
    return None


def diff_has_helper(patch: str, attrs: list[dict]) -> bool:
    """A revision diff that contains a helper block (or a small helper hunk
    on its own path)."""
    if not patch or not attrs:
        return False
    for path, seq in _hunks(patch):
        lines = [n for n, _ in seq]
        mine = {_h([n]) for n, a in seq if a}
        for a in attrs:
            if a["blocks"] and _windows(lines) & a["blocks"]:
                return True
            if path and any(sm["lines"] and sm["lines"] <= mine and sm["path"] and
                            (path.endswith(sm["path"]) or sm["path"].endswith(path))
                            for sm in a["small"]):
                return True
    return False


def attribute_steps(messages: list[dict], attrs: list[dict]) -> list[dict]:
    """Re-attribute local assistant turns that carried out frontier code.
    Returns [{index, author, basis, ref}] (also recorded in flags)."""
    out = []
    for i, m in enumerate(messages):
        if m["role"] != "assistant" or (m.get("author") or {}).get("kind") != "local":
            continue
        hit = step_author(m.get("tool_calls") or [], attrs, m.get("content") or "")
        if hit:
            m["author"] = {"kind": "helper", "model": hit["author"],
                           "written_by": m["author"].get("model"), "basis": hit["basis"]}
            out.append({"index": i, **hit})
    return out


def is_target(m: dict, allow_frontier: bool) -> bool:
    """Whether an assistant turn may be a training target. Tool, user and
    system turns are never targets; with allow_frontier_targets = false only
    the local model's own turns are."""
    if m.get("role") != "assistant":
        return False
    kind = (m.get("author") or {}).get("kind")
    return kind == "local" or (allow_frontier and kind not in ("tool", None))


# ---------------------------------------------------------------- inline corrections

def _finding_texts(label: dict) -> list[str]:
    if label["source"] == "gate_verdict":
        return [str(c.get("id") or "") for c in label.get("failed_checks") or [] if c.get("id")]
    return [str(f.get("text") or "")[:60] for f in label.get("findings") or [] if f.get("text")]


def red(label: dict) -> bool:
    return (label["source"] == "gate_verdict" and label.get("result") == "FAIL") or \
        (label["source"] == "vision_verdict" and label.get("verdict") == "CHANGES")


def inline_corrections(ep: dict, project_id: str, diff_fn=None) -> list[dict]:
    """(local attempt -> gate / vision findings -> corrected local code) inside
    one session: a red label at an earlier head, a later user turn that
    carries its findings (Builder B sends them into the SAME session), and the
    turns after it. The corrected part is the target; the attempt and the
    findings are context."""
    labels = ep.get("labels") or {}
    head = (ep.get("revision") or {}).get("head")
    msgs = ep.get("messages") or []
    out = []
    reds = sorted([l for l in (labels.get("gate") or []) + (labels.get("vision") or [])
                   if red(l) and not _same(l["head"], head)], key=lambda l: l["ts"])
    first_user = next((i for i, m in enumerate(msgs) if m["role"] == "user"), None)
    for lab in reds:
        texts = [t for t in _finding_texts(lab) if t]
        if not texts:
            continue
        idx = next((i for i, m in enumerate(msgs) if m["role"] == "user" and i != first_user
                    and any(t in (m.get("content") or "") for t in texts)), None)
        if idx is None or not any(m["role"] == "assistant" for m in msgs[idx + 1:]) or \
                not any(m["role"] == "assistant" for m in msgs[:idx]):
            continue
        if lab["source"] == "gate_verdict":
            items = [f"{c['id']}: {c.get('result')} {c.get('detail') or ''}".strip()
                     for c in lab["failed_checks"]]
            fb_author = {"kind": "coordinator", "model": "studio_gate"}
        else:
            items = [f"{f['n']}. {f['text']}" for f in lab["findings"]]
            fb_author = {"kind": "frontier", "model": lab.get("persona")}
        diff, cut = (diff_fn(lab["head"], head) if (diff_fn and head and lab["head"] != head)
                     else ("", False))
        attempt_authors = sorted({(m["author"]["kind"], m["author"].get("model") or "")
                                  for m in msgs[:idx] if m["role"] == "assistant"})
        fix_authors = sorted({(m["author"]["kind"], m["author"].get("model") or "")
                              for m in msgs[idx + 1:] if m["role"] == "assistant"})
        flags = dict(ep["flags"], authors=[list(a) for a in fix_authors],
                     attempt_authors=[list(a) for a in attempt_authors])
        out.append({
            "episode_id": stable_id(project_id, "correction-inline", ep["episode_id"],
                                    lab["event_id"]),
            "project_id": project_id, "task_id": ep["task_id"], "kind": "correction",
            "group_id": ep["group_id"], "session_id": ep["session_id"], "run_id": ep["run_id"],
            "attempt": ep["attempt"], "inline": True, "context_until": idx,
            "original": {"episode_id": ep["episode_id"],
                         "revision": {"base": ep["revision"]["base"], "head": lab["head"]},
                         "final_message": next((m for m in reversed(msgs[:idx])
                                                if m["role"] == "assistant"), None),
                         "outcome": {"label": lab}, "authors": [list(a) for a in attempt_authors]},
            "feedback": {"source": "gate" if lab["source"] == "gate_verdict" else "vision",
                         "items": items, "author": fb_author, "message_index": idx,
                         "label_event": lab["event_id"]},
            "correction": {"episode_id": ep["episode_id"], "messages": msgs, "tools": ep["tools"],
                           "revision": {"base": lab["head"], "head": head},
                           "diff": {"patch": diff, "truncated": cut,
                                    "helper_authored": bool(
                                        (ep.get("diff") or {}).get("helper_authored")
                                        or ep["flags"].get("helper_steps"))}},
            "messages": msgs, "tools": ep["tools"], "context_source": ep["context_source"],
            "revision": {"base": lab["head"], "head": head}, "outcome": ep["outcome"],
            "flags": flags, "labels": labels, "tags": ep.get("tags") or [],
            "sources": ep["sources"], "event_ids": sorted(set(ep["event_ids"]) |
                                                          {lab["event_id"]}),
            "first_ts": ep["first_ts"],
            "content_hash": content_hash(["correction-inline", ep["content_hash"],
                                          lab["event_id"]]),
        })
    return out
