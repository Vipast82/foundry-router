"""Persona system (design doc §4.8): DB-backed registry of named virtual
models. Each row is a routing *policy*, not a real model — `/api/tags`
advertises the enabled rows, and picking one in any client's model dropdown
selects the policy. Growing the system to new workload types (Foundry-Creative
etc.) is adding a row here via the web UI, never a code change.
"""

from __future__ import annotations

import json
from typing import Optional

from .db import Database, utcnow

PERSONA_FIELDS = ["description", "benchmark_category", "local_bias_strength",
                  "escalation_triggers", "preferred_mcp_tools",
                  "guardrail_overrides", "pinned_models", "enabled"]


class PersonaStore:
    def __init__(self, db: Database):
        self.db = db

    def list(self, enabled_only: bool = False) -> list[dict]:
        sql = "SELECT * FROM personas"
        if enabled_only:
            sql += " WHERE enabled=1"
        return self.db.query(sql + " ORDER BY virtual_name")

    def get(self, virtual_name: str) -> Optional[dict]:
        """Lookup tolerant of the ':latest'/':tag' suffix Ollama clients love
        to append, and of case differences."""
        base = virtual_name.split(":")[0]
        return self.db.query_one(
            "SELECT * FROM personas WHERE lower(virtual_name) IN (lower(?), lower(?))",
            (virtual_name, base))

    def upsert(self, virtual_name: str, **fields) -> None:
        now = utcnow()
        for k in ("escalation_triggers", "preferred_mcp_tools", "guardrail_overrides",
                  "pinned_models"):
            if k in fields and not isinstance(fields[k], (str, type(None))):
                fields[k] = json.dumps(fields[k])
        fields = {k: v for k, v in fields.items() if k in PERSONA_FIELDS}
        existing = self.get(virtual_name)
        if existing is None:
            cols = ["virtual_name", "created_at", "updated_at"] + list(fields)
            self.db.execute(
                f"INSERT INTO personas ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                [virtual_name, now, now] + list(fields.values()))
        elif fields:
            sets = ",".join(f"{k}=?" for k in fields)
            self.db.execute(
                f"UPDATE personas SET {sets}, updated_at=? WHERE virtual_name=?",
                list(fields.values()) + [now, existing["virtual_name"]])

    def delete(self, virtual_name: str) -> None:
        self.db.execute("DELETE FROM personas WHERE virtual_name=?", (virtual_name,))

    @staticmethod
    def guardrail_overrides(persona: Optional[dict]) -> dict:
        if not persona or not persona.get("guardrail_overrides"):
            return {}
        try:
            parsed = json.loads(persona["guardrail_overrides"])
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
