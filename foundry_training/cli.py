"""Command line for the training-data pipeline. Nothing here starts training.
`auto` uploads its redacted datasets to the configured Foundry router only
(`[foundry] url`, `[training.auto] upload`); every other command is local.

  python -m foundry_training [--store DIR] [--profiles PATH ...] <command>

Store: --store, $FOUNDRY_TRAINING_STORE or ~/.foundry-training (must not be
inside a git work tree unless ignored). Profiles: the same project.toml files
as the coordinator (only their [training] section and workspace are used);
--profiles or $FOUNDRY_TRAINING_PROFILES / $FOUNDRY_COORD_PROFILES.

  ingest kilo --export FILE [FILE ...]          `kilo export` files (preferred)
  ingest kilo --url URL --session ID [...]      a running `kilo serve` (follows children)
  ingest kilo --db PATH [--directory DIR]       Kilo's DB, read-only snapshot
  ingest foundry --url URL                      Foundry capture (key: $FOUNDRY_TRAINING_KEY)
  ingest foundry --capture-db PATH              or Foundry's capture.sqlite, read-only
  ingest coordinator --db PATH                  the coordinator's store, read-only
  ingest cline --sessions DIR                   Cline session folders
  ingest git --project P                        commits referenced by the project's records
  ingest gate --project P                       studio_gate verdict JSON (labels + captures)
  queue --spool DIR [--foundry-url U] [--kilo-url U]   coordinator closeout manifests
  curate --project P [--map SESSION=TASK ...]
  export --project P [...] --kind sft|correction|preference|multimodal --target canonical|chatml-tools|dpo
         [--purpose train|eval] [--tokenizer tokenizer.json]
  validate DATASET_DIR --target T
  status [--json]   inspect EPISODE   gaps [--project P]
  prefer --project P --chosen EVENT --rejected EVENT --by NAME --note TEXT
  forget EPISODE --reason TEXT [--sources]   retention   push --foundry-url U [--id NAME]
  recover                                        report interrupted ingestion runs
"""

from __future__ import annotations

import argparse
import json
import os
import time
import sys

from . import config as TC
from .pipeline import Pipeline
from .store import StoreError, TrainingStore


def _known_secrets() -> set:
    import re
    vals = set()
    for k, v in os.environ.items():
        if re.search(r"(KEY|TOKEN|SECRET|PASSWORD|PASSWD)", k, re.I) and v and len(v) >= 6:
            vals.add(v)
    return vals


def build(a) -> Pipeline:
    root = a.store or os.environ.get("FOUNDRY_TRAINING_STORE") or "~/.foundry-training"
    paths = a.profiles or [p for p in (os.environ.get("FOUNDRY_TRAINING_PROFILES") or
                                       os.environ.get("FOUNDRY_COORD_PROFILES") or "").split(
        os.pathsep) if p]
    return Pipeline(TrainingStore(root, allow_in_repo=a.allow_in_repo), TC.find(paths),
                    known_secrets=_known_secrets())


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="foundry_training", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--store")
    ap.add_argument("--profiles", action="append")
    ap.add_argument("--allow-in-repo", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)
    ing = sub.add_parser("ingest")
    ing.add_argument("source", choices=["kilo", "foundry", "coordinator", "cline", "git",
                                           "gate"])
    ing.add_argument("--export", nargs="*", default=[])
    ing.add_argument("--url")
    ing.add_argument("--session", nargs="*", default=[])
    ing.add_argument("--db")
    ing.add_argument("--directory", default="")
    ing.add_argument("--capture-db")
    ing.add_argument("--sessions")
    ing.add_argument("--project")
    q = sub.add_parser("queue")
    q.add_argument("--spool", required=True)
    q.add_argument("--foundry-url", default="")
    q.add_argument("--kilo-url", default="")
    cu = sub.add_parser("curate")
    cu.add_argument("--project", required=True)
    cu.add_argument("--map", nargs="*", default=[])
    ex = sub.add_parser("export")
    ex.add_argument("--project", action="append", required=True)
    ex.add_argument("--kind", required=True, choices=["sft", "correction", "preference",
                                                      "multimodal"])
    ex.add_argument("--target", required=True, choices=["canonical", "chatml-tools", "dpo"])
    ex.add_argument("--purpose", default="train", choices=["train", "eval"])
    ex.add_argument("--tokenizer")
    ex.add_argument("--tag", action="append", default=[],
                    help="only episodes carrying this tag (repeatable), e.g. ui_v3")
    va = sub.add_parser("validate")
    va.add_argument("dataset")
    va.add_argument("--target", required=True)
    st = sub.add_parser("status")
    st.add_argument("--json", action="store_true")
    ins = sub.add_parser("inspect")
    ins.add_argument("episode")
    gp = sub.add_parser("gaps")
    gp.add_argument("--project")
    pr = sub.add_parser("prefer")
    for k in ("project", "chosen", "rejected", "by", "note"):
        pr.add_argument("--" + k, required=True)
    fg = sub.add_parser("forget")
    fg.add_argument("episode")
    fg.add_argument("--reason", required=True)
    fg.add_argument("--sources", action="store_true")
    sub.add_parser("retention")
    sub.add_parser("recover")
    au = sub.add_parser("auto", help="post-sprint: queue + export + library + upload")
    au.add_argument("--spool", required=True)
    au.add_argument("--foundry-url", default="")
    au.add_argument("--kilo-url", default="")
    au.add_argument("--library", default="")
    au.add_argument("--no-upload", action="store_true")
    au.add_argument("--lock", default="", help="lock file (one automatic run at a time)")
    pu = sub.add_parser("push")
    pu.add_argument("--foundry-url", required=True)
    pu.add_argument("--id", default="workstation")
    a = ap.parse_args(argv)
    try:
        p = build(a)
        return _run(a, p)
    except (StoreError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes
        h = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)   # QUERY_LIMITED_INFORMATION
        if not h:
            return False
        code = ctypes.c_ulong()
        ok = ctypes.windll.kernel32.GetExitCodeProcess(h, ctypes.byref(code))
        ctypes.windll.kernel32.CloseHandle(h)
        return bool(ok) and code.value == 259                        # STILL_ACTIVE
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _take_lock(lock: str):
    """An O_EXCL lock file holding the owner's pid. A lock whose process is
    gone (crash, reboot) is taken over at once; a live one is respected."""
    for _ in range(2):
        try:
            return os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                with open(lock) as f:
                    pid = int((f.read() or "0").strip() or 0)
            except (OSError, ValueError):
                pid = 0
            if pid and _pid_alive(pid):
                return None
            try:                          # empty and brand new: its owner is
                young = not pid and time.time() - os.path.getmtime(lock) < 30
            except OSError:               # still writing its pid (POSIX race)
                young = False
            if young:
                return None
            try:
                os.remove(lock)
            except OSError:
                pass
    return None


def _own_pending(p: Pipeline, spool: str) -> bool:
    """Manifests for this run's projects still waiting in the spool."""
    if not os.path.isdir(spool):
        return False
    for name in os.listdir(spool):
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(spool, name)) as f:
                if json.load(f).get("project_id") in p.projects:
                    return True
        except (OSError, ValueError):
            continue
    return False


def _run(a, p: Pipeline) -> int:
    key = os.environ.get("FOUNDRY_TRAINING_KEY", "")
    out = None
    if a.cmd == "ingest":
        if a.source == "kilo":
            api = {"url": a.url, "sessions": a.session,
                   "password": os.environ.get("KILO_SERVER_PASSWORD", ""),
                   "directory": a.directory} if a.url else None
            out = p.ingest_kilo(exports=a.export, api=api, db=a.db, directory=a.directory)
        elif a.source == "foundry":
            out = p.ingest_foundry(url=a.url or "", key=key, capture_db=a.capture_db or "")
        elif a.source == "coordinator":
            out = p.ingest_coordinator(a.db)
        elif a.source == "cline":
            out = p.ingest_cline(a.sessions)
        elif a.source == "gate":
            out = p.ingest_gate(a.project)
        else:
            out = p.ingest_git(a.project)
    elif a.cmd == "queue":
        kapi = {"url": a.kilo_url, "password": os.environ.get("KILO_SERVER_PASSWORD", "")} \
            if a.kilo_url else None
        out = p.process_queue(a.spool, a.foundry_url, key, kapi)
    elif a.cmd == "curate":
        mapping = dict(m.split("=", 1) for m in a.map)
        out = p.curate(a.project, mapping)
    elif a.cmd == "export":
        from .export import export
        cfgs = {pid: p._cfg(pid) for pid in a.project}
        man = export(p.store, a.project, a.kind, a.target, cfgs, a.purpose, a.tokenizer,
                     p.known, tags=a.tag or None)
        out = {k: man[k] for k in ("dataset_id", "examples", "skipped", "validation")}
    elif a.cmd == "validate":
        from .validate import validate_dataset
        path = a.dataset if a.dataset.endswith(".jsonl") else os.path.join(a.dataset, "data.jsonl")
        out = validate_dataset(path, a.target, p.known)
    elif a.cmd == "status":
        s = p.status()
        if a.json:
            out = s
        else:
            for pid, d in s["projects"].items():
                print(f"{pid}: capture {'on' if d['capture']['enabled'] else 'off'} "
                      f"({', '.join(d['capture']['sources'])}); gaps {d['gaps']}; "
                      f"duplicates {d['duplicates']}")
                for e in d["episodes"]:
                    print(f"   {e['kind']:<11} {e['status']:<10} {e['split']:<6} {e['n']}")
                if d["exclusion_reasons"]:
                    print("   excluded because: " + ", ".join(
                        f"{k} {v}" for k, v in sorted(d["exclusion_reasons"].items())))
            for ds in s["datasets"][:10]:
                print(f"dataset {ds['dataset_id']}: {ds['examples']} examples, {ds['status']}")
            return 0
    elif a.cmd == "inspect":
        rows = p.store.cur.q("SELECT * FROM episodes WHERE episode_id=?", (a.episode,))
        if not rows:
            print("unknown episode", file=sys.stderr)
            return 2
        ep = json.loads(rows[0]["data"])
        ep["status"], ep["reasons"] = rows[0]["status"], json.loads(rows[0]["reasons"] or "[]")
        ep["in_datasets"] = p.store.datasets_containing(a.episode)
        out = ep
    elif a.cmd == "gaps":
        out = p.store.gaps(a.project)
    elif a.cmd == "prefer":
        from .curate import record_human_preference
        record_human_preference(p.store, a.project, a.chosen, a.rejected, a.by, a.note)
        out = {"ok": True}
    elif a.cmd == "forget":
        out = p.store.forget_episode(a.episode, a.reason, "sources" if a.sources else "episode")
    elif a.cmd == "retention":
        res = {}
        for pid, cfg in p.projects.items():
            res[pid] = p.store.retention(int(cfg.retention.get("raw_days", 30)),
                                         int(cfg.retention.get("normalized_days", 180)))
        out = res
    elif a.cmd == "recover":
        out = {"interrupted_runs": p.store.interrupted_runs(),
               "note": "ingestion is idempotent: run the same ingest command again"}
    elif a.cmd == "auto":
        from .auto import auto_run
        lock = a.lock or os.path.join(a.spool, ".auto.lock")
        fd = _take_lock(lock)
        if fd is None:
            print("another automatic run is in progress; it re-checks the spool before it "
                  "finishes, so this sprint is picked up")
            return 0
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        try:
            ov = {"foundry_url": a.foundry_url, "kilo_url": a.kilo_url, "library": a.library,
                  "upload": False if a.no_upload else None}
            log = lambda m: print(m, file=sys.stderr, flush=True)   # noqa: E731
            out = auto_run(p, a.spool, ov, log=log)
            # a manifest written while we exported/uploaded: run again before
            # releasing the lock (a run that found the lock busy relied on this)
            for _ in range(3):
                if not _own_pending(p, a.spool):
                    break
                out = auto_run(p, a.spool, ov, log=log)
        finally:
            try:
                os.remove(lock)
            except OSError:
                pass
    elif a.cmd == "push":
        out = p.push(a.foundry_url, key, a.id)
    print(json.dumps(out, indent=2, default=str))
    return 0
