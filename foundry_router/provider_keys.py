"""The router's own provider keys (Meridian, CLIProxyAPI, OpenRouter, LiteLLM, ...).

config.yaml refers to them as ${NAME}. They come from two places:
  - the container environment (the host's `.env` next to docker-compose.yml);
  - `provider-keys.env` beside config.yaml (mode 0600), which the router installer
    and the web UI write (Access → Secrets → Provider keys, the setup wizard).

A non-empty environment value wins, so an existing `.env` keeps working; the UI then
says the key comes from the host and can only be changed there.

Values are write-only: no route returns one. They are not in the secret store, so a
router restart never stops model calls (Victor 2026-10-31, fresh-install plan D4a).
Backups carry this file inside their encrypted archive (backup.py).
"""

from __future__ import annotations

import os
import re
import threading
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

FILE = "provider-keys.env"
NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
MAX_VALUE = 4096
_REF_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
# Shown even before config.yaml refers to them (the wizard's backend cards).
KNOWN = {
    "MERIDIAN_API_KEY": "Meridian bridge (Claude subscription)",
    "CLIPROXY_API_KEY": "CLIProxyAPI (ChatGPT subscription)",
    "CLIPROXY_MGMT_KEY": "CLIProxyAPI management key (reads ChatGPT usage)",
    "OPENROUTER_API_KEY": "OpenRouter",
    "LITELLM_KEY": "LiteLLM proxy",
}
_lock = threading.Lock()


class ProviderKeyError(ValueError):
    pass


def path_for(config_dir: Path | str) -> Path:
    return Path(config_dir) / FILE


def read(config_dir: Path | str) -> dict[str, str]:
    """NAME -> value from provider-keys.env ({} when there is none)."""
    p = path_for(config_dir)
    try:
        text = p.read_text(encoding="utf-8")
    except (FileNotFoundError, NotADirectoryError):
        return {}
    out: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        if NAME_RE.match(k):
            out[k] = v
    return out


def _write(config_dir: Path | str, values: dict[str, str]) -> None:
    p = path_for(config_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    body = ("# Foundry router provider keys: written by the installer and the web UI\n"
            "# (Access -> Secrets -> Provider keys). Never commit or share this file.\n"
            + "".join(f"{k}={values[k]}\n" for k in sorted(values)))
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
        f.write(body)
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, p)


def check_name(name: str) -> str:
    name = str(name or "").strip()
    if not NAME_RE.match(name):
        raise ProviderKeyError("name: UPPER_SNAKE_CASE, e.g. MERIDIAN_API_KEY")
    return name


def set_value(config_dir: Path | str, name: str, value: str) -> None:
    name = check_name(name)
    value = str(value if value is not None else "").strip()
    if not value:
        raise ProviderKeyError("empty value: use Clear to remove a key")
    if "\n" in value or "\r" in value:
        raise ProviderKeyError("a key is one line")
    if len(value) > MAX_VALUE:
        raise ProviderKeyError(f"longer than {MAX_VALUE} characters")
    with _lock:
        vals = read(config_dir)
        vals[name] = value
        _write(config_dir, vals)


def clear(config_dir: Path | str, name: str) -> bool:
    name = check_name(name)
    with _lock:
        vals = read(config_dir)
        if name not in vals:
            return False
        del vals[name]
        _write(config_dir, vals)
        return True


def refs(raw: Any, at: str = "") -> dict[str, list[str]]:
    """${NAME} references in the raw config document: NAME -> where they are used."""
    out: dict[str, list[str]] = {}

    def walk(o, where):
        if isinstance(o, dict):
            label = o.get("name") if isinstance(o.get("name"), str) else None
            for k, v in o.items():
                walk(v, f"{where}.{label}.{k}" if label and where else
                     (f"{where}.{k}" if where else str(k)))
        elif isinstance(o, list):
            for v in o:
                walk(v, where)
        elif isinstance(o, str):
            for m in _REF_RE.finditer(o):
                out.setdefault(m.group(1), []).append(where)
    walk(raw, at)
    return out


def status(raw: dict, config_dir: Path | str) -> list[dict]:
    """Every provider key: where it is used and where its value comes from. No values."""
    used = refs(raw)
    file_vals = read(config_dir)
    names = sorted(set(KNOWN) | {n for n in used if not n.startswith("FOUNDRY_")} | set(file_vals))
    rows = []
    for n in names:
        env = os.environ.get(n) or ""
        src = "environment" if env else ("file" if file_vals.get(n) else "unset")
        rows.append({"name": n, "label": KNOWN.get(n, ""), "used_by": used.get(n, []),
                     "source": src, "set": src != "unset"})
    return rows


# -- admin API (Access -> Secrets -> Provider keys, the setup wizard) ------------------

router = APIRouter()
A = "/admin/api/provider-keys"


def _svc(request: Request):
    return request.app.state.services


def _dir(svc) -> Path:
    return Path(svc.config_store.path).parent


def _bad(msg: str, status_code: int = 400) -> JSONResponse:
    return JSONResponse({"error": msg}, status_code=status_code,
                        headers={"Cache-Control": "no-store"})


@router.get(A)
async def list_keys(request: Request):
    svc = _svc(request)
    return {"keys": status(svc.config_store.raw, _dir(svc)),
            "file": str(path_for(_dir(svc)))}


async def _apply(svc) -> None:
    """Re-expand ${NAME} in the config and rebuild the backends, so a new key works now."""
    svc.config_store.reexpand()
    await svc.rebuild_pool()


@router.put(A + "/{name}")
async def put_key(request: Request, name: str):
    from .secrets_api import _need_transport
    from .secret_store import SecretStoreError
    svc = _svc(request)
    try:
        _need_transport(request)
    except SecretStoreError as e:
        return _bad(str(e), e.status)
    try:
        body = await request.json()
    except Exception:                                            # noqa: BLE001
        return _bad("expected JSON {value}")
    try:
        n = check_name(name)
        if os.environ.get(n):
            return _bad(f"{n} is set in the router host's .env, which wins over this page. "
                        "Change it there, or remove it there to manage it here.", 409)
        set_value(_dir(svc), n, (body or {}).get("value"))
    except ProviderKeyError as e:
        return _bad(str(e))
    svc.db.log_event("info", "provider_keys", f"provider key {n} set from the UI")
    await _apply(svc)
    return {"ok": True, "keys": status(svc.config_store.raw, _dir(svc))}


@router.delete(A + "/{name}")
async def delete_key(request: Request, name: str):
    svc = _svc(request)
    try:
        n = check_name(name)
        removed = clear(_dir(svc), n)
    except ProviderKeyError as e:
        return _bad(str(e))
    if removed:
        svc.db.log_event("info", "provider_keys", f"provider key {n} cleared from the UI")
        await _apply(svc)
    return {"ok": True, "removed": removed, "keys": status(svc.config_store.raw, _dir(svc))}
