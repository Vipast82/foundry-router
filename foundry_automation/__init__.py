"""Shared automation settings: one schema for the router's config store and GUI
and for the coordinator's profile loader.

Pure standard library on purpose: the router image and the coordinator
checkout both import it (like foundry_training).

  schema      every profile setting (type, default, limits, group, who reads
              it, when a change applies) and the typed table validators
  role_rules  per-role permission matrix, path scopes and shell rules, and the
              generators for Kilo session rules and kilo.jsonc agent blocks
  document    the versioned document: import from a profile TOML, overlay onto
              a local profile, validation, diff and hashing
  jsonc       a JSONC reader that keeps comments when one value is replaced
"""

FORMAT = "foundry.automation/1"
SCHEMA_VERSION = 1
ROUTER_DOC = "_router"          # the router-wide document (per-backend cutoffs)
