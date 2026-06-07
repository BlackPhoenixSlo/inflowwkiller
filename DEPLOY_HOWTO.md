# Deploying inflowwkiller to a VPS

`scripts/deploy-vps.sh` does the whole deploy: installs Docker, clones this
repo on the VPS, seeds the bind-mount targets, writes `.env`, and brings the
stack up behind a Cloudflare quick-tunnel. This doc is the **what-to-change-
before-you-run-it** checklist.

> TL;DR: edit the `EDIT BEFORE RUNNING` block at the top of
> `scripts/deploy-vps.sh`, then `./scripts/deploy-vps.sh`.
>
> **New here?** [DEPLOY.md](DEPLOY.md) is a friendlier, illustrated walkthrough
> of the same steps — and you can paste it straight into Claude Code to have the
> deploy done with you. This file is the dense reference.

---

## 0. Prerequisites

- A VPS (Hostinger / Hetzner / DO …) reachable over SSH **with your public key**
  installed (`ssh root@<ip>` must work without a password).
- This repo pushed to GitHub (e.g. `github.com/<you>/inflowwkiller`, branch
  `public`). The deploy script clones from there — it does **not** rsync your
  laptop.
- A DeepSeek API key (house default LLM). Grok is optional.

---

## 1. Edit the config block in `scripts/deploy-vps.sh`

Only the block between the two `EDIT BEFORE RUNNING` rules matters:

| Variable | Set it to | Notes |
|---|---|---|
| `SSH_TARGET` | `root@<your-vps-ip>` | The Hostinger box. Placeholder `YOUR_VPS_IP` makes the script refuse to run. |
| `SSH_PORT` | `22` (or custom) | |
| `REMOTE_REPO_URL` | `https://github.com/<you>/inflowwkiller.git` | Placeholder `YOUR_GH_USER` makes it refuse to run. Use an HTTPS URL for a public repo; for a private repo use SSH (`git@github.com:...`) and make sure the VPS has a deploy key. |
| `REMOTE_DIR` | `inflowwkiller` | Clones to `~/inflowwkiller` on the VPS. |
| `BRANCH` | `public` | Must match the branch you pushed. |
| `SESSION_SECRET` | `openssl rand -hex 32` | **Set once.** Cookie signing key. Leave empty on later redeploys to keep the existing value. |
| `CHATTER_SESSION_SECRET` | `openssl rand -hex 32` | Same, for chatter cookies. |
| `DEEPSEEK_API_KEY` | your key | Required before any AI send. |
| `GROK_API_KEY` | your key | Optional / legacy. |
| `SHARE_TOKEN` | empty, or `openssl rand -hex 24` | Empty = share-gate OFF (friend-auth cookie is the only gate). Non-empty = require `?t=<token>`. |
| `COPY_STATE` | `0` | `0` = fresh box, capture OF sessions via the Chrome extension after boot. `1` = scp your laptop's `service/sessions` + `proxies.json` + `chatterly.db` up. See §4. |

The secret values are written to `~/inflowwkiller/.env` **on the VPS** (chmod
600) — never into the repo. Empty values are skipped, so a redeploy preserves
keys you set on a previous run.

---

## 2. Run it

```bash
./scripts/deploy-vps.sh
# or override the target inline:
./scripts/deploy-vps.sh root@1.2.3.4 --branch public --no-state
```

Flags: `--port N`, `--branch B`, `--state` / `--no-state` (override `COPY_STATE`).

Success prints a `https://<random>.trycloudflare.com` URL. Open it, sign in via
friend-auth. The trycloudflare subdomain changes on every restart — fetch the
current one with the command the script prints.

---

## 3. Secrets & how auth keys resolve

This repo intentionally ships **no** secret files. `service/.session_secret`,
`service/.chatter_secret`, `.env`, `credentials.json`, `token.json`, and all
`*.db` files are gitignored and were never committed.

At runtime the app reads `SESSION_SECRET` / `CHATTER_SESSION_SECRET` from the
environment (env **beats** the on-disk fallback). Set them in the deploy config
(→ written to the VPS `.env`) so the signing keys live only on the host. If you
skip them, the app generates random keys into the fallback files on first boot —
fine for a throwaway box, but then every container rebuild that wipes those
files logs everyone out.

---

## 4. Database, migrations, and "send state later"

- **Fresh DB:** the app builds its schema on boot via `create_all`. A brand-new
  `chatterly.db` comes up with the full current schema (automations tables
  included) — no migration step needed.
- **Existing prod DB → YOU MUST RUN MIGRATIONS.** `create_all` only creates
  *missing tables*; it does **not** add new *columns* to tables that already
  exist. So when you ship an existing prod DB (the "send state later" path
  below), run alembic against it or the new code will error on missing columns:
  ```bash
  ssh root@<ip>
  cd ~/inflowwkiller
  # alembic IGNORES DATABASE_URL — it reads CHATTERLY_DB_URL. Point it at the
  # SAME sqlite file the relay uses, or it silently migrates the wrong DB.
  docker compose exec -e CHATTERLY_DB_URL=sqlite:///./service/chatterly.db \
    relay alembic upgrade head
  docker compose restart relay
  ```
  This head brings: `webhook_config_json`, `autoreply_config_json` +
  the `autoreply_state` table, and `style_config_json`. The new code reads them.
- **`CHATTERLY_DB_URL` vs `DATABASE_URL`:** the app reads `DATABASE_URL`; alembic
  reads `CHATTERLY_DB_URL`. Always point **both** at the same DB.
- **Sending DB/sessions separately:** leave `COPY_STATE=0` for the
  code deploy, then push state up afterwards:
  ```bash
  scp service/chatterly.db        root@<ip>:~/inflowwkiller/service/chatterly.db
  scp -r service/sessions/accounts root@<ip>:~/inflowwkiller/service/sessions/
  scp service/proxies.json        root@<ip>:~/inflowwkiller/service/proxies.json
  ssh root@<ip> 'cd ~/inflowwkiller && docker compose restart relay'
  ```
  Wipe any `chatterly.db-wal` / `-shm` on the VPS before dropping a fresh `.db`
  in, or SQLite replays a stale WAL into it.

---

## 5. Running ALONGSIDE another stack on the same VPS ⚠️

`docker-compose.yml` uses fixed container names (`chatterly-relay`,
`chatterly-app`, `chatterly-tunnel`) and binds `127.0.0.1:8787` / `:3001`. If
this VPS already runs another copy of this codebase, you'll collide on both.
Either deploy to a **fresh VPS** (simplest), or before step 2 change, in
`docker-compose.yml`: the three `container_name:` values, the two host
port binds, and the `image:` tags. The Cloudflare tunnel needs no domain/port
config, so a fresh box is genuinely the least-effort path.

---

## 6. Redeploy / rollback

```bash
# redeploy current branch (idempotent — pulls, rebuilds, preserves .env + state)
./scripts/deploy-vps.sh

# roll back to a previous commit
ssh root@<ip> 'cd ~/inflowwkiller && git checkout <sha> && docker compose up -d --build'

# stop the public URL without tearing down the stack
ssh root@<ip> 'cd ~/inflowwkiller && docker compose --profile tunnel stop tunnel'
```

---

## 7. Post-deploy account config (do this AFTER migrations)

These are per-account runtime settings, not code — set them in the dashboard
after the stack is up. If the server runs a separate DB, they don't travel with
a code deploy.

- **Fan-messaging automations are live the moment you enable them.** Auto-reply,
  welcome, follow-up, nudge, and funnel automations send to real fans as soon as
  they're switched on. Confirm which account each one targets in **Automations →
  per-feature settings** before enabling. Posting/unsend-only automations are
  safe to leave on.
- **`info_not_required` ships OFF everywhere** — the "Info not needed" autoreply
  mode is opt-in. Flip it per-account (Automations → Autoreply) when you want a
  bot to reply before it has a complete fan profile.
- **`silence_min` defaults to 30 min** for accounts with no stored value. An
  account that already has a stored value keeps it — set it explicitly per
  account if you want to change it.
