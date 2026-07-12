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


class AgentBrainConfig(BaseModel):
    provider: Literal["ollama", "meridian", "openrouter"] = "ollama"
    endpoint: str = "http://localhost:11434"
    model: str = ""
    api_key: Optional[str] = None
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
    worker_max_tokens: int = 8192         # output budget for worker-model calls
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
    heartbeat_seconds: float = 25.0


class BackendConfig(BaseModel):
    name: str
    # `type` is required (§4.3): three different wire protocols, and e.g.
    # Meridian would silently fail if spoken to in Ollama protocol.
    type: Literal["ollama", "anthropic-compatible", "openai-compatible"]
    url: str
    api_key: Optional[str] = None
    priority: int = 100
    # Optional discovery fallback ONLY — used if the backend exposes no
    # model-list endpoint (§4.3: "do not hardcode model lists for backends
    # that can be discovered").
    models: list[str] = Field(default_factory=list)


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
    request_timeout_seconds: int = 300
    internal: InternalPoolConfig = Field(default_factory=InternalPoolConfig)
    olla: OllaConfig = Field(default_factory=OllaConfig)
    litellm: LiteLLMConfig = Field(default_factory=LiteLLMConfig)


class GuardrailsConfig(BaseModel):
    authority: Literal["internal", "defer_to_pool"] = "internal"
    max_steps_per_request: int = 12
    max_paid_calls_per_request: int = 3
    daily_spend_cap_usd: Optional[float] = None
    weekly_spend_cap_usd: Optional[float] = None


class MeridianConfig(BaseModel):
    # Real per-profile usage-window data (buckets of five_hour/weekly
    # utilization). The old default "/telemetry" pointed at the HTML dashboard
    # and could never work — deployed configs that still carry telemetry_path
    # are simply ignored (pydantic drops unknown keys) and get this default.
    quota_path: str = "/v1/usage/quota"
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
    usage_credits: Literal["never", "last_resort"] = "last_resort"


class ResearchToolRef(BaseModel):
    server: str = ""
    tool: str = ""
    query_param: str = "query"
    url_param: str = "url"


class ResearchConfig(BaseModel):
    enabled: bool = True
    sweep_hours: int = 24
    stale_days: int = 14
    max_pages_per_model: int = 3
    model: Optional[str] = None  # None => reuse the agent_brain model
    search: ResearchToolRef = Field(default_factory=ResearchToolRef)
    fetch: ResearchToolRef = Field(default_factory=ResearchToolRef)


class RegistryConfig(BaseModel):
    openrouter_poll_hours: int = 24
    research: ResearchConfig = Field(default_factory=ResearchConfig)


class MCPServerConfig(BaseModel):
    name: str
    url: str
    transport: Literal["streamable-http", "sse"] = "streamable-http"
    headers: dict[str, str] = Field(default_factory=dict)
    # Media generation (ComfyUI, TTS, music) can take far longer than a text
    # tool call — per-server budget instead of one global assumption.
    timeout_seconds: int = 300


class ToolSyncConfig(BaseModel):
    periodic_seconds: int = 300


class LoggingConfig(BaseModel):
    level: str = "INFO"


class AppConfig(BaseModel):
    server: ServerConfig = Field(default_factory=ServerConfig)
    agent_brain: AgentBrainConfig = Field(default_factory=AgentBrainConfig)
    backend_pool: BackendPoolConfig = Field(default_factory=BackendPoolConfig)
    guardrails: GuardrailsConfig = Field(default_factory=GuardrailsConfig)
    meridian: MeridianConfig = Field(default_factory=MeridianConfig)
    registry: RegistryConfig = Field(default_factory=RegistryConfig)
    mcp_servers: list[MCPServerConfig] = Field(default_factory=list)
    tool_sync: ToolSyncConfig = Field(default_factory=ToolSyncConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)


# --------------------------------------------------------------------------- #
# Load / save                                                                 #
# --------------------------------------------------------------------------- #

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
        self.config = AppConfig.model_validate(_expand_env(copy.deepcopy(self.raw)))
        return self.config

    def save(self, mutate) -> AppConfig:
        """Apply ``mutate(raw_dict)`` to the raw document, persist, reload.

        Callers mutate the *raw* document so ${VAR} references survive the
        round trip instead of being replaced by their expanded secrets.
        """
        mutate(self.raw)
        tmp = self.path.with_suffix(".yaml.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            yaml.safe_dump(self.raw, f, sort_keys=False, allow_unicode=True)
        tmp.replace(self.path)
        self.config = AppConfig.model_validate(_expand_env(copy.deepcopy(self.raw)))
        return self.config
