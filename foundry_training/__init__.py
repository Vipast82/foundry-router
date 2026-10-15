"""Training-data capture, curation and export for local coding models.

Project-agnostic and stdlib only. It turns what already happens during
development (coding sessions, model calls through Foundry, tool activity,
commits, verification, reviews and human corrections) into versioned,
provenance-carrying records, reconstructs bounded episodes, selects the
verified ones under a per-project policy, and exports model-neutral and
target-specific datasets with separate evaluation sets.

It never uploads data or starts training.

Layers:
  adapters/   capture: Kilo (export file, `kilo serve` API, read-only DB
              snapshot), Foundry inference capture, Cline sessions, the
              coordinator's store, git, artifacts
  store.py    raw / normalized / curated / evaluation / artifact storage,
              idempotent ingestion, gaps, lineage, deletion tracking
  redact.py   secret and personal-data redaction (also used by Foundry before
              it persists a capture)
  correlate.py, episodes.py, curate.py, export.py, validate.py
"""

__version__ = "0.1.0"
SCHEMA_VERSION = 1
