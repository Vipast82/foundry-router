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
  eval_tps_avg REAL,                -- observed WARM tokens/sec (running mean), from Ollama eval_duration
  eval_samples INTEGER DEFAULT 0,   -- calls behind eval_tps_avg (confidence scales with this)
  cold_load_ms_avg REAL,            -- informational only: typical cold-load time (Ollama load_duration);
  cold_load_samples INTEGER DEFAULT 0,  -- NEVER mixed into a quality/speed score — pool-contention noise
  adequacy_ok INTEGER DEFAULT 0,    -- outcome-judge verdicts on this model's answers (observed quality):
  adequacy_failed INTEGER DEFAULT 0,--   ok/failed roll into an `adequacy` benchmark, confidence-scaled
  calls_ok INTEGER DEFAULT 0,       -- call-level reliability: usable output vs failure/timeout/empty;
  calls_failed INTEGER DEFAULT 0,   --   used as a within-tier score MULTIPLIER (penalty), not a booster
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

-- Named, real, independently-verifiable benchmark results (SWE-Bench Verified,
-- MMLU-Pro, BFCL, Chatbot Arena, ...), kept SEPARATE from model_benchmarks on
-- purpose: these have heterogeneous, real scales (0-100 pass rate vs ELO vs raw
-- problem count) that must NOT be silently coerced into the router's internal
-- 0-100 composite. They are not a ranking category (most models have most of
-- them missing — sparse); they serve as an explicit within-tier TIEBREAKER when
-- composite scores are near-equal (see ranked_for_category). scale='percent'
-- rows participate in tiebreaks; elo/count/score are stored but flagged out.
CREATE TABLE IF NOT EXISTS model_named_benchmarks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  model_id TEXT REFERENCES models(id),
  benchmark_name TEXT,              -- canonical, e.g. "SWE-Bench Verified"
  category TEXT,                    -- router category this benchmark speaks to (coding/reasoning/...)
  score REAL,                       -- as reported, on the benchmark's own scale
  scale TEXT,                       -- "percent" | "elo" | "count" | "score"
  source_url TEXT,
  measured_date TEXT,               -- when the eval was run, if the source says (else NULL)
  source TEXT DEFAULT 'research',   -- 'research' | 'manual' — manual entries are never clobbered by a research pass
  last_updated TEXT
);

CREATE INDEX IF NOT EXISTS idx_named_bench_model ON model_named_benchmarks(model_id, category);

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
  brain_handles_tools INTEGER DEFAULT 0,  -- 0 (default) = the selected WORKER owns the MCP tool
                                    -- loop for this persona's requests; 1 = old brain-mediated tool
                                    -- handling (the brain runs searxng/crawl4ai etc. itself)
  pipeline_check_enabled INTEGER DEFAULT 1,  -- pipeline mode: paid review of local output
  outcome_judge TEXT,               -- NULL = off | "paid" | "local_large" | "brain" — after a
                                    -- local answer, this judge decides adequate/escalate
  required_tags TEXT,               -- JSON list: when any candidate carries one of these tags,
                                    -- the candidate list is FILTERED to tag matches (Foundry-Vision)
  prefer_permissive INTEGER DEFAULT 0,  -- content_policy=permissive: prefer (this persona) vs the
                                    -- default avoid (permissive/uncensored models are for content other
                                    -- models refuse, not a general-quality choice) — see ranked_for_category
  context_window INTEGER,           -- admin-set override: tells clients (AnythingLLM, etc.) the
                                    -- max token budget to assume for this virtual persona;
                                    -- overrides the auto-detected MAX of routable workers
  selection_weights TEXT,           -- optional JSON overriding the multi-signal ranking weights for this
                                    -- persona, e.g. {"latency": 0.4} for a speed-sensitive workload
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

-- Output-quality tracking (feedback/quality spec, Phase 1) ---------------------
-- Everything downstream (review pass, eval harness, insight digest) writes into
-- or reads from these; they go first so the data accumulates from day one.

CREATE TABLE IF NOT EXISTS response_feedback (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT,
  request_log_id INTEGER,           -- best-effort link to request_log (nullable:
                                    -- API feedback may not identify a request)
  persona TEXT,
  model TEXT,                       -- model that produced the answer, if known
  rating INTEGER,                   -- +1 (thumbs up) / -1 (thumbs down)
  comment TEXT,
  source TEXT                       -- 'gui' (router UI) | 'api' (POST /v1/feedback)
);

CREATE INDEX IF NOT EXISTS idx_feedback_ts ON response_feedback(ts);

-- Durable per-event MCP tool-call record. request_log.tool_calls (JSON per
-- request) answers "what did THIS request do"; this table answers the
-- aggregate questions — per-caller-model/per-tool reliability over time —
-- that were previously only visible via fallback logging.
CREATE TABLE IF NOT EXISTS tool_call_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT,
  persona TEXT,
  caller TEXT,                      -- 'brain' or the worker model id that issued the call
  server TEXT,
  tool TEXT,
  ok INTEGER,
  duration_ms INTEGER,
  error TEXT
);

CREATE INDEX IF NOT EXISTS idx_tool_call_log_ts ON tool_call_log(ts);
CREATE INDEX IF NOT EXISTS idx_tool_call_log_caller ON tool_call_log(caller, tool);

-- Review-pass outcomes (schema ready ahead of Phase 2's tiered review).
CREATE TABLE IF NOT EXISTS review_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT,
  persona TEXT,
  trigger_reason TEXT,              -- 'review_enabled' | 'prefilter_flagged' | ...
  review_model TEXT,
  corrected INTEGER,                -- 1 = the delivered answer was changed by review
  verdict TEXT,                     -- short judge verdict / reviewer notes
  duration_ms INTEGER
);

CREATE INDEX IF NOT EXISTS idx_review_log_ts ON review_log(ts);

-- Semantic response cache (quality spec Phase 3) -------------------------------
-- Embeddings are little-endian float32 BLOBs (the format sqlite-vec's
-- vec_distance_cosine consumes directly when the extension loads; the Python
-- fallback reads the same bytes). Vectors are L2-normalized at store time.

CREATE TABLE IF NOT EXISTS semantic_cache (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT,
  persona TEXT,
  category TEXT,                    -- persona benchmark_category at store time (TTL policy key)
  prompt TEXT,                      -- the user message that produced the answer
  answer TEXT,
  embedding BLOB,
  dim INTEGER,
  ttl_seconds INTEGER,
  hits INTEGER DEFAULT 0,
  last_hit TEXT
);

CREATE INDEX IF NOT EXISTS idx_semcache_persona ON semantic_cache(persona, category);

-- Misc durable key/value state (last poll timestamps, etc.) --------------------

CREATE TABLE IF NOT EXISTS kv (
  key TEXT PRIMARY KEY,
  value TEXT
);
