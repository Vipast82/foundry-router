"""Build replay: one journal entry per Blender run (docs/plans/BLENDER_BUILD_REPLAY_PLAN.md R1).

An entry keeps what is needed to see how a model got where it is, never the big outputs:
  - where it ran: project, task, run, worktree, slot/host, commit + uncommitted files sent;
  - what ran: the Blender arguments, the builder script (-P/--python) with its sha256, a stored
    copy (<= 256 KB) and a diff from the previous run of the same script on this task;
  - timings, the exit code, the files that came back (names and sizes);
  - a 200-line log tail plus the lines that matter (mesh_report, REF_IOU, PASS/FAIL, ERROR, Saved);
  - up to N render thumbnails (Pillow; without it only names and sizes).
Files live under state_dir/build_journal/<project>/<task>/<run id>/; one row per run in the
coordinator store's `build_runs` table. Writing never raises into the caller: a Blender run's
exit code is never changed by its journal.

Settings ([blender] in the project profile, UI fields in foundry_automation/schema.py):
  journal (off), journal_renders (12), journal_thumb_px (256), journal_keep_days (30),
  journal_max_runs (200). `sweep` prunes FINISHED tasks only: by age, then to the newest N runs.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import shutil
import sqlite3
import time
import uuid
from typing import Optional

DEFAULTS = {"journal": False, "journal_renders": 12, "journal_thumb_px": 256,
            "journal_keep_days": 30, "journal_max_runs": 200,
            # R3: Blender on this PC, read from the sprint's transcript
            "journal_local": True, "journal_watch_seconds": 20,
            "journal_tools": ["selftest_all.py"]}
LOG_TAIL = 200                    # log lines kept
LOG_CAP = 40_000                  # log tail characters kept
KEY_LINES = 60                    # lines that matter kept
DIFF_LINES = 400                  # script diff lines kept
SCRIPT_CAP = 256 * 1024           # script copy kept up to this size
THUMB_CAP = 40_000                # one thumbnail's bytes at most
FILES_CAP = 400                   # files-back names kept
SWEEP_EVERY_S = 3600
ROOT = "build_journal"
DDL = ("CREATE TABLE IF NOT EXISTS build_runs (run_id TEXT PRIMARY KEY, project_id TEXT, "
       "task_id TEXT, session_id TEXT, source TEXT, ts REAL, data TEXT);"
       "CREATE INDEX IF NOT EXISTS idx_build_runs_task ON build_runs(project_id, task_id, ts);")
_KEY = re.compile(r"mesh_report|REF_IOU|\bPASS\b|\bFAIL\b|\bERROR\b|Error:|Traceback|Saved:|"
                  r"foundry-blender: (running on|blender exit)", re.I)
_VIEW = re.compile(r"front|back|left|right|top|bottom|three|quarter|3q|persp|iso|side|view",
                   re.I)
_SAFE = re.compile(r"[^A-Za-z0-9._-]")
_last_sweep: dict = {}


def settings(profile) -> dict:
    """The journal settings from the profile's [blender] table, typed, with defaults."""
    raw = dict(((getattr(profile, "raw", None) or {}).get("blender") or {}))
    out = dict(DEFAULTS)
    for k, v in DEFAULTS.items():
        x = raw.get(k, v)
        if isinstance(v, list):
            out[k] = [str(i) for i in x] if isinstance(x, list) else list(v)
            continue
        try:
            out[k] = bool(x) if isinstance(v, bool) else max(0, int(x))
        except (TypeError, ValueError):
            out[k] = v
    return out


def script_of(args: list) -> str:
    """The builder script a Blender command line runs (-P / --python), '' when none."""
    args = [str(a) for a in args or []]
    for i, a in enumerate(args):
        if a == "--":
            break
        if a in ("-P", "--python") and i + 1 < len(args):
            return args[i + 1].strip().strip('"\'')
        if a.startswith("--python="):
            return a.split("=", 1)[1].strip().strip('"\'')
    return ""


def key_lines(lines: list) -> list:
    return [ln[:300] for ln in lines if _KEY.search(ln)][-KEY_LINES:]


def pick_renders(paths: list, n: int) -> list:
    """PNG paths, render views first (by name), at most n, in a stable order."""
    pngs = [p for p in paths if str(p).lower().endswith(".png")]
    pngs.sort(key=lambda p: (0 if _VIEW.search(os.path.basename(str(p))) else 1, str(p)))
    return pngs[:max(0, int(n))]


def thumbnail(src: str, dest: str, px: int) -> bool:
    """A small PNG (or JPEG when the PNG is over THUMB_CAP) of `src` at `dest`; False without
    Pillow or for an unreadable image."""
    try:
        import io
        from PIL import Image
    except ImportError:
        return False
    try:
        with Image.open(src) as im:
            im.thumbnail((max(16, px), max(16, px)))
            buf = io.BytesIO()
            im.save(buf, "PNG", optimize=True)
            data = buf.getvalue()
            if len(data) > THUMB_CAP:
                buf = io.BytesIO()
                im.convert("RGB").save(buf, "JPEG", quality=80)
                data = buf.getvalue()
        if len(data) > THUMB_CAP:
            return False
        with open(dest, "wb") as f:
            f.write(data)
        return True
    except Exception:                                             # noqa: BLE001
        return False


def run_dir(state_dir: str, project_id: str, task_id: str, run_id: str) -> str:
    return os.path.join(state_dir, ROOT, _SAFE.sub("-", project_id or "_"),
                        _SAFE.sub("-", task_id or "_none"), _SAFE.sub("-", run_id))


def _db(state_dir: str) -> sqlite3.Connection:
    conn = sqlite3.connect(os.path.join(state_dir, "coordinator.sqlite"), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=10000")
    conn.executescript(DDL)
    return conn


def _previous(conn, state_dir: str, pid: str, tid: str, script: str) -> tuple[Optional[str], str]:
    """(the previous run's copy of `script` on this task, its run id)."""
    for r in conn.execute("SELECT run_id, data FROM build_runs WHERE project_id=? AND task_id=? "
                          "ORDER BY ts DESC LIMIT 50", (pid, tid)):
        try:
            d = json.loads(r["data"] or "{}")
        except ValueError:
            continue
        s = d.get("script") or {}
        if s.get("path") == script and s.get("copy"):
            try:
                with open(os.path.join(run_dir(state_dir, pid, tid, r["run_id"]), "script.txt"),
                          encoding="utf-8", errors="replace", newline="") as f:
                    return f.read(), r["run_id"]
            except OSError:
                return None, r["run_id"]
    return None, ""


def diff(old: str, new: str, path: str) -> tuple[str, int]:
    """(a unified diff capped at DIFF_LINES, the full diff's line count)."""
    lines = list(difflib.unified_diff(old.splitlines(), new.splitlines(), f"a/{path}",
                                      f"b/{path}", lineterm="", n=2))
    out = lines[:DIFF_LINES]
    if len(lines) > DIFF_LINES:
        out.append(f"... {len(lines) - DIFF_LINES} more diff lines")
    return "\n".join(out), len(lines)


def record(state_dir: str, *, project_id: str, task_id: str, source: str, repo: str,
           args: list, exit_code, started_ts: float, ended_ts: float, log_lines: list,
           files_back: Optional[list] = None, renders: Optional[list] = None,
           timings: Optional[dict] = None, where: Optional[dict] = None,
           session_id: str = "", run_id: str = "", opts: Optional[dict] = None,
           notes: Optional[list] = None, keys: Optional[list] = None) -> Optional[dict]:
    """Write one entry (files + row). `renders` are repo-relative PNG paths (default: the PNGs
    in `files_back`). Returns the entry, or None when nothing could be written."""
    try:
        return _record(state_dir, project_id, task_id, source, repo, args, exit_code,
                       started_ts, ended_ts, log_lines, files_back, renders, timings, where,
                       session_id, run_id, {**DEFAULTS, **(opts or {})}, notes, keys)
    except Exception:                                             # noqa: BLE001
        return None


def _record(state_dir, pid, tid, source, repo, args, exit_code, started_ts, ended_ts,
            log_lines, files_back, renders, timings, where, session_id, run_id, opts, notes,
            keys=None):
    run_id = _SAFE.sub("-", run_id or "") or \
        f"{source}-{time.strftime('%Y%m%d-%H%M%S', time.localtime(started_ts or time.time()))}-" \
        f"{uuid.uuid4().hex[:6]}"
    d = run_dir(state_dir, pid, tid, run_id)
    os.makedirs(os.path.join(d, "thumbs"), exist_ok=True)
    conn = _db(state_dir)
    try:
        entry = {"run_id": run_id, "project_id": pid, "task_id": tid, "session_id": session_id,
                 "source": source, "started_ts": started_ts, "ended_ts": ended_ts,
                 "seconds": round(max(0.0, (ended_ts or 0) - (started_ts or 0)), 1),
                 "exit": exit_code, "args": [str(a) for a in args or []][:80],
                 "where": dict(where or {}), "timings": dict(timings or {}),
                 "notes": list(notes or [])}
        # the builder script
        script = script_of(args)
        if script:
            full = os.path.join(repo, *script.replace("\\", "/").split("/")) \
                if not os.path.isabs(script) else script
            rel = os.path.relpath(os.path.realpath(full), os.path.realpath(repo)) \
                if repo else full
            inside = not rel.startswith("..") and not os.path.isabs(rel)
            s = {"path": (rel if inside else script).replace("\\", "/")}
            try:
                with open(full, "rb") as f:
                    raw = f.read()
                s["sha256"] = hashlib.sha256(raw).hexdigest()
                s["bytes"] = len(raw)
                s["mtime"] = os.path.getmtime(full)
                if ended_ts and s["mtime"] > ended_ts + 1:
                    s["edited_after_run"] = True
                if len(raw) <= SCRIPT_CAP:
                    text = raw.decode("utf-8", "replace")
                    with open(os.path.join(d, "script.txt"), "w", encoding="utf-8",
                              newline="") as f:
                        f.write(text)
                    s["copy"] = True
                    old, prev = _previous(conn, state_dir, pid, tid, s["path"])
                    if prev:
                        s["previous_run"] = prev
                    if old is not None:
                        s["diff"], s["diff_lines"] = diff(old, text, s["path"])
                        s["changed"] = old != text
                    else:
                        s["first"] = not prev
            except OSError as e:
                s["error"] = f"unreadable: {type(e).__name__}"
            entry["script"] = s
        # outputs
        back = [str(x).replace("\\", "/") for x in (files_back or [])]
        sizes = []
        for p in back[:FILES_CAP]:
            try:
                sizes.append([p, os.path.getsize(os.path.join(repo, *p.split("/")))])
            except OSError:
                sizes.append([p, None])
        entry["files_back"] = sizes
        entry["files_back_total"] = len(back)
        # the log
        lines = [str(x).rstrip("\r\n") for x in (log_lines or [])]
        tail = "\n".join(lines[-LOG_TAIL:])[-LOG_CAP:]
        with open(os.path.join(d, "log_tail.txt"), "w", encoding="utf-8", newline="") as f:
            f.write(tail)
        entry["log_tail"] = tail
        entry["key_lines"] = key_lines(keys if keys is not None else lines)
        # renders
        want = renders if renders is not None else back
        shots = []
        for i, p in enumerate(pick_renders(want, opts.get("journal_renders", 12))):
            rel = str(p).replace("\\", "/")
            src = os.path.join(repo, *rel.split("/")) if not os.path.isabs(rel) else rel
            item = {"path": rel}
            try:
                item["bytes"] = os.path.getsize(src)
            except OSError:
                item["missing"] = True
                shots.append(item)
                continue
            name = f"{i:02d}.png"
            if thumbnail(src, os.path.join(d, "thumbs", name),
                         int(opts.get("journal_thumb_px", 256))):
                item["thumb"] = f"thumbs/{name}"
            shots.append(item)
        entry["renders"] = shots
        with open(os.path.join(d, "journal.json"), "w", encoding="utf-8") as f:
            json.dump(entry, f, indent=1)
        conn.execute("INSERT OR REPLACE INTO build_runs (run_id, project_id, task_id, session_id, "
                     "source, ts, data) VALUES (?,?,?,?,?,?,?)",
                     (run_id, pid, tid, session_id, source, started_ts or time.time(),
                      json.dumps(entry)))
        conn.commit()
        return entry
    finally:
        conn.close()


def runs(state_dir: str, project_id: str, task_id: str) -> list:
    conn = _db(state_dir)
    try:
        return [json.loads(r["data"] or "{}") for r in conn.execute(
            "SELECT data FROM build_runs WHERE project_id=? AND task_id=? ORDER BY ts",
            (project_id, task_id))]
    finally:
        conn.close()


def sweep(engine, P, force: bool = False) -> int:
    """Prune finished tasks' journals (hourly): a task finished more than keep_days ago loses
    its whole journal; other finished tasks keep their newest max_runs runs. A task that is not
    finished is never pruned. Returns the runs removed."""
    now = time.time()
    if not force and now - _last_sweep.get(P.id, 0) < SWEEP_EVERY_S:
        return 0
    _last_sweep[P.id] = now
    from .states import TERMINAL
    o = settings(P.profile)
    keep_s = max(1, o["journal_keep_days"]) * 86400
    cap = max(1, o["journal_max_runs"])
    sd = engine.state_dir
    if not os.path.isfile(os.path.join(sd, "coordinator.sqlite")):
        return 0
    conn = _db(sd)
    removed = 0
    try:
        tids = [r[0] for r in conn.execute(
            "SELECT DISTINCT task_id FROM build_runs WHERE project_id=?", (P.id,))]
        for tid in tids:
            t = engine.store.task(P.id, tid) if tid else None
            if not t or t.get("state") not in TERMINAL:
                continue
            rows = [r[0] for r in conn.execute(
                "SELECT run_id FROM build_runs WHERE project_id=? AND task_id=? ORDER BY ts DESC",
                (P.id, tid))]
            old = rows if now - float(t.get("updated_ts") or now) > keep_s else rows[cap:]
            for rid in old:
                conn.execute("DELETE FROM build_runs WHERE run_id=?", (rid,))
                shutil.rmtree(run_dir(sd, P.id, tid, rid), ignore_errors=True)
                removed += 1
        conn.commit()
    finally:
        conn.close()
    return removed


# ----------------------------------------------------------------------------- R3: this PC

# a Blender start on this PC, as sprints write it (2026-11-01, 400 transcript calls):
#   $b="C:\...\blender.exe"; & $b -b ... -P tools/blender/x.py -- ...
#   py -3 tools\guard\run.py --name x -- "$b" -b --factory-startup -P ...
#   & "C:\Program Files\Blender Foundation\Blender 4.5\blender.exe" -b ...
#   blender -b scene.blend -P ...
_EXE = re.compile(r"""blender\.exe|(?:^|[\s&;'"(])blender["']?\s+-""", re.I)
# Blender's flags are case-sensitive, and `-Path` / `-Pattern` (PowerShell) are not `-P`
_RUNFLAG = re.compile(r"(?:^|\s)(?:-b|--background|-P|--python)(?=\s|=|$)")
_GUARD_LOG = re.compile(r"^LOG (\S+\.log)\s*$", re.M)
_DROPIN = re.compile(r"\.foundry-blender|slots\s+blender", re.I)
_VAR = re.compile(r"""^["']?\$\w+["']?$""")
_STOP = {";", "|", "&&", "||", "2>&1", "|&", ">", ">>", "1>&2"}
_pending: dict = {}                    # session id -> last observe time
_final: set = set()                    # sessions observed after they settled
WALK_CAP = 4000                        # files looked at per run when finding its renders


def is_local_blender(cmd: str, tools: list) -> bool:
    """A command that starts Blender on this PC (not through foundry-blender, which journals
    its own runs), or runs one of the tools that start Blender themselves."""
    if not cmd or _DROPIN.search(cmd):
        return False
    if _EXE.search(cmd) and _RUNFLAG.search(cmd):
        return True
    return any(t and re.search(r"(?:^|[\\/\s])" + re.escape(t) + r"(?:\s|$|\")", cmd)
               for t in tools)


def blender_args(cmd: str) -> list:
    """The arguments after the Blender executable in a command line (quotes taken off), cut at
    the first shell separator; [] when no executable token is found."""
    import shlex
    try:
        toks = shlex.split(cmd.replace("\\", "/"), posix=False)
    except ValueError:
        toks = cmd.replace("\\", "/").split()
    for i, tok in enumerate(toks):
        bare = tok.strip(";").strip("\"'").lower()
        if not (bare.endswith("blender.exe") or bare == "blender" or _VAR.match(tok.strip(";"))):
            continue
        if tok.endswith(";") or (i + 1 < len(toks) and not toks[i + 1].startswith("-")):
            continue                     # `$b="...blender.exe";` sets a variable, it is no call
        out = []
        for nxt in toks[i + 1:]:
            if nxt in _STOP or nxt.startswith(("|", ";")):
                break
            out.append(nxt.strip(";").strip("\"'"))
            if nxt.endswith(";"):
                break
        if out:
            return out
    return []


def guard_log(ws: str, output: str) -> Optional[dict]:
    """The game's guard runner (tools/guard/run.py) prints `LOG <path>` and keeps the whole run
    in that file (CMD/START, the output, EXIT=, END:). None when the output names no log or the
    log is unreadable; {"exit": None, ...} while the run has no EXIT= line yet."""
    m = _GUARD_LOG.search(output or "")
    if not m:
        return None
    rel = m.group(1).replace("\\", "/")
    path = os.path.join(ws, *[x for x in rel.split("/") if x])
    if not os.path.realpath(path).startswith(os.path.realpath(ws) + os.sep):
        return None
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            text = f.read()[-2_000_000:]
    except OSError:
        return None
    lines = text.splitlines()
    out = {"path": rel, "exit": None, "start": None, "end": None, "lines": []}
    seps = [i for i, ln in enumerate(lines) if ln.strip() == "----"]
    body = lines[seps[0] + 1:seps[-1]] if len(seps) >= 2 else \
        lines[seps[0] + 1:] if seps else lines
    out["lines"] = body
    for ln in lines:
        for key, tag in (("start", "START: "), ("end", "END: ")):
            if ln.startswith(tag):
                try:
                    out[key] = time.mktime(time.strptime(ln[len(tag):].strip(),
                                                         "%Y-%m-%d %H:%M:%S"))
                except ValueError:
                    pass
        if ln.startswith("EXIT="):
            try:
                out["exit"] = int(ln[5:].strip())
            except ValueError:
                pass
    return out


def _render_dirs(ws: str, args: list, task_id: str) -> list:
    root = os.path.realpath(ws)
    dirs = []
    for a in args:
        if "/" not in a or a.startswith("-"):
            continue
        p = os.path.join(ws, *[x for x in a.split("/") if x])
        d = p if os.path.isdir(p) else os.path.dirname(p)
        if os.path.isdir(d) and os.path.realpath(d).startswith(root + os.sep):
            dirs.append(d)
    ev = os.path.join(ws, "docs", "evidence", task_id) if task_id else ""
    if ev and os.path.isdir(ev):
        dirs.append(ev)
    out = []
    for d in dirs:
        if d not in out:
            out.append(d)
    return out


def renders_in_window(ws: str, args: list, task_id: str, start: float, end: float) -> list:
    """Repo-relative PNGs written while the call ran (mtime in start-2..end+2), looked for in the
    folders the arguments name and the task's evidence folder."""
    found, seen = [], 0
    for d in _render_dirs(ws, args, task_id):
        for root, subdirs, files in os.walk(d):
            subdirs[:] = [x for x in subdirs if x not in (".git", "__pycache__")]
            for f in files:
                seen += 1
                if seen > WALK_CAP:
                    return found
                if not f.lower().endswith(".png"):
                    continue
                p = os.path.join(root, f)
                try:
                    m = os.path.getmtime(p)
                except OSError:
                    continue
                if start - 2 <= m <= end + 2:
                    rel = os.path.relpath(p, ws).replace(os.sep, "/")
                    if rel not in found:
                        found.append(rel)
    return found


def observe(engine, P, t: dict, sid: str, final: bool = False) -> int:
    """R3: journal a sprint session's Blender runs on this PC, read from its transcript (every
    journal_watch_seconds while it is busy, once more when it settles). Each tool call is
    journaled once (run id `local-<part id>`). Returns the runs added."""
    o = settings(P.profile)
    if not (o["journal"] and o["journal_local"]) or not sid:
        return 0
    now = time.time()
    if final:
        if sid in _final:
            return 0
        _final.add(sid)
    elif now - _pending.get(sid, 0) < max(5, o["journal_watch_seconds"]):
        return 0
    _pending[sid] = now
    ws = str(P.profile.workspace)
    try:
        msgs = P.client.transcript(sid, directory=ws) or []
    except Exception:                                             # noqa: BLE001
        return 0
    return journal_transcript(engine.state_dir, P, t["task_id"], sid, msgs, ws, o, now)


def journal_transcript(state_dir: str, P, task_id: str, sid: str, msgs: list, ws: str,
                       o: dict, now: Optional[float] = None) -> int:
    from .evidence import tool_view
    now = now or time.time()
    conn = _db(state_dir)
    try:
        known = {r[0] for r in conn.execute(
            "SELECT run_id FROM build_runs WHERE project_id=? AND session_id=?", (P.id, sid))}
    finally:
        conn.close()
    added, head = 0, None
    for m in msgs:
        for part in (m.get("parts") or []):
            tv = tool_view(part, session_id=sid)
            if not tv or tv["tool"] not in ("bash", "shell") or \
                    tv["status"] not in ("completed", "error"):
                continue
            key = _SAFE.sub("-", tv["part_id"] or tv["call_id"] or "")[:80]
            rid = "local-" + key
            if not key or rid in known:
                continue
            cmd = tv["input"]
            if not is_local_blender(cmd, o.get("journal_tools") or []):
                continue
            args = blender_args(cmd) or cmd.replace("\\", "/").split()[:40]
            start, end = tv["started_ts"] or now, tv["ended_ts"] or now
            exit_code, lines = tv["exit_code"], (tv["output"] or "").splitlines()
            notes = []
            g = guard_log(ws, tv["output"])
            if g is not None:
                if g["exit"] is None:
                    continue                 # still running in the background: read it later
                exit_code, lines = g["exit"], g["lines"]
                start, end = g["start"] or start, g["end"] or end
                notes.append(f"output, exit and times from the guard log {g['path']}")
            elif "--bg" in cmd.split():
                notes.append("started in the background (guard --bg) and its log was not found: "
                             "the exit and times are the start command's")
            if now - end > 60:
                notes.append(f"journaled {int(now - end)} s after the run: renders written "
                             "since then are not caught, and the script may have changed")
            if head is None:
                try:
                    head = P.ws.head()
                except Exception:                                 # noqa: BLE001
                    head = ""
            e = record(state_dir, project_id=P.id, task_id=task_id, source="local", repo=ws,
                       args=args, exit_code=exit_code, started_ts=start, ended_ts=end,
                       log_lines=lines, files_back=[],
                       renders=renders_in_window(ws, args, task_id, start, end),
                       timings={"blender": round(max(0.0, end - start), 1)},
                       where={"machine": "this PC", "worktree": ws, "commit": head,
                              "command": cmd[:600]},
                       session_id=sid, run_id=rid, opts=o, notes=notes)
            if e:
                known.add(rid)
                added += 1
    return added


__all__ = ["DEFAULTS", "settings", "script_of", "key_lines", "pick_renders", "record", "runs",
           "sweep", "run_dir", "observe", "journal_transcript", "is_local_blender",
           "blender_args"]
