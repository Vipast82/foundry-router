"""Images and audio made by persona MCP tools reach the chat user.

Before this, an image or audio result from a persona tool became a text note
("[image: image/png, 512 KB]") that only the model saw; nothing reached the
person chatting. Now, on the brain, worker and direct paths:

1. **Store.** Image/audio blocks, image/audio resource blobs, and media URLs in
   the tool's text (kokoro / stable-audio / acestep return
   ``http://<their host>/file.wav``) are saved in the media store
   (media_jobs rows with op ``tool``, files under <data>/media/<id>/), with a
   sha256, a per-file size cap and a total cap. URLs are fetched only from the
   MCP server's own host (plus ``media.tool_fetch_hosts``) — never from an
   arbitrary host a model or web page wrote.
2. **Signed links.** ``/media/a/<token>`` serves one stored file to anyone
   holding the token until it expires (``media.public_link_ttl_hours``). It is
   the one deliberate unauthenticated read in Foundry: a browser rendering a
   chat sends no Foundry key. HMAC-SHA256 over (job, index, expiry) with a
   secret kept in the DB.
3. **Hand-back.** The reply content gets ``![label](link)`` per image and a
   🔊 link per audio file after the model's text. The model only sees the text
   note (plus the image itself when it has vision) and is told the files are
   already shown, so it doesn't repeat or invent links.
4. **Long jobs.** A tool on a server marked ``long_running`` runs as a
   background job (op ``tool_job``). The request waits up to
   ``media.tool_inline_wait_seconds`` and then hands the model a job id it
   checks with the Foundry-owned ``tool_job_status`` tool. A client's own
   request timeout can't kill a 10-minute generation, and polling never
   resubmits anything.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import hmac
import json
import logging
import re
import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional
from urllib.parse import parse_qsl, urlsplit

log = logging.getLogger(__name__)

JOBS_SERVER = "foundry-jobs"          # pseudo MCP server for tool_job_status
STATUS_TOOL = "tool_job_status"
_SECRET_KV = "media_link_secret"
_MAX_WAIT = 60.0                      # tool_job_status long-poll cap
_DEDUPE_S = 600.0                     # same long call from the same client -> same job
_URL_RE = re.compile(r"https?://[^\s<>\"'`\])}]+", re.I)
_MEDIA_EXT = {"png", "jpg", "jpeg", "webp", "gif", "wav", "mp3", "ogg", "oga", "flac",
              "m4a", "webm", "opus"}
_MAX_URLS = 8                         # media URLs fetched per tool result
_FETCH_TIMEOUT = 120.0

STATUS_SPEC = {
    "type": "function",
    "function": {
        "name": STATUS_TOOL,
        "description": (
            "Check a long-running tool job (image/music/sound generation) that an earlier "
            "tool call handed back as 'job img_…'. Returns 'still running' or the finished "
            "result; finished images/audio are shown to the user automatically. Never call "
            "the original tool again for the same request — that would start a second "
            "generation."),
        "parameters": {
            "type": "object",
            "properties": {
                "job_id": {"type": "string", "description": "The job id, e.g. img_0123abcd4567ef89"},
                "wait_seconds": {"type": "number",
                                 "description": f"Wait up to this long for it to finish "
                                                f"(0–{int(_MAX_WAIT)}, default 30)."}},
            "required": ["job_id"]}}}


@dataclass
class MediaItem:
    """One stored file to show the user."""
    job_id: str
    index: int
    mime: str
    bytes: int
    label: str

    @property
    def kind(self) -> str:
        return "audio" if self.mime.startswith("audio/") else "image"


@dataclass
class PendingJob:
    """A long-running tool job still working when the reply went out."""
    job_id: str
    label: str


@dataclass
class _Captured:
    items: list = field(default_factory=list)
    notes: list = field(default_factory=list)     # skipped files, for the model


# -- signed links -------------------------------------------------------------

def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _secret(db) -> bytes:
    v = db.kv_get(_SECRET_KV)
    if not v:
        v = secrets.token_urlsafe(32)
        db.kv_set(_SECRET_KV, v)
        v = db.kv_get(_SECRET_KV) or v      # another worker may have won the race
    return v.encode()


def sign(db, job_id: str, index: int, ttl_seconds: float, now: Optional[float] = None) -> str:
    exp = int((now if now is not None else time.time()) + max(60.0, float(ttl_seconds)))
    payload = f"{job_id}.{int(index)}.{exp}".encode()
    sig = hmac.new(_secret(db), payload, hashlib.sha256).digest()[:18]
    return f"{_b64(payload)}.{_b64(sig)}"


def verify(db, token: str, now: Optional[float] = None) -> Optional[tuple[str, int]]:
    """(job_id, index) for a valid, unexpired token; None otherwise."""
    try:
        p64, s64 = (token or "").split(".", 1)
        payload, sig = _unb64(p64), _unb64(s64)
        want = hmac.new(_secret(db), payload, hashlib.sha256).digest()[:18]
        if not hmac.compare_digest(sig, want):
            return None
        job_id, index, exp = payload.decode().split(".")
        if int(exp) < (now if now is not None else time.time()):
            return None
        return job_id, int(index)
    except (ValueError, binascii.Error, UnicodeDecodeError):
        return None


def public_base(cfg) -> str:
    """The origin the chat user's browser reaches Foundry on."""
    base = (getattr(cfg.media, "public_base_url", "") or "").strip().rstrip("/")
    if base:
        return base
    from . import request_context
    return request_context.public_origin() or ""


def link(svc, item: MediaItem) -> str:
    cfg = svc.config_store.config
    token = sign(svc.db, item.job_id, item.index, float(cfg.media.public_link_ttl_hours) * 3600)
    return f"{public_base(cfg)}/media/a/{token}"


def _size(n: int) -> str:
    return f"{n / 1024 / 1024:.1f} MB" if n >= 1024 * 1024 else f"{max(1, n // 1024)} KB"


def render(svc, sink: list) -> str:
    """Markdown to append to the reply for everything in the sink ("" = none)."""
    if not sink:
        return ""
    cfg = svc.config_store.config
    lines: list[str] = []
    seen: set = set()
    for it in sink:
        if isinstance(it, MediaItem):
            key = (it.job_id, it.index)
            if key in seen:
                continue
            seen.add(key)
            url = link(svc, it)
            if it.kind == "image":
                lines.append(f"![{_md(it.label)}]({url})")
            else:
                ext = it.mime.split("/", 1)[1].replace("mpeg", "mp3")
                lines.append(f"🔊 [{_md(it.label)} ({ext}, {_size(it.bytes)})]({url})")
                if getattr(cfg.media, "audio_html", False):
                    lines.append(f'<audio controls src="{url}"></audio>')
        elif isinstance(it, PendingJob):
            if ("job", it.job_id) in seen:
                continue
            seen.add(("job", it.job_id))
            lines.append(f"⏳ {it.label} is still running (job `{it.job_id}`) — ask me to "
                         f"check on it.")
    return ("\n\n" + "\n\n".join(lines)) if lines else ""


def _md(text: str) -> str:
    return re.sub(r"[\[\]()\n]", " ", text or "file").strip()[:80] or "file"


def model_note(items: list, notes: list) -> str:
    """What the model is told about the files it can't hand back itself."""
    out = []
    if items:
        n_img = sum(1 for i in items if i.kind == "image")
        n_aud = len(items) - n_img
        what = ", ".join(p for p in (f"{n_img} image(s)" if n_img else "",
                                     f"{n_aud} audio file(s)" if n_aud else "") if p)
        out.append(f"[Foundry: {what} from this tool are shown to the user automatically "
                   f"under your reply. Don't repeat or invent links to them; just say what "
                   f"you made.]")
    out.extend(notes)
    return "\n".join(out)


# -- capture --------------------------------------------------------------------

def _allowed_hosts(svc, server: str) -> set[str]:
    hosts = {h.strip().lower() for h in (svc.config_store.config.media.tool_fetch_hosts or [])
             if h and h.strip()}
    cfg = (getattr(svc.mcp, "servers", {}) or {}).get(server)
    if cfg is not None:
        h = urlsplit(getattr(cfg, "url", "") or "").hostname
        if h:
            hosts.add(h.lower())
    return hosts


def _media_url(url: str) -> bool:
    parts = urlsplit(url)
    ext = parts.path.rsplit(".", 1)[-1].lower() if "." in parts.path.rsplit("/", 1)[-1] else ""
    if ext in _MEDIA_EXT:
        return True
    # ComfyUI style: /view?filename=x.png&type=output
    for k, v in parse_qsl(parts.query):
        if k.lower() in ("filename", "file", "name") and v.rsplit(".", 1)[-1].lower() in _MEDIA_EXT:
            return True
    return False


async def _fetch(svc, url: str, cap: int) -> tuple[Optional[bytes], str]:
    """(bytes or None, why-not)."""
    try:
        async with svc.http.stream("GET", url, timeout=_FETCH_TIMEOUT,
                                   follow_redirects=False) as r:
            if r.status_code != 200:
                return None, f"HTTP {r.status_code}"
            clen = int(r.headers.get("content-length") or 0)
            if clen and clen > cap:
                return None, f"larger than {_size(cap)} (media.tool_output_max_mb)"
            buf = bytearray()
            async for chunk in r.aiter_bytes():
                buf.extend(chunk)
                if len(buf) > cap:
                    return None, f"larger than {_size(cap)} (media.tool_output_max_mb)"
            return bytes(buf), ""
    except Exception as e:                                       # noqa: BLE001
        from .errors import describe_exception
        return None, describe_exception(e)[:160]


async def _collect(svc, server: str, tool: str, rich, fetch_urls: bool) -> tuple[list, list]:
    """([{raw, mime, w, h, label}], notes) from one tool result."""
    from .media import sniff_media
    mcfg = svc.config_store.config.media
    cap = int(float(mcfg.tool_output_max_mb or 0) * 1024 * 1024) or (100 * 1024 * 1024)
    found: list[dict] = []
    notes: list[str] = []
    seen: set[str] = set()

    def add(raw: Optional[bytes], label: str) -> None:
        if not raw:
            return
        if len(raw) > cap:
            notes.append(f"[Foundry: a {label} of {_size(len(raw))} was not shown to the user "
                         f"— over media.tool_output_max_mb.]")
            return
        mime, w, h = sniff_media(raw)
        if not mime:
            return
        digest = hashlib.sha256(raw).hexdigest()
        if digest in seen:
            return
        seen.add(digest)
        found.append({"raw": raw, "mime": mime, "w": w, "h": h, "label": label})

    for b in getattr(rich, "blocks", None) or []:
        t = b.get("type")
        data = None
        if t in ("image", "audio"):
            data = b.get("data")
        elif t == "resource" and b.get("blob") and str(b.get("mimeType") or "").startswith(
                ("image/", "audio/")):
            data = b.get("blob")
        if not data:
            continue
        if len(data) * 3 // 4 > cap:
            notes.append(f"[Foundry: a {t} of {_size(len(data) * 3 // 4)} was not shown to the "
                         f"user — over media.tool_output_max_mb.]")
            continue
        try:
            add(base64.b64decode(data), f"{tool} {t}")
        except (binascii.Error, ValueError):
            continue

    if fetch_urls and mcfg.tool_fetch_urls:
        hosts = _allowed_hosts(svc, server)
        texts = [b.get("text") or "" for b in getattr(rich, "blocks", None) or []
                 if b.get("type") == "text"]
        texts += [b.get("uri") or "" for b in getattr(rich, "blocks", None) or []
                  if b.get("type") == "resource_link"]
        urls: list[str] = []
        for txt in texts:
            for u in _URL_RE.findall(txt):
                u = u.rstrip(".,;:!?")
                if u not in urls and _media_url(u) \
                        and (urlsplit(u).hostname or "").lower() in hosts:
                    urls.append(u)
        for u in urls[:_MAX_URLS]:
            raw, why = await _fetch(svc, u, cap)
            if raw is None:
                notes.append(f"[Foundry: couldn't fetch {u} to show the user ({why}); give the "
                             f"user that link instead.]")
                continue
            kind = "audio" if (sniff_media(raw)[0] or "").startswith("audio/") else "image"
            add(raw, f"{tool} {kind}")
    return found, notes


async def store_result(svc, server: str, tool: str, args: Optional[dict], rich, *,
                       persona: Optional[str] = None, job_id: Optional[str] = None,
                       fetch_urls: bool = True) -> _Captured:
    """Save the result's media; return the items and notes for the model.
    job_id: write into this existing tool_job row instead of a new 'tool' row."""
    out = _Captured()
    media = getattr(svc, "media", None)
    if media is None or not svc.config_store.config.media.tool_outputs:
        return out
    try:
        found, out.notes = await _collect(svc, server, tool, rich, fetch_urls)
        if not found:
            return out
        n_img = n_aud = 0
        for f in found:
            if f["mime"].startswith("audio/"):
                n_aud += 1
                f["label"] = f"{tool} audio" + (f" {n_aud}" if n_aud > 1 else "")
            else:
                n_img += 1
                f["label"] = f"{tool} image" + (f" {n_img}" if n_img > 1 else "")
        rid = job_id or media.new_tool_row("tool", server, tool, args, persona=persona)
        arts = await asyncio.to_thread(media.write_tool_artifacts, rid, found)
        if job_id is None:
            media.finish_tool_row(rid, status="succeeded", artifacts=arts)
        else:
            media._update(rid, artifacts=json.dumps(arts))
        out.items = [MediaItem(rid, a["index"], a["mime"], a["bytes"], a["label"]) for a in arts]
    except Exception:                                            # noqa: BLE001
        log.exception("storing media from %s/%s failed", server, tool)
    return out


def items_of(row: dict) -> list[MediaItem]:
    from .media import _jl
    return [MediaItem(row["id"], a["index"], a["mime"], int(a.get("bytes") or 0),
                      a.get("label") or f"{(row.get('model') or '').split('/')[-1]} file")
            for a in _jl(row.get("artifacts"), [])]


# -- long-running tool jobs ------------------------------------------------------

class ToolJobs:
    """Background tool calls, one asyncio task each, state in media_jobs."""

    def __init__(self, svc):
        self.svc = svc
        self._tasks: dict[str, asyncio.Task] = {}
        self._events: dict[str, asyncio.Event] = {}
        self._progress: dict[str, Callable] = {}     # job -> live narrator while inline

    def is_long(self, server: str) -> bool:
        cfg = (getattr(self.svc.mcp, "servers", {}) or {}).get(server)
        return bool(cfg is not None and getattr(cfg, "long_running", False))

    def _key(self, server: str, tool: str, args: Optional[dict]) -> str:
        try:
            a = json.dumps(args or {}, sort_keys=True, default=str)
        except (TypeError, ValueError):
            a = str(args)
        return "t:" + hashlib.sha256(f"{server}/{tool}:{a}".encode()).hexdigest()[:32]

    def _running_duplicate(self, key: str) -> Optional[str]:
        from . import client_auth
        cid = client_auth.current().get("client_id") or "anonymous"
        row = self.svc.db.query_one(
            "SELECT id FROM media_jobs WHERE op='tool_job' AND client_id=? AND idem_key=? "
            "AND status='running' AND created_ts >= ? ORDER BY created_ts DESC LIMIT 1",
            (cid, key, time.time() - _DEDUPE_S))
        return row["id"] if row and row["id"] in self._tasks else None

    def start(self, mcp, server: str, tool: str, args: Optional[dict],
              persona: Optional[str]) -> tuple[str, bool]:
        """(job id, reused an identical running job)."""
        key = self._key(server, tool, args)
        dup = self._running_duplicate(key)
        if dup:
            return dup, True
        media = self.svc.media
        jid = media.new_tool_row("tool_job", server, tool, args, status="running",
                                 persona=persona, idem_key=key)
        self._events[jid] = asyncio.Event()
        self._tasks[jid] = asyncio.create_task(self._run(mcp, jid, server, tool, args, persona))
        return jid, False

    async def _run(self, mcp, jid: str, server: str, tool: str, args, persona) -> None:
        media = self.svc.media

        async def relay(progress, total, message):
            fn = self._progress.get(jid)
            if fn is not None:
                pct = f" {progress:.0f}/{total:.0f}" if total else ""
                try:
                    fn(f"⏳ {server}/{tool}{pct} {message or ''}".rstrip() + "\n")
                except Exception:                                # noqa: BLE001
                    pass

        try:
            from .brain.agent import raw_call_tool_rich
            rich = await raw_call_tool_rich(mcp, server, tool, args or {}, progress_callback=relay)
            cap = await store_result(self.svc, server, tool, args, rich, persona=persona,
                                     job_id=jid)
            text = rich.text or "(empty tool result)"
            media.finish_tool_row(jid, status="succeeded",
                                  artifacts=[a for a in _artifacts(media, jid)],
                                  result_text="\n".join(p for p in (text, "\n".join(cap.notes))
                                                        if p))
        except asyncio.CancelledError:
            media.finish_tool_row(jid, status="cancelled", artifacts=_artifacts(media, jid),
                                  error="cancelled", error_code="cancelled")
            raise
        except Exception as e:                                   # noqa: BLE001
            from .errors import describe_exception
            media.finish_tool_row(jid, status="failed", artifacts=_artifacts(media, jid),
                                  error=describe_exception(e)[:1000], error_code="tool_error")
            self.svc.db.log_event("warning", "media", f"tool job {jid} ({server}/{tool}) failed",
                                  describe_exception(e))
        finally:
            self._tasks.pop(jid, None)
            self._progress.pop(jid, None)
            ev = self._events.pop(jid, None)
            if ev is not None:
                ev.set()

    async def wait(self, jid: str, timeout: float, progress: Optional[Callable] = None,
                   label: str = "") -> dict:
        """Wait up to timeout for the job; narrate progress meanwhile."""
        ev = self._events.get(jid)
        if ev is not None and timeout > 0:
            if progress is not None:
                self._progress[jid] = progress
            t0 = time.monotonic()
            beat = 15.0
            try:
                while not ev.is_set():
                    left = timeout - (time.monotonic() - t0)
                    if left <= 0:
                        break
                    try:
                        await asyncio.wait_for(ev.wait(), timeout=min(beat, left))
                    except asyncio.TimeoutError:
                        if progress is not None and not ev.is_set():
                            from .keepalive import fmt_elapsed
                            try:
                                progress(f"⏳ {label or jid}: still working · "
                                         f"{fmt_elapsed(int(time.monotonic() - t0))}\n")
                            except Exception:                    # noqa: BLE001
                                pass
            finally:
                if self._progress.get(jid) is progress:
                    self._progress.pop(jid, None)
        return self.svc.media.get(jid) or {}


def _artifacts(media, jid: str) -> list:
    from .media import _jl
    return _jl((media.get(jid) or {}).get("artifacts"), [])


def _owned(row: dict) -> bool:
    from . import client_auth
    c = client_auth.current()
    return "admin" in (c.get("scopes") or []) or row.get("client_id") == (
        c.get("client_id") or "anonymous")


def _job_text(row: dict, sink: Optional[list]) -> str:
    st = row.get("status")
    label = row.get("model") or "tool"
    if st == "running":
        el = int(time.time() - (row.get("started_ts") or time.time()))
        return (f"Job {row['id']} ({label}) is still running ({el}s so far). Do NOT call the "
                f"original tool again. Tell the user it's still working; check again later "
                f"with {STATUS_TOOL}.")
    if st == "succeeded":
        items = items_of(row)
        if sink is not None:
            sink.extend(items)
        body = row.get("result_text") or "(empty tool result)"
        return f"Job {row['id']} ({label}) finished.\n{body}" + (
            "\n" + model_note(items, []) if items else "")
    return (f"Job {row['id']} ({label}) {st}: {row.get('error') or 'no detail'}. "
            f"You may start it again only if the user asks.")


async def job_status(svc, args: dict, sink: Optional[list] = None,
                     progress: Optional[Callable] = None) -> str:
    jid = str((args or {}).get("job_id") or "").strip().strip("`")
    row = svc.media.get(jid) if getattr(svc, "media", None) else None
    if not row or row.get("op") != "tool_job" or not _owned(row):
        return (f"No tool job {jid!r} for this client. Job ids look like img_ followed by 16 "
                f"hex digits and come from an earlier tool result.")
    try:
        wait = max(0.0, min(_MAX_WAIT, float((args or {}).get("wait_seconds", 30))))
    except (TypeError, ValueError):
        wait = 30.0
    if row.get("status") == "running":
        row = await svc.tool_jobs.wait(jid, wait, progress, row.get("model") or jid)
    return _job_text(row, sink)


# -- the one entry point the chat paths use --------------------------------------

async def run_tool(svc, mcp, server: str, tool: str, args: Optional[dict], *,
                   sink: list, persona: Optional[str] = None,
                   progress: Optional[Callable] = None):
    """Run a persona tool call for a chat path: long-running servers become a
    tool job, every result's media is stored and queued in `sink` for the
    reply, and the returned ToolResult's text says what the model needs."""
    from .tools.mcp_client import ToolResult
    if server == JOBS_SERVER:
        got: list = []
        res = ToolResult([{"type": "text", "text": await job_status(svc, args, got, progress)}])
        sink.extend(got)
        res.media_items = [i for i in got if isinstance(i, MediaItem)]
        return res
    jobs: Optional[ToolJobs] = getattr(svc, "tool_jobs", None)
    if jobs is not None and jobs.is_long(server) and getattr(svc, "media", None) is not None:
        jid, reused = jobs.start(mcp, server, tool, args, persona)
        wait = float(svc.config_store.config.media.tool_inline_wait_seconds or 0)
        if progress is not None:
            try:
                progress(f"⏳ {server}/{tool} running as job {jid}"
                         + (" (same request already running)" if reused else "")
                         + f" — waiting up to {int(wait)}s here\n")
            except Exception:                                    # noqa: BLE001
                pass
        row = await jobs.wait(jid, wait, progress, f"{server}/{tool}")
        if row.get("status") == "running":
            sink.append(PendingJob(jid, f"{server}/{tool}"))
            text = (f"Started as background job {jid} ({server}/{tool}); still running after "
                    f"{int(wait)}s. Do NOT call {tool} again — that would start a second "
                    f"generation. Tell the user it's working and that they can ask you to check; "
                    f"check with {STATUS_TOOL}(job_id=\"{jid}\").")
            return ToolResult([{"type": "text", "text": text}])
        if row.get("status") != "succeeded":
            raise RuntimeError(f"{server}/{tool}: {row.get('error') or row.get('status')}")
        items = items_of(row)
        sink.extend(items)
        res = ToolResult([{"type": "text", "text": row.get("result_text") or ""}])
        res.text = "\n".join(p for p in (res.text, model_note(items, [])) if p)
        res.media_items = items
        return res
    from .brain.agent import raw_call_tool_rich
    rich = await raw_call_tool_rich(mcp, server, tool, args or {})
    cap = await store_result(svc, server, tool, args, rich, persona=persona)
    if cap.items or cap.notes:
        sink.extend(cap.items)
        rich.text = "\n".join(p for p in ((rich.text or ""), model_note(cap.items, cap.notes)) if p)
    rich.media_items = cap.items
    return rich


def status_tooldef():
    from .tools.sync import ToolDef
    f = STATUS_SPEC["function"]
    return ToolDef(name=STATUS_TOOL, kind="mcp", description=f["description"],
                   parameters=f["parameters"], server=JOBS_SERVER, mcp_tool=STATUS_TOOL,
                   read_only=True, destructive=False)
