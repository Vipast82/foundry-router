# Cline (VS Code) — plan/act routing for max quality + cost savings

Cline runs its own agent loop, so Foundry's job here is **model selection, not a
second agent**. Two thin-router personas ship for this, and you assign one to each
of Cline's modes:

| Cline mode | Persona | Routes to | Why |
|---|---|---|---|
| **Plan** | `claude-cline-plan` | Claude (Sonnet first, Opus for hard design) | Planning/architecture is high-value, low-volume — worth subscription tokens. |
| **Act** | `claude-cline-act` | Local-first; Claude for debugging | Bulk code writing/edits are voluminous — keep them local/free. |

Both names contain `claude`, so Cline's model-name gate is satisfied. Neither runs
Foundry's pipeline or injects Foundry MCP tools — Cline stays in control of its own
tools and loop.

## `execution_mode = direct` is required (not `agent`)

Both personas must use **`execution_mode = direct`** (the seed sets this; existing
installs auto-migrate). It's the thin-proxy path: Foundry picks **one** model per
turn — honoring `model_allowlist`, the `prefer_paid`/`strong` bias, and the usage
guardrails — then forwards Cline's request (and its tools) verbatim and returns the
model's output unchanged.

**Do NOT use `agent` mode for Cline.** `agent` runs Foundry's routing brain, which
delegates to workers via internal `ask_<model>` tool calls. Cline is itself an
agent and can't parse those — they leak into the chat as `<ask_claude_sonnet_5>…`
and Cline reports repeated tool-call failures. `direct` avoids that entirely.

### Consequence: escalation is by mode switch, not automatic

In `direct` mode nothing does mid-task delegation, so the Act persona's
`escalation_triggers` are informational only. You control Claude-vs-local by
**switching Cline between Act and Plan**: Act runs local, Plan runs Claude. Write
in Act (local/free), flip to Plan when you need Claude to reason through something
hard — predictable and cheap.

## 1. Point Cline at Foundry

In Cline's settings, add an API provider:
- **Ollama** provider → Base URL `http://<foundry-host>:11435` (personas appear as
  models), **or**
- **OpenAI-Compatible** provider → Base URL `http://<foundry-host>:11435/v1`.

Either way the personas show up as selectable models. (Foundry keeps its routing
narration in the reasoning channel, so Cline only ever sees clean output.)

## 2. Turn on separate Plan/Act models

In Cline: **Settings → enable "Use different models for Plan and Act modes."** Then:
- **Plan mode model** → `claude-cline-plan`
- **Act mode model** → `claude-cline-act`

That's the whole plan/act mechanism — no prompt-sniffing, and you can see which
model runs when.

## 3. Configure the models each persona may use (the one required step)

Personas → edit each → **model_allowlist**. This is a hard restriction:
**empty = Foundry picks from all; one id = locked to it; a few ids = choose among
those.** The field suggests your real model ids as you type.

**`claude-cline-plan`** — click **⊕ Claude** next to the field. It auto-fills your
paid/Claude model ids, cheapest tier first (Sonnet before Opus), so Plan is
guaranteed Claude in one click. The brain uses the cheapest that fits and only
escalates to Opus for genuinely hard architecture (the persona's escalation
trigger). If you leave the allow-list empty, it still *prefers* paid (via
`prefer_paid` bias) but isn't guaranteed — the button is the reliable path.

**`claude-cline-act`** — leave the allow-list **empty** to let Foundry pick the
best local coder and escalate to Claude for debugging. Or restrict it (the
**⊕ Local** button fills all local model ids to trim down):
- Lock to one local coder (avoids model-reload churn): `["qwen3.8:27b"]`
- Limit the pool: `["qwen3.8:27b", "claude-sonnet-4-6"]` (local writes, Claude for
  debugging, nothing else)

> Find your exact ids in the **Models** tab (or the allow-list field's dropdown).
> The Claude ids depend on your Meridian setup.

## 4. How the cost/quality behavior works

- **Plan → Claude.** `prefer_paid` bias + Claude-only allow-list ⇒ planning routes
  to Sonnet, Opus only for hard design.
- **Act → local.** `strong` local bias ⇒ code writing/edits stay on the local
  coder (free).
- **Debugging → Claude, until quota is tight.** Act's escalation triggers send
  debugging/root-causing to Claude; as your Meridian window fills, the **adaptive
  conserve** guardrails automatically step down (Opus→Sonnet→Haiku→local), so you
  fall back to local exactly as you asked. Tune the thresholds in **Backend Pool →
  Meridian** (`conserve_*_at`).
- **Repeated edit failures → escalate.** If the local model keeps botching Cline's
  SEARCH/REPLACE diffs, that trigger escalates to Claude to unstick you.

## 5. Best tools for coding in Cline

Cline drives its own tools (read/write file, run command, search, browser) and has
its **own** MCP support — so add coding MCP servers **in Cline**, not in these
personas:
- **context7** — live library/framework docs (huge for correct API usage).
- Your **Docker MCP Gateway** servers (filesystem, git, sqlite, etc.) if you want
  them available to Cline.

Keep the Foundry personas' `preferred_mcp_tools` **empty** — doubling tools between
Cline and Foundry causes conflicts.

## 6. Reasoning effort (thinking level)

Each persona has a **`reasoning_effort`** field (in the persona editor, next to
`output_style`). It sets how hard the worker thinks, and Foundry expresses it in
whatever form the chosen backend wants:

- **Local worker (Ollama):** sent as Ollama's top-level `think` level, but only
  to models that report a `thinking` capability (auto-detected from `/api/show`).
  Non-reasoning locals are left untouched. Most reasoning locals (Qwen3,
  DeepSeek-R1) treat this as on/off; the gpt-oss family honors graded levels.
- **Claude via Meridian:** mapped to a real extended-thinking budget —
  `low≈2k`, `medium≈8k`, `high≈16k`, `xhigh≈32k` `budget_tokens`. Foundry raises
  `max_tokens` above the budget and drops the temperature override (both required
  by the API), and streams Claude's returned thinking summary into Cline's
  reasoning pane.

**Defaults shipped for the pair:** `claude-cline-plan` = **high** (deep Claude
thinking for architecture/planning), `claude-cline-act` = **low** (light, fast
thinking on the local coder). Both are one-time seeds — change them freely in the
UI. Blank = fall through to the global **Agent Brain → reasoning_effort**.

**Precedence:** a level the client sends on the request > the persona's
`reasoning_effort` > the global Agent Brain default. So if Cline itself sends a
`think`/`reasoning_effort` field, that wins; whatever arrives is logged (events
log, source `facade`) so you can see exactly what Cline sends. If Cline sends
nothing, the persona setting is your knob.

**Seeing the model's actual thought process.** Whatever reasoning a backend
returns is streamed into Cline's thinking panel live: llama.cpp / vLLM
`reasoning_content`, Ollama `thinking`, Claude's thinking summary. If a server
leaves reasoning inline as `<think>…</think>` text (llama.cpp run with
`--reasoning-format none`, some templates), Foundry splits it out so it lands in
the panel instead of the reply. The first thinking line of every turn shows what
Foundry asked for and who decided — `[think: low by persona · …]`,
`[think: off by persona (forced) · …]`, `[think: model default · …]`. No
reasoning in the panel usually means one of:

- the persona (or Cline) turned thinking **off** — the label says so; raise the
  persona's `reasoning_effort`;
- the model doesn't reason (not a thinking model), or llama.cpp was started with
  `--reasoning-budget 0`;
- for llama.cpp, use `--jinja` with the default `--reasoning-format` (auto /
  deepseek) so reasoning comes back as `reasoning_content`;
- Claude only thinks when a level is set (it then returns a summary).

Foundry's own status lines (`⚙️ … streaming…`, `⏳ … still working · …`,
routing notes) are shown in the thinking panel, and Cline saves them with the
turn. Foundry strips them from the history before it reaches any model — a
model that saw them as its own past reasoning started generating fake
"still working… 5s / 10s" lines itself (faster than real time, using up its
output budget).

**Which levels a model actually supports** is shown per model on the Models tab
(`thinking_levels`) — a curated list, since no Ollama/Anthropic endpoint
enumerates the valid set.

## 7. Tuning notes

- **Local coder quality matters in Act.** Cline's diff edits are strict; a weak
  model loops on failed edits. Prefer a strong instruction-follower (e.g.
  `qwen3.8:27b`, or a dedicated coder like Qwen3-Coder/Devstral) and let the
  repeated-failure trigger escalate when needed.
- **Context window.** Cline sends large contexts. Set each persona's
  `context_window` to what your hardware loads (see [CONTEXT_SIZING.md](CONTEXT_SIZING.md));
  Claude is fixed at 200K.
- **"Output-token limit reached before a tool call".** That message is
  Cline's: the model's reply was cut at its output-token cap before the tool
  call (usually a `write_to_file` / `replace_in_file`) was complete, so Cline
  asks for a more concise answer (up to 3 attempts). The cap is the persona's
  **max output tokens** (blank = `worker_max_tokens`, default 32768) and the
  model's **reasoning counts toward it** — a thinking model writing a large
  edit easily passes 8k. Set **max output tokens** on your Cline personas
  (32768 is a good start; the Cline personas ship with it). Foundry says so
  in the thinking panel when a reply is cut (`⚠️ reply cut at the
  32,768-token output limit … the tool call was incomplete`). If the reply
  stopped well SHORT of the limit Foundry sent (`⚠️ reply cut at 8,192 output
  tokens, but Foundry allowed 32,768 — the BACKEND stopped it`), the cap is on
  the server: check llama.cpp's `-n` / `--n-predict` (also in llama-swap
  `cmd:` lines), Ollama's `num_predict`, or whether the context filled up.
  Every cut is also logged in Events (source `truncation`).
- **Never overflowing the window.** Cline auto-compacts when its last request
  gets close to the context size it *thinks* the model has, so:
  1. In Cline's provider settings set the **context window** to the persona's
     `context_window` (Foundry also reports it in `/api/show`).
  2. Foundry reports the **total** prompt size back to Cline. Ollama's own
     count excludes cached tokens, which made Cline think the context was
     small and skip compaction. Foundry now corrects that (the real re-processed
     count stays visible as `foundry.prompt_evaluated`).
  3. **Context guard** (Backends → Pool, on by default): if a request would
     still overflow — one huge file read can jump past Cline's threshold in a
     single turn — Foundry trims the copy it sends to the model: oversized old
     tool results first, then the oldest turns after your task. The system
     prompt and the task are always kept, and tool calls stay paired with their
     results. Cline keeps its full history, and the turn shows
     `⚠️ context guard: ~251k tokens would overflow the 262k window — dropped
     14 older message(s) …`. The Usage Log and Events record it too.
- **Only use these personas for Cline.** They're purpose-built thin routers; your
  other clients keep using `Foundry-Chat`/`Foundry-Coding`/etc.
