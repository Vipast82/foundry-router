# Re-authenticating Meridian from the UI (no SSH)

> **Important — read first.** `oauth source NULL` / "usage figures unavailable" is
> **not** an auth failure and re-login will **not** fix it. Confirmed live on
> Meridian 1.45.0: `/v1/usage/quota` returns 200 but `sources.oauth` and
> `utilization` are null while `/health` shows `loggedIn: true` — Claude works,
> Meridian just isn't reporting Anthropic's usage window. **Test Meridian Auth**
> reads `/health.loggedIn` for the real auth state and shows the usage gap
> separately. Only re-authenticate for a genuine **not logged in** / token-expiry
> state, described below.

When Meridian's Claude login genuinely expires (**not logged in** on the health
check), the host fix is `meridian profile login <profile> --headless`. You no
longer have to SSH in to run it: the **Backend Pool → Meridian re-authentication**
panel does it.

There are two tiers, because a stale login has two causes.

## Real usage window — including Claude **Pro** (the important one)

Meridian's own `/v1/usage/quota` returns null usage for non-Max plans (it *skips*
the OAuth usage fetch for non-claude-max profiles as of v1.57.1). But Anthropic's
`GET /api/oauth/usage` **does** serve Pro accounts — it returns the real
`five_hour` / `seven_day` utilization and Extra-Usage credits (the same numbers
the Claude app shows). Foundry pulls it directly through the companion:

1. Update the companion so it has the `/usage` endpoint — re-run the installer
   (`contrib/meridian-auth-service/install.sh`) or re-copy the script, then
   `systemctl restart meridian-auth`.
2. In **Backend Pool → Meridian re-authentication → Auth companion service**, set
   **usage profile** to your Meridian profile name (e.g. `victor`) and Save.

Foundry then polls `POST {companion}/usage` (which reads the profile's current
OAuth token from Meridian's credentials and calls `/api/oauth/usage`), and the
real window drives everything: the **Test Meridian Auth** readout, the brain's
usage-aware routing, and the **conservation guardrails** (`conserve_*_at`) — so
"use Claude until the window fills, then fall back to local" works on Pro too.
Companion env: `MERIDIAN_PROFILES_DIR` (default `/root/.config/meridian/profiles`).

## 1. Token expired → **Refresh token** (no companion, no SSH)

Meridian access tokens live ~8 hours. When only the token has expired, Foundry
asks Meridian directly: `POST {backend-origin}/auth/refresh` on every
anthropic-compatible backend. Click **Refresh token**; each backend reports
`refreshed` or the error. This is a plain HTTP call to Meridian — nothing else to
install. (A `404` means your Meridian build predates that endpoint — use tier 2.)

## 2. OAuth session dead (NULL) → **Re-authenticate** (companion service)

A refresh can't revive a *dead* credential — that needs a full OAuth sign-in,
which is interactive: the CLI prints a URL, you authenticate in a browser, then
paste a code back. Foundry can't answer that stdin prompt across the network, so
a small companion on the Meridian host does it.

### Install the companion (on the Meridian LXC/host)

`meridian` is a host CLI (you run it after `pct enter`), so the companion runs on
that host next to it — stdlib only, no pip install. **One-line install** (as root,
writes a systemd unit and starts it):

```bash
curl -fsSL https://raw.githubusercontent.com/Vipast82/foundry-router/main/contrib/meridian-auth-service/install.sh | bash
```

It prints the **URL + token** to paste below. (Manual alternative — run it in the
foreground to try it: `MERIDIAN_AUTH_TOKEN=$(openssl rand -hex 16)
MERIDIAN_AUTH_BIND=0.0.0.0 python3 contrib/meridian-auth-service/meridian_auth_service.py`.) Same trust model as the gateway inspect companion: it
runs host commands **and touches live Claude credentials**, so keep it
localhost-bound and firewall it to Foundry Router's host only. Run it under a
process manager (systemd/pm2) if you want it always available.

Then in Foundry: **Meridian re-authentication → Auth companion service →** set the
URL (e.g. `http://192.168.0.114:8898`) and the token, **Save**.

### Do the re-auth

1. Enter the **profile** (from `meridian profile list` — e.g. `victor`).
2. **Re-authenticate ▸** — the companion runs the headless login and Foundry
   shows the **sign-in URL**.
3. Open it, sign in to Claude, copy the code Claude shows.
4. Paste it into **paste code → Submit code**.
5. Foundry re-runs **Test Meridian Auth** — you should see `auth valid`.

### How it works

The login is a two-step exchange because of the paste-back:

- `POST /login/start` spawns `meridian profile login <profile> --headless`,
  scrapes the first `https://…` URL from its output, and **holds the process
  open** on its stdin prompt, returning a `session_id` + the URL.
- `POST /login/complete` writes your pasted code to that held process's stdin and
  returns the result. Sessions expire (`MERIDIAN_LOGIN_TTL`, default 600s).

The pasted code is a short-lived OAuth authorization code; it transits Foundry's
admin backend once and is not stored. Profile names and codes are constrained to
safe character sets before they reach the command line.

### Companion env vars

| Var | Default | Meaning |
|---|---|---|
| `MERIDIAN_AUTH_BIND` | `127.0.0.1` | bind address |
| `MERIDIAN_AUTH_PORT` | `8898` | port |
| `MERIDIAN_AUTH_TOKEN` | — | bearer token (strongly recommended) |
| `MERIDIAN_BIN` | `meridian` | CLI binary |
| `MERIDIAN_URL_TIMEOUT` | `60` | seconds to wait for the sign-in URL |
| `MERIDIAN_LOGIN_TTL` | `600` | seconds a pending login is held |
