-- Foundry Router SQLite schema.
-- Applied idempotently on every startup (CREATE TABLE IF NOT EXISTS), so this
-- file doubles as lightweight migrations for a v1: additive changes go here.
-- Starter personas are seeded from Python (db.py) so seeding logic can be
-- INSERT-OR-IGNORE and never clobber user edits.

-- §4.4 Model Registry ---------------------------------------------------------

CREATE TABLE IF NOT EXISTS models (
  id TEXT PRIMARY KEY,              -- e.g. "anthropic/claude-fable-5" or "qwen3.6:27b"
  display_name TEXT,
  provider TEXT,                    -- backend name that serves it, e.g. "meridian" | "openrouter" | "truenas-ollama"
  context_length INTEGER,
  cost_per_1k_input REAL,           -- NULL for subscription-based (Meridian) models
  cost_per_1k_output REAL,
  relative_cost_tier TEXT,          -- "low" | "medium" | "high" | "very_high"
  reasoning_style TEXT,             -- free-text summary from the Research Agent
  good_for TEXT,                    -- free-text, from Research Agent
  benefits_from_explicit_prompting INTEGER DEFAULT 0,  -- 1 => refine_prompt worth invoking for this target
  tags TEXT,                        -- JSON list of capability tags: coding, vision, tool-calling,
                                    -- reasoning, creative-writing, long-context. Seeded from name
                                    -- heuristics at discovery, refined by the Research Agent.
  content_policy TEXT,              -- "permissive" (uncensored/abliterated local models, detected
                                    -- from naming, confirmable via manual override) | "standard" | NULL
  research_status TEXT,             -- "queued" | "running" | "ok" | "failed" — surfaced in the UI so
                                    -- research never fails silently
  research_note TEXT,               -- human-readable outcome/error for the status above
  enabled INTEGER DEFAULT 1,        -- governance switch, independent of reachability: 0 excludes the
                                    -- model from ranking AND tool generation entirely
  tool_calls_ok INTEGER DEFAULT 0,  -- empirical tool-calling reliability counters, updated from live
  tool_calls_failed INTEGER DEFAULT 0,  -- traffic (a model can claim tool support and still misbehave)
  last_updated TEXT,
  source TEXT                       -- "openrouter_api" | "research_agent" | "discovery" | "manual_override"
);

CREATE TABLE IF NOT EXISTS model_benchmarks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  model_id TEXT REFERENCES models(id),
  category TEXT,                    -- "coding" | "reasoning" | "general_chat" | "tool_calling" | "agentic"
  score REAL,                       -- normalized 0-100
  score_type TEXT,                  -- "measured" | "estimated"
  source_type TEXT,                 -- "vendor" | "independent" | "community_report" | "manual_override"
  source_url TEXT,
  confidence REAL,                  -- 0-1
  last_updated TEXT
);

CREATE INDEX IF NOT EXISTS idx_benchmarks_model ON model_benchmarks(model_id, category);

-- §4.8 Personas ---------------------------------------------------------------

CREATE TABLE IF NOT EXISTS personas (
  virtual_name TEXT PRIMARY KEY,    -- advertised via /api/tags
  description TEXT,
  benchmark_category TEXT,          -- which model_benchmarks category to weight
  local_bias_strength TEXT,         -- "strong" | "moderate" | "cost_aware_default"
  escalation_triggers TEXT,         -- JSON list of trigger conditions (free-text, fed to the brain prompt)
  preferred_mcp_tools TEXT,         -- JSON list of MCP server/tool names to surface for this persona
  guardrail_overrides TEXT,         -- JSON object overriding global guardrail fields
  pinned_models TEXT,               -- JSON ordered list: models boosted (not hard-required)
                                    -- to the top of this persona's candidate list; guardrail/
                                    -- quota denial falls through to the next pin, then the pool
  execution_mode TEXT,              -- NULL/"agent" = generic brain loop | "pipeline" = the
                                    -- Prepare->Execute->Check coding pipeline (§ coding spec)
  pipeline_check_enabled INTEGER DEFAULT 1,  -- pipeline mode: paid review of local output
  outcome_judge TEXT,               -- NULL = off | "paid" | "local_large" | "brain" — after a
                                    -- local answer, this judge decides adequate/escalate
  required_tags TEXT,               -- JSON list: when any candidate carries one of these tags,
                                    -- the candidate list is FILTERED to tag matches (Foundry-Vision)
  prefer_permissive INTEGER DEFAULT 0,  -- boost content_policy=permissive models to the front
  enabled INTEGER DEFAULT 1,
  created_at TEXT,
  updated_at TEXT
);

-- §4.2 Tool Sync manual overrides ---------------------------------------------
-- The one manual action the Web UI supports on tools: disabling an
-- auto-discovered tool. Persisted here so it survives future sync cycles.

CREATE TABLE IF NOT EXISTS tool_overrides (
  tool_name TEXT PRIMARY KEY,
  disabled INTEGER DEFAULT 1,
  updated_at TEXT
);

-- §4.9 Usage log (item 6) -------------------------------------------------------

CREATE TABLE IF NOT EXISTS request_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT,                          -- ISO8601 UTC
  persona TEXT,
  client_model TEXT,                -- raw model name the client asked for
  mode TEXT,                        -- "agent" | "direct" | "passthrough" | "fallback"
  summary TEXT,                     -- first ~200 chars of the user message
  models_used TEXT,                 -- JSON list of {model, backend, prompt_tokens, completion_tokens, est_cost_usd}
  tool_calls TEXT,                  -- JSON list of {tool, server, duration_ms, ok, error?} — every MCP
                                    -- invocation the request made (visibility: which requests actually
                                    -- used searxng/crawl4ai, and what failed where)
  steps INTEGER,
  duration_ms INTEGER,
  guardrail_events TEXT,            -- JSON list of guardrail firings
  est_cost_usd REAL DEFAULT 0,      -- summed metered cost for spend-cap accounting
  status TEXT,                      -- "ok" | "error" | "asked_user"
  error TEXT
);

CREATE INDEX IF NOT EXISTS idx_request_log_ts ON request_log(ts);

-- §4.9 Troubleshooting/error log (item 7) ---------------------------------------

CREATE TABLE IF NOT EXISTS event_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT,
  level TEXT,                       -- "info" | "warning" | "error"
  source TEXT,                      -- "backend_pool" | "brain" | "tool_sync" | "research" | "guardrails" | ...
  message TEXT,
  detail TEXT
);

CREATE INDEX IF NOT EXISTS idx_event_log_ts ON event_log(ts);

-- §4.7 Subscription-window consumption (Claude via Meridian) -------------------
-- Dollars are the wrong unit for subscription models: tokens against the
-- 5-hour/weekly window are what deplete. Our own historical record, not just
-- Meridian's live snapshot at any moment.

CREATE TABLE IF NOT EXISTS claude_usage_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT,
  model TEXT,                       -- claude tier actually called
  backend TEXT,
  prompt_tokens INTEGER,
  completion_tokens INTEGER
);

CREATE INDEX IF NOT EXISTS idx_claude_usage_ts ON claude_usage_log(ts);

-- Misc durable key/value state (last poll timestamps, etc.) --------------------

CREATE TABLE IF NOT EXISTS kv (
  key TEXT PRIMARY KEY,
  value TEXT
);
