# Fastt

![Fastt preview](inflowkillerpreview.png)

Self-hosted creator dashboard for OnlyFans. A FastAPI relay that holds a
captured browser session, replays requests with proper TLS/headers via
`curl_cffi`, signs each call the way the OF web client does, and serves a
Next.js UI for messaging, mass-messaging, vault, queue, posts, stats,
realtime events, group chat, and per-account proxies.

```
   Next.js UI (app/)
        |  fetch /api/relay/...
        v
   FastAPI relay (service/server.py)
        |  OFClient — adds Sign + Time + X-Hash + ...
        v
   curl_cffi(impersonate=chrome)  ->  https://onlyfans.com/api2/v2/*
                  |
                  +-- (optional) per-session ISP / residential proxy
```

> **Disclaimer.** This project is an independent reverse-engineering of
> OnlyFans' public web API for personal / educational use. Running it
> against a live account may violate OnlyFans' Terms of Service. Use at
> your own risk. The project is not affiliated with or endorsed by
> OnlyFans.

---

## What's in the repo

| path | what it is |
| --- | --- |
| `service/` | FastAPI relay: signing, session bootstrap, OF client, WebSocket pump, SQLite store (`chatterly.db`) |
| `app/` | Next.js dashboard UI (chat, inbox, vault, group chat, setup, etc.) |
| `loginExtension/` | Chrome extension — one-click session capture (no proxy) |
| `noTeleportLoginExtension/` | Chrome extension — login routed through the relay's proxy so cookies mint on the same egress IP |
| `web/` | Legacy single-page UI (kept for parity; the Next.js app is the primary surface) |
| `Dockerfile`, `docker-compose.yml` | Container build + tunnel sidecar |

The SQLite database (`service/chatterly.db`), session blobs
(`service/sessions/`), proxy registry (`service/proxies.json`) and any
captured-cURL examples are **gitignored**. None of them are part of the
repo; they are created locally the first time you boot.

---

## Deploy to your own VPS

Put Fastt on a VPS behind a public URL. **No GitHub fork needed** — it clones this
public repo onto your server, installs Docker, builds, and starts the dashboard. A
brand-new single account needs **no secrets and no proxies**. Re-run the same line
anytime to update or to switch how it's exposed — it's idempotent.

> **Recommended host: Hostinger's _n8n_ VPS plan.** It ships with Traefik +
> Let's Encrypt already installed, which the free-https options (2 & 3) reuse. Any
> plain VPS (Hetzner, DigitalOcean…) works too — the default IP option needs
> nothing special, and `TUNNEL=1` gives https with no Traefik at all.

Open your VPS's terminal (Hostinger's in-browser **Browser terminal** works) and
paste **one** of these. Same command — a prefix just picks how it's exposed:

### 1. Just works — plain http on your server's IP  ★ start here

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/BlackPhoenixSlo/inflowwkiller/main/scripts/deploy-here.sh)
```

Zero config → `http://<your-ip>:3000`. Stable, instant, no domain. Session secrets
are auto-generated; no proxy is needed for one account. ⚠️ No TLS — the login token
travels in clear text, so move to option 2/3 for real use. If the page won't open,
allow the port: `ufw allow 3000/tcp`.

### 2. Free https with DuckDNS  ★ recommended

Make a free subdomain at [duckdns.org](https://www.duckdns.org), point it at your
server's IP, then:

```bash
DOMAIN=yourname.duckdns.org bash <(curl -fsSL https://raw.githubusercontent.com/BlackPhoenixSlo/inflowwkiller/main/scripts/deploy-here.sh)
```

Real Let's Encrypt cert, permanent URL, **$0**. _Needs Traefik on the box — i.e.
the n8n plan._

### 3. Your own domain

Add a DNS **A-record** for the host → your server's IP, then:

```bash
DOMAIN=app.yourdomain.com bash <(curl -fsSL https://raw.githubusercontent.com/BlackPhoenixSlo/inflowwkiller/main/scripts/deploy-here.sh)
```

### Bonus — throwaway https, no DNS, no Traefik

```bash
TUNNEL=1 bash <(curl -fsSL https://raw.githubusercontent.com/BlackPhoenixSlo/inflowwkiller/main/scripts/deploy-here.sh)
```

Random `trycloudflare.com` URL that changes on every restart. Works on **any** VPS.

> **Didn't get the n8n plan / no Traefik?** You're not stuck: option **1** (IP)
> always works, and **`TUNNEL=1`** gives https with no domain and no Traefik. You
> only need the n8n plan (or to install Traefik/Caddy yourself) for the _stable_
> https in options 2–3.

Other prefixes: `DEEPSEEK_API_KEY=sk-…` (bake in the **house** AI key — optional,
dashboard runs without it; note that the account you capture is owned by your
login and takes its key from **Setup → Your AI keys** in the dashboard, not from
`.env`, which is spent only on accounts nobody owns), `PORT=8080` (different
host port for option 1), `REPO_URL=` / `BRANCH=` (deploy a fork), `DIR=`
(install path).

### From your laptop, over SSH (alternative)

Drives the deploy from your machine (useful for copying up an existing database).
Requires `ssh root@<your-vps-ip>` with your key.

```bash
git clone https://github.com/BlackPhoenixSlo/inflowwkiller.git && cd inflowwkiller
./scripts/deploy-vps.sh root@<your-vps-ip>
```

The server boots with no OnlyFans login — connect one after deploy with the bundled
Chrome extension (Step 4 in the walkthrough).

- **[deploy/README.md](deploy/README.md)** — all exposure options at a glance.
- **[DEPLOY.md](DEPLOY.md)** — illustrated, click-by-click walkthrough.
- **[DEPLOY_HOWTO.md](DEPLOY_HOWTO.md)** — dense reference (secrets, migrations, rollback).
- **[CLAUDE.md](CLAUDE.md)** — orientation for an AI agent working in this repo.

---

## Quick start (Docker)

Requires Docker 20+ with the `compose` plugin.

```bash
git clone https://github.com/BlackPhoenixSlo/inflowwkiller.git
cd inflowwkiller

# First boot needs an empty proxy registry so the bind-mount has something
# to attach to. (You will fill it from the UI.)
echo '{"proxies": []}' > service/proxies.json

docker compose up -d --build
open http://127.0.0.1:8787/ui/
```

The UI loads but `/health` will be red — that is expected. There is no
captured session yet.

### Step 1 — capture a session

The easiest path is the Chrome extension shipped in this repo.

1. Load `loginExtension/` as an unpacked extension at `chrome://extensions`
   (toggle Developer mode, "Load unpacked").
2. Log into onlyfans.com in that browser.
3. Click the Fastt extension icon → **Capture**. The extension reads
   the live cookies + signing headers and posts them to the relay's
   bootstrap endpoint.
4. The relay parses the payload, downloads OF's signing chunk through
   your session, derives the signing rules, and writes a session file to
   `service/sessions/`. `/health` turns green.

If you need to bind the OF login session to a specific egress IP (i.e.
the same proxy the relay will use), use `noTeleportLoginExtension/`
instead. It routes the login itself through the configured proxy so the
session cookies mint on the IP that will later replay them.

### Step 2 — add proxies (optional but strongly recommended)

If you plan to host more than one account from this relay, each account
needs its own egress IP. Same WAN IP + multiple OF accounts ⇒
linked-account ban.

In **Setup → Proxies**:

- **+ add new proxy** — paste `host`, `port`, optional `user`/`pass`, and
  notes (city / ASN). Stored in `service/proxies.json`.
- **Test** — probes egress IP + geo through the proxy.
- **Assign** — binds the proxy to one captured session file. One session
  per proxy (1:1).

Datacenter proxies work for the initial wiring test but get soft-locked
by OF quickly. For production use ISP or static-residential proxies, one
per account's home city.

After assignment, reload `/health` — the JSON now includes
`proxy.label` and the live `egress_ip`. That is what OF sees.

### Step 3 — share the UI (optional)

`docker compose up -d` only listens on `127.0.0.1`. To hand out a public
URL without touching your router, set a share token and start the
Cloudflare quick-tunnel profile:

```bash
export SHARE_TOKEN=$(openssl rand -hex 24)
docker compose --profile tunnel up -d
docker compose logs -f tunnel        # wait for the trycloudflare.com line
```

Share the URL as:

```
https://<random>.trycloudflare.com/ui/?t=$SHARE_TOKEN
```

The token middleware accepts the `?t=` once and drops a 7-day cookie.
Without it every request returns 401.

> Anyone with the link is acting as your captured OF account. Treat the
> URL and the `SHARE_TOKEN` like a password.

The trycloudflare subdomain changes every restart because quick tunnels
are stateless. For a permanently-stable URL set up a named Cloudflare
tunnel (requires a Cloudflare account + a domain).

---

## Running without Docker (development)

```bash
# Relay
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
./venv/bin/uvicorn service.server:app --reload --port 8787

# Next.js UI (separate terminal)
cd app
pnpm install
pnpm dev          # http://localhost:3001
```

The UI talks to the relay through the rewrites declared in
`app/next.config.ts`. If you add a new relay route, add a matching
rewrite there — otherwise Next will 404 before the request ever reaches
the relay.

---

## On-disk layout

```
service/
  server.py              FastAPI app (relay + admin + WS fan-out)
  of_client.py           signed HTTP client (curl_cffi + proxy routing)
  of_signer.py           the OF signing algorithm, in Python
  of_ws.py               OF WebSocket pump (wss://ws2.onlyfans.com/...)
  session_bootstrap.py   extension + paste-curl bootstrap
  proxies.py             proxy registry (load/save proxies.json + probe)
  live_rev.py            tracks the current OF web revision
  db/                    SQLAlchemy models + migrations
  proxies.json           registry        (gitignored, bind-mounted)
  chatterly.db           local SQLite    (gitignored, bind-mounted)
  sessions/              captured sessions (gitignored, bind-mounted)
app/
  app/, components/,     Next.js 15 dashboard (chat, inbox, vault,
  hooks/, lib/, ...      group chat, setup, etc.)
loginExtension/          MV3 Chrome extension — one-click capture
noTeleportLoginExtension/ MV3 extension — login over the relay's proxy
web/                     legacy single-page UI
Dockerfile, docker-compose.yml, requirements.txt
```

### Backing up state

```bash
tar czf fastt-backup-$(date +%F).tgz \
  service/sessions service/proxies.json service/chatterly.db service/secrets
```

Drop the tarball back into a fresh clone and you are running again — no
re-bootstrap required (until the OF revision rolls and your stored
`x_of_rev` is stale, at which point one extension capture takes ~30 s).

---

## Day-2 operations

| task | how |
| --- | --- |
| OF rolled a new revision | Extension → Capture again |
| swap proxy on a session | Setup → Proxies → Assign |
| add a new account | Capture in a fresh browser profile for that account |
| revoke the public share link | `unset SHARE_TOKEN; docker compose --profile tunnel down` |
| update | `git pull && docker compose up -d --build` |
| nuke and restart | `docker compose down -v && docker compose up -d --build` |
| view live OF events | UI → Events tab (consumes `/ws/events`) |

---

## Scaling: sizing the relay to your VPS plan

Every concurrency cap is editable from the **Concurrency panel** the relay
serves itself:

    http://127.0.0.1:8787/lanes-panel

The relay port binds to loopback only, so on a VPS open it through an SSH
tunnel — `ssh -L 8787:127.0.0.1:8787 root@your-vps` — then browse the same
URL on your own machine.

**There is no login, anywhere.** The page is linked from here and from
nowhere in the app, and the API behind it (`/admin/lane-config`) answers
without a session **and without the `SHARE_TOKEN`**. Because the app proxies
`/admin/*` to the relay, that API is also reachable through the public app
port — on an internet-facing deploy, *anyone* can read and change the caps.
That is the intended trade for a friends-only box (retuning must never
require signing in). If your deploy is public and you don't want it, what
works depends on the mode:

- **Traefik/https modes** — block `/admin/lane-config` and `/lanes-panel` at
  the proxy.
- **Bare-port mode** — restrict who can reach the app port, but know that
  host `ufw` rules do **not** apply to Docker-published ports (Docker routes
  the traffic before ufw ever sees it — the same caveat applies to the
  `ufw allow` line in the deploy section above). What works: your VPS
  provider's edge firewall; an iptables rule in the `DOCKER-USER` chain,
  matched on the **container** port (`--dport 3001`) or on
  `-m conntrack --ctorigdstport <public port>` — Docker rewrites the
  destination port before that chain runs, so a rule keyed to the public
  port matches nothing; or republishing the app on `127.0.0.1` in
  `docker-compose.override.yml` behind your own proxy — but re-running
  `deploy-here.sh` regenerates that file and silently restores the public
  bind, so re-apply the edit after every re-run.
- **Quick-tunnel mode** — no firewall or path rule can help: cloudflared
  dials *out* to Cloudflare and hands visitors straight to the app over the
  docker network. Stop just the tunnel with `docker compose stop tunnel`
  when you don't need it — **not** `docker compose --profile tunnel down`,
  which tears down the relay and app too — or accept the trade.

The panel shows every knob's current value, its default, where the value came
from, and a history of past changes. Saved values apply on the next relay
**restart**, never live. The same caps also answer to environment variables —
but only ones passed into the relay container, i.e. named in the compose
`environment:` block (a bare `.env` line reaches nothing here) — and an env
var **pins** the knob: the panel shows it locked and cannot change it until
the env line is removed. If the page 404s, your build predates the panel — it
arrives with a future code sync, and `git pull && docker compose up -d
--build` picks it up once it has landed upstream. Until then the only
env-tunable caps in older builds are the four lanes that already existed
(`VIDEO_STREAM_CONCURRENCY`, `IMG_FETCH_CONCURRENCY`,
`STORYBOARD_BUILD_CONCURRENCY`, `VAULT_MEDIA_CONCURRENCY`); everything else
in this section is hardcoded in that code and there is nothing to tune.

The defaults are sized for a **2 vCPU** box. Suggested values by Hostinger
plan (on another provider, read the vCPU column):

| | KVM 1<br>1 vCPU · 4 GB | KVM 2<br>2 vCPU · 8 GB | KVM 4<br>4 vCPU · 16 GB | KVM 8<br>8 vCPU · 32 GB |
| --- | --- | --- | --- | --- |
| `STORYBOARD_BUILD_CONCURRENCY` — ffmpeg, the one CPU-bound lane | 1 | **2** (default) | 3 | 6 |
| `VAULT_MEDIA_CONCURRENCY` — queues, cheap to raise | 4 | **6** (default) | 10 | 12 |
| `cpus:` — only if you cap the container; always leave a core for the OS | don't cap | 1.5 | 3.0 | 6.0 |
| `mem_limit:` — a runaway backstop; exceeding it is an OOM-kill, not a slowdown | 2g | 3g | 4g | 8g |
| comfortable account count | 2–3 | ~10 | ~25–30 | 30+ needs code, not cores — see below |

**Scaled by accounts, not cores:** `HEALTH_ALL_CONCURRENCY` — set it to at
least your account count. A slow account holds a probe slot for its full
timeout, so the cap must exceed the number of *simultaneously slow* accounts
or perfectly healthy accounts start reporting as timeouts.

**Leave alone at any plan size:**

- `VIDEO_STREAM_CONCURRENCY` (6) and `IMG_FETCH_CONCURRENCY` (8) — sized to
  one operator's *browser*, not the fleet. Raise them with operator headcount,
  never with cores.
- `ACCOUNT_LANE_TOTAL` (5) and `ACCOUNT_LANE_BACKGROUND` (2) — the per-account
  OF ceiling. Measured blocked rate at the defaults is ~0; background is
  automatically clamped to total − 1.

**Why the table stops at ~30 accounts.** The automation lane
(`AUTOMATION_MAX_CONCURRENT_RUNS`, default 4, plus 2 bulk — both panel knobs)
saturates around ~30 accounts at typical volume, and the symptom is "the bot
replied late", not an error. You can raise it from the panel, but raise
`RELAY_EXECUTOR_THREADS` (default 64) with it — every OF call shares that one
executor, and an automation cap above what the threads can carry just moves
the queue somewhere with no counter. What no knob fixes: both ingest loops
walk accounts serially, and SQLite serializes all writes on a single writer
whatever the core count — Postgres, not cores, is that fix (the engine is
wired for a `postgresql+asyncpg://` `DATABASE_URL`, but the drivers are not
shipped: add `asyncpg` — and a psycopg driver for the sync init path — to
`requirements.txt` first, or the relay fails at boot). A KVM 8 buys ffmpeg
headroom and burst tolerance, not more write throughput.

**If you're upgrading because the box was throttled:** hypervisor CPU
throttles show as high `st` (steal) in `top`, and they can outlive the load
that triggered them. After the resize, confirm steal sits at ~0 before judging
whether the upgrade helped. (`docker stats` CPU percentages are not
trustworthy under steal — use `top -H -p <pid>`.)

---

## Security notes

- The relay is authenticated as your OF account. Anyone with read access
  to the UI or to `service/sessions/` can act as you on OF. Treat the
  sessions volume like a password file.
- `service/proxies.json` stores proxy credentials in plaintext. Both
  that file and `service/sessions/` are in `.gitignore`; keep them out
  of any copy you push to a remote.
- The cloudflared tunnel terminates TLS at Cloudflare's edge, then talks
  to the container over the compose-internal docker network. The
  `SHARE_TOKEN` middleware is the only thing standing between a leaked
  `trycloudflare` URL and full account access.
- The relay never falls back to direct egress if an assigned proxy
  fails. A `502 upstream` with a `proxy: {...}` block in `/health` means
  the proxy itself is down — fix or unassign before continuing.

---

## Contributing

Issues and PRs are welcome. Please do not include real session files,
proxy credentials, account IDs, fan IDs, or screenshots containing
those in bug reports — open a private channel for anything that could
identify a real account.

---

## License

MIT — see [LICENSE](LICENSE).
