"""Dataset preparation on the router (v0.181.159).

The router holds every captured model call. Until now a dataset only existed
when the training pipeline on a workstation pulled the records and exported
one. This module builds the keep-everything dataset on the router itself, on a
schedule, so the archive (training_archive.py) always has a current copy for a
trainer to read:

    <data>/training/prepared/              the worker's own store (each message once)
    <data>/training/datasets/router/all-sessions/
        train.jsonl, validation.jsonl, *.text.jsonl, README.md, manifest.json

The work is done by a separate low-priority process (training_prep_worker.py);
this module starts it, reads its status file, stops it, and decides when it is
due. Settings (training_capture.*): `prep_hours` (0 = never on its own),
`prep_max_tokens`, `prep_unbound_project`.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

NAME = "all-sessions"
FILES = ("train.jsonl", "validation.jsonl", "train.text.jsonl", "validation.text.jsonl",
         "README.md", "manifest.json")
_proc: dict = {"p": None}


def _cfg(svc):
    return svc.config_store.config.training_capture


def _dirs(svc) -> dict:
    root = Path(svc.capture.data_dir) / "training"
    return {"root": root, "store": root / "prepared", "out": root / "datasets" / "router",
            "status": root / "prep-status.json", "stop": root / "prep.stop"}


def dataset_dir(svc) -> Path:
    return _dirs(svc)["out"] / NAME


def running() -> bool:
    p = _proc["p"]
    return p is not None and p.poll() is None


def status(svc) -> dict:
    d = _dirs(svc)
    try:
        st = json.loads(d["status"].read_text(encoding="utf-8"))
        if not isinstance(st, dict):
            st = {}
    except (OSError, ValueError):
        st = {}
    if st.get("state") == "running" and not running():
        # the worker is gone without saying so (a router restart, a kill)
        st["state"], st["error"] = "stopped", st.get("error") or "interrupted (it continues on the next run)"
    cfg = _cfg(svc)
    out = {"hours": int(getattr(cfg, "prep_hours", 24) or 0),
           "max_tokens": int(getattr(cfg, "prep_max_tokens", 32768) or 32768),
           "unbound_project": getattr(cfg, "prep_unbound_project", "chat") or "chat",
           "run": st or {"state": "never"}, "where": str(dataset_dir(svc)), "files": []}
    try:
        for n in FILES:
            f = dataset_dir(svc) / n
            if f.is_file():
                out["files"].append({"name": n, "bytes": f.stat().st_size, "modified": f.stat().st_mtime})
    except OSError:
        pass
    return out


def due(svc, now: Optional[float] = None) -> bool:
    hours = int(getattr(_cfg(svc), "prep_hours", 24) or 0)
    if hours <= 0 or running():
        return False
    try:
        if not Path(svc.capture.store.path).exists():
            return False
    except Exception:                                             # noqa: BLE001
        return False
    run = status(svc)["run"]
    if run.get("state") == "stopped":
        return True                                               # unfinished: carry on
    return (now or time.time()) - float(run.get("finished") or 0) >= hours * 3600


def start(svc) -> dict:
    if running():
        return status(svc)
    d = _dirs(svc)
    d["root"].mkdir(parents=True, exist_ok=True)
    try:
        d["stop"].unlink()
    except OSError:
        pass
    cfg = _cfg(svc)
    cmd = [sys.executable, "-m", "foundry_router.training_prep_worker",
           "--capture", str(svc.capture.store.path), "--store", str(d["store"]),
           "--out", str(d["out"]), "--status", str(d["status"]),
           "--router-db", str(getattr(svc.db, "path", "") or ""),
           "--unbound", str(getattr(cfg, "prep_unbound_project", "chat") or "chat"),
           "--name", NAME, "--max-tokens", str(int(getattr(cfg, "prep_max_tokens", 32768) or 32768))]
    d["status"].write_text(json.dumps({"state": "running", "phase": "starting", "started": time.time(),
                                       "records": 0}), encoding="utf-8")
    _proc["p"] = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL, cwd=str(Path(__file__).resolve().parent.parent))
    return status(svc)


def stop(svc) -> dict:
    """Ask the worker to stop after the batch it is on; it is ended if it has not
    within ten seconds. What it has read stays; the next run carries on."""
    d = _dirs(svc)
    p = _proc["p"]
    if running():
        try:
            d["stop"].write_text("stop", encoding="utf-8")
        except OSError:
            pass
        try:
            p.wait(timeout=10)
        except subprocess.TimeoutExpired:
            p.terminate()
    return status(svc)
