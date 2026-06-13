# Deploy Fastt on your own server

One script, run it **on the server itself** (e.g. Hostinger's browser terminal —
no laptop, no SSH). It installs Docker, pulls the code, and starts the dashboard.
It's idempotent: re-run anytime to update or to switch how it's exposed.

> Recommended host: a **Hostinger VPS with the *n8n* template** — it ships with
> Traefik already installed, which the https options below reuse for free TLS.
> A plain VPS works too (the default IP option needs nothing special).

The entry script lives at [`scripts/deploy-here.sh`](../scripts/deploy-here.sh).

---

## Option 1 — Just works (plain http on your IP) ★ start here

Zero hassle. No domain, no DNS. Paste this and you get `http://<your-ip>:3000`:

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/BlackPhoenixSlo/inflowwkiller/main/scripts/deploy-here.sh)
```

- ✅ stable URL, instant, nothing to configure
- ⚠️ **no encryption** — the login/session token travels in clear text. Fine to
  get started; move to Option 2 or 3 for anything real.
- If the page doesn't open, allow the port: `ufw allow 3000/tcp` (or your
  provider's firewall panel). Pick another port with `PORT=8080 …`.

---

## Option 2 — Free https with DuckDNS ★ recommended

A real `https://` URL for **$0**, no domain purchase. DuckDNS gives free
subdomains that point at your server.

1. Make a subdomain at <https://www.duckdns.org> (e.g. `yourname`), set its IP to
   your server's IP.
2. Run:

```bash
DOMAIN=yourname.duckdns.org bash <(curl -fsSL https://raw.githubusercontent.com/BlackPhoenixSlo/inflowwkiller/main/scripts/deploy-here.sh)
```

The script finds the box's Traefik, auto-detects its network + cert-resolver, and
gets a Let's Encrypt cert for your subdomain. Permanent, encrypted, fast.
*(Needs Traefik on the box — i.e. the n8n template.)*

---

## Option 3 — Your own domain

Same as Option 2 but with a domain you own. Add a DNS **A-record** for the host
(e.g. `app.yourdomain.com`) pointing at your server's IP, then:

```bash
DOMAIN=app.yourdomain.com bash <(curl -fsSL https://raw.githubusercontent.com/BlackPhoenixSlo/inflowwkiller/main/scripts/deploy-here.sh)
```

---

## Bonus — throwaway https link, no DNS

For a quick demo without any domain. Gives a random Cloudflare URL that **changes
on every restart**:

```bash
TUNNEL=1 bash <(curl -fsSL https://raw.githubusercontent.com/BlackPhoenixSlo/inflowwkiller/main/scripts/deploy-here.sh)
```

---

## Quick reference

| Goal | Command prefix | URL | TLS |
|---|---|---|---|
| Just works | *(none)* | `http://<ip>:3000` | ❌ |
| Free + secure + stable | `DOMAIN=you.duckdns.org` | `https://you.duckdns.org` | ✅ |
| Own brand | `DOMAIN=app.yourdomain.com` | `https://app.yourdomain.com` | ✅ |
| Throwaway demo | `TUNNEL=1` | random `*.trycloudflare.com` | ✅ (ephemeral) |

Other env vars: `PORT=` (host port for Option 1), `DEEPSEEK_API_KEY=` (bake in an
AI key — optional, the dashboard runs without it), `REPO_URL=`/`BRANCH=` (deploy a
fork), `DIR=` (install path).

After it's up, open the URL and connect an OnlyFans account by capturing your
session with the Chrome extension in `loginExtension/` (see `DEPLOY.md`, Step 4).
