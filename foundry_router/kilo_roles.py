"""Canonical persona ids for a Kilo + coordinator setup, and the legacy aliases.

The coordinator profile names personas by role (roles.reviewer =
"Foundry-Kilo-Review", ...). Client allowlists match persona ids exactly
(client_auth.persona_allowed): a legacy single-model alias such as
Foundry-Kilo-Sonnet never authorizes Foundry-Kilo-Review, even when both route
to the same model. These tables are used for labels, presets and diagnostics
only. Nothing here renames, deletes or re-authorizes anything.
"""

from __future__ import annotations

# role -> canonical persona id (AnimalUprising coordinator profile)
CANONICAL = {
    "implementer": "Foundry-Kilo-Local",
    "planner": "Foundry-Kilo-Planner",
    "researcher": "Foundry-Kilo-Research",
    "reviewer": "Foundry-Kilo-Review",
    "deep_reviewer": "Foundry-Kilo-Deep-Review",
    "visual_reviewer": "Foundry-Kilo-Visual-Review",
}
ROLE_OF = {v: k for k, v in CANONICAL.items()}

ROLE_LABEL = {
    "implementer": "primary worker",
    "planner": "planner helper",
    "researcher": "research helper",
    "reviewer": "normal reviewer",
    "deep_reviewer": "high-risk reviewer",
    "visual_reviewer": "visual reviewer",
}

# Single-model personas from the animaluprising-kilo profile v1. Still valid
# personas, but not the ids the coordinator's roles use.
LEGACY_ALIASES = ("Foundry-Kilo-Haiku", "Foundry-Kilo-Sonnet", "Foundry-Kilo-Opus",
                  "Foundry-Kilo-Luna", "Foundry-Kilo-Sol", "Foundry-Kilo-Astra")

# What the coordinator calls with ITS OWN key: direct exact-commit reviews.
# The primary worker and helpers run on Kilo's inference key instead, so a
# coordinator key needs none of those.
COORDINATOR_DIRECT_ROLES = ("reviewer", "deep_reviewer")
COORDINATOR_REQUIRED = ("reviewer",)


def display_name(persona: str) -> str:
    """Name shown in a generated Kilo config: the id plus its role, or a
    legacy-alias note, so a picker doesn't suggest Sonnet is the reviewer."""
    if persona in ROLE_OF:
        return f"{persona} ({ROLE_LABEL[ROLE_OF[persona]]})"
    if persona in LEGACY_ALIASES:
        return f"{persona} (legacy alias, not a coordinator role)"
    return persona


def coordinator_role_gaps(scopes: list, personas: list) -> list[dict]:
    """For a client that can orchestrate: required coordinator roles its
    persona allowlist doesn't permit. An empty allowlist permits everything."""
    if "orchestration" not in (scopes or []) or not personas:
        return []
    allowed = set(personas)
    gaps = []
    for role in COORDINATOR_REQUIRED:
        want = CANONICAL[role]
        if want in allowed:
            continue
        legacy = sorted(a for a in allowed if a in LEGACY_ALIASES)
        gaps.append({
            "role": role, "persona": want,
            "message": (f"{want} ({ROLE_LABEL[role]}) is not in this client's allowed personas, "
                        f"so direct reviews get 403 model_not_allowed"
                        + (f"; {', '.join(legacy)} {'is' if len(legacy) == 1 else 'are'} allowed "
                           f"but legacy aliases don't authorize {want}" if legacy else "")
                        + ". Add it explicitly (edit this client) if this key runs the "
                          "coordinator.")})
    return gaps
