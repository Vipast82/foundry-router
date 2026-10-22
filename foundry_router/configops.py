"""Configuration operations: history and rollback, state snapshots,
export/import, and non-secret diagnostic bundles.

History    every saved config.yaml state is a version (raw YAML, so ${VAR}
           references stay as written), with the top-level sections it
           changed. Restoring a version is itself a save (so it can be undone)
           and applies what can change live; the rest is listed as "restart
           to apply".
Snapshots  personas, pause switches, tool and model overrides aren't in
           config.yaml; they are snapshotted before bulk changes (an import,
           a profile apply) and can be restored.
Export     one JSON bundle: config (secrets redacted unless asked for),
           personas, pauses, overrides. Import merges it back; a redacted
           value never overwrites the real one already configured.
Diagnostics a zip for troubleshooting: versions, status, health, budgets,
           recent events and request metadata. No secrets (redacted by key
           name AND by value) and no prompts or images.
"""

from __future__ import annotations

import copy
import difflib
import io
import json
import logging
import os
import platform
import re
import sys
import time
import zipfile
from typing import Any, Optional

import yaml

from .db import utcnow

log = logging.getLogger(__name__)

REDACTED = "<redacted>"
_SECRET_KEY_RE = re.compile(r"(^|_)(api_?key|key|token|secret|password|passwd|credentials?)$", re.I)
_ENV_REF_RE = re.compile(r"^\$\{[A-Za-z_][A-Za-z0-9_]*\}$")
_SECRET_ENV_RE = re.compile(r"(KEY|TOKEN|SECRET|PASSWORD|PASSWD)", re.I)
_BEARER_RE = re.compile(r"(?i)(bearer\s+|x-api-key[\"':=\s]+)([A-Za-z0-9._\-]{8,})")
_KEYLIKE_RE = re.compile(r"\b(sk-[A-Za-z0-9_\-]{12,}|sk-ant-[A-Za-z0-9_\-]{12,})\b")


# -- redaction -------------------------------------------------------------------

def is_secret_key(key: str) -> bool:
    return bool(_SECRET_KEY_RE.search(str(key)))


def redact(obj: Any) -> Any:
    """Replace secret values (by key name) with <redacted>; ${VAR} references
    are kept (they hold no secret themselves)."""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if is_secret_key(k) and isinstance(v, str) and v and not _ENV_REF_RE.match(v):
                out[k] = REDACTED
            else:
                out[k] = redact(v)
        return out
    if isinstance(obj, list):
        return [redact(v) for v in obj]
    return obj


def merge_redacted(new: Any, current: Any) -> Any:
    """`new` with every <redacted> value replaced by the value at the same
    path in `current` (list items matched by `name` when they have one), or
    dropped when there is none."""
    if isinstance(new, dict):
        cur = current if isinstance(current, dict) else {}
        out = {}
        for k, v in new.items():
            if v == REDACTED:
                if k in cur and cur[k] != REDACTED:
                    out[k] = cur[k]
                continue
            out[k] = merge_redacted(v, cur.get(k))
        return out
    if isinstance(new, list):
        cur = current if isinstance(current, list) else []
        by_name = {c.get("name"): c for c in cur if isinstance(c, dict) and c.get("name")}
        out = []
        for i, v in enumerate(new):
            match = by_name.get(v.get("name")) if isinstance(v, dict) and v.get("name") else (
                cur[i] if i < len(cur) else None)
            out.append(merge_redacted(v, match))
        return out
    return new


def secret_values(svc) -> set[str]:
    """Every secret value Foundry knows (resolved config secrets, client keys,
    secret-looking environment variables) — scrubbed from diagnostic text."""
    vals: set[str] = set()

    def walk(o):
        if isinstance(o, dict):
            for k, v in o.items():
                if is_secret_key(k) and isinstance(v, str) and len(v) >= 6:
                    vals.add(v)
                else:
                    walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
    walk(svc.config_store.config.model_dump())
    for k, v in os.environ.items():
        if _SECRET_ENV_RE.search(k) and v and len(v) >= 6:
            vals.add(v)
    return vals


def scrub(text: str, secrets: set[str]) -> str:
    for s in sorted(secrets, key=len, reverse=True):
        if s and s in text:
            text = text.replace(s, REDACTED)
    text = _BEARER_RE.sub(lambda m: m.group(1) + REDACTED, text)
    return _KEYLIKE_RE.sub(REDACTED, text)


def _dump(raw: dict) -> str:
    return yaml.safe_dump(raw, sort_keys=False, allow_unicode=True)


# -- config history ------------------------------------------------------------------

def changed_sections(before: dict, after: dict) -> list[str]:
    keys = list(dict.fromkeys(list(before or {}) + list(after or {})))
    return [k for k in keys if (before or {}).get(k) != (after or {}).get(k)]


class ConfigHistory:
    def __init__(self, svc):
        self.svc = svc
        self.db = svc.db
        self._source: Optional[str] = None
        svc.config_store.on_saved = getattr(svc.config_store, "on_saved", [])
        svc.config_store.on_saved.append(self._saved)
        self._record_startup()

    def _last(self) -> Optional[dict]:
        return self.db.query_one("SELECT * FROM config_versions ORDER BY id DESC LIMIT 1")

    def _record_startup(self) -> None:
        raw = self.svc.config_store.raw
        text = _dump(raw)
        last = self._last()
        if last is None or last["yaml"] != text:
            before = yaml.safe_load(last["yaml"]) if last else {}
            src = "startup" if last is None else "changed outside Foundry (file edited)"
            notes = getattr(self.svc.config_store, "migration_notes", None)
            if notes:
                src += " · " + "; ".join(notes)
            self._insert(src, changed_sections(before or {}, raw), text)

    def _insert(self, source: str, sections: list, text: str) -> int:
        return self.db.execute("INSERT INTO config_versions (ts, source, sections, yaml) "
                               "VALUES (?,?,?,?)", (utcnow(), source, json.dumps(sections), text))

    def _saved(self, before: dict, after: dict) -> None:
        secs = changed_sections(before, after)
        if not secs:
            return
        self._insert(self._source or "save", secs, _dump(after))

    def versions(self, limit: int = 50) -> list[dict]:
        rows = self.db.query("SELECT id, ts, source, sections FROM config_versions "
                             "ORDER BY id DESC LIMIT ?", (limit,))
        return [{**r, "sections": json.loads(r["sections"] or "[]")} for r in rows]

    def get(self, vid: int) -> Optional[dict]:
        return self.db.query_one("SELECT * FROM config_versions WHERE id=?", (vid,))

    def redacted_yaml(self, vid: int) -> Optional[str]:
        row = self.get(vid)
        return _dump(redact(yaml.safe_load(row["yaml"]) or {})) if row else None

    def diff(self, vid: int) -> Optional[str]:
        """Unified diff (redacted) from version `vid` to the current config."""
        row = self.get(vid)
        if row is None:
            return None
        old = _dump(redact(yaml.safe_load(row["yaml"]) or {})).splitlines(keepends=True)
        new = _dump(redact(self.svc.config_store.raw)).splitlines(keepends=True)
        return "".join(difflib.unified_diff(old, new, f"version {vid}", "current", n=2))

    async def restore(self, vid: int) -> dict:
        row = self.get(vid)
        if row is None:
            raise KeyError(vid)
        target = yaml.safe_load(row["yaml"]) or {}
        return await apply_raw(self.svc, target, source=f"restore of version {vid}")


async def apply_raw(svc, new_raw: dict, source: str) -> dict:
    """Save a whole raw config (validated first) and apply what can change
    live. Returns {sections, applied, restart_needed}."""
    store = svc.config_store
    old_cfg = store.config
    before = copy.deepcopy(store.raw)
    hist: Optional[ConfigHistory] = getattr(svc, "config_history", None)
    if hist:
        hist._source = source
    try:
        def mutate(raw):
            raw.clear()
            raw.update(copy.deepcopy(new_raw))
        store.save(mutate)
    finally:
        if hist:
            hist._source = None
    secs = changed_sections(before, store.raw)
    applied, restart = await apply_live(svc, old_cfg, store.config, secs)
    return {"sections": secs, "applied": applied, "restart_needed": restart}


# Sections every component reads live from config_store.config (no action needed).
_LIVE = {"budgets", "media", "health", "subscription_usage", "clients", "client_auth",
         "persona_profiles", "data_retention", "config_version", "turn_assist",
         "training_capture", "perf"}
_RESTART = {"server", "mcp_servers", "agents", "mcp_aggregator", "logging", "registry",
            "tool_sync", "semantic_cache"}


async def apply_live(svc, old, new, sections: list) -> tuple[list, list]:
    applied, restart = [], []
    for sec in sections:
        try:
            if sec == "backend_pool":
                await svc.rebuild_pool()
                applied.append(sec)
            elif sec == "agent_brain":
                svc.rebuild_brain()
                applied.append(sec)
            elif sec == "guardrails":
                svc.guardrails.cfg = new.guardrails
                applied.append(sec)
            elif sec == "meridian":
                svc.meridian_usage.cfg = new.meridian
                svc.meridian_usage.clear_cache()
                applied.append(sec)
            elif sec in _LIVE:
                applied.append(sec)
            elif sec in _RESTART:
                restart.append(sec)
            else:
                restart.append(sec)
        except Exception as e:                                     # noqa: BLE001
            log.exception("applying %s live failed", sec)
            restart.append(f"{sec} (live apply failed: {e})")
    return applied, restart


# -- state snapshots (personas & co) -------------------------------------------------

_PERSONA_SKIP = ("created_at", "updated_at")


def state_data(svc) -> dict:
    personas = [{k: v for k, v in p.items() if k not in _PERSONA_SKIP} for p in svc.personas.list()]
    try:
        models = svc.db.query("SELECT * FROM models WHERE source='manual_override'")
    except Exception:                                              # noqa: BLE001
        models = []
    return {"personas": personas,
            "budget_pauses": svc.db.query("SELECT scope, value, note FROM budget_pauses"),
            "tool_overrides": svc.db.query("SELECT tool_name, disabled FROM tool_overrides"),
            "model_overrides": models}


def snapshot(svc, reason: str) -> int:
    return svc.db.execute("INSERT INTO state_snapshots (ts, reason, data) VALUES (?,?,?)",
                          (utcnow(), reason, json.dumps(state_data(svc), default=str)))


def snapshots(svc, limit: int = 30) -> list[dict]:
    rows = svc.db.query("SELECT id, ts, reason, data FROM state_snapshots ORDER BY id DESC LIMIT ?",
                        (limit,))
    out = []
    for r in rows:
        d = json.loads(r["data"] or "{}")
        out.append({"id": r["id"], "ts": r["ts"], "reason": r["reason"],
                    "personas": len(d.get("personas") or []),
                    "pauses": len(d.get("budget_pauses") or [])})
    return out


def import_state(svc, data: dict, sections: list, persona_mode: str = "merge",
                 replace: bool = False) -> dict:
    """Write personas / pauses / overrides from a bundle or snapshot.
    persona_mode: merge (add + update), add (only missing). replace=True also
    removes personas that aren't in `data` (a snapshot restore)."""
    from .personas import PERSONA_FIELDS
    report: dict = {}
    if "personas" in sections and data.get("personas") is not None:
        incoming = {p["virtual_name"]: p for p in data["personas"] if p.get("virtual_name")}
        added = updated = removed = 0
        for name, p in incoming.items():
            exists = svc.personas.get(name) is not None
            if exists and persona_mode == "add":
                continue
            fields = {k: p.get(k) for k in PERSONA_FIELDS if k in p}
            svc.personas.upsert(name, **fields)
            extra = {k: p.get(k) for k in ("profile", "profile_hash", "validation") if k in p}
            if extra:
                cols = ", ".join(f"{k}=?" for k in extra)
                svc.db.execute(f"UPDATE personas SET {cols} WHERE virtual_name=?",
                               (*extra.values(), name))
            updated += exists
            added += not exists
        if replace:
            for p in svc.personas.list():
                if p["virtual_name"] not in incoming:
                    svc.personas.delete(p["virtual_name"])
                    removed += 1
        report["personas"] = {"added": added, "updated": updated, "removed": removed}
    if "budget_pauses" in sections and data.get("budget_pauses") is not None:
        if replace:
            svc.db.execute("DELETE FROM budget_pauses")
        for p in data["budget_pauses"]:
            svc.db.execute("INSERT OR REPLACE INTO budget_pauses (scope, value, paused_at, note) "
                           "VALUES (?,?,?,?)", (p["scope"], p["value"], utcnow(), p.get("note")))
        report["budget_pauses"] = len(data["budget_pauses"])
    if "tool_overrides" in sections and data.get("tool_overrides") is not None:
        for t in data["tool_overrides"]:
            svc.db.execute("INSERT OR REPLACE INTO tool_overrides (tool_name, disabled, updated_at) "
                           "VALUES (?,?,?)", (t["tool_name"], t.get("disabled", 1), utcnow()))
        report["tool_overrides"] = len(data["tool_overrides"])
    if "model_overrides" in sections and data.get("model_overrides"):
        cols = {r["name"] for r in svc.db.query("PRAGMA table_info(models)")}
        n = 0
        for m in data["model_overrides"]:
            row = {k: v for k, v in m.items() if k in cols}
            if not row.get("id"):
                continue
            keys = list(row)
            svc.db.execute(f"INSERT OR REPLACE INTO models ({','.join(keys)}) VALUES "
                           f"({','.join('?' * len(keys))})", [row[k] for k in keys])
            n += 1
        report["model_overrides"] = n
    return report


def restore_snapshot(svc, sid: int) -> dict:
    row = svc.db.query_one("SELECT * FROM state_snapshots WHERE id=?", (sid,))
    if row is None:
        raise KeyError(sid)
    snapshot(svc, f"before restoring snapshot {sid}")
    return import_state(svc, json.loads(row["data"]), ["personas", "budget_pauses"],
                        persona_mode="merge", replace=True)


# -- export / import -----------------------------------------------------------------

EXPORT_SECTIONS = ("config", "personas", "budget_pauses", "tool_overrides", "model_overrides")


def export_bundle(svc, include_secrets: bool = False) -> dict:
    from . import __version__
    raw = copy.deepcopy(svc.config_store.raw)
    return {"format": "foundry-router-export", "format_version": 1,
            "foundry_version": __version__, "exported_at": utcnow(),
            "secrets_included": include_secrets,
            "config": raw if include_secrets else redact(raw),
            **state_data(svc)}


async def import_bundle(svc, bundle: dict, sections: list, persona_mode: str = "merge") -> dict:
    if bundle.get("format") != "foundry-router-export":
        raise ValueError("not a Foundry export bundle")
    sections = [s for s in sections if s in EXPORT_SECTIONS]
    report: dict = {"snapshot": snapshot(svc, "before import")}
    if "config" in sections and isinstance(bundle.get("config"), dict):
        from .config import migrate_raw
        new_raw, notes = migrate_raw(merge_redacted(copy.deepcopy(bundle["config"]),
                                                    svc.config_store.raw))
        report["config"] = await apply_raw(svc, new_raw, source="import")
        if notes:
            report["config"]["migrated"] = notes
    report.update(import_state(svc, bundle, sections, persona_mode))
    return report


# -- diagnostics ---------------------------------------------------------------------

async def diagnostics_zip(svc) -> bytes:
    from . import __version__, budget
    secrets = secret_values(svc)
    files: dict[str, Any] = {}
    files["manifest.json"] = {
        "foundry_version": __version__, "created_at": utcnow(),
        "python": sys.version.split()[0], "platform": platform.platform(),
        "config_version": svc.config_store.raw.get("config_version"),
        "contains": "config (redacted), status, health, queues, personas, profiles, budgets, "
                    "media summary, recent events, request metadata, timing samples, warnings log",
        "excludes": "secrets (by key name and by value), prompts and replies, request "
                    "summaries, images and media artifacts, the auth/token store",
    }
    files["config.redacted.yaml"] = _dump(redact(svc.config_store.raw))
    hist = getattr(svc, "config_history", None)
    files["config_versions.json"] = hist.versions(30) if hist else []
    files["status.json"] = {
        "pool_mode": svc.config_store.config.backend_pool.mode,
        "backends": [{k: v for k, v in b.items() if k not in ("api_key",)}
                     for b in svc.pool.backend_status()],
        "queues": getattr(svc.pool, "queue_status", lambda: [])(),
        "breakers": getattr(svc.pool, "breaker_status", lambda: [])(),
        "brain": getattr(svc, "_brain_health", None),
    }
    try:
        files["health.json"] = svc.health.status()
    except Exception as e:                                         # noqa: BLE001
        files["health.json"] = {"error": str(e)}
    files["personas.json"] = [{k: v for k, v in p.items() if k not in ("assist",)}
                              for p in svc.personas.list()]
    try:
        from . import profiles
        files["profiles.json"] = profiles.list_profiles(svc)
    except Exception as e:                                         # noqa: BLE001
        files["profiles.json"] = {"error": str(e)}
    try:
        mon = await budget.monitor(svc)
        mon.pop("denials", None)
        files["budgets.json"] = mon
    except Exception as e:                                         # noqa: BLE001
        files["budgets.json"] = {"error": str(e)}
    try:
        m = svc.media.summary()
        files["media.json"] = m
    except Exception as e:                                         # noqa: BLE001
        files["media.json"] = {"error": str(e)}
    files["events.json"] = svc.db.query(
        "SELECT ts, level, source, message, substr(detail, 1, 500) AS detail FROM event_log "
        "ORDER BY id DESC LIMIT 500")
    files["requests.json"] = svc.db.query(
        "SELECT ts, request_id, client_id, session_id, task_id, persona, client_model, mode, "
        "routing, served_model, served_backend, status, outcome, attempts, retries, duration_ms, "
        "steps, substr(error, 1, 300) AS error FROM request_log ORDER BY id DESC LIMIT 300")
    files["timing.json"] = svc.db.query(
        "SELECT ts, request_id, model, backend, mode, prompt_tokens, completion_tokens, "
        "cached_tokens, ttft_ms, prefill_ms, decode_ms, decode_tps, prefill_tps, wall_ms, "
        "finish_reason, load_ms FROM perf_samples ORDER BY id DESC LIMIT 300")
    try:
        lines = [f"{r['ts']} {r['level']} {r['logger']}: {r['message']}"
                 + (("\n" + r["traceback"]) if r.get("traceback") else "")
                 for r in svc.logbuffer.snapshot(level="WARNING", limit=400)]
        files["warnings.log"] = "\n".join(lines)
    except Exception:                                              # noqa: BLE001
        files["warnings.log"] = "(log buffer unavailable)"
    files["README.txt"] = (
        "Foundry Router diagnostic bundle.\n\n"
        "Secrets are removed by key name (api_key, token, secret, password, ...) and every "
        "known secret VALUE is scrubbed from all files. Prompts, replies, request summaries, "
        "images and media artifacts are not included. Check before sharing anyway.\n")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, content in files.items():
            text = content if isinstance(content, str) else json.dumps(content, indent=1, default=str)
            z.writestr(name, scrub(text, secrets))
    return buf.getvalue()
