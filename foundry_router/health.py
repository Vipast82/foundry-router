"""Functional health: can each backend actually SERVE, not just answer HTTP?

The pool's health loop only proves a backend's HTTP server responds (model
list / props). This module tracks whether it can complete a generation:

- Evidence from real traffic first: the budget ledger records every call, so
  the last successful call and the last failed one per backend are free.
- A tiny probe ("reply OK", 16 tokens, thinking off) only when a backend has
  had no successful call for `health.probe_minutes` and is idle. Local
  backends are probed automatically; paid/subscription backends only on
  demand (or with health.probe_paid), because a probe spends quota.

Status per backend:
  serving     a successful generation (real or probe) within fresh_minutes,
              and no failure after it
  failing     the most recent generation attempt failed
  unverified  the HTTP server answers but nothing has generated recently
  down        the pool's health check is failing
  disabled    parked in config
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

log = logging.getLogger(__name__)


class HealthMonitor:
    def __init__(self, svc):
        self.svc = svc
        self.probes: dict[str, dict] = {}       # backend -> last probe result
        self._status: dict[str, str] = {}       # last reported status (event on change)

    @property
    def cfg(self):
        return self.svc.config_store.config.health

    # -- evidence ---------------------------------------------------------------

    def _ledger_evidence(self, since: float) -> dict:
        out: dict = {}
        try:
            rows = self.svc.db.query(
                "SELECT backend, model, status, ts, settled_ts, detail FROM usage_events "
                "WHERE ts >= ? AND status IN ('settled','error','released') ORDER BY id", (since,))
        except Exception:                                         # noqa: BLE001
            return out
        hour = time.time() - 3600
        for r in rows:
            e = out.setdefault(r["backend"], {"last_ok": None, "last_error": None,
                                              "calls_1h": 0, "errors_1h": 0})
            t = r["settled_ts"] or r["ts"]
            if r["status"] == "settled":
                e["last_ok"] = {"at": t, "model": r["model"], "source": "real call"}
            else:
                e["last_error"] = {"at": t, "model": r["model"], "detail": r["detail"] or r["status"]}
            if r["ts"] >= hour:
                e["calls_1h"] += 1
                e["errors_1h"] += r["status"] != "settled"
        return out

    def _paid(self, cfg) -> bool:
        from .budget import is_paid
        return is_paid(cfg)

    def status(self) -> list[dict]:
        now = time.time()
        fresh = max(1, self.cfg.fresh_minutes) * 60
        ev = self._ledger_evidence(now - 86400)
        out = []
        pool = self.svc.pool
        for s in (getattr(pool, "backends", None) or {}).values():
            c = s.config
            e = ev.get(c.name, {"last_ok": None, "last_error": None, "calls_1h": 0, "errors_1h": 0})
            probe = self.probes.get(c.name)
            last_ok = e["last_ok"]
            if probe and probe["ok"] and (not last_ok or probe["at"] > last_ok["at"]):
                last_ok = {"at": probe["at"], "model": probe["model"], "source": "probe"}
            last_err = e["last_error"]
            if probe and not probe["ok"] and (not last_err or probe["at"] > last_err["at"]):
                last_err = {"at": probe["at"], "model": probe["model"],
                            "detail": "probe: " + (probe.get("detail") or "failed")}
            if s.ever_checked and not s.healthy:
                st = "down"
            elif last_err and (not last_ok or last_err["at"] > last_ok["at"]) \
                    and now - last_err["at"] < fresh:
                st = "failing"
            elif last_ok and now - last_ok["at"] < fresh:
                st = "serving"
            else:
                st = "unverified"
            out.append({
                "backend": c.name, "type": c.type, "flavor": c.effective_flavor,
                "paid": self._paid(c), "status": st,
                "reachable": (s.healthy if s.ever_checked else None),
                "reachable_error": s.last_error or None,
                "last_ok": _aged(last_ok, now), "last_error": _aged(last_err, now),
                "calls_1h": e["calls_1h"], "errors_1h": e["errors_1h"],
                "busy": s.busy, "probe": _aged(probe, now) if probe else None,
                "auto_probe": (self.cfg.probe_minutes > 0
                               and (not self._paid(c) or self.cfg.probe_paid)),
            })
            self._note_change(c.name, st, out[-1])
        for c in getattr(pool, "disabled", []) or []:
            out.append({"backend": c.name, "type": c.type, "flavor": c.effective_flavor,
                        "paid": self._paid(c), "status": "disabled", "reachable": None,
                        "last_ok": None, "last_error": None, "calls_1h": 0, "errors_1h": 0,
                        "busy": 0, "probe": None, "auto_probe": False})
        return out

    def _note_change(self, name: str, st: str, row: dict) -> None:
        prev = self._status.get(name)
        self._status[name] = st
        if prev is None or prev == st:
            return
        if st in ("failing", "down"):
            why = (row.get("last_error") or {}).get("detail") or row.get("reachable_error") or ""
            self.svc.db.log_event("warning", "health", f"backend {name}: {prev} → {st}", why[:300])
        elif st == "serving":
            self.svc.db.log_event("info", "health", f"backend {name}: {prev} → serving")

    # -- probes -----------------------------------------------------------------

    def _probe_model(self, s) -> Optional[str]:
        from .media import is_image_model
        for m in s.models:
            meta = self.svc.registry.get(m) or {}
            if meta.get("embedding") or is_image_model(m):
                continue
            return m
        return None

    async def probe(self, name: str, model: Optional[str] = None) -> dict:
        """One tiny real generation on exactly this backend."""
        s = (getattr(self.svc.pool, "backends", None) or {}).get(name)
        if s is None:
            raise KeyError(name)
        model = model or self._probe_model(s)
        if not model:
            res = {"ok": False, "at": time.time(), "model": None, "ms": None,
                   "detail": "no chat model listed by this backend"}
            self.probes[name] = res
            return res
        return await asyncio.create_task(self._probe(name, model))   # isolated context

    async def _probe(self, name: str, model: str) -> dict:
        from . import client_auth, request_context
        client_auth._client.set({"client_id": "foundry:health", "internal": True,
                                 "scopes": ["inference"], "personas": []})
        request_context._request_id.set(f"health-{name}-{int(time.time())}")
        request_context.pin_backend(name)
        t0 = time.monotonic()
        try:
            r, _b = await asyncio.wait_for(self.svc.pool.chat(
                model, [{"role": "user", "content": "Reply with the single word OK."}],
                max_tokens=16, think=False), timeout=max(10, self.cfg.probe_timeout_seconds))
            ok = bool((r.content or "").strip() or r.thinking or r.completion_tokens)
            res = {"ok": ok, "detail": "" if ok else "empty reply",
                   "reply": (r.content or "")[:40]}
        except asyncio.TimeoutError:
            res = {"ok": False, "detail": f"no reply within {self.cfg.probe_timeout_seconds}s"}
        except Exception as e:                                    # noqa: BLE001
            res = {"ok": False, "detail": str(e)[:300]}
        res.update(at=time.time(), model=model, ms=round((time.monotonic() - t0) * 1000))
        self.probes[name] = res
        return res

    async def loop(self) -> None:
        """Probe idle backends that have no recent success (local only unless
        probe_paid). Busy or recently-successful backends are never probed."""
        while True:
            await asyncio.sleep(60)
            minutes = self.cfg.probe_minutes
            if minutes <= 0:
                continue
            try:
                for row in self.status():
                    if row["status"] in ("disabled", "down") or not row["auto_probe"] or row["busy"]:
                        continue
                    last = max([x["at"] for x in (row["last_ok"], row["last_error"]) if x] or [0])
                    if time.time() - last >= minutes * 60:
                        await self.probe(row["backend"])
            except Exception:                                     # noqa: BLE001
                log.exception("functional health loop error")


def _aged(d: Optional[dict], now: float) -> Optional[dict]:
    if not d:
        return None
    return {**d, "age_s": round(now - d["at"])}
