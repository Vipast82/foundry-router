"""Configuration loading and persistence.

Config lives in one YAML file on a mounted volume (default /config/config.yaml,
overridable via FOUNDRY_CONFIG). The web UI edits config through save_config(),
which re-serializes the whole document — comments in a hand-edited file are
lost on the first UI-driven save. That tradeoff (full round-trip vs. carrying a
comment-preserving YAML dependency) was accepted to keep dependencies minimal
(design doc §2); config.example.yaml remains the commented reference.

``${VAR}`` strings are expanded from the environment at *load* time, but the
raw (unexpanded) document is kept for saving, so secrets never get baked into
the file by a UI round-trip.
"""

from __future__ import annotations

import copy
import logging
import os
import re
import shutil
from pathlib import Path
from typing import Any, Literal, Optional

import yaml
from pydantic import BaseModel, Field

log = logging.getLogger(__name__)

_ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _expand_env(value: Any) -> Any:
    if isinstance(value, str):
        return _ENV_RE.sub(lambda m: os.environ.get(m.group(1), ""), value)
    if isinstance(value, list):
        return [_expand_env(v) for v in value]
    if isinstance(value, dict):
        return {k: _expand_env(v) for k, v in value.items()}
    return value


# --------------------------------------------------------------------------- #
# Pydantic models mirroring config.example.yaml                               #
# --------------------------------------------------------------------------- #

class ServerConfig(BaseModel):
    host: str = "0.0.0.0"
    port: int = 11435
    # Browser-based clients (e.g. PhishGuard) enforce CORS; without these headers
    # a cross-origin fetch fails with a bare "NetworkError". Default "*" suits the
    # internal, no-auth posture — restrict to specific origins if the endpoint is
    # ever exposed. Empty list disables CORS entirely.
    cors_origins: list[str] = Field(default_factory=lambda: ["*"])


class AgentBrainConfig(BaseModel):
    # How persona requests are routed:
    #   auto        — use the brain while it's configured and healthy; the
    #                 moment it's down / unconfigured, skip it instantly and
    #                 serve requests in passthrough (no waiting on timeouts).
    #   brain       — always route through the brain; a failed call degrades
    #                 per request to the static fallback (the original design).
    #   passthrough — never call the brain: each persona picks a worker by its
    #                 static policy (pins / allowlist / ranking / guardrails)
    #                 and the request — client tools, thinking, sampling — is
    #                 forwarded directly. Foundry then acts as a smart proxy +
    #                 MCP / backend manager with failover.
    routing_mode: Literal["auto", "brain", "passthrough"] = "auto"
    provider: Literal["ollama", "meridian", "openrouter"] = "ollama"
    endpoint: str = "http://localhost:11434"
    model: str = ""
    api_key: Optional[str] = None
    # Meridian routing profile for the brain when provider=meridian (sent as
    # x-meridian-profile). None = Meridian's own active/sticky routing.
    meridian_profile: Optional[str] = None
    keep_alive: int | str = -1
    max_tokens: int = 4096
    options: dict = Field(default_factory=dict)

    # Context-budget knobs — per-deployment tuning, editable live in the web
    # UI (Backends tab). Found the hard way: a 6GB local brain at num_ctx 6144
    # has only ~1500 tokens of headroom after the system prompt + tool
    # schemas; feeding a 22k-char tool result back into it silently truncates
    # the conversation (dropping the user's message) instead of erroring.
    # Defaults are sized for a small local brain; raise them if the brain is
    # Claude-class. The FULL untruncated tool result always reaches the user
    # via return_to_user(use_last_result=true) — these only cap what the
    # brain itself sees when deciding what to do next.
    tool_result_limit_chars: int = 2000   # per tool result fed back to the brain
    mcp_result_limit_chars: int = 2000    # same, for MCP tool results
    # Output budget for worker-model calls. Reasoning counts toward it, so a
    # thinking model writing a large file edit needs room (8192 cut Cline's
    # tool calls short). Per-persona override: max_output_tokens.
    worker_max_tokens: int = 32768
    # How much of an MCP tool result a WORKER model sees (its own tool loop and
    # direct-mode persona tools). Separate from mcp_result_limit_chars, which
    # protects the small brain's context: a worker with a large context should
    # get the whole page / search result, not a 2k-char preview.
    worker_tool_result_chars: int = 24000
    # keep_alive passed to Ollama workers so a heavy model (e.g. a 27B at 131K
    # context ≈ 22GB) stays RESIDENT between requests instead of cold-loading
    # ~22GB every call — the main cause of multi-minute first-token latency (and
    # proxy 504s) for coding clients like Cline. None = backend default (~5min);
    # "30m"/"24h"/"-1" keep it warm. Applies to direct-dispatch worker calls.
    worker_keep_alive: Optional[str] = "30m"
    # Direct-dispatch (Cline/coding clients) streams the worker's output live when
    # ON: each token is real proof the backend is generating (keeps the connection
    # alive, resets the read timeout per chunk — no total-time wall) and the
    # client shows it typing in real time. OFF (default) = one blocking call, the
    # full answer at the end. Streams for Ollama, openai-dialect (llama.cpp /
    # vLLM / Unsloth / OpenRouter) and Claude-via-Meridian backends.
    direct_stream: bool = True
    # Direct-stream keep-alive: while streaming to a coding client (Cline), if the
    # backend sends no chunk for this many seconds (a silent prompt-eval or a
    # model that buffers its reasoning), Foundry injects a small "still working…
    # Ns" thinking chunk. Keeps the client showing progress instead of a frozen
    # "Thinking…", and the bytes reset idle timers along the path (client read
    # timeout, proxy) so long generations don't get killed. 0 = off. direct_stream
    # only.
    direct_stream_heartbeat_seconds: int = 10
    # Visible status cadence for ALL keep-alives: every heartbeat sends
    # invisible keep-alive bytes, but a "⏳ … still working · 2m 05s" line is
    # shown only at ~30s, ~60s, then every N seconds (each visible line is a new
    # line in the client's thinking panel). 0 = never show, bytes only.
    heartbeat_visible_seconds: int = 60
    # Context guard (direct / raw-model paths): before dispatch, estimate the
    # prompt (messages + tool definitions) and, if it would overflow the model's
    # window minus the output reserve, trim the copy sent to the model — oldest
    # tool results cut first, then the oldest turns after the task. The client
    # keeps its full history; the turn says what was cut. "off" = send as-is.
    context_guard: Literal["trim", "off"] = "trim"
    # Sticky guard: remember per conversation how many steps are already
    # collapsed and only ever collapse more (never un-trim, never move the cut
    # back). The stateless guard flipped between trimmed and untrimmed prompts
    # turn to turn near the limit and every flip cost a full re-prefill.
    context_guard_sticky: bool = True
    # Output tokens kept free for the answer when the guard decides whether to
    # trim. 0 = auto: the request's max tokens capped at 8192 (and 1/4 of the
    # window), so the guard only trims a conversation that truly won't fit and
    # the client's own compaction (Cline: 90% of the window) runs first.
    context_guard_reserve_tokens: int = 0
    # Stall watchdog (direct_stream): if a backend sends NO output (no token,
    # thinking or tool call) for this long, the call is abandoned (the upstream
    # request is closed so it stops using the GPU / Claude session) and — if
    # nothing reached the client yet — the next model the persona allows takes
    # over. Keep-alive pings from the backend don't count as output. Set it
    # above your slowest legitimate prefill (a 27B model reading a 150k-token
    # Cline context can take several minutes). 0 = off.
    direct_stream_stall_seconds: int = 900
    # AGENT mode: stream the worker's REASONING (its native thinking tokens) live
    # as narration while it generates, WITHOUT streaming the answer — the brain
    # still gets the full content and reviews it (refusal/permissive fallback,
    # outcome-judge, escalation) before forwarding. So you watch the model reason
    # in real time, but the final answer is still gated. Ollama workers only.
    stream_worker_reasoning: bool = False
    # Reasoning effort for local (Ollama) workers that support thinking: sent as
    # the /api/chat `think` field. A level string ("low"/"medium"/"high"/"xhigh"
    # — model-dependent, e.g. Qwen3.8 accepts low/medium/xhigh) requests that
    # effort; "off" disables thinking (fastest); blank = the model's default.
    # Applied ONLY to models whose /api/show reports a `thinking` capability, so
    # it never breaks non-reasoning locals. One backend-wide knob for the fleet.
    reasoning_effort: Optional[str] = None
    # Global default sampling/generation options merged into every worker call
    # (Ollama `options`: temperature, top_p, top_k, min_p, repeat_penalty, seed,
    # stop, …). A persona's sampling_options override these, and a client's
    # explicit options override both. Empty = send nothing (each model's own
    # defaults). "Best default, then tweak per persona."
    sampling_defaults: dict = Field(default_factory=dict)
    # Worker-side tool calling (opt-out default): when the selected worker owns
    # the MCP tool loop itself, this bounds its tool-call turns before the
    # request is handed to the brain to finish — mirrors the brain's own step
    # cap, prevents a worker looping indefinitely on tool calls.
    worker_tool_max_steps: int = 6
    # Input-side twin of tool_result_limit_chars: caps what the brain sees of
    # a large pasted user message (file/code) before its FIRST routing
    # decision — otherwise a 23k-char paste blows the brain's context before
    # it ever gets to route. The worker still receives the complete original
    # via the ask_* tools' include_full_user_message flag.
    user_input_preview_chars: int = 2000
    # Streaming keep-alive: while a worker/brain/MCP call is in flight, emit a
    # "still waiting" narration line every this-many seconds so reverse
    # proxies and clients never mistake a working request for a dead one
    # (found live: a failover-then-cold-load chain took 422s and produced a
    # real answer, but the client's connection had already been closed —
    # raising the proxy timeout can't keep up with worst-case chains; flowing
    # bytes can). 0 disables.
    heartbeat_seconds: float = 10.0


class BackendConfig(BaseModel):
    name: str
    # `type` is required (§4.3): three different wire protocols, and e.g.
    # Meridian would silently fail if spoken to in Ollama protocol.
    type: Literal["ollama", "anthropic-compatible", "openai-compatible"]
    url: str
    api_key: Optional[str] = None
    priority: int = 100
    # False = parked: kept in the config (and listed in the UI) but never
    # probed, routed to, or alerted on — for switching servers off while
    # testing without deleting their settings.
    enabled: bool = True
    # `flavor` names the SERVER SOFTWARE behind the wire protocol so the Host
    # Admin UI can offer the right management surface. `type` is only the wire
    # dialect (ollama / openai / anthropic); several very different servers speak
    # the openai dialect (llama.cpp, Unsloth, vLLM, plain OpenAI) but expose
    # different admin endpoints. Ollama has full model CRUD (pull/copy/rename/
    # delete/create); the openai-dialect servers expose only READ-ONLY
    # diagnostics (list models / props / health / slots). None = infer from
    # `type` (ollama -> "ollama", else -> "openai").
    flavor: Optional[Literal["ollama", "llamacpp", "unsloth",
                             "vllm", "openai", "cliproxyapi"]] = None
    # Per-backend concurrency: at most this many calls run on the backend at
    # once (e.g. llama.cpp's -np slots); more wait in a queue that takes turns
    # between clients, so one client's burst can't starve another. None = no
    # limit (the backend queues internally, invisibly).
    max_concurrent: Optional[int] = None
    queue_timeout_seconds: float = 300.0
    # Helper streams this backend serves next to your main one, counted as
    # distinct client sessions active on it (a session stays active until 60s
    # after its last call): 0 = the main stream only, 1 = main + one sub-agent,
    # ... None = no limit. A session over the limit waits up to
    # queue_timeout_seconds for a slot, then is refused (budget.py).
    helpers_allowed: Optional[int] = Field(default=None, ge=0)
    # Optional discovery fallback ONLY — used if the backend exposes no
    # model-list endpoint (§4.3: "do not hardcode model lists for backends
    # that can be discovered").
    models: list[str] = Field(default_factory=list)
    # Meridian routing profile (anthropic-compatible backends only). Meridian
    # picks the Claude account/profile per request from the `x-meridian-profile`
    # header; set this to pin every call from THIS backend to a named profile
    # (e.g. run two Foundry backends, one per Claude account). None = let
    # Meridian use its active/sticky routing.
    meridian_profile: Optional[str] = None
    # Meridian adapter to use (sent as x-meridian-agent), e.g. "opencode" or
    # "passthrough". None = Meridian's own default for an unrecognised client
    # (its MERIDIAN_DEFAULT_AGENT, else the OpenCode adapter — which forwards
    # thinking blocks and returns tool_use to the caller, what Foundry needs).
    meridian_agent: Optional[str] = None
    # Send a stable x-session-affinity derived from the conversation when the
    # client didn't send its own session header, so Meridian resumes the same
    # Claude session turn to turn (warm prompt cache, model sees its own prior
    # turns). Off by default: two concurrent conversations that open with the
    # identical system prompt + first message would share a session.
    meridian_session_affinity: bool = False
    # Subscription backend that is NOT Meridian (e.g. CLIProxyAPI in front of a
    # ChatGPT Plus login): treated as paid for the guardrails — never as metered
    # dollars, never as free local. Meridian (anthropic-compatible) is always a
    # subscription. A $20 plan reports no usage to Foundry, so the call budgets
    # below are the guard: calls this backend served in the last 5 hours / 24
    # hours, counted from Foundry's own subscription usage log. None = no cap.
    subscription: bool = False
    call_budget_5h: Optional[int] = None
    call_budget_daily: Optional[int] = None
    # CLIProxyAPI usage readings (flavor "cliproxyapi"): its management key
    # lets Foundry read the usage windows each signed-in account reported
    # (ChatGPT 5-hour / weekly %, Claude Code 5h / 7d utilization) from
    # GET /v0/management/auth-files, and stop using the backend's models once
    # a window reaches the limit. Needs CLIProxyAPI's management.secret-key
    # (and management.allow-remote: true when Foundry runs on another host).
    # management_url defaults to the backend url without /v1.
    management_key: Optional[str] = None
    management_url: Optional[str] = None
    # Per-backend override of subscription_usage.limit_pct.
    usage_limit_pct: Optional[float] = None

    @property
    def is_subscription(self) -> bool:
        # flavor "cliproxyapi" (CLIProxyAPI: ChatGPT/Codex, Claude Code, Gemini
        # sign-ins behind the openai dialect) is a subscription by definition.
        return (self.subscription or self.type == "anthropic-compatible"
                or self.flavor == "cliproxyapi")

    @property
    def effective_flavor(self) -> str:
        """The admin flavor to drive the Host Admin panel: the explicit `flavor`
        if set, else inferred from the wire `type` (ollama -> ollama, anything
        else -> a generic openai diagnostics surface)."""
        if self.flavor:
            return self.flavor
        return "ollama" if self.type == "ollama" else "openai"


class InternalPoolConfig(BaseModel):
    backends: list[BackendConfig] = Field(default_factory=list)


class OllaConfig(BaseModel):
    url: str = "http://localhost:40114"


class LiteLLMConfig(BaseModel):
    url: str = "http://localhost:4000"
    api_key: Optional[str] = None


class BackendPoolConfig(BaseModel):
    mode: Literal["internal", "olla", "litellm"] = "internal"
    health_check_interval_seconds: int = 15
    failure_threshold: int = 3
    cooldown_seconds: int = 60
    # httpx READ timeout for a worker call (connect stays short at 5s for fast
    # failover). A big single-shot local generation — a coding client asking a
    # 27B to write a whole file — legitimately runs past 300s, tripping a
    # ReadTimeout, so the default is generous. Set lower only if your models are
    # small/fast. (The ultimate fix for unbounded generations is streaming, where
    # the timeout resets per chunk.)
    request_timeout_seconds: int = 1200
    internal: InternalPoolConfig = Field(default_factory=InternalPoolConfig)
    olla: OllaConfig = Field(default_factory=OllaConfig)
    litellm: LiteLLMConfig = Field(default_factory=LiteLLMConfig)


class GuardrailsConfig(BaseModel):
    authority: Literal["internal", "defer_to_pool"] = "internal"
    max_steps_per_request: int = 12
    max_paid_calls_per_request: int = 3
    daily_spend_cap_usd: Optional[float] = None
    weekly_spend_cap_usd: Optional[float] = None


class SubscriptionUsageConfig(BaseModel):
    # Subscription proxies that report usage windows (CLIProxyAPI): a model is
    # not used once any window covering it is at or above this percentage, so
    # persona cascades move on to the next model before the plan runs out.
    limit_pct: float = 85.0
    # Background refresh for the dashboard; requests also read it on demand.
    poll_seconds: int = 60
    # How long one read of the management API is reused.
    cache_seconds: float = 20.0


class MeridianConfig(BaseModel):
    # Real per-profile usage-window data (buckets of five_hour/weekly
    # utilization). The old default "/telemetry" pointed at the HTML dashboard
    # and could never work — deployed configs that still carry telemetry_path
    # are simply ignored (pydantic drops unknown keys) and get this default.
    quota_path: str = "/v1/usage/quota"
    # Profile name to pull REAL usage for via the auth companion's /usage endpoint
    # (Anthropic's /api/oauth/usage, which serves Pro accounts — Meridian's own
    # /v1/usage/quota skips non-Max). When set AND the auth companion is
    # configured, this becomes the usage source that drives conservation; blank =
    # use Meridian's quota endpoint directly. See docs/MERIDIAN_AUTH.md.
    usage_profile: Optional[str] = None
    min_window_fraction: float = 0.05
    # Active oauth-staleness watch: poll the quota endpoint on this interval
    # so `sources.oauth` going null is caught within minutes (Events alert +
    # UI banner) instead of whenever someone happens to compare numbers with
    # the Claude app. The endpoint is read-only and free to poll. 0 disables.
    quota_poll_seconds: int = 180
    # Adaptive tier conservation (usage-aware routing): as the window fills,
    # progressively deny more expensive Claude tiers so remaining budget goes
    # to work that needs it. Both live-editable in the Guardrails tab.
    conserve_premium_at: float = 0.7   # general window >=70% used: deny Opus-class and above
    conserve_strong_at: float = 0.85   # general window >=85% used: deny Sonnet too (Haiku/local only)
    # Fable/Mythos-class has its OWN weekly bucket on the plan (~50% of total
    # usage) — this threshold applies to THAT bucket, not the general window.
    conserve_fable_at: float = 0.8     # Fable bucket >=80% used: deny Fable, point at Opus
    # Purchased usage credits kick in when windows exhaust. "last_resort":
    # first Claude attempt after exhaustion is denied with instructions to try
    # local; an insistent second attempt is permitted and logged as spending
    # credits. "never": exhaustion is a hard stop (credits never burned).
    usage_credits: Literal["never", "last_resort"] = "never"
    # User-directed paid routing: when the user's own message explicitly
    # requests a paid model ("use claude", "route this to opus"), the request
    # is honored past the free-first default — but if the relevant usage
    # window is at/above this fraction (or exhausted, or tier conservation
    # would deny the call), the router first pauses and asks the user in chat
    # to confirm spending the usage. Their "yes" bypasses tier conservation
    # for that request; dollar spend caps are never bypassed.
    confirm_user_paid_at: float = 0.5


class ResearchToolRef(BaseModel):
    server: str = ""
    tool: str = ""
    query_param: str = "query"
    url_param: str = "url"


class ResearchConfig(BaseModel):
    enabled: bool = True
    sweep_hours: int = 24
    stale_days: int = 14
    # Fetch-attempt budget. Ranking spends it best-first and an early-stop halts
    # once enough page material fills the corpus cap, so a higher ceiling mainly
    # buys resilience to dead/404 top candidates rather than more fetches.
    max_pages_per_model: int = 8
    # Total characters of assembled research corpus fed to the extraction LLM.
    # The ceiling is that model's context: the default 6GB brain (num_ctx ~6144)
    # is already near its limit at 24000 chars, so DON'T raise this unless you
    # point registry.research.model at a larger dedicated research model — then
    # a higher cap lets the extra pages actually reach the extractor.
    corpus_char_limit: int = 24000
    # Per-PAGE chars kept from each fetched page. Long model cards (HuggingFace)
    # and spec pages carry their benchmark tables well past 8000 chars; raise
    # this (with corpus_char_limit) when the extractor keeps missing scores that
    # are visibly on the page. Costs corpus budget per page.
    page_char_cap: int = 8000
    # Per-SEARCH chars kept from each search result blob (snippets). Pages carry
    # the real tables, so this stays smaller than page_char_cap by default.
    search_snippet_chars: int = 2500
    # Extra search-query templates, appended to the 7 built-ins. Use {name} for
    # the model name (e.g. "{name} specs hardware requirements",
    # "{name} site:huggingface.co", "{name} LiveCodeBench AIME GPQA"). More
    # queries surface more candidate pages before ranking spends the fetch budget.
    extra_queries: list[str] = Field(default_factory=list)
    # Explicit context size (num_ctx) for the research model, in tokens. 0 = auto:
    # size num_ctx to just fit corpus_char_limit + the reply. Set this to match a
    # persona's context_window when the research model is SHARED with a worker
    # persona — otherwise the sweep reloads that model at this smaller size and
    # thrashes with the persona's larger load (Foundry stays the single context
    # source of truth). Capped at the model's trained window either way.
    context_window: int = 0
    model: Optional[str] = None  # None => reuse the agent_brain model
    # Optional text prepended to every research search query. Its purpose is
    # SearXNG "bang" engine selection: setting e.g. "!google !bing !startpage"
    # restricts research searches to reliable engines and EXCLUDES the ones that
    # wall automated scraping — Brave (429 suspensions) and DuckDuckGo (CAPTCHA),
    # which no amount of client-side backoff clears. Empty = SearXNG's defaults.
    search_prefix: str = ""
    search: ResearchToolRef = Field(default_factory=ResearchToolRef)
    fetch: ResearchToolRef = Field(default_factory=ResearchToolRef)
    # NOTE: SearXNG pacing / 429-backoff moved to the MCP server config
    # (mcp.servers[].pace_seconds / rate_limit_retries / rate_limit_backoff_
    # seconds) so it covers EVERY caller — the research sweep, the worker-side
    # tool loop, and the brain's own tool calls — not just this background pass.


class RegistryConfig(BaseModel):
    openrouter_poll_hours: int = 24
    research: ResearchConfig = Field(default_factory=ResearchConfig)


class MCPServerConfig(BaseModel):
    name: str
    url: str
    transport: Literal["streamable-http", "sse"] = "streamable-http"
    # Operator on/off switch. Disabled = the connection is kept but the server
    # is skipped everywhere: no discovery/sync, no dialing, its tools drop from
    # the registry (so workers, the brain, and the Foundry-MCP aggregator stop
    # seeing them). Flip it back on to restore, no re-add needed. Default on so
    # existing servers are unchanged.
    enabled: bool = True
    headers: dict[str, str] = Field(default_factory=dict)
    # Media generation (ComfyUI, TTS, music) can take far longer than a text
    # tool call — per-server budget instead of one global assumption.
    timeout_seconds: int = 300
    # Rate-limit handling, applied to EVERY caller of this server (research
    # sweep, worker-side tool loop, the brain's own tool calls) since they all
    # funnel through MCPManager. SearXNG fans each query out to external engines
    # that 429 a burst; set pace_seconds on that server to space calls. 429-
    # backoff is on by default so any caller recovers even without pacing.
    pace_seconds: float = 0.0                    # min gap between calls to this server (0 = off)
    rate_limit_retries: int = 3                  # extra attempts on a 429 before giving up
    rate_limit_backoff_seconds: float = 30.0     # escalating backoff between those retries
    # Code-execution flag (code-sandbox spec). Purely operator-declared — there
    # is NO name-based special-casing anywhere; any server the operator marks
    # gets: the full-code audit trail (arguments captured in tool_call_log), a
    # persona "code execution" badge, and the sandbox controls in the MCP
    # editor. Default false keeps every existing/normal server unchanged.
    executes_code: bool = False
    # Arguments FORCE-MERGED into every tool call to this server, config winning
    # over whatever the model passed. Generic by design — the operator supplies
    # whatever keys THIS sandbox expects (cpus/memory/network/etc.), so no wire
    # vocabulary is baked into Foundry, and a second sandbox instance is just
    # another entry with different values. For a code sandbox this is the safety
    # gate: a model literally cannot flip network on if the operator set it off
    # here. The GUI's CPU/memory/network steppers are a convenience editor over
    # these keys; anything else can be set as raw JSON.
    call_defaults: dict[str, Any] = Field(default_factory=dict)
    # Keep one MCP session open per server and reuse it (no connect +
    # initialize handshake per tool call). A broken session is reopened
    # automatically; turn off for a server that misbehaves with long sessions.
    persistent_session: bool = True


class ToolSyncConfig(BaseModel):
    periodic_seconds: int = 300


class LoggingConfig(BaseModel):
    level: str = "INFO"


class SemanticCacheConfig(BaseModel):
    """Semantic response cache (quality spec Phase 3). OFF by default; the
    embedding source is a config setting like an mcp_servers entry (name-less:
    one embedder), never hardcoded — nomic-embed-text on TrueNAS today, a
    different model on a different host tomorrow. All fields GUI-editable and
    live (Guardrails tab card), per the MCP-pacing precedent."""
    enabled: bool = False
    embed_url: str = ""               # Ollama-compatible /api/embed endpoint, e.g. http://truenas:11434
    embed_model: str = ""             # e.g. nomic-embed-text
    embed_api_key: Optional[str] = None
    # Cosine similarity a stored answer must clear to count as a hit. 0.92 is
    # deliberately conservative — a wrong cached answer costs more trust than
    # a cache miss costs latency.
    min_similarity: float = 0.92
    default_ttl_seconds: int = 7 * 24 * 3600
    # Per-category TTL policy (persona benchmark_category). 0 = bypass the
    # cache entirely for that category. agentic/tool_calling default to 0 —
    # freshness and side effects matter more than latency there — and personas
    # with MCP tools attached bypass regardless of category.
    category_ttls: dict[str, int] = Field(default_factory=lambda: {
        "agentic": 0,
        "tool_calling": 0,
        "coding": 7 * 24 * 3600,
        "general_chat": 3 * 24 * 3600,
        "reasoning": 3 * 24 * 3600,
    })
    max_entries: int = 2000


class MCPAggregatorConfig(BaseModel):
    """Foundry-MCP: re-expose Foundry's own connected MCP tools as a single
    Streamable-HTTP MCP server that an MCP client (AnythingLLM, etc.) can point
    at. Off by default. The client owns the tool loop — this just hands it the
    tool manifest and executes calls through Foundry's existing MCP client, so a
    client with no server-side tools gains all of Foundry's, alongside its own.
    """
    enabled: bool = False
    # Shared secret the client must send. Empty = no auth (trusted LAN only).
    token: Optional[str] = None
    token_header: str = "X-API-KEY"
    # Base path of the all-tools endpoint; profiles are served at {base}/p/{name}.
    # The endpoints live on Foundry's own server port (path-based) — there is no
    # separate port to open.
    base_path: str = "/mcp"
    # Externally reachable base URL of THIS Foundry (e.g. http://192.168.1.50:8080
    # or https://foundry.mydomain) used only to render copy-paste client configs.
    # Empty = fall back to the configured server host:port.
    advertise_url: Optional[str] = None
    # Named tool subsets ("MCP personas"): profile name -> list of MCP server
    # names to expose on that endpoint. The base path always exposes them all.
    profiles: dict[str, list[str]] = Field(default_factory=dict)
    # Serve {base}/persona/<persona-name>: exactly the MCP tools attached to
    # that persona (whole servers or scoped per-tool grants), resolved live on
    # every call. Lets an external client (AnythingLLM) load a persona's small
    # tool set instead of every tool Foundry knows — context stays low.
    persona_endpoints: bool = True
    # Keep-alive for long tool calls (media generation). 0 = off: single JSON
    # reply (default). >0 = the endpoints run in SSE-streaming mode and emit an
    # MCP progress notification every N seconds while a tool runs, so the client
    # sees "still working…" and the held-open connection never idles out. Needs
    # a client that sends a progressToken and accepts SSE responses.
    progress_heartbeat_seconds: int = 0
    # Poll guard: how many identical (same tool + same args) calls within a
    # ~2-minute window before the aggregator STOPS executing them and returns a
    # "stop polling" message instead. Defends against a client agent (e.g.
    # AnythingLLM) tight-polling a status tool to its own tool-call limit while a
    # job is still running. 0 = off.
    poll_guard_threshold: int = 4


class AgentConfig(BaseModel):
    """An external AGENT Foundry can route to (Hermes Agent by Nous Research,
    via its OpenAI-compatible API server — `API_SERVER_ENABLED=true`, port
    8642). An agent is not a model: it runs its own multi-step loop with its
    own tools, skills and memory, so Foundry treats it two ways:

      * as a TOOL (`expose_as_tool`): `<name>_run` / `<name>_status` /
        `<name>_stop` appear under the MCP server "agent-<name>" — grant them
        to a persona like any MCP server, or load them from the aggregator
        (Cline / AnythingLLM) to delegate a long task;
      * as a BACKEND: a persona whose "served by agent" field names this agent
        forwards the whole conversation to it and streams the answer back, with
        the agent's tool progress shown as thinking.

    Agents are never picked by automatic model ranking."""
    name: str
    kind: Literal["hermes"] = "hermes"
    url: str = "http://localhost:8642"
    # Bearer token = the agent's API_SERVER_KEY. Empty = no auth.
    api_key: Optional[str] = None
    enabled: bool = True
    # Model name sent to the agent (Hermes advertises its profile name, e.g.
    # "hermes-agent"); empty = whatever /v1/models lists first.
    model: str = ""
    # Budget for one agent task — agents run long (minutes, not seconds).
    timeout_seconds: int = 1800
    # Offer <name>_run/_status/_stop as tools (MCP server "agent-<name>").
    expose_as_tool: bool = True
    # How long <name>_run blocks for the answer before handing back a run_id
    # the caller polls with <name>_status (keeps MCP clients from timing out).
    tool_wait_seconds: int = 600
    # Loop protection: the API key THIS agent uses when it calls Foundry as its
    # own model provider / MCP server. Requests carrying it are marked
    # agent-originated: they can never be routed back into an agent (no
    # agent-backed personas, no agent tools) — an agent can't call itself.
    caller_token: Optional[str] = None
    # Keep one agent session per client conversation (X-Hermes-Session-Id), so
    # the agent keeps its own memory of earlier turns. Off = stateless: the
    # full history is sent on every turn.
    session_continuity: bool = True


class TurnAssistConfig(BaseModel):
    """Global turn-assist settings (docs/TURN_ASSIST.md), editable live in
    Routing -> Guardrails -> Turn assist. Precedence per request: built-in
    defaults < `defaults` here < the persona's `assist` JSON."""
    # Any ASSIST_DEFAULTS key (thresholds, prompts, packet sizes, ...).
    defaults: dict = Field(default_factory=dict)
    # Pre-judge tables. Empty = the built-in rules. Each block rule:
    # {pattern, rule, advice, enabled}; each fix rule:
    # {pattern, replacement, desc, enabled}.
    block_rules: list[dict] = Field(default_factory=list)
    fix_rules: list[dict] = Field(default_factory=list)
    # Regexes that mark a tool RESULT as failed. Empty = built-in markers.
    error_patterns: list[str] = Field(default_factory=list)


class DataRetentionConfig(BaseModel):
    """Days to keep rows per log table (0 / absent = keep forever). Pruned
    on startup and every `prune_interval_minutes`. perf_samples keeps its own
    retention (perf_history.RETENTION_DAYS)."""
    days: dict[str, int] = Field(default_factory=lambda: {
        "interventions": 180,      # fine-tuning material — keep longer
        "assist_turns": 90,
    })
    prune_interval_minutes: int = 60


class ClientConfig(BaseModel):
    """One API client (Kilo, Cline, OpenWebUI, a script) with its own key.
    See client_auth.py for what each scope reaches."""
    name: str
    key: Optional[str] = None          # use a ${VAR} reference in config.yaml
    # Keys made in the web UI (Setup -> Clients & keys) are stored only as a
    # SHA-256 hash: config.yaml, its version history and export bundles never
    # hold the key itself. key_hint is the first/last characters, for display.
    key_hash: Optional[str] = None
    key_hint: Optional[str] = None
    scopes: list[Literal["inference", "media", "admin", "orchestration"]] = Field(
        default_factory=lambda: ["inference"])
    # Optional: only these personas are visible/usable for this client.
    personas: list[str] = Field(default_factory=list)
    enabled: bool = True
    note: str = ""                     # free text: what/where this client is
    created: Optional[str] = None      # ISO time the key was made / last rotated


class ClientAuthConfig(BaseModel):
    # optional: keyless requests pass as "anonymous" (wrong keys are refused);
    # required: inference/media need a valid client key.
    mode: Literal["optional", "required"] = "optional"
    # Also require an admin-scoped key for the web UI and /admin API.
    protect_admin: bool = False
    # A key that matches no client: refuse (default; a typo must not become
    # "anonymous"), or, in optional mode only, let it through as anonymous
    # while keys are rolled out (clients such as Cline's OpenAI-compatible
    # provider always send whatever is in their API-key field).
    unknown_keys: Literal["refuse", "anonymous"] = "refuse"


class MediaConfig(BaseModel):
    """Image generation/editing jobs (media.py). Jobs run on ONE backend and
    only on the models listed here — never on another model or provider."""
    enabled: bool = True
    # Backend name from backend_pool; "" = the CLIProxyAPI backend (flavor
    # cliproxyapi). A backend billed per call (OpenAI/OpenRouter API key) is
    # refused unless allow_paid_api is set.
    backend: str = ""
    allow_paid_api: bool = False
    models: list[str] = Field(default_factory=lambda: [
        "gpt-image-2.5-sunburst", "gpt-image-2.5-flare", "gpt-image-2.5", "gpt-image-2"])
    default_model: str = "gpt-image-2.5-sunburst"     # "final"
    draft_model: str = "gpt-image-2.5-flare"          # "draft"
    # Image budget, separate from chat: jobs that reached the backend count.
    max_concurrent: int = 1
    max_queued: int = 8
    jobs_per_5h: int = 20             # 0 = no cap
    jobs_per_day: int = 60
    client_jobs_per_day: int = 0      # per client key; 0 = no cap
    max_images_per_job: int = 4
    job_timeout_seconds: int = 600
    # Retries only when the request provably did not start an image (connect
    # failure, 502/503 from the proxy). A timeout is never resubmitted.
    max_attempts: int = 2
    # The same request (client, model, prompt, settings, input images) within
    # this window returns the existing job instead of generating again.
    dedupe_minutes: int = 30
    max_input_mb: float = 20.0
    retention_days: int = 30          # artifacts and job rows
    # Serve generate_image / edit_image / image_job_status at {mcp base}/media
    # (needs mcp_aggregator.enabled). Never added to the other MCP endpoints.
    mcp_tools: bool = True


class TrainingCaptureConfig(BaseModel):
    """Opt-in capture of inference traffic for training data (capture.py).
    Off by default; redacted before it is written; a separate store."""
    enabled: bool = False
    projects: list[str] = Field(default_factory=list)       # empty = every bound project
    exclude_projects: list[str] = Field(default_factory=list)
    include_unbound: bool = False      # traffic no coordinator bound (plain Kilo/Cline use)
    clients: list[str] = Field(default_factory=list)        # empty = every client
    retention_days: int = 30
    max_body_bytes: int = 4_000_000
    store_path: Optional[str] = None   # default <data>/training/capture.sqlite
    allowed_email_domains: list[str] = Field(default_factory=list)


class BudgetRule(BaseModel):
    """One shared limit in the budget ledger (budget.py).

    `match` selects the model calls it covers (all keys optional; a value is a
    glob or a list of globs): backend, persona, client, account (e.g.
    "cliproxy:codex", "meridian:claude"), model; and paid: true|false
    (subscription/paid backends vs local); and, for sessions a development
    coordinator registered: project, work_task (its sprint/task id), run (one
    attempt) and role (implementer, reviewer, ...). `per` splits the limit into
    one allowance per client / persona / session / task (a helper run) /
    request / backend / account / project / work_task / run / role; none = one
    shared allowance."""
    name: str
    enabled: bool = True
    mode: Literal["enforce", "observe"] = "enforce"   # observe = log "would deny" only
    match: dict = Field(default_factory=dict)
    per: Optional[Literal["client", "persona", "session", "task", "request",
                          "backend", "account", "project", "work_task", "run",
                          "role"]] = None
    window_minutes: int = 300          # 0 = no window: the whole life of the group
    max_calls: Optional[int] = None    # model rounds, retries included
    max_tokens: Optional[int] = None   # input + output tokens
    max_concurrent: Optional[int] = None   # calls in flight at once
    # Distinct helpers active at once (a helper = task id, else session, else
    # client+persona); one stays "active" until idle_seconds after its last call.
    max_active: Optional[int] = None
    active_by: Literal["task", "session", "client", "persona", "run"] = "task"
    idle_seconds: int = 60
    wait_seconds: float = 0            # wait this long for a free slot before refusing


def _default_budget_rules() -> list:
    # Helper limits live on each backend (helpers_allowed); rules are for
    # call / token caps and anything more specific.
    return []


class BudgetsConfig(BaseModel):
    enabled: bool = True
    rules: list[BudgetRule] = Field(default_factory=_default_budget_rules)
    retention_days: int = 90           # ledger rows (usage_events)


class HealthConfig(BaseModel):
    """Functional health (health.py): reachable vs actually serving."""
    # Probe a backend with a tiny generation when it has had no successful call
    # for this long and is idle. 0 = never probe automatically.
    probe_minutes: int = 15
    # Also auto-probe paid/subscription backends (each probe spends quota).
    probe_paid: bool = False
    probe_timeout_seconds: int = 120
    # A success within this window counts as "serving".
    fresh_minutes: int = 15


class AppConfig(BaseModel):
    # Config file format version (see migrate_raw); stamped on load.
    config_version: int = 1
    server: ServerConfig = Field(default_factory=ServerConfig)
    agent_brain: AgentBrainConfig = Field(default_factory=AgentBrainConfig)
    backend_pool: BackendPoolConfig = Field(default_factory=BackendPoolConfig)
    guardrails: GuardrailsConfig = Field(default_factory=GuardrailsConfig)
    meridian: MeridianConfig = Field(default_factory=MeridianConfig)
    subscription_usage: SubscriptionUsageConfig = Field(default_factory=SubscriptionUsageConfig)
    registry: RegistryConfig = Field(default_factory=RegistryConfig)
    mcp_servers: list[MCPServerConfig] = Field(default_factory=list)
    tool_sync: ToolSyncConfig = Field(default_factory=ToolSyncConfig)
    semantic_cache: SemanticCacheConfig = Field(default_factory=SemanticCacheConfig)
    turn_assist: TurnAssistConfig = Field(default_factory=TurnAssistConfig)
    data_retention: DataRetentionConfig = Field(default_factory=DataRetentionConfig)
    mcp_aggregator: MCPAggregatorConfig = Field(default_factory=MCPAggregatorConfig)
    agents: list[AgentConfig] = Field(default_factory=list)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    clients: list[ClientConfig] = Field(default_factory=list)
    client_auth: ClientAuthConfig = Field(default_factory=ClientAuthConfig)
    media: MediaConfig = Field(default_factory=MediaConfig)
    # Default persona sets (profiles.py) applied at startup: missing personas
    # are created, untouched ones updated to the profile's newer version, edited
    # ones left alone. Also available on demand from the Personas tab.
    persona_profiles: list[str] = Field(default_factory=list)
    budgets: BudgetsConfig = Field(default_factory=BudgetsConfig)
    training_capture: TrainingCaptureConfig = Field(default_factory=TrainingCaptureConfig)
    health: HealthConfig = Field(default_factory=HealthConfig)


# --------------------------------------------------------------------------- #
# Load / save                                                                 #
# --------------------------------------------------------------------------- #

# --------------------------------------------------------------------------- #
# Versioned config migrations                                                 #
# --------------------------------------------------------------------------- #
# Each entry upgrades the RAW document (so ${VAR} references survive) from
# version N-1 to N. Add a function and bump CONFIG_VERSION when a setting is
# renamed or reshaped; load() applies the pending ones, keeps a backup of the
# old file (config.yaml.v<N>.bak) and writes the upgraded document.

def _m2_stamp(raw: dict) -> list[str]:
    """v2: versioned config format introduced (no settings change)."""
    return []


def _m3_helper_per_account(raw: dict) -> list[str]:
    """v3: the default one-frontier-helper rule allows one helper per
    subscription account instead of one across all of them. Only the rule as
    shipped (no `per` set) is changed; an edited one is left alone."""
    rules = ((raw.get("budgets") or {}).get("rules")) or []
    for r in rules:
        if (isinstance(r, dict) and r.get("name") == "one-frontier-helper" and "per" not in r
                and r.get("match") == {"persona": "Foundry-Kilo-*", "paid": True}):
            r["per"] = "account"
            return ["budget rule one-frontier-helper: one helper per subscription account (per: account)"]
    return []


def _shipped_helper_rule(r) -> bool:
    return (isinstance(r, dict) and r.get("name") == "one-frontier-helper"
            and r.get("match") == {"persona": "Foundry-Kilo-*", "paid": True}
            and r.get("per") in (None, "account") and r.get("max_active") == 1)


def _m4_helpers_on_backends(raw: dict) -> list[str]:
    """v4: helper limits move from the one-frontier-helper budget rule to each
    backend's helpers_allowed. The shipped rule (or the implicit default, when
    the file has no budget rules) becomes helpers_allowed: 0 on every paid
    backend that doesn't set it; a rule the user edited is left alone."""
    budgets = raw.get("budgets") if isinstance(raw.get("budgets"), dict) else None
    if budgets and budgets.get("enabled") is False:
        return []                                    # limits were off; keep them off
    rules = budgets.get("rules") if budgets else None
    if rules is None:
        shipped = True                               # defaults were in effect
    else:
        keep = [r for r in rules if not _shipped_helper_rule(r)]
        shipped = len(keep) != len(rules)
        if shipped:
            budgets["rules"] = keep
    if not shipped:
        return []
    def is_paid(c) -> bool:                          # budget.is_paid, without its imports
        return (c.type == "anthropic-compatible" or c.is_subscription
                or (c.type == "openai-compatible"
                    and c.effective_flavor not in ("llamacpp", "vllm", "unsloth")))
    set_on = []
    backends = (((raw.get("backend_pool") or {}).get("internal") or {}).get("backends")) or []
    for b in backends:
        if not isinstance(b, dict) or "helpers_allowed" in b:
            continue
        try:
            paid = is_paid(BackendConfig(**b))
        except Exception:                                  # noqa: BLE001
            continue
        if paid:
            b["helpers_allowed"] = 0
            set_on.append(str(b.get("name")))
    notes = ["budget rule one-frontier-helper replaced by per-backend helpers_allowed"]
    if set_on:
        notes.append("helpers_allowed: 0 (main stream only) on " + ", ".join(set_on))
    return notes


MIGRATIONS = [(2, _m2_stamp), (3, _m3_helper_per_account), (4, _m4_helpers_on_backends)]
CONFIG_VERSION = MIGRATIONS[-1][0]


def migrate_raw(raw: dict) -> tuple[dict, list[str]]:
    """(raw upgraded to CONFIG_VERSION, notes). A file from a NEWER Foundry
    is left untouched."""
    notes: list[str] = []
    ver = int(raw.get("config_version") or 1)
    for target, fn in MIGRATIONS:
        if ver < target:
            notes += fn(raw) or []
            notes.append(f"config migrated to version {target}")
            ver = target
            raw["config_version"] = target
    return raw, notes


def config_path() -> Path:
    return Path(os.environ.get("FOUNDRY_CONFIG", "/config/config.yaml"))


def data_dir() -> Path:
    d = Path(os.environ.get("FOUNDRY_DATA_DIR", "/data"))
    d.mkdir(parents=True, exist_ok=True)
    return d


def _example_path() -> Path:
    # config.example.yaml is copied into the image next to the package dir.
    candidates = [
        Path(__file__).resolve().parent.parent / "config.example.yaml",
        Path.cwd() / "config.example.yaml",
    ]
    for c in candidates:
        if c.exists():
            return c
    return candidates[0]


class ConfigStore:
    """Holds both the raw (unexpanded) YAML document and the parsed config.

    The raw document is the write-back target for UI edits; the parsed
    AppConfig (with ${VAR} expanded) is what the rest of the app consumes.
    """

    def __init__(self, path: Optional[Path] = None):
        self.path = path or config_path()
        self.raw: dict = {}
        self.config: AppConfig = AppConfig()

    def load(self) -> AppConfig:
        if not self.path.exists():
            example = _example_path()
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if example.exists():
                shutil.copy(example, self.path)
                log.warning(
                    "No config found at %s — copied config.example.yaml there. "
                    "Edit it for your deployment (backend URLs are placeholders).",
                    self.path,
                )
            else:
                self.path.write_text("{}\n", encoding="utf-8")
        with open(self.path, "r", encoding="utf-8") as f:
            self.raw = yaml.safe_load(f) or {}
        before = int(self.raw.get("config_version") or 1)
        self.raw, notes = migrate_raw(self.raw)
        if notes:
            backup = self.path.with_name(self.path.name + f".v{before}.bak")
            try:
                if not backup.exists():
                    shutil.copy(self.path, backup)
                self._write_raw()
                log.warning("config upgraded v%s -> v%s (backup %s): %s", before,
                            self.raw["config_version"], backup.name, "; ".join(notes))
            except OSError as e:
                log.warning("config upgraded in memory only (could not write: %s)", e)
        self.migration_notes = notes
        self.config = AppConfig.model_validate(_expand_env(copy.deepcopy(self.raw)))
        return self.config

    def save(self, mutate) -> AppConfig:
        """Apply ``mutate(raw_dict)`` to the raw document, persist, reload.

        Callers mutate the *raw* document so ${VAR} references survive the
        round trip instead of being replaced by their expanded secrets.
        """
        before = copy.deepcopy(self.raw)
        mutate(self.raw)
        try:
            config = AppConfig.model_validate(_expand_env(copy.deepcopy(self.raw)))
        except Exception:
            self.raw = before            # never persist a document that doesn't load
            raise
        self._write_raw()
        self.config = config
        for cb in list(getattr(self, "on_saved", [])):
            try:
                cb(before, self.raw)
            except Exception:
                log.exception("config on_saved hook failed")
        return self.config

    def _write_raw(self) -> None:
        tmp = self.path.with_suffix(".yaml.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            yaml.safe_dump(self.raw, f, sort_keys=False, allow_unicode=True)
        tmp.replace(self.path)
