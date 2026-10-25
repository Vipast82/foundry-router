"""OpenAI audio endpoints (audit B4).

  POST /v1/audio/speech            JSON {model, input, voice, response_format, speed}
                                   -> audio bytes (Kokoro / Chatterbox via their MCP server)
  POST /v1/audio/transcriptions    501: no speech-to-text backend exists yet
  POST /v1/audio/translations      501: same

Open WebUI ("OpenAI" TTS engine) and AnythingLLM ("OpenAI compatible" TTS)
call /v1/audio/speech with these shapes. Both need the `media` client scope.

Speech is made by a TTS MCP server's speech tool (`audio.speech_tool`,
generate_speech). Those servers return a URL on their own host, not bytes;
Foundry downloads it, only from that MCP server's host (or audio.fetch_hosts),
and returns the bytes. Output is whatever the server makes (WAV for Kokoro
and Chatterbox) with its real Content-Type: no transcoding, so a request for
mp3 still gets WAV (browsers and both clients play it).
"""

from __future__ import annotations

import base64
import re
from typing import Optional
from urllib.parse import urlparse

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

router = APIRouter()

# Kokoro voice ids: <lang letter><f|m>_<name>, e.g. af_heart, bm_george, jf_alpha
_KOKORO_VOICE = re.compile(r"^[a-z][fm]_[a-z0-9]+$")
_URL = re.compile(r"https?://[^\s\"'<>)\]]+")
_AUDIO_EXT = re.compile(r"\.(wav|mp3|ogg|opus|flac|aac|m4a|pcm)(\?|$)", re.I)


def _cfg(svc):
    return svc.config_store.config.audio


def _error(status: int, message: str, code: str, typ: str = "invalid_request_error"):
    return JSONResponse({"error": {"message": message, "type": typ, "code": code}},
                        status_code=status)


def resolve_speech_server(cfg, model: Optional[str]) -> tuple[str, str]:
    """(model key, MCP server name) for an OpenAI `model` value: a configured
    key (kokoro / chatterbox), an OpenAI alias (tts-1 ...), else the default."""
    m = (model or "").strip().lower()
    key = m if m in cfg.speech_servers else cfg.speech_aliases.get(m) or cfg.default_speech
    return key, cfg.speech_servers.get(key, "")


def speech_arguments(cfg, key: str, text: str, voice: Optional[str], speed) -> dict:
    """Tool arguments for the server. Kokoro takes voice + speed (an OpenAI
    voice name like `alloy` becomes the default Kokoro voice); Chatterbox
    takes text only."""
    args: dict = {"text": text}
    if key == "kokoro":
        v = (voice or "").strip()
        args["voice"] = v if _KOKORO_VOICE.match(v) else cfg.default_voice
        try:
            s = float(speed) if speed is not None else None
        except (TypeError, ValueError):
            s = None
        if s is not None:
            args["speed"] = min(max(s, 0.25), 4.0)    # OpenAI's range
    return args


def sniff_mime(data: bytes, fallback: str = "application/octet-stream") -> str:
    if data[:4] == b"RIFF" and data[8:12] == b"WAVE":
        return "audio/wav"
    if data[:3] == b"ID3" or data[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"):
        return "audio/mpeg"
    if data[:4] == b"OggS":
        return "audio/ogg"
    if data[:4] == b"fLaC":
        return "audio/flac"
    return fallback


def audio_url(text: str) -> Optional[str]:
    """The audio URL in a tool's text result: the first one that looks like an
    audio file, else the first URL at all."""
    urls = [u.rstrip(".,;:!") for u in _URL.findall(text or "")]
    return next((u for u in urls if _AUDIO_EXT.search(u)), urls[0] if urls else None)


def _allowed_host(svc, server: str, url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    if not host:
        return False
    allowed = {h.lower() for h in (_cfg(svc).fetch_hosts or [])}
    scfg = (getattr(svc.mcp, "servers", None) or {}).get(server)
    if scfg is not None and getattr(scfg, "url", ""):
        allowed.add((urlparse(scfg.url).hostname or "").lower())
    return host in allowed


async def _fetch(svc, url: str, max_bytes: int) -> tuple[bytes, str]:
    cfg = _cfg(svc)
    async with svc.http.stream("GET", url, timeout=cfg.fetch_timeout_seconds) as r:
        if r.status_code >= 400:
            raise RuntimeError(f"audio download {url} answered HTTP {r.status_code}")
        chunks, size = [], 0
        async for chunk in r.aiter_bytes():
            size += len(chunk)
            if size > max_bytes:
                raise RuntimeError(f"audio larger than {max_bytes // 1_000_000} MB")
            chunks.append(chunk)
        ctype = (r.headers.get("content-type") or "").split(";")[0].strip()
    return b"".join(chunks), ctype


async def synthesize(svc, server: str, args: dict) -> tuple[bytes, str]:
    """Run the speech tool and return (audio bytes, mime type). Accepts an
    MCP audio block, an embedded resource blob, or a URL in the text."""
    from .. import request_context
    cfg = _cfg(svc)
    max_bytes = int(cfg.max_audio_mb * 1_000_000)
    rich = await request_context.mcp_attributed(
        svc.mcp.call_tool_rich(server, cfg.speech_tool, args), "audio-api", "/v1/audio/speech")
    for b in rich.blocks:
        if b.get("type") == "audio" and b.get("data"):
            data = base64.b64decode(b["data"])
            return data, b.get("mimeType") or sniff_mime(data)
        if b.get("type") == "resource" and b.get("blob") and \
                str(b.get("mimeType") or "").startswith("audio/"):
            data = base64.b64decode(b["blob"])
            return data, b["mimeType"]
    links = [b.get("uri") for b in rich.blocks if b.get("type") == "resource_link"]
    url = next((u for u in links if u), None) or audio_url(rich.text)
    if not url:
        raise RuntimeError(f"{server}/{cfg.speech_tool} returned no audio and no URL: "
                           f"{rich.text[:200]}")
    if not _allowed_host(svc, server, url):
        raise PermissionError(f"{server} returned an audio URL on another host "
                              f"({urlparse(url).hostname}); add it to audio.fetch_hosts "
                              f"to allow it")
    data, ctype = await _fetch(svc, url, max_bytes)
    sniffed = sniff_mime(data, "")
    return data, sniffed or (ctype if ctype.startswith("audio/") else "audio/wav")


@router.post("/v1/audio/speech")
async def speech(request: Request):
    from ..usage import RequestLogger
    svc = request.app.state.services
    cfg = _cfg(svc)
    if not cfg.enabled:
        return _error(404, "audio endpoints are turned off (audio.enabled)", "audio_disabled")
    try:
        body = await request.json()
    except ValueError:
        return _error(400, "body must be JSON", "invalid_json")
    if not isinstance(body, dict):
        return _error(400, "body must be a JSON object", "invalid_json")
    text = body.get("input")
    if not isinstance(text, str) or not text.strip():
        return _error(400, "`input` (the text to speak) is required", "missing_input")
    if len(text) > cfg.max_input_chars:
        return _error(400, f"`input` is longer than {cfg.max_input_chars} characters",
                      "input_too_long")
    key, server = resolve_speech_server(cfg, body.get("model"))
    if not server:
        return _error(400, f"no speech server configured for model {body.get('model')!r} "
                           f"(audio.speech_servers)", "model_not_found")
    known = getattr(svc.mcp, "servers", None) or {}
    if server not in known:
        return _error(503, f"speech server {server!r} is not one of Foundry's MCP servers; "
                           f"add it on the MCP tab (or start it in the Foundry Client)",
                      "speech_server_missing", "api_error")
    args = speech_arguments(cfg, key, text, body.get("voice"), body.get("speed"))
    logger = RequestLogger(svc.db, "", str(body.get("model") or key), "speech", text[:200])
    try:
        data, mime = await synthesize(svc, server, args)
    except PermissionError as e:
        logger.finish("error", str(e))
        return _error(502, str(e), "audio_host_not_allowed", "api_error")
    except Exception as e:                                  # noqa: BLE001
        from ..errors import describe_exception
        detail = describe_exception(e)
        logger.finish("error", detail)
        return _error(502, f"speech generation failed on {server}: {detail[:400]}",
                      "speech_failed", "api_error")
    logger.finish("ok")
    return Response(content=data, media_type=mime,
                    headers={"x-foundry-speech-server": server,
                             "x-foundry-speech-voice": str(args.get("voice") or "")})


_NO_STT = ("Foundry has no speech-to-text backend yet: /v1/audio/transcriptions and "
           "/v1/audio/translations are not available. Use your client's own (browser / "
           "local Whisper) speech-to-text for now.")


@router.post("/v1/audio/transcriptions")
async def transcriptions(request: Request):
    return _error(501, _NO_STT, "no_stt_backend", "not_implemented")


@router.post("/v1/audio/translations")
async def translations(request: Request):
    return _error(501, _NO_STT, "no_stt_backend", "not_implemented")
