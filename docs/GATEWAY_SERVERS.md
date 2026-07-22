# MCP > Gateway Servers

Browse the Docker MCP Catalog and attach/detach servers on a live **Docker MCP
Gateway** from Foundry Router's admin UI — no SSH to `general-ai` required.

## What it is

Foundry Router already holds an MCP connection to the gateway. The gateway
injects its own management tools (`mcp-find`, `mcp-add`, `mcp-remove`, …) into
that connection when `dynamic-tools` is on. This panel lets the **operator**
drive those tools from Foundry's backend as a control panel.

The panel appears on the **MCP** tab whenever a connection is detected that
exposes the gateway management tools — detection is by tool presence
(`mcp-find` / `mcp-add`), never by a hardcoded connection name, so it works for
any gateway connection you configure.

## What it does

- **Search catalog** → runs `mcp-find`, lists servers (name, description, tool
  count, whether it requires secrets).
- **Add** a secret-free server → runs `mcp-add`, then re-runs Tool Sync and
  reports how many tools appeared.
- **Remove** a server → runs `mcp-remove` (with a confirm — removal is
  disruptive), then re-runs Tool Sync.
- **Currently attached** → derived from Tool Sync's view of the gateway
  connection's downstream tools, grouped by namespace when the gateway
  namespaces them. A manual *remove by ref* box covers anything grouping can't
  name.

Every action is a **backend-initiated** MCP call from these operator-only
routes (`/admin/api/gateway/*`). It is **never** routed through a persona or a
model tool-loop — same trust tier as the "add MCP connection" form.

## Security

`mcp-add` / `mcp-remove` / `mcp-create-profile` / `mcp-activate-profile` /
`mcp-config-set` are **root-level over the whole gateway** — whoever can add an
arbitrary catalog (or private) server effectively chooses what code runs in a
container on `general-ai`. They are therefore:

- available **only** on this operator page, and
- **excluded from every persona's grantable tool set** — they never appear as a
  checkbox in the persona editor and are filtered out of persona tool
  resolution in the backend, even under a whole-server grant. This holds
  regardless of this page.

## Tool-name-prefix grouping

The Docker MCP Gateway's `tool-name-prefix` feature names every routed tool
`server__bare` (e.g. `memory__read_graph`, `playwright__browser_click`,
`SQLite__append_insight` — the prefix is the exact, case-sensitive server ref
used at add time). Note the separator is `__` in practice, not the `:` shown in
`docker mcp feature enable`'s own example text; Foundry splits on `__` first and
falls back to `:` for future builds. Enable it on the host and restart the
gateway:

```bash
docker mcp feature enable tool-name-prefix && systemctl restart mcp-gateway.service
```

Once tools carry prefixes, Foundry groups them by originating server: the
**Currently attached** table shows one group per underlying server (Memory,
Playwright, SQLite, Sequential Thinking, Context7 …) instead of one
`(ungrouped)` blob, and the **persona editor's tool checklist** sub-groups the
connection's tools the same way (collapsible per server, bare tool names shown,
write/destructive badges preserved). Standalone connections (crawl4ai, searxng)
have no prefix and are unaffected.

**One-time re-check after enabling:** scoped persona grants store the exact tool
name, so a grant made against the old bare names (e.g. `Foundry-Chat`'s
`read_graph`/`search_nodes`/`open_nodes`) no longer matches once tools become
`memory__read_graph` etc. The dropped tools are logged to Events on the next
sync (not silently lost). Just re-check the three boxes under their new grouped
location (Memory) and save — no migration tooling, since it's the only existing
grant affected.

## Secrets — out of scope (needs a companion service)

Servers that require secrets (GitHub PAT, Proxmox token, Obsidian/Grafana keys,
…) show **Add disabled** with a tooltip. The gateway's secrets live in a
`chmod 600 ~/.docker/mcp/secrets.env` on the host, read directly by the gateway
process — there is **no MCP tool or API** to write it remotely (Docker
Desktop's secrets engine isn't present on headless Linux).

Closing this gap needs a small **companion HTTP service** on `general-ai`
(bound to localhost or firewalled to Foundry Router's host) that accepts
`{server, key, value}`, updates the matching line in `secrets.env`, and
restarts `mcp-gateway.service`. That is its own small project — not something
the gateway ships. Until it exists, credentialed servers stay a manual
SSH + `secrets.env` edit + `docker mcp profile server add` + service restart.

## Note on gateway response shapes

The gateway's tool argument names and `mcp-find` result shape are the Gateway's,
not Foundry's. Argument names are read from each tool's **discovered input
schema** (not hardcoded), and the catalog parser is tolerant of shape.

Each result row exposes **publisher**, **tools** (count), **config** (non-secret
required config, shown separately from secrets), and **secrets**. The parser
reads these across many likely field names and derives publisher from a
namespaced ref when needed. Every row has an inline **raw** disclosure, and the
whole response has a **show raw mcp-find response** toggle — the untouched
payload is always one click away, so you can confirm which fields the gateway
actually returns before the parser is tuned.

If a column shows `?` (tools) or `—` (config unknown), it means `mcp-find`
didn't include that field for that server — confirmed live: `mcp-find` returns
only `name` / `description` / `long_lived` for most servers (config is sometimes
present, tool count and publisher never are). The richer per-server data comes
from `docker mcp catalog server inspect <catalog> <server>` on the host, a CLI
with no MCP tool.

## Inspect companion service (optional — enables tool count / publisher)

To surface that richer detail, run the small companion in
`contrib/gateway-admin-service/gateway_inspect_service.py` on the gateway host
(`general-ai`). It exposes one endpoint that runs `docker mcp catalog server
inspect` and returns its output. Stdlib only — no pip install.

```bash
# on general-ai, next to the gateway
GATEWAY_INSPECT_TOKEN=$(openssl rand -hex 16) \
  python3 gateway_inspect_service.py     # binds 127.0.0.1:8899
```

Bind it to localhost and firewall it to Foundry Router's host — it can run
docker commands on the host, so treat it as privileged (same trust tier as the
secrets companion). Then in **MCP > Gateway Servers → Inspect companion
service**, set its URL (e.g. `http://192.168.0.114:8899`) and token.

A per-row **Inspect ▾** button (a real toggle — click again to collapse) shows a
**structured summary** parsed from the inspect YAML: title, image, publisher
(the image namespace), and a **tool table** whose access badges come from each
tool's real MCP annotations — `destructiveHint: true` → red **destructive**,
`readOnlyHint: false` → amber **write**, `readOnlyHint: true` → **read-only**. A
tool with no annotation falls back to the name heuristic, shown as a dashed
**write?** so guesses are visually distinct from ground truth. The raw inspect
output stays available beneath the summary. These same annotation-based badges
flow into the **persona editor's tool checklist** wherever the server provides
them (via the MCP manifest), replacing the naming heuristic with real data.

### Config-set (servers needing non-secret config)

A server whose catalog metadata declares a **config schema** (e.g.
`playwright-mcp-server` needs a `data` location) shows a **set config ▾** form
generated from that schema — one input per property, typed per its JSON-schema
`type`. **Save config** calls the gateway's `mcp-config-set` tool (a backend
admin call, never persona-exposed) and shows the raw response, so a success or
failure is explicit. The tool's argument shape is read from its discovered input
schema; if it errors, the gateway's own message says what it expected. After a
successful config-set, **Add** should then succeed. Config is distinct from
secrets: a config-only server keeps **Add** enabled (it just fails until config
is set), whereas a secrets-requiring server keeps **Add** disabled.

The companion is deliberately inspect-only for now; it's the natural place to
later add the `secrets.env` write endpoint that would let secret-requiring
servers be added from the UI (currently out of scope, see above).
