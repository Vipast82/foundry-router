"""Finished check rows kept across a coordinator restart (E, 2026-10-28).

A restart or deploy in the middle of a verify used to throw away every finished check:
the new coordinator ran done_check and the gate binding again (Lune already had its own
cache, projects/animaluprising/lune_cache.py). An adapter stores a check's rows here
under (task, kind, head, input key) once it finished without an UNKNOWN row, and asks
for them before it runs the check. The input key is the adapter's: everything the check
reads besides the revision (for done_check: the working tree's status and the tool's own
source). Entries older than MAX_AGE_S or past MAX_ENTRIES are dropped.

One JSON file per project: <state>/check_cache/<project>.json.
"""

from __future__ import annotations

import dataclasses
import json
import os
import re
import threading
import time
from typing import Optional

MAX_ENTRIES = 300
MAX_AGE_S = 12 * 3600
_lock = threading.Lock()
_SAFE = re.compile(r"[^A-Za-z0-9_.-]+")


def path(state_dir: str, project_id: str) -> str:
    return os.path.join(state_dir, "check_cache", _SAFE.sub("_", project_id) + ".json")


def _key(task: str, kind: str, head: str, input_key: str) -> str:
    return f"{task}|{kind}|{head}|{input_key}"


def _load(p: str) -> dict:
    try:
        with open(p, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def get(state_dir: str, project_id: str, task: str, kind: str, head: str,
        input_key: str) -> Optional[list]:
    """The stored rows (VerificationResult) or None."""
    if not (state_dir and head and input_key):
        return None
    from .adapters.base import VerificationResult
    with _lock:
        e = _load(path(state_dir, project_id)).get(_key(task, kind, head, input_key))
    if not isinstance(e, dict) or time.time() - float(e.get("ts") or 0) > MAX_AGE_S:
        return None
    try:
        return [VerificationResult(**r) for r in e.get("rows") or []] or None
    except TypeError:
        return None


def put(state_dir: str, project_id: str, task: str, kind: str, head: str, input_key: str,
        rows: list) -> bool:
    """Keep finished rows; rows with an UNKNOWN result (the check could not run) are not
    kept. True when stored."""
    if not (state_dir and head and input_key and rows):
        return False
    if any(getattr(r, "result", "") == "UNKNOWN" for r in rows):
        return False
    p = path(state_dir, project_id)
    with _lock:
        d = _load(p)
        now = time.time()
        d[_key(task, kind, head, input_key)] = {
            "ts": now, "rows": [dataclasses.asdict(r) for r in rows]}
        live = sorted(((k, v) for k, v in d.items() if isinstance(v, dict)
                       and now - float(v.get("ts") or 0) <= MAX_AGE_S),
                      key=lambda kv: kv[1]["ts"])[-MAX_ENTRIES:]
        os.makedirs(os.path.dirname(p), exist_ok=True)
        tmp = f"{p}.{os.getpid()}.{threading.get_ident()}.tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(dict(live), f)
            os.replace(tmp, p)
        except OSError:
            try:
                os.remove(tmp)
            except OSError:
                pass
            return False
    return True
