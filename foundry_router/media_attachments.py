"""Chat attachments for media-gen (v0.181.117).

A picture (or audio / video) attached in a chat reaches Foundry as base64 inside the message.
The model can't copy megabytes of base64 into a tool argument, so "use this photo as the
singer" or "make this picture talk" failed. media-gen (the `Media_Gen` MCP server, v5.11)
takes media as references instead: a job id, an `upload:...` reference or a URL, and has a
JSON upload endpoint:

    POST <media-gen base>/upload  {"files": [{"name", "type", "data": "data:...;base64,..."}]}
    -> {"uploads": [{"ref": "upload:<sha8>_<name>", "kind", "file", "sha256"}], "errors": [...]}

When a request's tool set includes a Media_Gen tool and the persona's `media_attachments`
setting is on (the default), every media attachment in the conversation's user messages is
uploaded once (cached by sha256) and its reference is written into that same message as one
text line after the attachments:

    [Attached for media tools: image 1 = upload:a6ffd754_attachment-1.png; image 2 = upload:...]

The picture itself stays in the message (vision models still see it). Attachments are numbered
per kind in conversation order, so "use image 2" and "the photo I sent earlier" work. A public
http(s) URL is passed as its own reference (media-gen fetches URLs); a URL on Foundry's own file
store (/media/a/ links) or a host in media.attachments_fetch_hosts is fetched here and uploaded.
A failure never blocks the chat: the line says "[image 1 couldn't be passed to media tools:
<reason>]" and a warning is logged (persona and reason, never the data). A request without
attachments is untouched.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import logging
import os
import re
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urlparse

log = logging.getLogger(__name__)

MEDIA_SERVER = "Media_Gen"
UPLOAD_TIMEOUT_S = 15.0
FETCH_TIMEOUT_S = 15.0
MAX_ATTACHMENT_BYTES = 50 * 1024 * 1024     # per attachment
BATCH_FILES = 20                            # media-gen: up to 20 files and 64 MB per request
BATCH_BYTES = 60 * 1024 * 1024              # base64 grows by a third; stay under 64 MB
CACHE_SIZE = 400
# the private message keys the OpenAI facade sets for this module (dropped before any model)
URL_KEY = "_media_urls"
PART_KEY = "_media_parts"
GUIDE = ("Attached pictures/audio have media references in [Attached for media tools: ...]; "
         "pass those references to Media_Gen tools (image, photo, singer_image, audio, song, "
         "video, logo...).")

_CACHE: "OrderedDict[str, str]" = OrderedDict()      # sha256 -> upload ref (successes only)

_EXT = {"image/png": "png", "image/jpeg": "jpg", "image/gif": "gif", "image/webp": "webp",
        "image/bmp": "bmp", "audio/wav": "wav", "audio/x-wav": "wav", "audio/mpeg": "mp3",
        "audio/mp3": "mp3", "audio/flac": "flac", "audio/ogg": "ogg", "audio/webm": "webm",
        "audio/mp4": "m4a", "video/mp4": "mp4", "video/webm": "webm",
        "video/quicktime": "mov"}
_AUDIO_FMT = {"wav": "audio/wav", "mp3": "audio/mpeg", "flac": "audio/flac", "ogg": "audio/ogg",
              "webm": "audio/webm", "m4a": "audio/mp4", "pcm16": "audio/wav"}
_DATA_URL = re.compile(r"^data:([^;,]*)(;[^,]*)?,(.*)$", re.S)


@dataclass
class Attachment:
    msg: int                         # index in the conversation
    kind: str                        # image / audio / video
    number: int = 0                  # per kind, in conversation order
    data: Optional[bytes] = None
    mime: str = ""
    url: str = ""                    # an http(s) source
    name: str = ""
    ref: str = ""                    # the reference to give the model
    error: str = ""
    sha256: str = ""
    fetched: bool = field(default=False)


# ------------------------------------------------------------------ small helpers

def cache_get(sha: str) -> Optional[str]:
    ref = _CACHE.get(sha)
    if ref is not None:
        _CACHE.move_to_end(sha)
    return ref


def cache_put(sha: str, ref: str) -> None:
    _CACHE[sha] = ref
    _CACHE.move_to_end(sha)
    while len(_CACHE) > CACHE_SIZE:
        _CACHE.popitem(last=False)


def sniff_mime(data: bytes) -> str:
    """The media type from the first bytes ('' when not an image/audio/video we know)."""
    h = data[:16]
    if h.startswith(b"\x89PNG"):
        return "image/png"
    if h.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if h[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if h.startswith(b"RIFF") and h[8:12] == b"WEBP":
        return "image/webp"
    if h.startswith(b"BM"):
        return "image/bmp"
    if h.startswith(b"RIFF") and h[8:12] == b"WAVE":
        return "audio/wav"
    if h.startswith(b"ID3") or h[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"):
        return "audio/mpeg"
    if h.startswith(b"fLaC"):
        return "audio/flac"
    if h.startswith(b"OggS"):
        return "audio/ogg"
    if h[4:8] == b"ftyp":
        return "video/mp4"
    if h.startswith(b"\x1a\x45\xdf\xa3"):
        return "video/webm"
    return ""


def kind_of(mime: str) -> str:
    m = (mime or "").split(";")[0].strip().lower()
    for k in ("image", "audio", "video"):
        if m.startswith(k + "/"):
            return k
    return ""


def decode_data(value: str) -> tuple[Optional[bytes], str]:
    """(bytes, mime) from a data: URL or plain base64; (None, '') when it can't be read."""
    s = (value or "").strip()
    mime = ""
    m = _DATA_URL.match(s)
    if m:
        mime = (m.group(1) or "").strip().lower()
        s = m.group(3)
    try:
        data = base64.b64decode(s, validate=False)
    except (binascii.Error, ValueError):
        return None, ""
    return (data or None), mime


def _human(n: int) -> str:
    return f"{n / 1048576:.1f} MB" if n >= 1048576 else f"{max(1, n // 1024)} KB"


# ------------------------------------------------------------------ settings

def upload_url(svc) -> str:
    """MEDIA_GEN_UPLOAD_URL, else media.attachments_upload_url, else the Media_Gen MCP server's
    URL with its trailing /mcp (or /sse) replaced by /upload ('' when there is no such server)."""
    env = os.environ.get("MEDIA_GEN_UPLOAD_URL", "").strip()
    if env:
        return env
    cfg = svc.config_store.config
    own = str(getattr(cfg.media, "attachments_upload_url", "") or "").strip()
    if own:
        return own
    for s in getattr(cfg, "mcp_servers", None) or []:
        if getattr(s, "name", "") == MEDIA_SERVER and getattr(s, "enabled", True):
            u = str(s.url or "").rstrip("/")
            for tail in ("/mcp", "/sse"):
                if u.endswith(tail):
                    u = u[: -len(tail)]
                    break
            return u + "/upload" if u else ""
    return ""


def persona_on(persona: Optional[dict]) -> bool:
    """The persona's media_attachments setting: unset = on."""
    v = (persona or {}).get("media_attachments")
    if v is None or v == "":
        return True
    try:
        return bool(int(v))
    except (TypeError, ValueError):
        return str(v).strip().lower() not in ("off", "false", "no")


def media_in_toolset(svc, persona: dict, client_tools, direct: bool) -> bool:
    """Does this request's tool set include a Media_Gen tool (after tool policy and dedupe)?
    Direct mode: the tools _direct_mcp_split offers (or, with find on demand, the granted set
    the finder reaches). Agent / pipeline: the persona's granted tools."""
    try:
        granted = svc.tool_registry.mcp_tools_for_persona(persona)
    except Exception:                                             # noqa: BLE001
        return False
    if not any(t.server == MEDIA_SERVER for t in granted):
        return False
    if not direct:
        return True
    from .facade.ollama_api import _direct_mcp_split
    from .tool_finder import FINDER_SERVER
    try:
        offered, _, _ = _direct_mcp_split(svc, persona, client_tools)
    except Exception:                                             # noqa: BLE001
        return False
    return any(td.server in (MEDIA_SERVER, FINDER_SERVER) for td in offered.values())


def _fetch_hosts(svc) -> set:
    hosts = getattr(svc.config_store.config.media, "attachments_fetch_hosts", None) or []
    return {str(h).strip().lower() for h in hosts if str(h).strip()}


def _own_url(svc, url: str) -> bool:
    """A URL Foundry should fetch itself: its own file store (/media/a/ signed links) or a host
    listed in media.attachments_fetch_hosts (e.g. a chat front end's file store)."""
    p = urlparse(url)
    return "/media/a/" in p.path or (p.hostname or "").lower() in _fetch_hosts(svc)


# ------------------------------------------------------------------ extraction

def extract(raw_messages: list, messages: list) -> list[Attachment]:
    """Every media attachment of the user messages, in conversation order. `messages` are the
    canonical (Ollama-shaped) messages: their `images` are base64 strings. `raw_messages` are
    the same messages as the facade passed them (aligned by index): the OpenAI facade keeps
    http(s) image URLs under _media_urls and audio / video / file parts under _media_parts.
    A raw message whose content is still an OpenAI part list is read directly."""
    out: list[Attachment] = []
    for i, m in enumerate(messages or []):
        if (m or {}).get("role") != "user":
            continue
        raw = raw_messages[i] if i < len(raw_messages or []) and isinstance(raw_messages[i], dict) \
            else {}
        for b64 in m.get("images") or []:
            data, mime = decode_data(b64) if isinstance(b64, str) else (None, "")
            if data is None:
                out.append(Attachment(i, "image", error="the picture's data could not be read"))
                continue
            mime = mime or sniff_mime(data) or "image/png"
            out.append(Attachment(i, kind_of(mime) or "image", data=data, mime=mime))
        for url in raw.get(URL_KEY) or []:
            if isinstance(url, str) and url.startswith(("http://", "https://")):
                out.append(Attachment(i, "image", url=url))
        for part in raw.get(PART_KEY) or []:
            if not isinstance(part, dict):
                continue
            if part.get("url"):
                out.append(Attachment(i, kind_of(part.get("mime") or "") or "image",
                                      url=str(part["url"]), name=str(part.get("name") or "")))
                continue
            data, mime = decode_data(str(part.get("data") or ""))
            mime = (part.get("mime") or mime or (sniff_mime(data) if data else "")).lower()
            k = kind_of(mime)
            if not k:
                continue                                     # not media: never sent
            if data is None:
                out.append(Attachment(i, k, error=f"the {k}'s data could not be read"))
                continue
            out.append(Attachment(i, k, data=data, mime=mime, name=str(part.get("name") or "")))
    counts: dict = {}
    for a in out:
        counts[a.kind] = counts.get(a.kind, 0) + 1
        a.number = counts[a.kind]
        if a.data is not None:
            a.sha256 = hashlib.sha256(a.data).hexdigest()
            ext = _EXT.get(a.mime, a.mime.split("/")[-1] if "/" in a.mime else "bin")
            if not a.name:
                a.name = f"attachment-{a.kind}{a.number}.{ext}"
    return out


def openai_side_parts(content: list) -> tuple[list, list]:
    """(_media_urls, _media_parts) from an OpenAI content part list: http(s) image URLs, and
    input_audio / file parts carrying audio, video or images. Nothing else is kept."""
    urls, parts = [], []
    for part in content or []:
        if not isinstance(part, dict):
            continue
        t = part.get("type")
        if t in ("image_url", "input_image"):
            url = part.get("image_url")
            url = url.get("url") if isinstance(url, dict) else url
            if isinstance(url, str) and url.startswith(("http://", "https://")):
                urls.append(url)
        elif t == "input_audio":
            a = part.get("input_audio") or {}
            fmt = str(a.get("format") or "wav").lower()
            if a.get("data"):
                parts.append({"data": a["data"], "mime": _AUDIO_FMT.get(fmt, "audio/" + fmt),
                              "name": f"attachment-audio.{fmt}"})
        elif t in ("file", "input_file"):
            f = part.get("file") if isinstance(part.get("file"), dict) else part
            data = f.get("file_data") or f.get("data") or ""
            name = str(f.get("filename") or f.get("name") or "")
            if isinstance(data, str) and data:
                mime = ""
                m = _DATA_URL.match(data.strip())
                if m:
                    mime = (m.group(1) or "").lower()
                if kind_of(mime) or not mime:
                    parts.append({"data": data, "mime": mime, "name": name})
            elif isinstance(f.get("file_url") or f.get("url"), str):
                u = f.get("file_url") or f.get("url")
                if u.startswith(("http://", "https://")):
                    parts.append({"url": u, "mime": str(f.get("mime_type") or ""), "name": name})
    return urls, parts


# ------------------------------------------------------------------ upload

async def _fetch(a: Attachment, client) -> None:
    try:
        r = await client.get(a.url, timeout=FETCH_TIMEOUT_S, follow_redirects=True)
    except Exception as e:                                        # noqa: BLE001
        a.error = f"could not fetch it ({type(e).__name__})"
        return
    if r.status_code != 200:
        a.error = f"could not fetch it (HTTP {r.status_code})"
        return
    data = r.content
    if len(data) > MAX_ATTACHMENT_BYTES:
        a.error = f"too large ({_human(len(data))}, the limit is 50 MB)"
        return
    mime = (r.headers.get("content-type") or "").split(";")[0].strip().lower()
    mime = mime if kind_of(mime) else (sniff_mime(data) or mime)
    if not kind_of(mime):
        a.error = "not an image, audio or video file"
        return
    a.data, a.mime, a.kind, a.fetched = data, mime, kind_of(mime), True
    a.sha256 = hashlib.sha256(data).hexdigest()
    a.name = a.name or f"attachment-{a.kind}{a.number}.{_EXT.get(mime, 'bin')}"


async def _upload(url: str, items: list[Attachment], client) -> None:
    """Upload `items` (bytes, not cached yet) in batches; set each one's ref or error."""
    batches, cur, size = [], [], 0
    for a in items:
        n = len(a.data or b"") * 4 // 3
        if cur and (len(cur) >= BATCH_FILES or size + n > BATCH_BYTES):
            batches.append(cur)
            cur, size = [], 0
        cur.append(a)
        size += n
    if cur:
        batches.append(cur)
    for batch in batches:
        names = {}
        files = []
        for a in batch:
            # media-gen stores <sha8>_<name> and reports `file` = this name; a clash of two
            # client-given names still matches by sha256 below
            name = a.name
            names.setdefault(name, a)
            files.append({"name": name, "type": a.mime,
                          "data": f"data:{a.mime};base64," + base64.b64encode(a.data).decode()})
        try:
            r = await client.post(url, json={"files": files}, timeout=UPLOAD_TIMEOUT_S)
        except Exception as e:                                    # noqa: BLE001
            for a in batch:
                a.error = f"media-gen unreachable ({type(e).__name__})"
            continue
        if r.status_code == 413:
            for a in batch:
                a.error = "too large for media-gen's upload"
            continue
        try:
            body = r.json()
        except ValueError:
            for a in batch:
                a.error = f"media-gen answered HTTP {r.status_code} without JSON"
            continue
        for u in body.get("uploads") or []:
            a = names.get(str(u.get("file") or ""))
            if a is None:                      # matched by sha256 when the name differs
                a = next((x for x in batch if x.sha256 and x.sha256 == u.get("sha256")), None)
            if a is not None and u.get("ref"):
                a.ref = str(u["ref"])
                cache_put(a.sha256, a.ref)
        errs = [str(e) for e in body.get("errors") or []]
        for a in batch:
            if a.ref:
                continue
            mine = next((e.split(":", 1)[1].strip() for e in errs
                         if e.split(":", 1)[0].strip() == a.name),
                        "")
            a.error = mine or (f"media-gen refused it (HTTP {r.status_code})"
                               if r.status_code >= 400 else "media-gen did not store it")


async def resolve(svc, atts: list[Attachment], client=None) -> None:
    """Give every attachment a ref or an error: cached sha256 -> ref, public URLs as
    themselves, own-store URLs fetched and uploaded, the rest uploaded in batches."""
    import httpx
    url = upload_url(svc)
    own = client is None
    client = client or httpx.AsyncClient()
    try:
        for a in atts:
            if a.error or a.ref:
                continue
            if a.url and a.data is None:
                if _own_url(svc, a.url):
                    await _fetch(a, client)
                else:
                    a.ref = a.url                      # media-gen fetches public URLs itself
                    continue
            if a.data is None:
                continue
            if len(a.data) > MAX_ATTACHMENT_BYTES:
                a.error = f"too large ({_human(len(a.data))}, the limit is 50 MB)"
                continue
            ref = cache_get(a.sha256)
            if ref:
                a.ref = ref
        todo = [a for a in atts if not a.ref and not a.error and a.data is not None]
        if todo:
            if not url:
                for a in todo:
                    a.error = "no media-gen upload address (Media_Gen MCP server not configured)"
            else:
                await _upload(url, todo, client)
    finally:
        if own:
            await client.aclose()


# ------------------------------------------------------------------ injection

def inject(messages: list, atts: list[Attachment]) -> list:
    """A copy of `messages` with one line per user message that has attachments, after its
    attachments (in the text), and the guide sentence once, on the last such message.
    Pictures stay in place."""
    by_msg: dict = {}
    for a in atts:
        by_msg.setdefault(a.msg, []).append(a)
    if not by_msg:
        return messages
    last = max(by_msg)
    out = list(messages)
    for i, items in by_msg.items():
        refs = [f"{a.kind} {a.number} = {a.ref}" for a in items if a.ref]
        lines = []
        if refs:
            lines.append("[Attached for media tools: " + "; ".join(refs) + "]")
        lines += [f"[{a.kind} {a.number} couldn't be passed to media tools: {a.error}]"
                  for a in items if not a.ref]
        if i == last and any(a.ref for a in atts):
            lines.append(GUIDE)
        m = dict(out[i])
        text = m.get("content") or ""
        m["content"] = (text.rstrip() + "\n\n" if text.strip() else "") + "\n".join(lines)
        out[i] = m
    return out


async def apply(svc, persona: Optional[dict], raw_messages: list, messages: list,
                client_tools, direct: bool, client=None) -> list:
    """The hook for one chat request: the messages to send (the same list object when nothing
    applies, so a request without attachments is untouched)."""
    if not persona or not persona_on(persona):
        return messages
    atts = extract(raw_messages, messages)
    if not atts:
        return messages
    if not media_in_toolset(svc, persona, client_tools, direct):
        return messages
    t0 = time.monotonic()
    try:
        await resolve(svc, atts, client)
    except Exception as e:                                        # noqa: BLE001
        for a in atts:
            if not a.ref:
                a.error = a.error or f"upload failed ({type(e).__name__})"
    pname = persona.get("virtual_name") or "?"
    for a in atts:
        if a.error:
            msg = f"persona {pname}: {a.kind} {a.number} not passed to media tools: {a.error}"
            log.warning(msg)
            try:
                svc.db.log_event("warning", "media_attachments", msg)
            except Exception:                                     # noqa: BLE001
                pass
    ok = [a for a in atts if a.ref]
    if ok:
        kinds: dict = {}
        for a in ok:
            kinds[a.kind] = kinds.get(a.kind, 0) + 1
        size = sum(len(a.data or b"") for a in ok)
        line = (f"persona {pname}: {len(ok)} media reference(s) injected ("
                + ", ".join(f"{n} {k}" for k, n in sorted(kinds.items())) + f"), "
                f"{_human(size) if size else 'URLs only'}, {int((time.monotonic() - t0) * 1000)} ms")
        log.info(line)
        try:
            svc.db.log_event("info", "media_attachments", line)
        except Exception:                                         # noqa: BLE001
            pass
    return inject(messages, atts)
