"""Versioned default persona sets ("profiles").

A profile is a named, versioned list of persona definitions. Applying it:
- creates personas that don't exist yet (with `profile` = "<id>@<version>" and a
  hash of the values the profile set);
- updates a profile-managed persona to a newer version ONLY while its managed
  values still match the hash it was given, i.e. nobody has edited it;
- never touches a persona someone edited, a same-named persona the profile
  didn't create, or the `enabled` switch and validation results (those are
  the operator's).

The result is a report: created / updated / unchanged / kept (your edits) /
conflict (same name, not ours).

Backends are named by kind (local / meridian / cliproxyapi) and resolved to the
actual backend name at apply time, so the persona records an exact backend.
"""

from __future__ import annotations

import hashlib
import json
from typing import Optional

from .db import utcnow

# Fields a profile manages (and hashes). `enabled` is set only on creation.
MANAGED = ("description", "execution_mode", "routing", "backend", "model_allowlist",
           "pinned_models", "capabilities", "kind", "max_output_tokens",
           "context_window", "reasoning_effort", "fallback_policy", "benchmark_category",
           "local_bias_strength")
_JSON_FIELDS = ("model_allowlist", "pinned_models", "capabilities")

_HELPER = "Read-only helper for Kilo: returns findings or proposed patches, never edits."


def _p(name, kind, model, role, caps, *, enabled=False, out=8192, effort=None,
       persona_kind="chat"):
    return {"virtual_name": name, "backend_kind": kind, "model": model,
            "description": role, "capabilities": caps, "enabled": enabled,
            "max_output_tokens": out, "reasoning_effort": effort, "kind": persona_kind}


PROFILES = {
    "animaluprising-kilo": {
        "version": 1,
        "title": "AnimalUprising: Kilo personas",
        "description": "Kilo-Local for primary coding (enabled) plus frontier helpers, "
                       "vision reviewers and image policies (all disabled until verified). "
                       "Every persona uses strict routing: its one backend and model, or a "
                       "clear refusal.",
        "personas": [
            _p("Foundry-Kilo-Local", "local", "qwen3.8-flash-next-iq3_s",
               "Primary coding, planning, exploration and summaries (local Strata).",
               ["text", "tools"], enabled=True, out=32768),
            _p("Foundry-Kilo-Haiku", "meridian", "claude-haiku-5-5",
               "Bounded research, extraction, small second opinions. " + _HELPER,
               ["text", "tools"], effort="low"),
            _p("Foundry-Kilo-Sonnet", "meridian", "claude-sonnet-5-5",
               "Focused coding advice and review. " + _HELPER, ["text", "tools"],
               effort="medium"),
            _p("Foundry-Kilo-Opus", "meridian", "claude-opus-5-5",
               "Difficult blockers and architecture review. " + _HELPER, ["text", "tools"],
               effort="high"),
            _p("Foundry-Kilo-Luna", "cliproxyapi", "gpt-6-luna",
               "Efficient GPT helper for focused tasks. " + _HELPER, ["text", "tools"],
               effort="low"),
            _p("Foundry-Kilo-Sol", "cliproxyapi", "gpt-6.1-sol",
               "Debugging, test advice, independent review. " + _HELPER, ["text", "tools"],
               effort="medium"),
            _p("Foundry-Kilo-Astra", "cliproxyapi", "gpt-6-astra",
               "Difficult reasoning and high-risk review. " + _HELPER, ["text", "tools"],
               effort="high"),
            _p("Foundry-Kilo-Vision", "meridian", "claude-sonnet-5-5",
               "Routine UI / Blender screenshot diagnosis. Reports visible defect, evidence "
               "and location, likely cause (labelled as inference), proposed correction and "
               "how to verify it.", ["text", "image_input", "tools"], effort="medium"),
            _p("Foundry-Kilo-Vision-Deep", "cliproxyapi", "gpt-6-astra",
               "Difficult visual diagnosis (same report format as Foundry-Kilo-Vision).",
               ["text", "image_input", "tools"], effort="high"),
            _p("Foundry-Kilo-Images", "cliproxyapi", "gpt-image-2.5-sunburst",
               "Image policy: precise asset generation and editing (generate_image / "
               "edit_image with model 'Foundry-Kilo-Images').",
               ["image_output", "image_input"], out=None, persona_kind="media"),
            _p("Foundry-Kilo-Images-Draft", "cliproxyapi", "gpt-image-2.5-flare",
               "Image policy: fast concept generation.", ["image_output", "image_input"],
               out=None, persona_kind="media"),
        ],
    },
}


def _resolve_backend(svc, kind: str) -> Optional[str]:
    backends = getattr(svc.pool, "backends", None) or {}
    ranked = sorted(backends.values(), key=lambda s: -s.config.priority)
    for s in ranked:
        c = s.config
        if not getattr(c, "enabled", True):
            continue
        if kind == "meridian" and c.type == "anthropic-compatible":
            return c.name
        if kind == "cliproxyapi" and c.effective_flavor == "cliproxyapi":
            return c.name
    if kind == "local":
        # The local backend that serves the model is decided per model below.
        return None
    return None


def _local_backend_for(svc, model: str) -> Optional[str]:
    from .facade.ollama_api import _is_paid_backend
    backends = getattr(svc.pool, "backends", None) or {}
    for s in sorted(backends.values(), key=lambda s: -s.config.priority):
        info = {"type": s.config.type, "flavor": s.config.effective_flavor,
                "subscription": s.config.is_subscription}
        if (model in s.models or model in (s.config.models or [])) and not _is_paid_backend(info):
            return s.config.name
    return None


def definition_fields(svc, d: dict) -> dict:
    """The persona column values a definition sets (backend resolved now)."""
    backend = (_local_backend_for(svc, d["model"]) if d["backend_kind"] == "local"
               else _resolve_backend(svc, d["backend_kind"]))
    # Not found: a placeholder that matches no backend, so strict routing
    # refuses instead of taking the model from whichever backend has it (e.g.
    # a Claude account inside CLIProxyAPI). Filled in once the backend exists.
    backend = backend or f"<{d['backend_kind']}>"
    return {
        "description": d["description"],
        "execution_mode": "direct",
        "routing": "strict",
        "backend": backend,
        "model_allowlist": [d["model"]],
        "pinned_models": [d["model"]],
        "capabilities": list(d["capabilities"]),
        "kind": d["kind"],
        "max_output_tokens": d["max_output_tokens"],
        "context_window": None,
        "reasoning_effort": d["reasoning_effort"],
        "fallback_policy": "refuse",
        "benchmark_category": "coding" if d["kind"] == "chat" else "general_chat",
        "local_bias_strength": "strong" if d["backend_kind"] == "local" else "prefer_paid",
    }


def _norm(field: str, value):
    if field in _JSON_FIELDS:
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except ValueError:
                return value
        return value or []
    if value == "":
        return None
    if field in ("max_output_tokens", "context_window") and value is not None:
        try:
            return int(value) or None
        except (TypeError, ValueError):
            return value
    return value


def values_hash(row: dict) -> str:
    """Hash of the managed values as stored (JSON fields and numbers
    normalised), so a UI save of unchanged values isn't mistaken for an edit."""
    data = {f: _norm(f, row.get(f)) for f in MANAGED}
    return hashlib.sha256(json.dumps(data, sort_keys=True, default=str).encode()).hexdigest()[:24]


def status(svc, profile_id: str) -> dict:
    """What applying the profile would do, persona by persona (no changes)."""
    return _apply(svc, profile_id, dry_run=True)


def apply(svc, profile_id: str) -> dict:
    return _apply(svc, profile_id, dry_run=False)


def _apply(svc, profile_id: str, dry_run: bool) -> dict:
    prof = PROFILES.get(profile_id)
    if prof is None:
        raise KeyError(profile_id)
    tag = f"{profile_id}@{prof['version']}"
    report = []
    for d in prof["personas"]:
        name = d["virtual_name"]
        fields = definition_fields(svc, d)
        row = svc.personas.get(name)
        entry = {"persona": name, "model": d["model"], "backend": fields["backend"],
                 "backend_kind": d["backend_kind"], "kind": d["kind"],
                 "enabled_default": d["enabled"]}
        if row is None:
            entry["action"] = "create"
            if not dry_run:
                svc.personas.upsert(name, **fields, enabled=1 if d["enabled"] else 0)
                _stamp(svc, name, tag)
        elif not row.get("profile") or not str(row["profile"]).startswith(profile_id + "@"):
            entry["action"] = "conflict"
            entry["note"] = "a persona with this name exists and wasn't created by this profile"
        elif values_hash(row) != row.get("profile_hash"):
            entry["action"] = "kept"
            entry["note"] = "edited since it was installed; your values are kept"
        elif row["profile"] == tag:
            entry["action"] = "unchanged"
            if _unresolved(row.get("backend")) and not _unresolved(fields["backend"]) and not dry_run:
                svc.personas.upsert(name, backend=fields["backend"])   # backend appeared later
                _stamp(svc, name, tag)
        else:
            entry["action"] = "update"
            entry["from"] = row["profile"]
            if not dry_run:
                svc.personas.upsert(name, **fields)
                _stamp(svc, name, tag)
        if row is not None:
            entry["enabled"] = bool(row.get("enabled"))
        if _unresolved(fields["backend"]):
            entry["backend"] = None
            entry["warning"] = (f"no {d['backend_kind']} backend found"
                                + (f" serving {d['model']}" if d["backend_kind"] == "local" else "")
                                + "; strict routing will refuse until one exists")
        report.append(entry)
    counts: dict = {}
    for e in report:
        counts[e["action"]] = counts.get(e["action"], 0) + 1
    if not dry_run and (counts.get("create") or counts.get("update")):
        svc.db.log_event("info", "profiles", f"profile {tag} applied: " +
                         ", ".join(f"{v} {k}" for k, v in sorted(counts.items())))
    return {"profile": profile_id, "version": prof["version"], "title": prof["title"],
            "description": prof["description"], "personas": report, "counts": counts}


def _unresolved(backend) -> bool:
    return not backend or str(backend).startswith("<")


def _stamp(svc, name: str, tag: str) -> None:
    row = svc.personas.get(name)
    svc.db.execute("UPDATE personas SET profile=?, profile_hash=?, updated_at=? "
                   "WHERE virtual_name=?", (tag, values_hash(row), utcnow(), row["virtual_name"]))


def list_profiles(svc) -> list[dict]:
    out = []
    for pid, prof in PROFILES.items():
        st = status(svc, pid)
        installed = st["counts"].get("create", 0) < len(prof["personas"])
        out.append({"id": pid, "version": prof["version"], "title": prof["title"],
                    "description": prof["description"], "installed": installed,
                    "counts": st["counts"], "personas": st["personas"]})
    return out
