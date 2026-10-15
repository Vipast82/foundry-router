"""Image generation and editing as tracked jobs, with an artifact store.

Image endpoints on CLIProxyAPI (and OpenAI-style servers) are synchronous and
slow (30 s to several minutes). Foundry turns each request into a job:

  submit  -> validated, budget-checked, recorded, run in the background
  status  -> poll (optionally long-poll) until succeeded / failed / cancelled
  artifact-> the image bytes, served from <data dir>/media/<job id>/

Rules this module enforces (AnimalUprising spec §12):
- One configured backend and only the models in `media.models`. A refused,
  unavailable or over-budget model fails clearly; there is no fallback to
  another model, provider, or a backend billed per call.
- Duplicate protection: an idempotency key, or the same request (client,
  model, prompt, settings, input images) within `dedupe_minutes`, returns the
  existing job instead of generating again.
- Bounded retries, only when the request provably never started an image
  (connect failure, 502/503 from the proxy). A timeout is NEVER resubmitted,
  and polling a job never submits anything.
- Its own budget (jobs per 5 h / day / client, concurrency, queue length),
  separate from chat, plus the CLIProxyAPI usage limit for the account.
- Results are files with ids, sizes and hashes; nothing base64 is pushed into
  a conversation unless a client explicitly asks for the bytes.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import json
import logging
import re
import secrets
import shutil
import struct
import time
from pathlib import Path
from typing import Optional

import httpx

from .db import utcnow

log = logging.getLogger(__name__)

TERMINAL = ("succeeded", "failed", "cancelled")
_JOB_ID_RE = re.compile(r"^img_[0-9a-f]{16}$")
_SIZE_RE = re.compile(r"^(auto|\d{2,5}x\d{2,5})$")
_WORD_RE = re.compile(r"^[a-z0-9_-]{1,24}$")
_EXT = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}
_IMAGE_MODEL_RE = re.compile(r"(^|[-_/])(gpt-image|dall-e|grok-imagine|imagen|flux|"
                             r"stable-diffusion|sdxl)", re.I)
_ERR_TYPE = {400: "invalid_request_error", 401: "authentication_error",
             403: "permission_error", 404: "not_found_error", 409: "invalid_request_error",
             429: "rate_limit_error"}

NEXT_RUNNING = ("Still running. Check again with image_job_status(job_id). Do NOT submit "
                "the request again: that would generate (and count) a second image.")


def is_image_model(model_id: str) -> bool:
    """Image-generation-only model (gpt-image-*, dall-e, grok-imagine, ...)."""
    return bool(_IMAGE_MODEL_RE.search(model_id or ""))


class MediaError(Exception):
    def __init__(self, status: int, code: str, message: str, **extra):
        super().__init__(message)
        self.status, self.code, self.message, self.extra = status, code, message, extra

    def body(self) -> dict:
        return {"error": {"message": self.message, "code": self.code,
                          "type": _ERR_TYPE.get(self.status, "api_error"), **self.extra}}


# -- image bytes -----------------------------------------------------------------

def image_info(data: bytes) -> tuple[Optional[str], Optional[int], Optional[int]]:
    """(mime, width, height) from the file header; mime None = not PNG/JPEG/WebP."""
    try:
        if data[:8] == b"\x89PNG\r\n\x1a\n" and len(data) >= 24:
            w, h = struct.unpack(">II", data[16:24])
            return "image/png", w, h
        if data[:3] == b"\xff\xd8\xff":
            i = 2
            while i + 9 < len(data):
                if data[i] != 0xFF:
                    i += 1
                    continue
                marker = data[i + 1]
                if marker in (0xD8, 0x01, 0xFF) or 0xD0 <= marker <= 0xD7:
                    i += 2 if marker != 0xFF else 1
                    continue
                seg = struct.unpack(">H", data[i + 2:i + 4])[0]
                if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                              0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                    h, w = struct.unpack(">HH", data[i + 5:i + 9])
                    return "image/jpeg", w, h
                i += 2 + seg
            return "image/jpeg", None, None
        if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            kind = data[12:16]
            if kind == b"VP8X":
                return ("image/webp", 1 + int.from_bytes(data[24:27], "little"),
                        1 + int.from_bytes(data[27:30], "little"))
            if kind == b"VP8 ":
                w, h = struct.unpack("<HH", data[26:30])
                return "image/webp", w & 0x3FFF, h & 0x3FFF
            if kind == b"VP8L":
                bits = int.from_bytes(data[21:25], "little")
                return "image/webp", (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
            return "image/webp", None, None
    except (struct.error, IndexError):
        pass
    return None, None, None


def decode_image_input(value, max_bytes: int, what: str = "image") -> bytes:
    """A data URL or bare base64 string -> validated PNG/JPEG/WebP bytes."""
    if isinstance(value, dict):        # {"image_url": "..."} (OpenAI edits JSON)
        value = value.get("image_url") or value.get("url") or ""
        if isinstance(value, dict):
            value = value.get("url") or ""
    if not isinstance(value, str) or not value.strip():
        raise MediaError(400, "invalid_image", f"{what} is empty")
    s = value.strip()
    if s.startswith("data:"):
        s = s.split(",", 1)[1] if "," in s else ""
    elif s.startswith(("http://", "https://")):
        raise MediaError(400, "invalid_image",
                         f"{what}: send the image bytes (data URL or base64), not a URL")
    if len(s) * 3 // 4 > max_bytes:
        raise MediaError(400, "image_too_large",
                         f"{what} is larger than {max_bytes // (1024 * 1024)} MB (media.max_input_mb)")
    try:
        raw = base64.b64decode(s, validate=False)
    except (binascii.Error, ValueError):
        raise MediaError(400, "invalid_image", f"{what} is not valid base64")
    if image_info(raw)[0] is None:
        raise MediaError(400, "invalid_image", f"{what} is not a PNG, JPEG or WebP image")
    return raw


def _data_url(raw: bytes) -> str:
    mime = image_info(raw)[0] or "image/png"
    return f"data:{mime};base64,{base64.b64encode(raw).decode()}"


def _upstream_message(r: httpx.Response) -> str:
    try:
        j = r.json()
        err = j.get("error") if isinstance(j, dict) else None
        if isinstance(err, dict) and err.get("message"):
            return str(err["message"])[:400]
        if isinstance(err, str):
            return err[:400]
    except Exception:                              # noqa: BLE001
        pass
    return (r.text or f"HTTP {r.status_code}")[:400]


def _retry_after(r: httpx.Response) -> Optional[float]:
    v = r.headers.get("retry-after")
    try:
        return float(v) if v else None
    except ValueError:
        return None


def _jl(v, default):
    if not v:
        return default
    if isinstance(v, (list, dict)):
        return v
    try:
        return json.loads(v)
    except (TypeError, ValueError):
        return default


class MediaService:
    def __init__(self, svc):
        self.svc = svc
        self.db = svc.db
        self.dir = Path(self.db.path).parent / "media"
        self._tasks: dict[str, asyncio.Task] = {}
        self._events: dict[str, asyncio.Event] = {}
        self._inputs: dict[str, tuple[list[bytes], Optional[bytes]]] = {}
        self._sem: Optional[asyncio.Semaphore] = None
        self._sem_n = 0
        self._recover()

    @property
    def cfg(self):
        return self.svc.config_store.config.media

    # -- startup / housekeeping ------------------------------------------------

    def _recover(self) -> None:
        """Jobs that were in flight when Foundry stopped can't be resumed (the
        input images lived in memory). Mark them failed; never resubmit."""
        n = self.db.execute_count(
            "UPDATE media_jobs SET status='failed', error_code='interrupted', error_status=503, "
            "error='Foundry restarted while this job was in progress. It was not resubmitted; "
            "submit it again if you still need it.', finished_ts=? "
            "WHERE status IN ('queued','running')", (time.time(),))
        if n:
            self.db.log_event("warning", "media", f"{n} image job(s) interrupted by a restart")

    def prune(self) -> int:
        days = self.cfg.retention_days or 0
        if days <= 0:
            return 0
        cutoff = time.time() - days * 86400
        rows = self.db.query("SELECT id FROM media_jobs WHERE created_ts < ? AND status IN "
                             "('succeeded','failed','cancelled')", (cutoff,))
        for r in rows:
            shutil.rmtree(self.dir / r["id"], ignore_errors=True)
            self.db.execute("DELETE FROM media_jobs WHERE id=?", (r["id"],))
        return len(rows)

    def _semaphore(self) -> asyncio.Semaphore:
        n = max(1, int(self.cfg.max_concurrent or 1))
        if self._sem is None or n != self._sem_n:
            self._sem, self._sem_n = asyncio.Semaphore(n), n
        return self._sem

    # -- model / backend selection (fail closed) -------------------------------

    def persona_backend(self, persona_name: Optional[str]) -> Optional[str]:
        if not persona_name:
            return None
        p = self.svc.personas.get(persona_name)
        return ((p or {}).get("backend") or "").strip() or None

    def resolve_model(self, requested: Optional[str]) -> tuple[str, Optional[str]]:
        """'final'/'draft'/'' -> configured models; a persona name -> its first
        listed image model; otherwise the exact id. Must be in media.models."""
        cfg = self.cfg
        req = (requested or "").strip()
        persona_name = None
        if req.lower() in ("", "final", "default"):
            model = cfg.default_model
        elif req.lower() == "draft":
            model = cfg.draft_model or cfg.default_model
        else:
            persona = self.svc.personas.get(req) if req not in cfg.models else None
            if persona:
                from . import client_auth
                if not persona.get("enabled", 1) and not client_auth.current().get("internal"):
                    raise MediaError(403, "persona_disabled", f"persona {req!r} is disabled")
                if not client_auth.persona_allowed(persona["virtual_name"]):
                    raise MediaError(403, "model_not_allowed",
                                     f"this client may not use persona {req!r}")
                listed = _jl(persona.get("pinned_models"), []) + _jl(persona.get("model_allowlist"), [])
                cands = [m for m in listed if m in cfg.models]
                if not cands:
                    raise MediaError(400, "model_not_allowed",
                                     f"persona {req!r} lists no image model from media.models "
                                     f"({', '.join(cfg.models)})")
                model, persona_name = cands[0], persona["virtual_name"]
            else:
                model = req
        if model not in cfg.models:
            raise MediaError(400, "model_not_allowed",
                             f"image model {model!r} is not allowed. Allowed: "
                             f"{', '.join(cfg.models) or 'none'} (or 'final' / 'draft').")
        return model, persona_name

    def resolve_backend(self, model: str, pinned: Optional[str] = None):
        """(BackendState, info dict) for the image backend: an image persona's
        own backend when it has one, else media.backend, else CLIProxyAPI.
        Refuses rather than picking another backend when that one can't serve."""
        backends = getattr(self.svc.pool, "backends", None)
        if backends is None:
            raise MediaError(503, "media_backend_unavailable",
                             "image jobs need Foundry's internal backend pool")
        cfg = self.cfg
        chosen = pinned or cfg.backend
        if chosen:
            s = backends.get(chosen)
            if s is None:
                raise MediaError(503, "media_backend_unavailable",
                                 f"image backend {chosen!r} is not a configured backend")
        else:
            cands = [s for s in backends.values()
                     if s.config.effective_flavor == "cliproxyapi"
                     and getattr(s.config, "enabled", True)]
            listing = [s for s in cands if model in s.models]
            pick = sorted(listing or cands, key=lambda s: -s.config.priority)
            if not pick:
                raise MediaError(503, "media_backend_unavailable",
                                 "no image backend: add a CLIProxyAPI backend (flavor "
                                 "cliproxyapi) or set media.backend")
            s = pick[0]
        c = s.config
        if not getattr(c, "enabled", True):
            raise MediaError(503, "media_backend_unavailable", f"backend {c.name} is disabled")
        info = {"name": c.name, "type": c.type, "url": c.url, "api_key": c.api_key,
                "flavor": c.effective_flavor, "subscription": bool(c.is_subscription),
                "management_key": c.management_key, "management_url": c.management_url,
                "usage_limit_pct": c.usage_limit_pct}
        from .facade.ollama_api import _is_paid_backend
        if _is_paid_backend(info) and not info["subscription"] and not cfg.allow_paid_api:
            raise MediaError(403, "paid_backend_refused",
                             f"backend {c.name} bills per call; image jobs refuse it unless "
                             f"media.allow_paid_api is set")
        if s.ever_checked and not s.healthy:
            raise MediaError(503, "media_backend_unavailable",
                             f"image backend {c.name} is unreachable ({s.last_error or 'health check failing'})")
        listed_images = [m for m in s.models if is_image_model(m)]
        if listed_images and model not in s.models:
            raise MediaError(503, "model_unavailable",
                             f"backend {c.name} does not offer {model} (it lists: "
                             f"{', '.join(sorted(listed_images))})")
        return s, info

    # -- budget -----------------------------------------------------------------

    def _count(self, since: float, client_id: Optional[str] = None) -> int:
        sql = ("SELECT COUNT(*) AS n FROM media_jobs WHERE created_ts >= ? AND "
               "(charged=1 OR status IN ('queued','running'))")
        args: list = [since]
        if client_id is not None:
            sql += " AND client_id=?"
            args.append(client_id)
        return int(self.db.query_one(sql, args)["n"])

    async def _check_budget(self, model: str, info: dict, client_id: str) -> None:
        cfg = self.cfg
        now = time.time()
        active = int(self.db.query_one(
            "SELECT COUNT(*) AS n FROM media_jobs WHERE status IN ('queued','running')")["n"])
        if active >= max(1, cfg.max_queued):
            raise MediaError(429, "media_queue_full",
                             f"{active} image jobs are already queued or running "
                             f"(media.max_queued {cfg.max_queued}); wait for one to finish")
        for window, cap, label in ((5 * 3600, cfg.jobs_per_5h, "5-hour"),
                                   (86400, cfg.jobs_per_day, "24-hour")):
            if cap and cap > 0:
                n = self._count(now - window)
                if n >= cap:
                    oldest = self.db.query_one(
                        "SELECT MIN(created_ts) AS t FROM media_jobs WHERE created_ts >= ? AND "
                        "(charged=1 OR status IN ('queued','running'))", (now - window,))["t"]
                    reset = int(max(0, (oldest or now) + window - now))
                    raise MediaError(429, "budget_denied",
                                     f"image budget reached: {n}/{cap} jobs in the {label} "
                                     f"window (media.jobs_per_{'5h' if window < 86400 else 'day'}). "
                                     f"Next slot in about {reset // 60} min.",
                                     reset_seconds=reset)
        if cfg.client_jobs_per_day and cfg.client_jobs_per_day > 0:
            n = self._count(now - 86400, client_id)
            if n >= cfg.client_jobs_per_day:
                raise MediaError(429, "budget_denied",
                                 f"client {client_id!r} used its image budget: {n}/"
                                 f"{cfg.client_jobs_per_day} jobs in 24 h")
        cu = getattr(self.svc, "cliproxy_usage", None)
        if cu is not None and cu.configured(info):
            st = await cu.model_status(model, info)
            if not st.get("available", True):
                raise MediaError(429, "budget_denied",
                                 f"{model}: subscription usage at the limit "
                                 f"({st.get('note') or 'window used up'}). Not retried on "
                                 f"another model.", reset_seconds=st.get("reset_s"))

    # -- jobs -------------------------------------------------------------------

    def get(self, job_id: str) -> Optional[dict]:
        if not _JOB_ID_RE.match(job_id or ""):
            return None
        return self.db.query_one("SELECT * FROM media_jobs WHERE id=?", (job_id,))

    def _update(self, job_id: str, **fields) -> None:
        if not fields:
            return
        cols = ", ".join(f"{k}=?" for k in fields)
        self.db.execute(f"UPDATE media_jobs SET {cols} WHERE id=?", (*fields.values(), job_id))

    def _find_duplicate(self, client_id: str, key: str, window_s: Optional[float]) -> Optional[dict]:
        sql = "SELECT * FROM media_jobs WHERE client_id=? AND idem_key=?"
        args: list = [client_id, key]
        if window_s is not None:
            sql += " AND created_ts >= ? AND status NOT IN ('failed','cancelled')"
            args.append(time.time() - window_s)
        return self.db.query_one(sql + " ORDER BY created_ts DESC LIMIT 1", args)

    async def submit(self, op: str, *, prompt: str, model: Optional[str] = None,
                     size: Optional[str] = None, quality: Optional[str] = None,
                     background: Optional[str] = None, output_format: Optional[str] = None,
                     n: int = 1, images: Optional[list] = None, mask=None,
                     input_fidelity: Optional[str] = None,
                     idempotency_key: Optional[str] = None, auto_dedupe: bool = True,
                     client: Optional[dict] = None) -> tuple[dict, bool]:
        """(job row, deduplicated). Raises MediaError on refusal."""
        cfg = self.cfg
        if not cfg.enabled:
            raise MediaError(503, "media_disabled", "image jobs are disabled (media.enabled)")
        if op not in ("generate", "edit"):
            raise MediaError(400, "invalid_request", f"unknown op {op!r}")
        prompt = (prompt or "").strip()
        if not prompt:
            raise MediaError(400, "invalid_request", "prompt is required")
        if len(prompt) > 32000:
            raise MediaError(400, "invalid_request", "prompt is longer than 32000 characters")
        try:
            n = int(n or 1)
        except (TypeError, ValueError):
            raise MediaError(400, "invalid_request", "n must be an integer")
        if not 1 <= n <= max(1, cfg.max_images_per_job):
            raise MediaError(400, "invalid_request",
                             f"n must be 1..{cfg.max_images_per_job} (media.max_images_per_job)")
        if size and not _SIZE_RE.match(size):
            raise MediaError(400, "invalid_request", "size must be 'auto' or WIDTHxHEIGHT, e.g. 1024x1024")
        for name, val in (("quality", quality), ("background", background),
                          ("output_format", output_format), ("input_fidelity", input_fidelity)):
            if val and not _WORD_RE.match(str(val).lower()):
                raise MediaError(400, "invalid_request", f"invalid {name} {val!r}")
        if background == "transparent" and output_format == "jpeg":
            raise MediaError(400, "invalid_request",
                             "a transparent background needs output_format png or webp")
        max_bytes = int(cfg.max_input_mb * 1024 * 1024)
        raw_images = [decode_image_input(v, max_bytes, f"images[{i}]")
                      for i, v in enumerate(images or [])]
        raw_mask = decode_image_input(mask, max_bytes, "mask") if mask else None
        if op == "edit" and not raw_images:
            raise MediaError(400, "invalid_request", "edit_image needs at least one input image")
        if op == "generate" and (raw_images or raw_mask):
            raise MediaError(400, "invalid_request", "input images belong to edit_image")

        model_id, persona = self.resolve_model(model)
        state, info = self.resolve_backend(model_id, self.persona_backend(persona))
        from . import client_auth
        client = client or client_auth.current()
        cid = client.get("client_id") or "anonymous"
        params = {"prompt": prompt, "size": size, "quality": quality, "background": background,
                  "output_format": output_format, "n": n, "input_fidelity": input_fidelity,
                  "inputs": [hashlib.sha256(b).hexdigest() for b in raw_images],
                  "mask": hashlib.sha256(raw_mask).hexdigest() if raw_mask else None}
        params = {k: v for k, v in params.items() if v not in (None, [], "")}
        digest = hashlib.sha256(json.dumps([cid, op, model_id, params], sort_keys=True)
                                .encode()).hexdigest()[:32]
        if idempotency_key:
            key, window = "k:" + str(idempotency_key)[:200], None
        else:
            key = "h:" + digest
            window = cfg.dedupe_minutes * 60 if auto_dedupe and cfg.dedupe_minutes > 0 else None
        if idempotency_key or window is not None:
            dup = self._find_duplicate(cid, key, window)
            if dup:
                return dup, True
        await self._check_budget(model_id, info, cid)
        if idempotency_key or window is not None:      # re-check: the budget check awaited
            dup = self._find_duplicate(cid, key, window)
            if dup:
                return dup, True
        job_id = "img_" + secrets.token_hex(8)
        self.db.execute(
            "INSERT INTO media_jobs (id, created_at, created_ts, op, status, model, backend, "
            "persona, client_id, session_id, task_id, idem_key, params) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (job_id, utcnow(), time.time(), op, "queued", model_id, state.config.name, persona,
             cid, client.get("session_id"), client.get("task_id"), key, json.dumps(params)))
        self._inputs[job_id] = (raw_images, raw_mask)
        self._events[job_id] = asyncio.Event()
        self._tasks[job_id] = asyncio.create_task(self._run(job_id))
        return self.get(job_id), False

    async def wait(self, job_id: str, timeout: float) -> Optional[dict]:
        row = self.get(job_id)
        if row is None or row["status"] in TERMINAL or timeout <= 0:
            return row
        ev = self._events.get(job_id)
        if ev is not None:
            try:
                await asyncio.wait_for(ev.wait(), timeout)
            except asyncio.TimeoutError:
                pass
        return self.get(job_id)

    async def cancel(self, job_id: str) -> Optional[dict]:
        row = self.get(job_id)
        if row is None or row["status"] in TERMINAL:
            return row
        task = self._tasks.get(job_id)
        if task is not None:
            task.cancel()
            try:
                await asyncio.wait_for(asyncio.shield(task), 5)
            except (asyncio.CancelledError, asyncio.TimeoutError, Exception):  # noqa: BLE001
                pass
        else:
            self._update(job_id, status="cancelled", finished_ts=time.time(),
                         error="cancelled", error_code="cancelled")
        return self.get(job_id)

    # -- execution --------------------------------------------------------------

    async def _run(self, job_id: str) -> None:
        try:
            async with self._semaphore():
                row = self.get(job_id)
                if row is None or row["status"] != "queued":
                    return
                started = time.time()
                self._update(job_id, status="running", started_ts=started)
                data = await self._call_backend(row)
                artifacts = await self._store(job_id, data)
                self._update(job_id, status="succeeded", finished_ts=time.time(),
                             duration_ms=round((time.time() - started) * 1000, 1),
                             artifacts=json.dumps(artifacts),
                             usage=json.dumps(data.get("usage")) if data.get("usage") else None)
        except asyncio.CancelledError:
            row = self.get(job_id) or {}
            note = ("cancelled before it was sent; nothing was generated"
                    if not row.get("charged") else
                    "cancelled while the backend was working; the image may still be "
                    "generated and counted upstream")
            self._update(job_id, status="cancelled", finished_ts=time.time(),
                         error=note, error_code="cancelled")
        except MediaError as e:
            self._update(job_id, status="failed", finished_ts=time.time(),
                         error=e.message, error_code=e.code, error_status=e.status)
            self.db.log_event("warning", "media", f"image job {job_id} failed: {e.message}")
        except Exception as e:                       # noqa: BLE001
            log.exception("image job %s crashed", job_id)
            self._update(job_id, status="failed", finished_ts=time.time(),
                         error=f"internal error: {e}", error_code="internal_error",
                         error_status=500)
        finally:
            self._inputs.pop(job_id, None)
            self._tasks.pop(job_id, None)
            ev = self._events.pop(job_id, None)
            if ev is not None:
                ev.set()

    def _request_body(self, row: dict) -> tuple[str, dict]:
        params = _jl(row["params"], {})
        body = {"model": row["model"], "prompt": params["prompt"], "n": params.get("n", 1),
                "response_format": "b64_json"}
        for k in ("size", "quality", "background", "output_format"):
            if params.get(k):
                body[k] = params[k]
        path = "images/generations"
        if row["op"] == "edit":
            imgs, mask = self._inputs.get(row["id"], ([], None))
            if not imgs:
                raise MediaError(500, "internal_error", "input images for this job are gone")
            body["images"] = [{"image_url": _data_url(b)} for b in imgs]
            if mask:
                body["mask"] = {"image_url": _data_url(mask)}
            if params.get("input_fidelity"):
                body["input_fidelity"] = params["input_fidelity"]
            path = "images/edits"
        return path, body

    async def _call_backend(self, row: dict) -> dict:
        cfg = self.cfg
        backends = getattr(self.svc.pool, "backends", {}) or {}
        s = backends.get(row["backend"])
        if s is None:
            raise MediaError(503, "media_backend_unavailable",
                             f"backend {row['backend']} was removed")
        base = s.config.url.rstrip("/")
        if not base.endswith("/v1"):
            base += "/v1"
        path, body = self._request_body(row)
        headers = {"Authorization": f"Bearer {s.config.api_key}"} if s.config.api_key else {}
        tmo = float(max(30, cfg.job_timeout_seconds))
        timeout = httpx.Timeout(connect=10.0, read=tmo, write=120.0, pool=30.0)
        attempt = 0
        while True:
            attempt += 1
            self._update(row["id"], attempts=attempt)
            try:
                r = await self.svc.http.post(f"{base}/{path}", json=body, headers=headers,
                                             timeout=timeout)
            except (httpx.ConnectError, httpx.ConnectTimeout) as e:
                if attempt < max(1, cfg.max_attempts):       # nothing reached the backend
                    await asyncio.sleep(2 * attempt)
                    continue
                raise MediaError(503, "backend_unreachable",
                                 f"could not connect to {s.config.name} ({attempt} attempt(s)): "
                                 f"{e or type(e).__name__}")
            except httpx.TimeoutException:
                self._update(row["id"], charged=1)
                raise MediaError(504, "backend_timeout",
                                 f"{s.config.name} did not answer within {int(tmo)} s. The job was "
                                 f"NOT resubmitted (the image may still have been generated and "
                                 f"counted against your plan).")
            except httpx.HTTPError as e:
                self._update(row["id"], charged=1)
                raise MediaError(502, "backend_error",
                                 f"{s.config.name}: {e or type(e).__name__}. Not resubmitted.")
            self._update(row["id"], charged=1)
            if r.status_code in (502, 503) and attempt < max(1, cfg.max_attempts):
                await asyncio.sleep(min(30.0, _retry_after(r) or 2.0 * attempt))
                continue
            if r.status_code == 429:
                ra = _retry_after(r)
                raise MediaError(429, "quota_exceeded",
                                 f"{s.config.name} refused ({_upstream_message(r)}). Not retried "
                                 f"on another model or provider.",
                                 **({"retry_after": ra} if ra else {}))
            if r.status_code >= 400:
                status = r.status_code if 400 <= r.status_code < 500 else 502
                raise MediaError(status, "backend_rejected",
                                 f"{s.config.name} HTTP {r.status_code}: {_upstream_message(r)}")
            try:
                data = r.json()
            except ValueError:
                raise MediaError(502, "backend_error",
                                 f"{s.config.name} returned a non-JSON reply")
            if not isinstance(data, dict):
                raise MediaError(502, "backend_error", "unexpected reply shape")
            return data

    async def _store(self, job_id: str, data: dict) -> list[dict]:
        items = data.get("data") or []
        if not items:
            raise MediaError(502, "no_image", "the backend returned no image"
                             + (f": {data.get('error')}" if data.get("error") else ""))
        d = self.dir / job_id
        d.mkdir(parents=True, exist_ok=True)
        out = []
        for i, it in enumerate(items[:max(1, self.cfg.max_images_per_job)]):
            raw = None
            if it.get("b64_json"):
                raw = base64.b64decode(it["b64_json"])
            elif str(it.get("url") or "").startswith("data:"):
                raw = base64.b64decode(it["url"].split(",", 1)[1])
            elif str(it.get("url") or "").startswith(("http://", "https://")):
                r = await self.svc.http.get(it["url"], timeout=120.0)
                r.raise_for_status()
                raw = r.content
            if not raw:
                continue
            mime, w, h = image_info(raw)
            if mime is None:
                raise MediaError(502, "bad_image", "the backend returned an unreadable image")
            name = f"{i}.{_EXT[mime]}"
            await asyncio.to_thread((d / name).write_bytes, raw)
            out.append({"id": f"{job_id}-{i}", "index": i, "file": name, "mime": mime,
                        "bytes": len(raw), "width": w, "height": h,
                        "sha256": hashlib.sha256(raw).hexdigest(),
                        "revised_prompt": it.get("revised_prompt")})
        if not out:
            raise MediaError(502, "no_image", "the backend returned no image data")
        return out

    def artifact_path(self, job_id: str, index: int) -> Optional[tuple[Path, dict]]:
        row = self.get(job_id)
        if row is None:
            return None
        for a in _jl(row.get("artifacts"), []):
            if a.get("index") == index:
                p = self.dir / job_id / a["file"]
                return (p, a) if p.is_file() else None
        return None

    # -- views ------------------------------------------------------------------

    def view(self, row: dict, base_url: str = "", deduplicated: bool = False) -> dict:
        params = _jl(row.get("params"), {})
        status = row["status"]
        arts = []
        for a in _jl(row.get("artifacts"), []):
            arts.append({k: a.get(k) for k in ("id", "index", "mime", "bytes", "width",
                                                "height", "sha256", "revised_prompt")}
                        | {"url": f"{base_url}/media/artifacts/{row['id']}/{a['index']}"})
        out = {
            "job_id": row["id"], "status": status, "op": row["op"], "model": row["model"],
            "backend": row["backend"], "persona": row.get("persona"),
            "client_id": row.get("client_id"), "created_at": row.get("created_at"),
            "attempts": row.get("attempts") or 0, "duration_ms": row.get("duration_ms"),
            "prompt": (params.get("prompt") or "")[:500],
            "settings": {k: params[k] for k in ("size", "quality", "background",
                                                "output_format", "n", "input_fidelity")
                         if k in params},
            "artifacts": arts,
        }
        if deduplicated:
            out["deduplicated"] = True
            out["note"] = ("An identical request already exists, so no new image was generated. "
                           "Pass a new idempotency_key (or allow_duplicate) for a fresh variation.")
        if status in ("queued", "running"):
            if row.get("started_ts"):
                out["elapsed_s"] = int(time.time() - row["started_ts"])
            out["next"] = NEXT_RUNNING
        elif status == "failed":
            out["error"] = {"code": row.get("error_code"), "message": row.get("error"),
                            "status": row.get("error_status")}
        elif status == "cancelled":
            out["error"] = {"code": "cancelled", "message": row.get("error")}
        return out

    def list_jobs(self, limit: int = 50, client_id: Optional[str] = None) -> list[dict]:
        sql, args = "SELECT * FROM media_jobs", []
        if client_id is not None:
            sql += " WHERE client_id=?"
            args.append(client_id)
        return self.db.query(sql + " ORDER BY created_ts DESC LIMIT ?",
                             (*args, max(1, min(500, int(limit)))))

    def summary(self) -> dict:
        now = time.time()
        cfg = self.cfg
        last_ok = {r["model"]: r["t"] for r in self.db.query(
            "SELECT model, MAX(created_at) AS t FROM media_jobs WHERE status='succeeded' "
            "GROUP BY model")}
        counts = {r["status"]: r["n"] for r in self.db.query(
            "SELECT status, COUNT(*) AS n FROM media_jobs WHERE created_ts >= ? GROUP BY status",
            (now - 86400,))}
        backend, backend_error = None, None
        try:
            backend = self.resolve_backend(cfg.default_model)[0].config.name
        except MediaError as e:
            backend_error = e.message
        return {"enabled": cfg.enabled, "backend": backend, "backend_error": backend_error,
                "models": [{"id": m, "role": ("final" if m == cfg.default_model else
                                              "draft" if m == cfg.draft_model else ""),
                            "verified_at": last_ok.get(m)} for m in cfg.models],
                "used_5h": self._count(now - 5 * 3600), "used_day": self._count(now - 86400),
                "jobs_per_5h": cfg.jobs_per_5h, "jobs_per_day": cfg.jobs_per_day,
                "active": counts.get("queued", 0) + counts.get("running", 0),
                "last_24h": counts}
