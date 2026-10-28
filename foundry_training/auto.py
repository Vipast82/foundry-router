"""Automatic post-sprint processing: parse, export, store centrally, upload.

The coordinator starts this in a background process right after it writes a
task's closeout manifest (foundry_coordinator/training_hook.py), so a finished
sprint becomes fine-tuning data without anyone running a command:

  1. queue      ingest everything the manifest points at (coordinator store,
                Kilo sessions, Foundry capture, git) and curate episodes —
                the same deterministic pipeline as `foundry_training queue`
  2. export     for each project and dataset kind whose selected episodes
                changed since the last export: a validated JSONL dataset in
                each configured target format (chatml-tools = the messages+
                tools JSONL that TRL / Unsloth / Axolotl read; canonical =
                full lineage records)
  3. library    a copy in ONE central folder, laid out for people and scripts:
                  <library>/INDEX.json                  every dataset, newest first
                  <library>/README.md                   how to read it
                  <library>/<project>/<kind>/<target>/latest.jsonl
                  <library>/<project>/<kind>/<target>/<dataset_id>/{data.jsonl, manifest.json}
  4. upload     each new valid train dataset to Foundry
                (POST /v1/foundry/training/datasets/{id}), where the Training
                tab lists and serves it. If Foundry is unreachable, refuses it
                or no key is set, the dataset simply stays in the library and
                INDEX.json says why; the next run retries.

No model is called anywhere: parsing, redaction, curation rules, export and
validation are plain code. Evaluation exports are never uploaded and never
mixed into the train library.

Settings (profile, all optional):
  [training.auto]
  enabled = true                 # default: on whenever [training] enabled = true
  upload = true
  foundry_url = ""               # default: the profile's [foundry] url
  key_env = "FOUNDRY_TRAINING_KEY"   # falls back to [foundry] key_env
  kilo_url = ""                  # default: the profile's [client] url
  kinds = ["sft", "correction", "multimodal"]   # + "calls", "preference"
  targets = []                   # default: [training.export] targets or ["chatml-tools", "canonical"]
  library = ""                   # default: <training store>/library
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
import tomllib
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional

KINDS = ("sft", "correction", "multimodal", "calls", "preference")
DEFAULT_KINDS = ["sft", "correction", "multimodal"]
README = """# Foundry fine-tuning datasets

Written automatically after each coordinator sprint by `foundry_training auto`.

* `INDEX.json` lists every dataset, newest first: project, kind, target format,
  example count, validation result, the episodes it contains, its folder, and
  whether it was uploaded to Foundry (and if not, why).
* `<project>/<kind>/<target>/latest.jsonl` is always the newest dataset of that
  kind and format; older ones stay in their `<dataset_id>/` folders.
* `chatml-tools` lines are `{"messages": [...], "tools": [...], "meta": {...}}`,
  the conversational tool-calling format TRL, Unsloth and Axolotl read.
  `canonical` lines keep full lineage (episode, task, revision, outcome).
* Kinds: `sft` = verified, reviewed local-model trajectories; `correction` =
  repairs after reviewer/human feedback; `multimodal` = visual tasks with images.
* Every dataset was redacted and re-validated before it was written. Frontier
  and reviewer text is never a training target. Evaluation sets are kept out.
"""


def auto_settings(cfg) -> dict:
    """[training.auto] from the project's profile (cfg = TrainingProject)."""
    try:
        with open(cfg.path, "rb") as f:
            raw = tomllib.load(f)
    except (OSError, ValueError):
        raw = {}
    t = raw.get("training") or {}
    a = dict(t.get("auto") or {})
    foundry = raw.get("foundry") or {}
    client = raw.get("client") or {}
    return {"enabled": bool(t.get("enabled")) and bool(a.get("enabled", True)),
            "upload": bool(a.get("upload", True)),
            "foundry_url": str(a.get("foundry_url") or foundry.get("url") or ""),
            "key_env": [e for e in (a.get("key_env"), "FOUNDRY_TRAINING_KEY",
                                    foundry.get("key_env")) if e],
            "kilo_url": str(a.get("kilo_url") or client.get("url") or ""),
            "kilo_password_env": str(client.get("password_env") or "KILO_SERVER_PASSWORD"),
            "kinds": [k for k in (a.get("kinds") or list(DEFAULT_KINDS)) if k in KINDS],
            "targets": [x for x in (a.get("targets") or (t.get("export") or {}).get("targets")
                                    or ["chatml-tools", "canonical"])
                        if x in ("chatml-tools", "canonical")],
            "library": str(a.get("library") or "")}


def _key(names: list) -> str:
    for n in names:
        v = os.environ.get(n, "")
        if v:
            return v
    return ""


class Library:
    def __init__(self, root: Path):
        self.root = Path(os.path.expanduser(str(root))).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.index_path = self.root / "INDEX.json"

    def load(self) -> dict:
        try:
            with open(self.index_path, encoding="utf-8") as f:
                d = json.load(f)
            return d if isinstance(d, dict) else {"datasets": []}
        except (OSError, ValueError):
            return {"datasets": []}

    def save(self, idx: dict) -> None:
        idx["updated"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        idx["datasets"] = sorted(idx.get("datasets") or [], key=lambda d: d.get("created") or "",
                                 reverse=True)
        tmp = self.index_path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(idx, f, indent=2)
        os.replace(tmp, self.index_path)
        if not (self.root / "README.md").exists():
            (self.root / "README.md").write_text(README, encoding="utf-8")

    def publish(self, manifest: dict, src_dir: str) -> dict:
        pid = manifest["projects"][0] if len(manifest["projects"]) == 1 else "multi"
        base = self.root / pid / manifest["kind"] / manifest["target"]
        dest = base / manifest["dataset_id"]
        dest.mkdir(parents=True, exist_ok=True)
        for name in ("data.jsonl", "manifest.json"):
            shutil.copy2(os.path.join(src_dir, name), dest / name)
        latest = base / "latest.jsonl"
        tmp = base / ".latest.tmp"
        shutil.copy2(dest / "data.jsonl", tmp)
        os.replace(tmp, latest)
        (base / "latest.json").write_text(json.dumps({"dataset_id": manifest["dataset_id"],
                                                      "path": str(dest)}, indent=2),
                                          encoding="utf-8")
        return {"dataset_id": manifest["dataset_id"], "project_id": pid,
                "kind": manifest["kind"], "target": manifest["target"],
                "created": manifest["created"], "examples": manifest["examples"],
                "valid": bool((manifest.get("validation") or {}).get("ok")),
                "episodes": manifest.get("episodes") or [],
                "episode_hash": manifest.get("lineage", {}).get("episode_ids_hash"),
                "path": str(dest), "data": str(dest / "data.jsonl"),
                "upload": {"status": "pending"}}


MAX_UPLOAD_BYTES = 190 * 1024 * 1024          # Foundry refuses bodies over 200 MB
PERMANENT_HTTP = (409, 413, 422)               # conflict, too large, secret detected


def upload(entry: dict, foundry_url: str, key: str, timeout: float = 120) -> dict:
    """POST one dataset to Foundry. Returns the entry's new upload status:
    uploaded | failed (retried next run) | refused (permanent; not retried) |
    not_configured | missing (its files were removed from the library)."""
    if not foundry_url:
        return {"status": "not_configured", "reason": "no Foundry URL in the profile"}
    if not key:
        return {"status": "not_configured", "reason": "no key (set FOUNDRY_TRAINING_KEY or the "
                                                      "profile's [foundry] key_env)"}
    try:
        if os.path.getsize(entry["data"]) > MAX_UPLOAD_BYTES:
            return {"status": "refused", "at": time.time(),
                    "reason": f"over {MAX_UPLOAD_BYTES // 2**20} MB; kept in the library only"}
        with open(entry["data"], encoding="utf-8") as f:
            data = f.read()
        with open(os.path.join(entry["path"], "manifest.json"), encoding="utf-8") as f:
            manifest = json.load(f)
    except (OSError, ValueError) as e:
        return {"status": "missing", "at": time.time(), "reason": f"library files: {e}"[:300]}
    body = json.dumps({"manifest": manifest, "data": data,
                       "sha256": hashlib.sha256(data.encode()).hexdigest()}).encode()
    req = urllib.request.Request(foundry_url.rstrip("/") + "/v1/foundry/training/datasets/"
                                 + entry["dataset_id"], data=body, method="POST")
    req.add_header("content-type", "application/json")
    req.add_header("authorization", f"Bearer {key}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            res = json.loads(r.read() or b"{}")
        return {"status": "uploaded", "at": time.time(), "foundry": foundry_url,
                "stored": res.get("path"), "examples": res.get("examples")}
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read() or b"{}").get("error")
        except ValueError:
            msg = ""
        return {"status": "refused" if e.code in PERMANENT_HTTP else "failed", "at": time.time(),
                "reason": f"HTTP {e.code}{': ' + str(msg)[:300] if msg else ''}"}
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return {"status": "failed", "at": time.time(), "reason": f"unreachable: {e}"[:300]}


def _selected_hash(store, pid: str, kind: str) -> tuple[int, str]:
    if kind in ("calls", "preference"):
        # selected at export time: export, then compare the dataset's lineage
        return 1, ""
    want = "correction" if kind == "correction" else "trajectory"
    rows = store.cur.q("SELECT episode_id, content_hash, data FROM episodes WHERE project_id=? AND "
                       "kind=? AND deleted=0 AND status='selected' ORDER BY episode_id",
                       (pid, want))
    if kind == "multimodal":
        rows = [r for r in rows if any(a.get("refs") for a in
                                       (json.loads(r["data"] or "{}").get("flags") or {})
                                       .get("artifacts") or [])]
    h = hashlib.sha256("|".join(f"{r['episode_id']}:{r['content_hash']}" for r in rows)
                       .encode()).hexdigest()[:24]
    return len(rows), h


def auto_run(pipeline, spool: str, override: Optional[dict] = None, log=print,
             passes: int = 5) -> dict:
    """One automatic run. Never raises for a single project's failure; every
    outcome is in the returned report and in the library INDEX.json."""
    from .export import export
    override = override or {}
    report = {"started": time.time(), "queue": [], "exports": [], "uploads": [], "errors": []}
    projects = [c for c in pipeline.projects.values() if c.enabled]
    sets = {c.id: {**auto_settings(c), **{k: v for k, v in override.items() if v not in
                                          (None, "")}} for c in projects}
    active = [c for c in projects if sets[c.id]["enabled"]]
    if not active:
        report["skipped"] = "no project has [training] enabled with automatic processing on"
        return report
    first = sets[active[0].id]
    kilo_api = None
    if first.get("kilo_url"):
        kilo_api = {"url": first["kilo_url"],
                    "password": os.environ.get(first["kilo_password_env"], "")}
    key = _key(first["key_env"])
    # a manifest written while we were running is picked up by the next pass
    for _ in range(max(1, passes)):
        pending = [p for p in os.listdir(spool) if p.endswith(".json")] if os.path.isdir(spool) \
            else []
        if not pending:
            break
        try:
            report["queue"] += pipeline.process_queue(spool, foundry_url=first["foundry_url"],
                                                      foundry_key=key, kilo_api=kilo_api)
        except Exception as e:                                    # noqa: BLE001
            report["errors"].append(f"queue: {type(e).__name__}: {e}"[:400])
            break
        done_now = report["queue"][-len(pending):]
        if all(r.get("error") or r.get("left") for r in done_now):
            break                     # failures / other projects' manifests stay in the spool
    lib_root = first["library"] or str(Path(pipeline.store.root) / "library")
    lib = Library(Path(lib_root))
    idx = lib.load()
    # the NEWEST dataset per project/kind/format decides "same episodes" (INDEX
    # is newest first); invalid ones count too, so a failing set isn't re-added
    known: dict = {}
    for d in sorted(idx["datasets"], key=lambda d: d.get("created") or "", reverse=True):
        known.setdefault((d["project_id"], d["kind"], d["target"]), d.get("episode_hash"))
    for c in active:
        st = sets[c.id]
        for kind in st["kinds"]:
            n, h = _selected_hash(pipeline.store, c.id, kind)
            if not n:
                continue
            targets = ["dpo"] if kind == "preference" else \
                [t for t in st["targets"] if t != "dpo"]
            for target in targets:
                if kind == "multimodal" and target == "dpo":
                    continue
                try:
                    m = export(pipeline.store, [c.id], kind, target, pipeline.projects,
                               known_secrets=pipeline.known)
                except Exception as e:                            # noqa: BLE001
                    report["errors"].append(f"export {c.id}/{kind}/{target}: "
                                            f"{type(e).__name__}: {e}"[:400])
                    continue
                eh = m.get("lineage", {}).get("episode_ids_hash")
                src = os.path.join(str(pipeline.store.root), "datasets", m["dataset_id"])
                if known.get((c.id, kind, target)) == eh or not m["examples"]:
                    # same episodes as the newest dataset, or nothing usable: no new dataset
                    shutil.rmtree(src, ignore_errors=True)
                    pipeline.store.cur.x("DELETE FROM datasets WHERE dataset_id=?",
                                         (m["dataset_id"],))
                    pipeline.store.cur.x("DELETE FROM dataset_members WHERE dataset_id=?",
                                         (m["dataset_id"],))
                    continue
                entry = lib.publish(m, src)
                idx["datasets"].append(entry)
                known[(c.id, kind, target)] = eh
                report["exports"].append({k: entry[k] for k in (
                    "dataset_id", "project_id", "kind", "target", "examples", "valid", "path")})
                log(f"dataset {entry['dataset_id']}: {entry['examples']} example(s) -> "
                    f"{entry['path']}")
    # uploads: every valid dataset not yet in Foundry (retries earlier failures)
    for d in idx["datasets"]:
        st = sets.get(d.get("project_id")) or first
        if not st.get("upload"):
            # an upload-off run never erases what an earlier run recorded
            if (d.get("upload") or {}).get("status") not in ("uploaded", "refused"):
                d["upload"] = {"status": "disabled", "reason": "[training.auto] upload = false"}
            continue
        if not d.get("valid"):
            d["upload"] = {"status": "skipped", "reason": "dataset failed validation"}
            continue
        if (d.get("upload") or {}).get("status") in ("uploaded", "refused"):
            continue                       # refused = permanent (conflict, too large, secret)
        try:
            d["upload"] = upload(d, st["foundry_url"], _key(st["key_env"]))
        except Exception as e:                                    # noqa: BLE001
            d["upload"] = {"status": "failed", "at": time.time(),
                           "reason": f"{type(e).__name__}: {e}"[:300]}
        report["uploads"].append({"dataset_id": d["dataset_id"], **d["upload"]})
        log(f"upload {d['dataset_id']}: {d['upload']['status']} "
            f"{d['upload'].get('reason') or ''}")
    lib.save(idx)
    if first.get("upload") and first["foundry_url"] and key:
        try:                                     # refresh the Training tab's counts (best effort)
            pipeline.push(first["foundry_url"], key, "auto")
        except Exception as e:                                    # noqa: BLE001
            report["errors"].append(f"status push: {type(e).__name__}: {e}"[:300])
    report["library"] = str(lib.root)
    report["finished"] = time.time()
    return report
