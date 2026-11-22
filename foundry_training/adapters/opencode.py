"""OpenCode sessions for the training pipeline (v0.181.247).

Read through the server API only (never OpenCode's private storage): the coordinator's
OpenCode clients fetch the session, its messages and its children and hand them back in
the opencode 1.x `{info, parts}` shape Kilo also uses (OpenCode 2.x is translated by
foundry_coordinator.adapters.oc2), so `adapters.kilo.normalize` reads them unchanged. The
events are tagged `opencode` (`_adapter`), not `kilo`.
"""

from __future__ import annotations

ADAPTER = "opencode"


class OpenCodeSourceError(RuntimeError):
    pass


def from_api(url: str, session_id: str, password: str = "", directory: str = "",
             api: str = "auto") -> dict:
    from foundry_coordinator.adapters.opencode import OpenCodeClient
    c = OpenCodeClient(url, password=password, api=api).impl
    if hasattr(c, "session"):                                   # 2.x
        info = c.session(session_id, directory)
    else:                                                       # 1.x: the Kilo API
        info = c._req("GET", f"/session/{session_id}", query={"directory": directory} if
                      directory else None, ok404=True)
    if not info:
        raise OpenCodeSourceError(f"no session {session_id}")
    msgs = c.messages(session_id, directory)
    kids = c.children(session_id, directory=directory) or []
    return {"info": info, "messages": msgs, "children": [k.get("id") for k in kids if k.get("id")],
            "_sanitized": False, "_adapter": ADAPTER}


__all__ = ["ADAPTER", "OpenCodeSourceError", "from_api"]
