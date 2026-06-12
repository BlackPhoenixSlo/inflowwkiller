# CLAUDE.md — orientation for an AI agent working in this repo

You're in **Fastt** (repo name `inflowwkiller`): a self-hosted creator dashboard
for OnlyFans. A FastAPI relay holds a captured browser session, replays signed
requests to OnlyFans via `curl_cffi`, and serves a Next.js UI for messaging,
mass-messaging, vault, queue, posts, stats, realtime events, and per-account
automations. SQLite store + an in-process automation executor that boots inside
the relay.

```
Next.js UI (app/) --fetch /api/relay/...--> FastAPI relay (service/server.py)
  --OFClient signs each call--> curl_cffi(impersonate=chrome) --> onlyfans.com
```

## Layout

| path | what |
|---|---|
| `service/` | relay: `server.py`, `of_client.py`, `of_signer.py`, `of_ws.py`, `session_bootstrap.py`, `automations/`, `db/` (SQLAlchemy models + alembic migrations) |
| `app/` | Next.js dashboard (chat, inbox, vault, setup, automations) |
| `loginExtension/`, `noTeleportLoginExtension/` | Chrome extensions that capture the OF session (no password typed into Fastt) |
| `scripts/` | ops: `deploy-vps.sh`, `start.sh`/`stop.sh`, `run-tests.sh`, `diag.sh`, `setup-caddy.sh` |
| `Dockerfile`, `docker-compose.yml` | container build + cloudflared tunnel sidecar (`--profile tunnel`) |

## Deploying to a VPS (the common ask)

Two entry points, both clone this public repo and bring up the Docker stack
behind a Cloudflare quick-tunnel (no fork needed unless deploying modified code):

- **On the VPS itself** (simplest — Hostinger browser terminal, no laptop/SSH):
  `scripts/deploy-here.sh`. Runs non-interactively — session secrets are
  auto-generated, no proxy is needed for a single account, and the DeepSeek key
  is optional (the dashboard boots without it). One-liner:
  `bash <(curl -fsSL https://raw.githubusercontent.com/BlackPhoenixSlo/inflowwkiller/main/scripts/deploy-here.sh)`
- **From a laptop over SSH:** `scripts/deploy-vps.sh` — the user edits one line
  (`SSH_TARGET` → `root@<ip>`); repo URL + branch `main` are pre-filled. Use this
  path when copying up an existing database.

A **fresh single account needs no secrets and no proxies** to get running.
DeepSeek is only required to switch on AI auto-messaging; proxies only matter
once multiple OF accounts share one server. Read **DEPLOY.md** (illustrated) and
**DEPLOY_HOWTO.md** (dense reference) before deploying. Redeploy = re-run the same
script (idempotent: pulls, rebuilds, preserves `.env` + data).

## Rules you must not break

- **Never commit secrets or state.** `.env`, `service/.session_secret`,
  `service/.chatter_secret`, `credentials.json`, `token.json`, every `*.db`,
  `service/sessions/`, and `service/proxies.json` are gitignored and contain
  live cookies / proxy creds / fan PII. They are written on the host only. Do
  not add them to a commit, a PR, or any doc — and never paste real account IDs,
  fan IDs, proxy creds, or session blobs into this repo (it is **public**).
- **Schema heals itself on boot — do NOT run alembic on a fresh DB.** `init_db`
  (`service/db/engine.py`) runs `create_all` + an additive nullable-column
  catch-up. A brand-new `chatterly.db` comes up fully formed. Running
  `alembic upgrade` on a self-healed DB can fail with duplicate-column. Run
  alembic **only** when bringing an *existing* DB that has data and is missing
  new columns — and note alembic reads **`CHATTERLY_DB_URL`**, not
  `DATABASE_URL`; point both at the same file.
- **Fan-messaging automations go live the instant they're enabled.** Auto-reply,
  welcome, follow-up, nudge, and funnel automations send to real fans on enable.
  Never enable one without confirming which account it targets. Posting/unsend
  automations are safe to leave on.
- **One stack per box by default.** Container names (`chatterly-relay`,
  `chatterly-app`, `chatterly-tunnel`) and host ports (`8787`/`3001`) are fixed —
  two copies on one VPS collide. Use a fresh VPS, or rename containers + ports in
  `docker-compose.yml` first.

## Gotchas

- The `trycloudflare.com` URL **rotates on every restart** — fetch the current
  one from the tunnel logs (the deploy script prints the command).
- **Adding a relay route needs a matching rewrite** in `app/next.config.ts`, or
  Next 404s before the request reaches the relay.
- In containers the relay runs **without `--reload`** — restart it
  (`docker compose restart relay`, or rebuild) after editing relay code.
- A captured session goes stale when OF rolls its web revision — one extension
  re-capture (~30s) fixes it. The relay never falls back to direct egress if an
  assigned proxy is down (`/health` shows the proxy block).

## Dev & tests

```bash
# native dev
./venv/bin/uvicorn service.server:app --reload --port 8787   # relay
cd app && pnpm dev                                           # UI on :3001

# tests (plain-assert scripts, not pytest)
./scripts/run-tests.sh
```
