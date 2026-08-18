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

## 6. Tuning notes

- **Local coder quality matters in Act.** Cline's diff edits are strict; a weak
  model loops on failed edits. Prefer a strong instruction-follower (e.g.
  `qwen3.8:27b`, or a dedicated coder like Qwen3-Coder/Devstral) and let the
  repeated-failure trigger escalate when needed.
- **Context window.** Cline sends large contexts. Set each persona's
  `context_window` to what your hardware loads (see [CONTEXT_SIZING.md](CONTEXT_SIZING.md));
  Claude is fixed at 200K.
- **Only use these personas for Cline.** They're purpose-built thin routers; your
  other clients keep using `Foundry-Chat`/`Foundry-Coding`/etc.
