"""Alerts that reach Victor without a watcher, and the daily digest.

Self-driving plan, parts 4 and 5 (reports/SELF_DRIVING_PLAN.md):

  coordinator down   The router is always on and receives the coordinator's
                     heartbeat. When the freshest one is older than
                     alerts.coordinator_down_minutes, ONE "coordinator down"
                     alert is raised (and one "recovered" alert when a
                     heartbeat arrives again). The state lives in the database,
                     so a router restart neither repeats nor loses it.
  alert policy       Every control-center alert stays on the page (bell,
                     browser notification). Only these classes are PUSHED to
                     the webhook, each switchable in the UI: product_decision,
                     user_action, image_ask (2D asks), stack_down,
                     self_repair_exhausted, prep_failed, pause_forgotten.
                     Everything else waits for the digest.
  webhook            Optional alerts.webhook_url: an ntfy topic (text body,
                     Title / Priority / Tags headers) or any URL that takes
                     JSON {title, text, class, severity, ts}. Sent from an
                     outbox with backoff retries; the URL and token never reach
                     a log line or an error message.
  daily digest       At alerts.digest_hour (router local time) a digest of the
                     last 24 h of coordinator facts the router holds: task
                     transitions and completions, self-heal actions with their
                     reasons, parked tasks, decisions by class, notices avoided
                     (automation decisions resolved without Victor), time per
                     state and tokens per sprint. Shown in the control center
                     (last 7) and sent to the webhook when set. Also on demand.

Decision class (wt-sd-decisions) is read defensively: decision["class"], then
decision["alert_class"], then decision["decision_class"] when it holds one of
product / user_action / automation; an older coordinator without classes falls
back to the decision kind.
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import re
import threading
import time
from typing import Any, Callable, Optional
from urllib.parse import urlsplit

from .config import ALERT_CLASSES, AlertsConfig

log = logging.getLogger(__name__)

SCHEMA = [
    "CREATE TABLE IF NOT EXISTS al_state (key TEXT PRIMARY KEY, value TEXT)",
    """CREATE TABLE IF NOT EXISTS al_outbox (
      id INTEGER PRIMARY KEY AUTOINCREMENT, dedupe TEXT UNIQUE, ts REAL, cls TEXT,
      title TEXT, body TEXT, severity TEXT, status TEXT, attempts INTEGER DEFAULT 0,
      next_ts REAL, last_error TEXT, sent_ts REAL)""",
    "CREATE INDEX IF NOT EXISTS idx_al_outbox_status ON al_outbox(status, next_ts)",
    """CREATE TABLE IF NOT EXISTS al_digests (
      id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, period_from REAL, period_to REAL,
      trigger TEXT, actor TEXT, data TEXT, text TEXT, sent TEXT)""",
]

CLASS_LABELS = {
    "product_decision": "Product decision",
    "user_action": "Needs you to act",
    "image_ask": "2D / image ask",
    "stack_down": "Stack down / recovered",
    "self_repair_exhausted": "Self-repair exhausted",
    "prep_failed": "Repo change did not land",
    "pause_forgotten": "Forgotten pause or hold",
}
MAX_ATTEMPTS = 6
RETRY_BASE_S = 30
DIGESTS_KEPT = 30
PARK_STATES = ("BLOCKED", "PARKED")
# decision kinds an older coordinator (no class field) opens for tooling, budget
# and infrastructure problems: digest only (decision_policy.PARK_KINDS)
AUTOMATION_KINDS = frozenset({"recovery_escalation", "no_progress", "review_access",
                              "lead_escalation", "obligation_conflict"})
HUMAN_AUTHORITIES = frozenset({"operator", "human", "victor", "owner"})


def _j(v) -> str:
    return json.dumps(v, separators=(",", ":"), sort_keys=True, default=str)


def _uj(v, default=None):
    try:
        return json.loads(v) if isinstance(v, str) else (v if v is not None else default)
    except (TypeError, ValueError):
        return default


def _host(url: str) -> str:
    try:
        return urlsplit(url).netloc.rsplit("@", 1)[-1] or "?"
    except ValueError:
        return "?"


def webhook_format(url: str, fmt: str) -> str:
    if fmt in ("ntfy", "json"):
        return fmt
    return "ntfy" if "ntfy" in _host(url).lower() else "json"


def decision_class(d: dict) -> str:
    """product | user_action | automation for one decision (see module doc)."""
    for k in ("class", "alert_class", "decision_class"):
        v = d.get(k)
        if isinstance(v, str) and v in ("product", "user_action", "automation"):
            return v
    kind = str(d.get("kind") or "")
    blob = " ".join(str(x) for x in (d.get("evidence") or []) if isinstance(x, str))
    if kind in AUTOMATION_KINDS:
        return "automation"
    if kind == "implementer_blocked":
        return "user_action" if "user_action" in blob else "automation"
    if "user_action" in blob:
        return "user_action"
    return "product"


def is_image_ask(d: dict) -> bool:
    """The 2D-asset decision (foundry_coordinator/services.images_hold)."""
    opts = " ".join(str(x) for x in (d.get("options") or []))
    q = str(d.get("question") or "")
    return ("2D asset" in opts or "2D image" in q or bool(d.get("image_ask"))
            or str(d.get("kind") or "") in ("image_ask", "images"))


def _resolved_by_automation(d: dict) -> bool:
    auth = str(d.get("authority") or "").lower()
    actor = str(d.get("actor") or "").lower()
    return bool(auth) and auth not in HUMAN_AUTHORITIES and actor not in HUMAN_AUTHORITIES


class WebhookError(Exception):
    def __init__(self, msg: str, permanent: bool = False):
        super().__init__(msg)
        self.permanent = permanent


def _default_post(url: str, body: bytes, headers: dict, timeout: float) -> int:
    import httpx
    r = httpx.post(url, content=body, headers=headers, timeout=timeout)
    return r.status_code


class AlertService:
    def __init__(self, cc, cfg_fn: Callable[[], AlertsConfig],
                 clock: Callable[[], float] = time.time,
                 post: Callable[[str, bytes, dict, float], int] = _default_post,
                 localtime: Callable[[float], _dt.datetime] = None):
        self.cc = cc
        self.db = cc.db
        self.cfg_fn = cfg_fn
        self.clock = clock
        self.post = post
        self.localtime = localtime or (lambda ts: _dt.datetime.fromtimestamp(ts))
        self.started = clock()
        # one sender at a time (the tick, a manual digest, a test): an outbox row
        # is never sent twice
        self._lock = threading.RLock()
        for sql in SCHEMA:
            self.db.execute(sql)

    # ------------------------------------------------------------------ state

    def _get(self, key: str, default=None):
        r = self.db.query_one("SELECT value FROM al_state WHERE key=?", (key,))
        return _uj(r["value"], default) if r else default

    def _put(self, key: str, value) -> None:
        self.db.execute("INSERT INTO al_state (key, value) VALUES (?,?) ON CONFLICT(key) DO "
                        "UPDATE SET value=excluded.value", (key, _j(value)))

    def cfg(self) -> AlertsConfig:
        c = self.cfg_fn()
        return c if isinstance(c, AlertsConfig) else AlertsConfig()

    # ------------------------------------------------------------------ tick

    def tick(self) -> dict:
        """One pass (the router's watchdog loop, every 15 s): stack-down check,
        policy over new alerts, the outbox, and the digest when it is due."""
        with self._lock:
            out: dict[str, Any] = {}
            cfg = self.cfg()
            if not cfg.enabled:
                return {"enabled": False}
            out["coordinator"] = self.check_coordinator(cfg)
            out["queued"] = self.scan_alerts(cfg)
            out["delivered"] = self.deliver(cfg)
            out["digest"] = self.digest_if_due(cfg)
            return out

    # ------------------------------------------------------------------ coordinator down

    def coordinator_age(self) -> Optional[float]:
        r = self.db.query_one("SELECT MAX(last_seen) AS t FROM cc_sources WHERE kind='coordinator'")
        t = (r or {}).get("t")
        return None if t is None else self.clock() - float(t)

    def check_coordinator(self, cfg: AlertsConfig) -> str:
        """'unknown' (never seen) | 'up' | 'down' | 'went_down' | 'recovered'."""
        age = self.coordinator_age()
        if age is None:
            return "unknown"
        limit = cfg.coordinator_down_minutes * 60
        state = self._get("coordinator_down") or {}
        now = self.clock()
        if age > limit:
            if state.get("down"):
                return "down"
            if now - self.started < limit:
                # the router itself just started: the coordinator gets the full
                # window to heart-beat before a down alert (no false alarm after
                # a router restart)
                return "up"
            since = now - age
            self._put("coordinator_down", {"down": True, "since": since, "alert_ts": now})
            self.cc.raise_alert(
                "coordinator_down", "stack_down", "critical",
                f"Coordinator down: no heartbeat for {int(age // 60)} min",
                "The router has had no coordinator heartbeat since "
                f"{self.localtime(since):%Y-%m-%d %H:%M} (router time). The workstation may be "
                "off, asleep or rebooted without the Foundry Client starting, or the coordinator "
                "crashed. Nothing moves until it is back.", {})
            return "went_down"
        if state.get("down"):
            self._put("coordinator_down", {"down": False, "since": state.get("since"),
                                           "recovered_ts": now})
            mins = int((now - age - float(state.get("since") or now)) // 60)
            self.cc._resolve("coordinator_down", "coordinator heartbeat received again")
            self.cc.raise_alert(
                f"coordinator_recovered:{int(now)}", "stack_recovered", "info",
                f"Coordinator back after about {max(mins, 1)} min",
                "Heartbeats arrive again; the self-repair sweep picks up from here.", {})
            return "recovered"
        return "up"

    # ------------------------------------------------------------------ policy

    def classify(self, a: dict) -> str:
        """The push class of one control-center alert ('' = digest only)."""
        kind = str(a.get("kind") or "")
        if kind in ("stack_down", "stack_recovered"):
            return "stack_down"
        if kind in ("self_repair_exhausted", "prep_failed", "pause_forgotten"):
            return kind
        if kind == "approval":
            return "product_decision"
        if kind == "question":
            return "product_decision"
        if kind == "permission":
            return "user_action"
        tdata = {}
        if a.get("project_id") and a.get("task_id"):
            r = self.db.query_one("SELECT data FROM cc_tasks WHERE project_id=? AND task_id=?",
                                  (a["project_id"], a["task_id"]))
            tdata = _uj((r or {}).get("data"), {}) or {}
        if kind == "decision":
            did = str(a.get("dedupe_key") or "").split(":", 3)[-1]
            d = next((x for x in (tdata.get("decisions") or [])
                      if isinstance(x, dict) and x.get("decision_id") == did), None)
            if d is None:
                return "product_decision"          # unknown: never silence a real question
            if is_image_ask(d):
                return "image_ask"
            cls = decision_class(d)
            return {"product": "product_decision", "user_action": "user_action"}.get(cls, "")
        if kind == "blocker":
            bk = str(tdata.get("blocker_kind") or tdata.get("blocker_class") or "")
            return "user_action" if bk == "user_action" else ""
        return ""

    def scan_alerts(self, cfg: AlertsConfig) -> int:
        """Queue a push for each NEW alert (seq after the cursor) whose class is
        switched on. The first pass only sets the cursor (no flood of history)."""
        cur = self._get("alerts_cursor")
        top = int((self.db.query_one("SELECT MAX(seq) AS m FROM cc_alerts") or {}).get("m") or 0)
        if cur is None:
            self._put("alerts_cursor", top)
            return 0
        n = 0
        rows = self.db.query("SELECT * FROM cc_alerts WHERE seq > ? AND status='new' ORDER BY seq",
                             (int(cur),))
        for a in rows:
            cls = self.classify(a)
            if not cls or not cfg.class_on(cls) or not cfg.webhook_url:
                continue
            title = a["title"] or a["kind"]
            body = (a.get("detail") or "").strip()
            ref = " · ".join(x for x in (a.get("project_id"), a.get("task_id")) if x)
            if ref and ref not in title:
                body = f"{ref}\n{body}".strip()
            if self._enqueue(f"alert:{a['alert_id']}:{int(float(a['updated_ts'] or 0))}", cls,
                             title, body, a.get("severity") or "high"):
                n += 1
        if rows or top > int(cur):
            self._put("alerts_cursor", max(top, int(cur)))
        return n

    # ------------------------------------------------------------------ outbox / webhook

    def _enqueue(self, dedupe: str, cls: str, title: str, body: str, severity: str) -> bool:
        now = self.clock()
        cur = self.db.execute_count(
            "INSERT OR IGNORE INTO al_outbox (dedupe, ts, cls, title, body, severity, status, "
            "attempts, next_ts) VALUES (?,?,?,?,?,?,'pending',0,?)",
            (dedupe, now, cls, title[:250], body[:3000], severity, now))
        return bool(cur)

    def _scrub(self, text: str, cfg: AlertsConfig) -> str:
        text = str(text or "")
        for s in (cfg.webhook_url, cfg.webhook_token):
            if s:
                text = text.replace(s, "<webhook>")
        # an exception text may carry the URL in another spelling (no query, etc.)
        return re.sub(r"https?://\S+", "<url>", text)[:300]

    def _send(self, cfg: AlertsConfig, title: str, body: str, cls: str, severity: str) -> None:
        url = cfg.webhook_url
        if not url:
            raise WebhookError("no webhook URL set", permanent=True)
        fmt = webhook_format(url, cfg.webhook_format)
        headers: dict[str, str] = {}
        if cfg.webhook_token:
            headers["Authorization"] = f"Bearer {cfg.webhook_token}"
        if fmt == "ntfy":
            # HTTP headers are latin-1: keep the title readable without breaking
            headers["Title"] = title.encode("latin-1", "replace").decode("latin-1")[:200]
            headers["Priority"] = {"critical": "urgent", "high": "high",
                                   "info": "default"}.get(severity, "default")
            headers["Tags"] = {"stack_down": "rotating_light", "digest": "newspaper"}.get(
                cls, "bell")
            headers["Content-Type"] = "text/plain; charset=utf-8"
            data = (body or title).encode("utf-8")
        else:
            headers["Content-Type"] = "application/json"
            data = json.dumps({"title": title, "text": body, "class": cls, "severity": severity,
                               "ts": self.clock(), "source": "foundry-router"}).encode("utf-8")
        try:
            status = int(self.post(url, data, headers, 15.0))
        except Exception as e:                                   # noqa: BLE001
            raise WebhookError(f"{type(e).__name__}: {self._scrub(str(e), cfg)}") from None
        if status >= 400:
            raise WebhookError(f"HTTP {status}",
                               permanent=400 <= status < 500 and status not in (408, 429))

    def deliver(self, cfg: AlertsConfig) -> int:
        """Send what is due; failed sends retry with backoff (30 s, 1, 2, 4, 8
        min), a 4xx other than 408/429 is final. Returns how many were sent."""
        now = self.clock()
        sent = 0
        for r in self.db.query("SELECT * FROM al_outbox WHERE status='pending' AND next_ts <= ? "
                               "ORDER BY id LIMIT 20", (now,)):
            if not cfg.webhook_url:
                self.db.execute("UPDATE al_outbox SET status='skipped', last_error=? WHERE id=?",
                                ("no webhook URL set", r["id"]))
                continue
            attempts = int(r["attempts"] or 0) + 1
            try:
                self._send(cfg, r["title"], r["body"], r["cls"], r["severity"])
            except WebhookError as e:
                final = e.permanent or attempts >= MAX_ATTEMPTS
                self.db.execute("UPDATE al_outbox SET attempts=?, last_error=?, status=?, "
                                "next_ts=? WHERE id=?",
                                (attempts, str(e)[:300], "failed" if final else "pending",
                                 now + RETRY_BASE_S * 2 ** (attempts - 1), r["id"]))
                log.warning("alert webhook to %s failed (attempt %d%s): %s",
                            _host(cfg.webhook_url), attempts, ", giving up" if final else "",
                            str(e)[:200])
                if final:
                    self.db.log_event("warning", "alerts",
                                      f"webhook push '{r['title'][:80]}' failed after "
                                      f"{attempts} attempt(s): {str(e)[:200]}")
                continue
            self.db.execute("UPDATE al_outbox SET attempts=?, status='sent', sent_ts=?, "
                            "last_error=NULL WHERE id=?", (attempts, now, r["id"]))
            sent += 1
        self.db.execute_count("DELETE FROM al_outbox WHERE ts < ? AND status != 'pending'",
                              (now - 30 * 86400,))
        return sent

    def test_alert(self, actor: str) -> dict:
        """Settings' "Send test alert": one control-center alert, plus a webhook
        push sent right now (the result says what the webhook answered)."""
        with self._lock:
            return self._test_alert(actor, self.cfg())

    def _test_alert(self, actor: str, cfg: AlertsConfig) -> dict:
        now = self.clock()
        self.cc.raise_alert(f"alert_test:{int(now)}", "alert_test", "info",
                            "Test alert from Foundry",
                            f"Sent by {actor} from Alerts & digest settings.", {})
        if not cfg.webhook_url:
            return {"page": True, "webhook": "not set"}
        try:
            self._send(cfg, "Foundry test alert",
                       f"Test alert sent by {actor}. If this reached your phone, alerts work.",
                       "stack_down", "default")
        except WebhookError as e:
            return {"page": True, "webhook": "failed", "error": str(e),
                    "host": _host(cfg.webhook_url)}
        return {"page": True, "webhook": "sent", "host": _host(cfg.webhook_url),
                "format": webhook_format(cfg.webhook_url, cfg.webhook_format)}

    def outbox(self, limit: int = 30) -> list[dict]:
        return self.db.query("SELECT id, ts, cls, title, severity, status, attempts, last_error, "
                             "sent_ts FROM al_outbox ORDER BY id DESC LIMIT ?", (limit,))

    # ------------------------------------------------------------------ digest

    def digest_if_due(self, cfg: AlertsConfig) -> Optional[int]:
        if not cfg.digest_enabled:
            return None
        now = self.clock()
        lt = self.localtime(now)
        if lt.hour < cfg.digest_hour:
            return None
        day = lt.strftime("%Y-%m-%d")
        if self._get("digest_day") == day:
            return None
        self._put("digest_day", day)
        return self.build_digest("schedule", "foundry", cfg)["id"]

    def build_digest(self, trigger: str = "manual", actor: str = "",
                     cfg: Optional[AlertsConfig] = None, hours: float = 24) -> dict:
        with self._lock:
            return self._build_digest(trigger, actor, cfg or self.cfg(), hours)

    def _build_digest(self, trigger: str, actor: str, cfg: AlertsConfig, hours: float) -> dict:
        now = self.clock()
        since = now - hours * 3600
        data = self.digest_data(since, now)
        text = self.digest_text(data)
        did = self.db.execute("INSERT INTO al_digests (ts, period_from, period_to, trigger, "
                              "actor, data, text, sent) VALUES (?,?,?,?,?,?,?,?)",
                              (now, since, now, trigger, actor, _j(data), text, ""))
        sent = ""
        if cfg.webhook_url and cfg.digest_to_webhook:
            title = f"Foundry digest {self.localtime(now):%a %d %b}"
            self._enqueue(f"digest:{did}", "digest", title, text, "info")
            sent = "queued"
        self.db.execute("UPDATE al_digests SET sent=? WHERE id=?", (sent, did))
        self.db.execute_count("DELETE FROM al_digests WHERE id NOT IN (SELECT id FROM al_digests "
                              "ORDER BY id DESC LIMIT ?)", (DIGESTS_KEPT,))
        if sent:
            self.deliver(cfg)
        return self.digest(did) or {"id": did}

    def digest(self, did: int) -> Optional[dict]:
        r = self.db.query_one("SELECT * FROM al_digests WHERE id=?", (did,))
        if not r:
            return None
        r["data"] = _uj(r["data"], {})
        return r

    def digests(self, limit: int = 7) -> list[dict]:
        rows = self.db.query("SELECT * FROM al_digests ORDER BY id DESC LIMIT ?",
                             (max(1, min(30, limit)),))
        for r in rows:
            r["data"] = _uj(r["data"], {})
        return rows

    def digest_data(self, since: float, now: float) -> dict:
        db = self.db
        # transitions
        trs = db.query("SELECT project_id, task_id, ts, from_state, to_state, data FROM "
                       "cc_transitions WHERE ts >= ? AND ts <= ? ORDER BY ts", (since, now))
        by_state: dict[str, int] = {}
        completed, parked_ids = [], []
        for t in trs:
            by_state[t["to_state"] or "?"] = by_state.get(t["to_state"] or "?", 0) + 1
            if t["to_state"] == "COMPLETE":
                completed.append({"project_id": t["project_id"], "task_id": t["task_id"],
                                  "ts": t["ts"]})
        # time per state (window-clipped) per task
        time_state: dict[str, float] = {}
        per_task: dict[tuple, dict] = {}
        keys = {(t["project_id"], t["task_id"]) for t in trs}
        for pid, tid in keys:
            rows = db.query("SELECT ts, from_state, to_state FROM cc_transitions WHERE "
                            "project_id=? AND task_id=? AND ts <= ? ORDER BY ts", (pid, tid, now))
            prior = [r for r in rows if r["ts"] < since]
            inside = [r for r in rows if r["ts"] >= since]
            state = prior[-1]["to_state"] if prior else (inside[0]["from_state"] if inside else "")
            cur_ts = since
            spans: dict[str, float] = {}
            for r in inside:
                if state:
                    spans[state] = spans.get(state, 0.0) + max(0.0, r["ts"] - cur_ts)
                state, cur_ts = r["to_state"], r["ts"]
            if state and state not in ("COMPLETE", "CANCELLED"):
                spans[state] = spans.get(state, 0.0) + max(0.0, now - cur_ts)
            for s, v in spans.items():
                time_state[s] = time_state.get(s, 0.0) + v
            per_task[(pid, tid)] = {k: round(v) for k, v in spans.items()}
        # parked tasks (now) and how they got there
        parked = []
        for r in db.query("SELECT project_id, task_id, state, data FROM cc_tasks WHERE state IN "
                          f"({','.join('?' * len(PARK_STATES))})", PARK_STATES):
            d = _uj(r["data"], {}) or {}
            parked.append({"project_id": r["project_id"], "task_id": r["task_id"],
                           "state": r["state"],
                           "why": str(d.get("blocker") or d.get("reason") or "")[:200],
                           "class": str(d.get("blocker_kind") or d.get("blocker_class") or "")})
            parked_ids.append(r["task_id"])
        # coordinator events: self-heal actions, notices
        evs = db.query("SELECT project_id, task_id, kind, ts, detail FROM cc_coord_events WHERE "
                       "ts >= ? AND ts <= ? ORDER BY ts", (since, now))
        heals, notices, ev_kinds = [], {"total": 0, "by_class": {}}, {}
        for e in evs:
            d = _uj(e["detail"], {}) or {}
            ev_kinds[e["kind"]] = ev_kinds.get(e["kind"], 0) + 1
            if e["kind"] == "self_heal" and d.get("digest", True) is not False:
                heals.append({"project_id": e["project_id"], "task_id": e["task_id"],
                              "ts": e["ts"],
                              "action": str(d.get("action") or d.get("rule") or "")[:80],
                              "reason": str(d.get("reason") or d.get("text") or d.get("why")
                                            or "")[:240],
                              "outcome": str(d.get("outcome") or "")[:60]})
            elif e["kind"] == "operator_notice":
                notices["total"] += 1
                c = str(d.get("class") or d.get("blocker_kind") or "unclassified")
                notices["by_class"][c] = notices["by_class"].get(c, 0) + 1
        # decisions by class, and notices avoided
        dec_opened: dict[str, int] = {}
        avoided, avoided_list = 0, []
        for r in db.query("SELECT project_id, task_id, data FROM cc_tasks"):
            d = _uj(r["data"], {}) or {}
            for x in d.get("decisions") or []:
                if not isinstance(x, dict):
                    continue
                cls = "image_ask" if is_image_ask(x) else decision_class(x)
                if since <= float(x.get("opened_ts") or 0) <= now:
                    dec_opened[cls] = dec_opened.get(cls, 0) + 1
                if (cls == "automation" and x.get("status") != "open"
                        and since <= float(x.get("answered_ts") or 0) <= now
                        and _resolved_by_automation(x)):
                    avoided += 1
                    if len(avoided_list) < 20:
                        avoided_list.append({"task_id": r["task_id"], "kind": x.get("kind"),
                                             "authority": x.get("authority")})
        # tokens per sprint: Foundry's own measured calls
        tokens = []
        try:
            for r in db.query("SELECT project_id, work_task_id AS task_id, COUNT(*) AS calls, "
                              "SUM(COALESCE(input_tokens,0)) AS i, "
                              "SUM(COALESCE(output_tokens,0)) AS o FROM usage_events WHERE ts >= ? "
                              "AND ts <= ? AND status IN ('settled','error') AND "
                              "COALESCE(work_task_id,'') != '' GROUP BY project_id, work_task_id "
                              "ORDER BY i + o DESC LIMIT 20", (since, now)):
                tokens.append({"project_id": r["project_id"], "task_id": r["task_id"],
                               "calls": r["calls"], "input": int(r["i"] or 0),
                               "output": int(r["o"] or 0)})
        except Exception:                                        # noqa: BLE001
            tokens = []
        pushed = db.query("SELECT cls, status, COUNT(*) AS n FROM al_outbox WHERE ts >= ? AND "
                          "cls != 'digest' GROUP BY cls, status", (since,))
        down = self._get("coordinator_down") or {}
        return {
            "since": since, "until": now,
            "transitions": {"total": len(trs), "by_state": by_state},
            "completed": completed,
            "parked": parked,
            "self_heal": heals,
            "notices": notices,
            "decisions_opened": dec_opened,
            "notices_avoided": {"count": avoided, "items": avoided_list},
            "time_per_state_s": {k: round(v) for k, v in sorted(time_state.items(),
                                                                key=lambda kv: -kv[1])},
            "time_per_task_s": [{"project_id": k[0], "task_id": k[1], "states": v}
                                for k, v in sorted(per_task.items())][:30],
            "tokens_per_sprint": tokens,
            "events": ev_kinds,
            "alerts_pushed": [dict(r) for r in pushed],
            "coordinator": {"down": bool(down.get("down")), "since": down.get("since")},
        }

    @staticmethod
    def _dur(s: float) -> str:
        s = int(s or 0)
        return f"{s // 3600}h{(s % 3600) // 60:02d}" if s >= 3600 else f"{s // 60}m"

    def digest_text(self, d: dict) -> str:
        lines = []
        if d["coordinator"]["down"]:
            lines.append("⚠ The coordinator is DOWN right now.")
        done = d["completed"]
        lines.append(f"Sprints completed: {len(done)}"
                     + (f" ({', '.join(x['task_id'] for x in done[:8])})" if done else ""))
        tr = d["transitions"]
        lines.append(f"Task transitions: {tr['total']}"
                     + (" (" + ", ".join(f"{k} {v}" for k, v in sorted(tr["by_state"].items()))
                        + ")" if tr["by_state"] else ""))
        heals = d["self_heal"]
        lines.append(f"Self-repairs: {len(heals)}")
        for h in heals[:10]:
            lines.append(f"  - {h['task_id'] or h['project_id']}: {h['action'] or 'repair'}"
                         + (f": {h['reason']}" if h["reason"] else ""))
        lines.append(f"Parked tasks: {len(d['parked'])}")
        for p in d["parked"][:8]:
            lines.append(f"  - {p['task_id']} ({p['state']}" + (f", {p['class']}" if p["class"]
                                                                 else "") + ")"
                         + (f": {p['why'][:120]}" if p["why"] else ""))
        dec = d["decisions_opened"]
        lines.append("Decisions opened: " + (", ".join(f"{k} {v}" for k, v in sorted(dec.items()))
                                             or "none"))
        lines.append(f"Stops avoided (automation decisions resolved without you): "
                     f"{d['notices_avoided']['count']}")
        tps = d["time_per_state_s"]
        if tps:
            lines.append("Time per state: " + ", ".join(f"{k} {self._dur(v)}"
                                                        for k, v in list(tps.items())[:6]))
        toks = d["tokens_per_sprint"]
        if toks:
            lines.append("Tokens per sprint: " + ", ".join(
                f"{t['task_id']} {round((t['input'] + t['output']) / 1000)}k"
                for t in toks[:6]))
        return "\n".join(lines)

    # ------------------------------------------------------------------ settings view

    def settings_view(self) -> dict:
        cfg = self.cfg()
        return {"enabled": cfg.enabled, "coordinator_down_minutes": cfg.coordinator_down_minutes,
                "webhook_url": cfg.webhook_url, "webhook_format": cfg.webhook_format,
                "webhook_token_set": bool(cfg.webhook_token),
                "classes": {c: cfg.class_on(c) for c in ALERT_CLASSES},
                "class_labels": CLASS_LABELS,
                "digest_enabled": cfg.digest_enabled, "digest_hour": cfg.digest_hour,
                "digest_to_webhook": cfg.digest_to_webhook,
                "coordinator": {"age_s": self.coordinator_age(),
                                **(self._get("coordinator_down") or {})},
                "outbox": self.outbox(15)}
