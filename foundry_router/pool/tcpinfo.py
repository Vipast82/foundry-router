"""Kernel TCP counters for one backend request (Linux TCP_INFO).

Answers "was the time before the first token spent on the wire?" without
tcpdump: per request, Foundry reads the connection's TCP_INFO and reports
the change since the previous request on the same connection (or since the
connection opened):

  retrans   segments the kernel had to RESEND (packet loss on the path);
  rwnd_ms   time the send was stalled because the SERVER's receive window was
            full — i.e. llama.cpp wasn't reading its socket;

Both zero means the request reached the server machine promptly and any wait
is inside the server process. Linux-only; elsewhere every value is None.
"""

from __future__ import annotations

import socket
import struct
import weakref
from typing import Any, Optional

# struct tcp_info (linux/tcp.h): 8 u8, 24 u32 (total_retrans is the last),
# then u64 pacing_rate, max_pacing_rate, bytes_acked, bytes_received,
# 6 u32 (segs_out … data_segs_out), u64 delivery_rate, busy_time,
# rwnd_limited, sndbuf_limited.
_OFF_TOTAL_RETRANS = 8 + 23 * 4          # 100
_OFF_RWND_LIMITED = 176                  # µs, kernel >= 4.10
_LEN = 192

_last: "weakref.WeakKeyDictionary[Any, tuple]" = weakref.WeakKeyDictionary()


def _read(sock) -> Optional[tuple]:
    if sock is None or not hasattr(socket, "TCP_INFO"):
        return None
    try:
        raw = sock.getsockopt(socket.IPPROTO_TCP, socket.TCP_INFO, _LEN)
    except (OSError, ValueError, TypeError):
        return None
    if len(raw) < _OFF_TOTAL_RETRANS + 4:
        return None
    retrans = struct.unpack_from("I", raw, _OFF_TOTAL_RETRANS)[0]
    rwnd = (struct.unpack_from("Q", raw, _OFF_RWND_LIMITED)[0]
            if len(raw) >= _OFF_RWND_LIMITED + 8 else None)
    return retrans, rwnd


def socket_of(response) -> Any:
    """The raw socket under an httpx response (None if unavailable)."""
    try:
        return response.extensions["network_stream"].get_extra_info("socket")
    except Exception:                                            # noqa: BLE001
        return None


def request_delta(sock) -> dict:
    """{retrans, rwnd_ms} for the request that just finished on this
    socket: counters now minus what they were after the previous request on
    the same connection (a fresh connection starts from zero)."""
    now = _read(sock)
    if now is None:
        return {}
    retrans, rwnd = now
    try:
        prev = _last.get(sock)
        _last[sock] = (retrans, rwnd)
    except TypeError:                     # not weak-referenceable
        prev = None
    p_retrans, p_rwnd = prev or (0, 0)
    out = {"retrans": max(0, retrans - p_retrans)}
    if rwnd is not None:
        out["rwnd_ms"] = round(max(0, rwnd - (p_rwnd or 0)) / 1000.0, 1)
    return out
