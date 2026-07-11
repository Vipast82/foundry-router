"""MCP wrapper for devnen/Chatterbox-TTS-Server (voice cloning / TTS).

Reference implementation (untested against a live install — the server
exposes an OpenAI-compatible speech endpoint; adjust env vars to your build):

  CHATTERBOX_URL   base URL of the TTS server        (default http://127.0.0.1:8004)
  CHATTERBOX_PATH  speech endpoint path              (default /v1/audio/speech)
  ARTIFACT_DIR     where to save generated audio     (default ./artifacts)
  MCP_HOST / MCP_PORT  wrapper listen address        (default 0.0.0.0:8766)

Run: python chatterbox_mcp.py   →   MCP endpoint at http://<host>:8766/mcp
"""

import os
import time
from pathlib import Path

import httpx
from mcp.server.fastmcp import FastMCP

CHATTERBOX_URL = os.environ.get("CHATTERBOX_URL", "http://127.0.0.1:8004").rstrip("/")
SPEECH_PATH = os.environ.get("CHATTERBOX_PATH", "/v1/audio/speech")
ARTIFACT_DIR = Path(os.environ.get("ARTIFACT_DIR", "./artifacts"))

mcp = FastMCP("chatterbox", host=os.environ.get("MCP_HOST", "0.0.0.0"),
              port=int(os.environ.get("MCP_PORT", "8766")))


@mcp.tool()
async def text_to_speech(text: str, voice: str = "default",
                         speed: float = 1.0) -> str:
    """Convert text to speech with the local Chatterbox TTS server. `voice`
    selects a configured/cloned voice by name. Returns a file path to the
    generated audio."""
    payload = {"model": "chatterbox", "input": text, "voice": voice,
               "speed": speed, "response_format": "wav"}
    async with httpx.AsyncClient(timeout=600) as client:
        r = await client.post(f"{CHATTERBOX_URL}{SPEECH_PATH}", json=payload)
        r.raise_for_status()
        audio = r.content

    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    out = ARTIFACT_DIR / f"chatterbox_{int(time.time())}.wav"
    out.write_bytes(audio)
    return f"Speech generated ({len(audio)} bytes): {out.resolve()}"


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
