"""Subscription usage from CLIProxyAPI — the ChatGPT/Codex (and Claude Code)
equivalent of Meridian's usage windows.

CLIProxyAPI keeps, per signed-in account, the usage headers the provider sent
with its last response and serves them from its management API:

    GET /v0/management/auth-files   (Authorization: Bearer <management key>)
    -> {"files": [{"provider": "codex", "label": ..., "disabled": false,
                   "unavailable": false,
                   "quota": {"observed_at": "...", "signals": {
                       "X-Codex-Plan-Type": "plus",
                       "X-Codex-Primary-Used-Percent": "42",
                       "X-Codex-Primary-Window-Minutes": "300",
                       "X-Codex-Primary-Reset-After-Seconds": "3600",
                       "X-Codex-Secondary-Used-Percent": "61", ...}}}, ...]}

Codex reports two windows (5-hour and weekly) plus optional named per-model
limits; Claude Code reports anthropic-ratelimit-unified-<window>-utilization
(a 0..1 fraction) with a reset timestamp. A Retry-After still in force means
the account is rate-limited right now.

The readings are passive: they refresh whenever a request goes through
CLIProxyAPI, so each carries its age, and a window whose reset time has
passed counts as reset. A model is usable while at least one account that can
serve it is under the limit (default 85%) on every window covering it; with no
readings yet it is allowed (the call budgets, if set, still apply) unless
subscription_usage.fail_closed is on. Near the limit the cached reading is
bypassed and calls admitted since the last reading count against the limit.
"""

from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timezone
from typing import Optional

from .config import SubscriptionUsageConfig

log = logging.getLogger(__name__)

_CODEX_WINDOW = re.compile(r"^x-codex-(?P<ns>.*?)(?P<slot>primary|secondary)-used-percent$")
_CLAUDE_WINDOW = re.compile(r"^anthropic-ratelimit-unified-(?P<w>.+)-utilization$")


def _num(v) -> Optional[float]:
    try:
        return float(str(v).strip())
    except (TypeError, ValueError):
        return None


def _parse_ts(v) -> Optional[float]:
    if not v:
        return None
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _window_label(minutes: Optional[float], slot: str) -> str:
    if minutes:
        m = int(minutes)
        if m == 300:
            return "5-hour"
        if m == 10080:
            return "weekly"
        if m % 1440 == 0:
            return f"{m // 1440}-day"
        if m % 60 == 0:
            return f"{m // 60}-hour"
        return f"{m}-min"
    return slot


def _kind(minutes: Optional[float], slot: str) -> str:
    """session (the 5-hour window), weekly, or other."""
    if minutes:
        return "session" if int(minutes) == 300 else "weekly" if int(minutes) == 10080 else "other"
    return {"primary": "session", "secondary": "weekly"}.get(slot, "other")


def provider_of(model_id: str) -> Optional[str]:
    """Which CLIProxyAPI account type serves this model."""
    m = (model_id or "").lower()
    if any(k in m for k in ("claude", "opus", "sonnet", "haiku", "fable")):
        return "claude"
    if "gpt" in m or "codex" in m or re.match(r"^o\d", m):
        return "codex"
    return None


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9.]+", "-", (name or "").lower()).strip("-")


def parse_account(entry: dict, now: Optional[float] = None) -> Optional[dict]:
    """One auth-files entry -> {provider, label, plan, unavailable, disabled,
    observed_at, age_s, windows: [{name, used, resets_in_s, scope}]}.
    `used` is the EFFECTIVE percentage (0 once its reset time has passed);
    `scope` is None (all of the provider's models) or a model-name fragment."""
    provider = str(entry.get("provider") or entry.get("type") or "").lower()
    if provider not in ("codex", "claude"):
        return None
    now = now or time.time()
    quota = entry.get("quota") or {}
    sig = {str(k).lower(): v for k, v in (quota.get("signals") or {}).items()}
    observed = _parse_ts(quota.get("observed_at"))
    age = (now - observed) if observed else None
    windows: list[dict] = []

    def _effective(used: float, reset_after: Optional[float], reset_at: Optional[float]):
        if reset_after is not None and observed is not None and observed + reset_after <= now:
            return 0.0, 0
        if reset_at is not None and reset_at <= now:
            return 0.0, 0
        left = None
        if reset_after is not None and observed is not None:
            left = int(observed + reset_after - now)
        elif reset_at is not None:
            left = int(reset_at - now)
        return used, left

    if provider == "codex":
        for key, val in sig.items():
            m = _CODEX_WINDOW.match(key)
            if not m:
                continue
            used = _num(val)
            if used is None:
                continue
            ns, slot = m.group("ns"), m.group("slot")
            pre = f"x-codex-{ns}{slot}-"
            minutes = _num(sig.get(pre + "window-minutes"))
            reset_after = _num(sig.get(pre + "reset-after-seconds"))
            reset_at = _num(sig.get(pre + "reset-at"))
            eff, left = _effective(used, reset_after, reset_at)
            limit_name = sig.get(f"x-codex-{ns}limit-name") if ns else None
            if ns and not limit_name:
                # A namespaced limit with no name (x-codex-<short>-*): shown,
                # but its models are unknown, so it doesn't gate anything.
                scope = "?"
                name = f"{ns.rstrip('-')} {_window_label(minutes, slot)}"
            else:
                scope = _norm(limit_name) if limit_name else None
                name = (f"{limit_name} " if limit_name else "") + _window_label(minutes, slot)
            windows.append({"name": name, "used": round(eff, 1), "reported": used,
                            "resets_in_s": left, "scope": scope, "minutes": minutes,
                            "kind": _kind(minutes, slot), "rolled_over": eff == 0.0 and used > 0
                            and left == 0})
    else:
        for key, val in sig.items():
            m = _CLAUDE_WINDOW.match(key)
            if not m:
                continue
            frac = _num(val)
            if frac is None:
                continue
            used = frac * 100.0 if frac <= 1.5 else frac
            w = m.group("w")
            reset_at = _num(sig.get(f"anthropic-ratelimit-unified-{w}-reset"))
            eff, left = _effective(used, None, reset_at)
            scope = next((f for f in ("opus", "sonnet", "haiku", "fable") if f in w), None)
            windows.append({"name": w.replace("_", " "), "used": round(eff, 1),
                            "reported": round(used, 1), "resets_in_s": left, "scope": scope,
                            "kind": "session" if w.startswith("5h") else "weekly"
                            if w.startswith("7d") else "other",
                            "rolled_over": eff == 0.0 and used > 0 and left == 0})

    ra = _num(sig.get("retry-after"))
    if ra is not None and observed is not None and observed + ra > now:
        windows.append({"name": "rate-limited (Retry-After)", "used": 100.0, "reported": 100.0,
                        "resets_in_s": int(observed + ra - now), "scope": None})
    windows.sort(key=lambda w: -w["used"])
    return {"provider": provider, "label": entry.get("label") or entry.get("name") or "",
            "plan": sig.get("x-codex-plan-type") or "",
            "disabled": bool(entry.get("disabled")),
            "unavailable": bool(entry.get("unavailable")),
            "observed_at": quota.get("observed_at"), "age_s": int(age) if age is not None else None,
            "windows": windows}


def _applies(window: dict, model_id: str) -> bool:
    scope = window.get("scope")
    if scope == "?":
        return False
    return scope is None or scope in _norm(model_id)


def _fmt_reset(s: Optional[int]) -> str:
    if s is None:
        return ""
    if s >= 86400:
        return f", resets in {s // 86400}d {s % 86400 // 3600}h"
    if s >= 3600:
        return f", resets in {s // 3600}h {s % 3600 // 60}m"
    return f", resets in {max(0, s) // 60}m"


class CliproxyUsage:
    def __init__(self, cfg, client, db=None):
        # `cfg` is a SubscriptionUsageConfig or a callable returning the live
        # one (so edits saved from the UI apply without a restart).
        self._cfg = cfg
        self.client = client
        self.db = db
        self._cache: dict[str, tuple[float, dict]] = {}
        self._denied: set[str] = set()       # backends/providers already logged at limit
        self._inflight: dict[tuple, list[float]] = {}    # (mgmt root, provider) -> admit times
        self._obs_seen: dict[tuple, float] = {}          # newest reading seen per key

    @property
    def cfg(self) -> SubscriptionUsageConfig:
        return self._cfg() if callable(self._cfg) else self._cfg

    @staticmethod
    def configured(info: Optional[dict]) -> bool:
        return bool(info and info.get("flavor") == "cliproxyapi" and info.get("management_key"))

    @staticmethod
    def _mgmt_root(info: dict) -> str:
        url = (info.get("management_url") or info.get("url") or "").rstrip("/")
        return url[:-3] if url.endswith("/v1") else url

    def limit_for(self, info: dict) -> float:
        v = info.get("usage_limit_pct")
        return float(v) if v else float(self.cfg.limit_pct)

    async def snapshot(self, info: dict, force: bool = False) -> dict:
        """All accounts behind one CLIProxyAPI backend (cached briefly)."""
        root = self._mgmt_root(info)
        hit = self._cache.get(root)
        if hit and not force and time.monotonic() - hit[0] < self.cfg.cache_seconds:
            return hit[1]
        snap: dict = {"backend": info.get("name") or "", "ok": False, "error": "",
                      "accounts": [], "limit_pct": self.limit_for(info)}
        try:
            r = await self.client.get(f"{root}/v0/management/auth-files", timeout=10,
                                      headers={"Authorization": f"Bearer {info['management_key']}"})
            if r.status_code != 200:
                hint = (" — set management.allow-remote: true in CLIProxyAPI when Foundry "
                        "runs on another host" if r.status_code in (403, 404) else "")
                snap["error"] = f"HTTP {r.status_code}{hint}"
            else:
                now = time.time()
                accts = [parse_account(f, now) for f in (r.json() or {}).get("files") or []]
                snap["accounts"] = [a for a in accts if a]
                snap["ok"] = True
        except Exception as e:                                   # noqa: BLE001
            snap["error"] = str(e)[:200]
        self._cache[root] = (time.monotonic(), snap)
        return snap

    # -- in-flight accounting ---------------------------------------------------
    # A reading moves only when a response completes, so calls admitted since
    # the last reading are invisible to it. Each admitted call is held as a
    # reservation (admit time) per (management root, provider); a newer reading
    # retires the oldest, and none outlives inflight_ttl_seconds.

    def _inflight_count(self, key: tuple, accounts: list[dict]) -> int:
        now = time.time()
        q = [t for t in self._inflight.get(key, []) if now - t < self.cfg.inflight_ttl_seconds]
        obs = max((o for o in (_parse_ts(a.get("observed_at")) for a in accounts) if o),
                  default=None)
        if obs is not None:
            seen = self._obs_seen.get(key)
            if seen is not None and obs > seen and q:
                q.pop(0)
            self._obs_seen[key] = max(obs, seen or 0.0)
        self._inflight[key] = q
        return len(q)

    @staticmethod
    def _pick(accounts: list[dict], model_id: str, extra: float):
        """The least-used readable account: (pct incl. extra, account, window,
        raw pct), plus how many accounts have no reading."""
        best, unknown = None, 0
        for a in accounts:
            ws = [w for w in a["windows"] if _applies(w, model_id)]
            if not ws and not a["unavailable"]:
                unknown += 1            # no readings for this account yet
                continue
            worst = max(ws, key=lambda w: w["used"]) if ws else None
            raw = 100.0 if a["unavailable"] else (worst["used"] if worst else 0.0)
            pct = min(100.0, raw + extra)
            if best is None or pct < best[0]:
                best = (pct, a, worst, raw)
        return best, unknown

    async def model_status(self, model_id: str, info: dict, reserve: bool = False) -> dict:
        """{available, known, worst, note, reset_s} for one model. `reserve`
        counts an allowed call as in flight until a newer reading arrives."""
        provider = provider_of(model_id)
        out = {"available": True, "known": False, "worst": None, "note": "", "reset_s": None,
               "provider": provider, "inflight": 0}
        if not provider or not self.configured(info):
            return out
        cfg = self.cfg
        closed = bool(cfg.fail_closed)
        snap = await self.snapshot(info)
        if not snap["ok"]:
            out["note"] = f"usage unavailable ({snap['error']})"
            if closed:
                out["available"] = False
                out["note"] += " — refused (fail_closed)"
            return out
        limit = self.limit_for(info)
        rk = (self._mgmt_root(info), provider)

        def _read(s: dict):
            accts = [a for a in s["accounts"] if a["provider"] == provider and not a["disabled"]]
            n = self._inflight_count(rk, accts) if cfg.inflight_pct > 0 else 0
            return (*self._pick(accts, model_id, n * cfg.inflight_pct), n)

        best, unknown, n = _read(snap)
        # Near the cutoff the cached reading may predate the crossing: re-read.
        if best is not None and best[0] >= limit - (cfg.reserve_pct + 2.0):
            snap = await self.snapshot(info, force=True)
            if snap["ok"]:
                best, unknown, n = _read(snap)
            elif closed:
                out["available"] = False
                out["note"] = f"usage unavailable ({snap['error']}) — refused (fail_closed)"
                return out
        if best is None:
            out["note"] = f"{provider}: no usage reading yet"
            if closed:
                out["available"] = False
                out["note"] += " — refused (fail_closed)"
            return out
        pct, acct, worst, raw = best
        out.update(known=True, worst=raw, reset_s=(worst or {}).get("resets_in_s"), inflight=n)
        label = f"{'ChatGPT' if provider == 'codex' else 'Claude'}" + \
                (f" {acct['plan']}" if acct.get("plan") else "")
        parts = ", ".join(f"{w['name']} {w['used']:.0f}%" for w in
                          [w for w in acct["windows"] if _applies(w, model_id)][:3])
        out["note"] = f"{label} usage: {parts or 'unavailable account'}"
        if pct >= limit:
            out["available"] = False
            why = (worst or {}).get("name") or "account unavailable"
            out["note"] = (f"{label} {why} at {pct:.0f}% (limit {limit:.0f}%)"
                           f"{f', incl. {n} call(s) in flight' if pct > raw else ''}"
                           f"{_fmt_reset((worst or {}).get('resets_in_s'))}")
            key = f"{snap['backend']}:{provider}:{why}"
            if key not in self._denied and self.db is not None:
                self._denied.add(key)
                try:
                    self.db.log_event("warning", "subscription_usage",
                                      f"{snap['backend']}: {out['note']} — {model_id} not used "
                                      f"until it drops below the limit", "")
                except Exception:                                # noqa: BLE001
                    pass
        else:
            self._denied = {k for k in self._denied
                            if not k.startswith(f"{snap['backend']}:{provider}:")}
            if reserve and cfg.inflight_pct > 0:
                self._inflight.setdefault(rk, []).append(time.time())
        return out
