"""Kilo coding client: the supported `kilo serve` HTTP API.

Since v0.181.247 the HTTP code lives in `adapters/oc1.py` (`Oc1Client`), shared with
OpenCode 1.x, which speaks the same API. `KiloClient` is `Oc1Client` with the Kilo spec
(`foundry_automation.agent_backends.KILO`): the same requests, bodies, parsing, error
texts and checks as before. Every name this module exported is still exported here.

Verified against Kilo CLI 7.8.8 / 7.8.27; see oc1.py for the endpoint list.
"""

from __future__ import annotations

from foundry_automation.agent_backends import KILO

from .denial import _FOUNDRY_CATEGORIES, foundry_denial
from .oc1 import AgentServerError, Oc1Client, _is_compaction, wait_until_idle

# the old name of the error every agent client raises (callers and tests catch it)
KiloError = AgentServerError


class KiloClient(Oc1Client):
    name = "kilo"
    spec = KILO
    label = "Kilo"


__all__ = ["KiloClient", "KiloError", "AgentServerError", "foundry_denial", "wait_until_idle",
           "_is_compaction", "_FOUNDRY_CATEGORIES"]
