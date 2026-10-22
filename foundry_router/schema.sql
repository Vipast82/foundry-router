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

-- Per-model-call PERFORMANCE HISTORY -------------------------------------------
-- One row per completed model call, capturing every response metric we can pull
-- from llama.cpp/Ollama, so the dashboard can chart throughput / speculative
-- acceptance / KV-cache reuse over a multi-day run and correlate them against
-- context fill (prompt_tokens). This is the time-series behind the Performance
-- tab; the `models` table keeps only rolling averages, which can't show WHEN
-- something started degrading. Pruned to a retention window (see perf_history).
CREATE TABLE IF NOT EXISTS perf_samples (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT,                          -- ISO8601 UTC of completion
  model TEXT,
  backend TEXT,
  persona TEXT,
  mode TEXT,                        -- "agent" | "direct" | "passthrough" | ...
  prompt_tokens INTEGER,            -- context fill at this turn (KV occupancy proxy)
  completion_tokens INTEGER,
  cached_tokens INTEGER,            -- prompt tokens served from the KV prefix cache
  draft_n INTEGER,                  -- speculative-decoding tokens proposed
  draft_n_accepted INTEGER,         -- ...and verified/kept by the target
  decode_tps REAL,                  -- output tokens / decode time
  prefill_tps REAL,                 -- prompt tokens / prefill time
  decode_ms REAL,                   -- decode wall time
  prefill_ms REAL,                  -- prefill wall time (~TTFT warm)
  ttft_ms REAL,                     -- measured time-to-first-token (streaming only)
  eff_tps REAL,                     -- output tokens / end-to-end wall time
  wall_ms INTEGER,                  -- total request wall time
  cache_hit_pct REAL,               -- cached_tokens / prompt_tokens * 100
  spec_accept_pct REAL,             -- draft_n_accepted / draft_n * 100
  finish_reason TEXT,
  run_label TEXT,                   -- operator tag (e.g. "2x2080ti-22gb") for before/after comparison
  timing_src TEXT,                  -- "server" | "estimated" | "" — where the durations came from
  prefill_tokens INTEGER,           -- prompt tokens actually prefilled (excl. cache hits)
  reasoning_tokens INTEGER,         -- hidden thinking tokens inside completion_tokens
  load_ms REAL,                     -- cold model-load time this call (NULL when warm)
  headers_ms REAL,                  -- request sent -> response headers (streaming llama.cpp)
  start_ms REAL,                    -- request sent -> engine started processing the prompt
  tcp_retrans INTEGER,              -- TCP segments resent during this request (packet loss)
  tcp_rwnd_ms REAL,                 -- ms the upload stalled on the server's full receive window
  client_gap_ms REAL,               -- previous reply finished -> this request arrived (client/tool time)
  client_tool TEXT,                 -- tool(s) the client was running in that gap
  router_ms REAL,                   -- arrival at Foundry -> sent to the backend
  arrived_at TEXT,                  -- system clock (UTC): request arrived at Foundry
  sent_at TEXT,                     -- system clock: sent to the backend
  first_token_at TEXT,              -- system clock: first token back
  prev_reply_at TEXT,               -- system clock: previous reply in this conversation finished
  clock_diff_ms REAL                -- stopwatch vs system clock disagreement for this call
);

CREATE INDEX IF NOT EXISTS idx_perf_samples_ts ON perf_samples(ts);
CREATE INDEX IF NOT EXISTS idx_perf_samples_model_ts ON perf_samples(model, ts);

-- Paid-service token pricing, for the "what would this run have cost on a paid
-- API?" calculator. Editable in the UI (seeded with current public rates); the
-- point is a semi-close local-vs-cloud comparison, not billing-grade accuracy.
-- Rates are USD per 1,000,000 tokens.
CREATE TABLE IF NOT EXISTS service_pricing (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT UNIQUE,                 -- "Claude Opus 4.8", "GPT-5.6", ...
  input_per_1m REAL,                -- USD / 1M input (prompt) tokens
  output_per_1m REAL,               -- USD / 1M output (completion) tokens
  cached_input_per_1m REAL,         -- USD / 1M cached (prompt-cache hit) input tokens; NULL = no cache discount
  enabled INTEGER DEFAULT 1,        -- include in the comparison
  notes TEXT,
  updated_at TEXT,
  locked INTEGER DEFAULT 0,         -- 1 = never touched by the automatic price update
  source TEXT,                      -- "seed" | "manual" | "openrouter"
  source_id TEXT,                   -- matched OpenRouter model id (remembered for next update)
  last_checked TEXT                 -- last automatic price check
);

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
  error TEXT,
  arguments TEXT,                   -- JSON of the call's arguments. For a code-
                                    -- execution server this captures the actual
                                    -- submitted code — the best audit trail in
                                    -- the system, per the code-sandbox spec.
  executed_code INTEGER DEFAULT 0   -- 1 = this call ran on an executes_code server
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

-- Eval harness (quality spec Phase 4) ------------------------------------------
-- A fixed, EDITABLE set of representative prompts per category, shape-checked
-- (not exact-match) and optionally LLM-judged; scores accumulate per persona
-- over time so trend lines are visible, not just before/after snapshots.

CREATE TABLE IF NOT EXISTS eval_prompts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  category TEXT,                    -- "coding" | "chat" | "research" | "rag"
  prompt TEXT,
  checks TEXT,                      -- JSON list of shape checks, e.g.
                                    -- ["code_block","no_refusal","min_length:100",
                                    --  "cites_source","valid_json","mentions:1912"]
  enabled INTEGER DEFAULT 1,
  created_at TEXT
);

CREATE TABLE IF NOT EXISTS eval_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT,
  persona TEXT,
  judge_model TEXT,                 -- '' = shape checks only (judge skipped/denied)
  prompts_run INTEGER,
  shape_pass_rate REAL,             -- 0-1
  avg_judge_score REAL,             -- 0-10, NULL when no judge scored anything
  note TEXT,
  status TEXT                       -- "running" | "done" | "failed"
);

CREATE INDEX IF NOT EXISTS idx_eval_runs_persona ON eval_runs(persona, ts);

CREATE TABLE IF NOT EXISTS eval_results (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id INTEGER REFERENCES eval_runs(id),
  prompt_id INTEGER,
  prompt TEXT,                      -- copied so history survives prompt edits
  response_chars INTEGER,
  shape_pass INTEGER,
  shape_detail TEXT,                -- per-check outcomes, human-readable
  judge_score REAL,
  judge_notes TEXT,
  duration_ms INTEGER
);

CREATE INDEX IF NOT EXISTS idx_eval_results_run ON eval_results(run_id);

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

-- Every MCP tool call that passes through Foundry, whoever made it: the brain,
-- a worker's tool loop, direct-mode (client + persona tools), the Foundry-MCP
-- aggregator (external clients like AnythingLLM), the research agent, the
-- gateway admin. One row per call — the data behind the MCP Metrics tab.
CREATE TABLE IF NOT EXISTS mcp_call_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT,
  source TEXT,              -- brain | worker | direct | aggregator | research | gateway | other
  caller TEXT,              -- persona / aggregator scope / model that owned the loop
  client TEXT,              -- external client (User-Agent) when known
  server TEXT,
  tool TEXT,
  ok INTEGER,
  duration_ms INTEGER,      -- total, incl. pacing + retries
  connect_ms INTEGER,       -- session setup (0 when a pooled session was reused)
  pace_ms INTEGER,          -- time spent waiting on the server's pace_seconds
  attempts INTEGER,         -- 1 + 429 retries
  rate_limited INTEGER,
  timed_out INTEGER,
  session TEXT,             -- "reused" | "new" | "per-call"
  error TEXT,
  args_chars INTEGER,
  result_chars INTEGER,
  result_tokens INTEGER,    -- ~chars/4: what the result costs in model context
  content_types TEXT,       -- e.g. "text,image"
  request_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_mcp_call_log_ts ON mcp_call_log(ts);
CREATE INDEX IF NOT EXISTS idx_mcp_call_log_tool ON mcp_call_log(server, tool, ts);

-- External agent runs (Hermes Agent): one row per task, whether the agent was
-- used as a tool (<agent>_run) or as a persona's backend.
CREATE TABLE IF NOT EXISTS agent_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT,
  agent TEXT,
  mode TEXT,                -- tool | backend
  caller TEXT,              -- persona / aggregator scope / client
  run_id TEXT,              -- the agent's own run id, when it has one
  session_id TEXT,
  status TEXT,              -- running | completed | failed | cancelled | timeout | handed_off
  duration_ms INTEGER,
  ttft_ms INTEGER,
  tool_calls INTEGER,       -- tools the AGENT ran inside this task
  tools_used TEXT,          -- comma list of those tool names
  prompt_tokens INTEGER,
  completion_tokens INTEGER,
  cache_read_tokens INTEGER,
  model TEXT,               -- the model the agent itself ran on
  input_chars INTEGER,
  output_chars INTEGER,
  error TEXT
);
CREATE INDEX IF NOT EXISTS idx_agent_runs_ts ON agent_runs(ts);

-- Turn assist (rule-based supervisor / pre-judge / auditor / advisor for coding
-- clients). One row per router intervention — the record of what Foundry did
-- to a conversation, and the raw material for fine-tuning data (a trigger
-- excerpt paired with the intervention text).
CREATE TABLE IF NOT EXISTS interventions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT,
  conversation_key TEXT,
  persona TEXT,
  model TEXT,
  request_id TEXT,
  kind TEXT,                -- supervisor_loop | prejudge_block | prejudge_fix | dedupe | roblox |
                            -- audit_lint | audit_accept | audit_reject | advisor | auto_advice |
                            -- blocked | fallback_refused | frontier_denied
  mode TEXT,                -- observe | nudge | enforce | ...
  detail TEXT,              -- what was found / the rule hit
  intervention TEXT,        -- the text the model (or user) received
  excerpt TEXT,             -- what triggered it (tool result / report tail)
  frontier_model TEXT,
  prompt_tokens INTEGER,
  completion_tokens INTEGER,
  next_action TEXT,         -- the model's next reply after this intervention
  next_ts TEXT
);
CREATE INDEX IF NOT EXISTS idx_interventions_conv ON interventions(conversation_key, ts);
CREATE INDEX IF NOT EXISTS idx_interventions_ts ON interventions(ts);

-- One row per assisted request: this turn's new tool calls / errors and the
-- context size, so the scoreboard can show errors per 100 calls by context zone.
CREATE TABLE IF NOT EXISTS assist_turns (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT,
  conversation_key TEXT,
  persona TEXT,
  model TEXT,
  sprint TEXT,              -- sprint id parsed from the kickoff message, if any
  context_tokens INTEGER,   -- estimate of what the client sent
  new_calls INTEGER,
  new_errors INTEGER,
  total_calls INTEGER,
  think TEXT,               -- the thinking level this request actually ran with
  think_why TEXT,
  output_tokens INTEGER,
  prompt_tokens INTEGER,    -- full prompt of the last model call this request
  cached_tokens INTEGER,    -- of it, served from the server's KV cache
  prefill_ms REAL,          -- prompt-processing time summed over this request's calls
  cache_miss INTEGER,       -- 1 = a >20K-token prompt was mostly re-read (cached < 50%)
  cache_cause TEXT          -- conversation_start | guard_trim | think_change | compaction | unknown
);
CREATE INDEX IF NOT EXISTS idx_assist_turns_conv ON assist_turns(conversation_key, ts);
CREATE INDEX IF NOT EXISTS idx_assist_turns_ts ON assist_turns(ts);

-- Image generation/editing jobs (media.py). Input images are never stored;
-- outputs live under <data dir>/media/<id>/ and are pruned with the row.
CREATE TABLE IF NOT EXISTS media_jobs (
  id TEXT PRIMARY KEY,               -- img_<16 hex>
  created_at TEXT,
  created_ts REAL,                   -- epoch seconds (budget windows)
  started_ts REAL,
  finished_ts REAL,
  op TEXT,                           -- generate | edit
  status TEXT,                       -- queued | running | succeeded | failed | cancelled
  model TEXT,
  backend TEXT,
  persona TEXT,
  client_id TEXT,
  session_id TEXT,
  task_id TEXT,
  idem_key TEXT,                     -- k:<client key> or h:<request hash>
  params TEXT,                       -- JSON: prompt, size, quality, ... input hashes
  attempts INTEGER DEFAULT 0,
  charged INTEGER DEFAULT 0,         -- 1 = the request reached the backend (counts toward the budget)
  error TEXT,
  error_code TEXT,
  error_status INTEGER,
  artifacts TEXT,                    -- JSON list
  usage TEXT,                        -- JSON as the backend reported it
  duration_ms REAL
);
CREATE INDEX IF NOT EXISTS idx_media_jobs_ts ON media_jobs(created_ts);
CREATE INDEX IF NOT EXISTS idx_media_jobs_key ON media_jobs(client_id, idem_key);

-- Budget ledger (budget.py): one row per model call (reserved before the call,
-- settled after with actual tokens), plus denials. Shared by every client.
CREATE TABLE IF NOT EXISTS usage_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL,                           -- epoch seconds
  request_id TEXT,
  client_id TEXT,
  session_id TEXT,
  task_id TEXT,
  parent_task_id TEXT,
  persona TEXT,
  backend TEXT,
  model TEXT,
  account TEXT,                      -- backend:provider for subscription backends
  paid INTEGER DEFAULT 0,
  status TEXT,                       -- reserved | settled | error | released | denied | observed
  est_tokens INTEGER DEFAULT 0,
  input_tokens INTEGER,
  output_tokens INTEGER,
  rule TEXT,                         -- denials: the rule (or "paused")
  detail TEXT,
  settled_ts REAL
);
CREATE INDEX IF NOT EXISTS idx_usage_events_ts ON usage_events(ts);
CREATE INDEX IF NOT EXISTS idx_usage_events_session ON usage_events(session_id);
CREATE INDEX IF NOT EXISTS idx_usage_events_task ON usage_events(task_id);

-- Pause switches: scope = account | backend | persona | client.
CREATE TABLE IF NOT EXISTS budget_pauses (
  scope TEXT,
  value TEXT,
  paused_at TEXT,
  note TEXT,
  PRIMARY KEY (scope, value)
);

-- Config history (configops.py): every saved config.yaml state. The raw YAML
-- keeps ${VAR} references as written; it is shown and diffed redacted.
CREATE TABLE IF NOT EXISTS config_versions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT,
  source TEXT,                       -- save | startup | restore #N | import
  sections TEXT,                     -- JSON list of top-level sections changed
  yaml TEXT
);

-- Snapshots of state that isn't in config.yaml (personas, pauses, overrides),
-- taken before bulk changes (import, profile apply) so they can be restored.
CREATE TABLE IF NOT EXISTS state_snapshots (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT,
  reason TEXT,
  data TEXT                          -- JSON {personas, budget_pauses, tool_overrides, model_overrides}
);

-- Work bindings (orchestration.py): a coding session (e.g. a Kilo session id
-- sent as x-session-id) registered by a development coordinator with its
-- project, task, run and role.
CREATE TABLE IF NOT EXISTS work_sessions (
  session_id TEXT PRIMARY KEY,
  project_id TEXT,
  task_id TEXT,
  run_id TEXT,
  role TEXT,
  parent_session_id TEXT,
  persona TEXT,
  client_id TEXT,
  meta TEXT,
  created_ts REAL,
  updated_ts REAL,
  ended_ts REAL,
  outcome TEXT
);
CREATE INDEX IF NOT EXISTS idx_work_sessions_project ON work_sessions(project_id, task_id);

-- The coordinator's latest reported state per task (display only).
CREATE TABLE IF NOT EXISTS work_tasks (
  project_id TEXT,
  task_id TEXT,
  state TEXT,
  data TEXT,
  updated_ts REAL,
  PRIMARY KEY (project_id, task_id)
);

-- Coordinator lifecycle events (display only); event_id dedupes resends.
CREATE TABLE IF NOT EXISTS work_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  event_id TEXT UNIQUE,
  ts REAL,
  project_id TEXT,
  task_id TEXT,
  session_id TEXT,
  kind TEXT,
  state TEXT,
  detail TEXT
);
CREATE INDEX IF NOT EXISTS idx_work_events_ts ON work_events(ts);

-- Training-data pipeline status as reported by foundry_training (display
-- only; redacted summaries, never raw payloads).
CREATE TABLE IF NOT EXISTS training_status (
  pipeline_id TEXT PRIMARY KEY,
  data TEXT,
  updated_ts REAL
);
CREATE TABLE IF NOT EXISTS training_episodes (
  episode_id TEXT PRIMARY KEY,
  pipeline_id TEXT,
  project_id TEXT,
  task_id TEXT,
  kind TEXT,
  status TEXT,               -- selected | excluded | eval | candidate | deleted
  data TEXT,                 -- JSON summary (redacted, truncated previews)
  updated_ts REAL
);
CREATE INDEX IF NOT EXISTS idx_training_episodes_project ON training_episodes(project_id, status);
-- Fine-tuning datasets uploaded by `foundry_training auto` after a sprint
-- (validated, redaction re-checked on arrival). Files live under
-- <data>/training/datasets/<project>/<dataset_id>/{data.jsonl, manifest.json}.
CREATE TABLE IF NOT EXISTS training_datasets (
  dataset_id TEXT PRIMARY KEY,
  project_id TEXT,
  kind TEXT,
  target TEXT,
  examples INTEGER,
  sha256 TEXT,
  bytes INTEGER,
  path TEXT,
  manifest TEXT,
  client_id TEXT,
  uploaded_ts REAL
);

-- Coordinator outcome labels for captured calls (training_quality.py): a verified
-- gate PASS / review APPROVED / vision approval, attached to the request ids it
-- covers. Joined to capture rows by request_id when they are read.
CREATE TABLE IF NOT EXISTS training_outcomes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL,
  request_id TEXT,
  label TEXT,                        -- gate_pass | review_approved | vision_approved | *_fail / *_changes
  source TEXT,                       -- who posted it (client id)
  project_id TEXT,
  task_id TEXT,
  evidence TEXT,                     -- JSON, small
  UNIQUE(request_id, label)
);
CREATE INDEX IF NOT EXISTS idx_training_outcomes_req ON training_outcomes(request_id);

-- Engine /metrics history (server_history.py): one row per backend, model and
-- metric every sample interval (perf.server_sample_seconds). Retention:
-- perf.retention_days.
CREATE TABLE IF NOT EXISTS server_samples (
  ts REAL,                           -- epoch seconds
  backend TEXT,
  model TEXT,                        -- loaded model when the engine says (else '')
  metric TEXT,                       -- e.g. kv_cache_usage_pct, avg_decode_tps, slots_busy
  value REAL
);
CREATE INDEX IF NOT EXISTS idx_server_samples ON server_samples(backend, metric, ts);
CREATE INDEX IF NOT EXISTS idx_server_samples_ts ON server_samples(ts);
