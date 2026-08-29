# Wiring guide — connecting the Infloww-skin pages to the live relay

Every page in this folder is served by the relay itself at `http://127.0.0.1:<port>/infloww/<page>.html`
(same origin as the API — see the `/infloww` StaticFiles mount in `service/server.py`). Wiring a page
means: load real data on open, save real config on change, through `_shared/fastt.js`.

## The contract (follow exactly)

1. **Include the client** once, before the page's own script:
   ```html
   <script src="_shared/fastt.js"></script>
   ```
2. **All wiring goes inside `Fastt.ready(async () => { … })`** — it runs after the account picker
   resolves, so `Fastt.account()` is always set (localStorage `of_account_id`, shared with /ui).
3. **Never invent a payload key.** Ground every key in the relevant `service/*_api.py` (or the
   serializer in `service/server.py`). If a control has no backend, DO NOT fake a save —
   call `Fastt.staticBadge(cardEl, "STATIC DEMO")` on its card and leave the control inert.
4. **Reads**: `const cfg = await Fastt.get("/admin/ai-chatter-config")` — the client auto-appends
   `account_id=<current>` on `/admin/*` and the `X-Account-Id` header everywhere.
   On a MULTI-CREATOR page (combined inbox, group chat) the account is per-REQUEST, not the
   globally selected one: pass `{acct: id}` — `Fastt.get(path, params, {acct: id})`. Never
   hand-roll `noAccount:true` plus your own `X-Account-Id` header; that is this same rule
   copied into the page, and it drifts.
4b. **Agency-wide reads** (added 2026-08-29): for a card that is about EVERY creator rather
   than one — an agency total, a roster table — pass `{scope: "agency"}`:
   `Fastt.get(path, params, {scope: "agency"})`. It suppresses both the `account_id` param
   and the `X-Account-Id` header so the server's `clamp_account_filter(None)` resolves the
   roster from the SESSION. Rule 4's escape hatch (`{acct: id}`) only ever names a *different
   single creator*; before 4b existed there was no vocabulary for "all of them", and pages
   hand-rolled bare `noAccount:true` — the drift rule 4 warns about. Keep `noAccount:true`
   for routes with no account dimension at all.
   - ⚠️ **`scope` is a NAME, not a security boundary.** The gate is the relay's
     `_account_isolation_middleware`, which 401s anonymous `/admin/*`. Under
     `ALLOW_ANONYMOUS_ADMIN=1`, `clamp_account_filter` returns `None` — no WHERE clause,
     every tenant in the DB. Never exercise agency reads under that flag on multi-tenant data.
   - ⚠️ **"Agency" is the signed-in principal's roster, and it VARIES.** On the live box no
     principal owns all seven accounts, and three are co-owned, so four different operators
     see four different legitimate totals. Label such a tile for the viewer ("your 6
     creators"), never a bare "Total".
   - ⚠️ **Check the capability before you rely on it**: `Fastt.supportsScope("agency")`. A
     client that predates 4b silently ignores `scope` and renders a per-creator number under
     an all-creators label. A page whose numbers depend on agency scope must blank those
     cards rather than show the wrong ones — see `dashboard.js`'s boot guard.
5. **Writes**: `await Fastt.put("/admin/ai-chatter-config", body)` then `Fastt.saved()`;
   wrap in `try { … } catch (e) { Fastt.oops(e); }`.
6. **Automation-rule pages** (toggle cards): use `Fastt.rule(kind)` / `Fastt.upsertRule(kind,
   {is_enabled}, {payload, every_seconds})`. Rule rows serialize as
   `{id, account_id, kind, name, trigger, payload, quiet_hours, is_enabled, last_run_at, …}`.
7. **Money**: config JSON uses `*_cents` everywhere — display via `Fastt.fmtCents`, parse UI
   dollars → cents on save. Never send floats for cents.
8. **Do not change the look.** No new CSS frameworks, no layout rewrites; only add hidden hooks
   (ids/data-attrs), `Fastt.liveBadge(cardEl)` on wired cards, and JS at the bottom of the page.
9. **Keep pages self-contained** — relative paths only, no CDN. A page's wiring lives in a
   SIBLING `<page>.js` (`<script src="messages.js"></script>` at the same spot the inline block
   sat); the relay's `/infloww` StaticFiles mount serves it and sends `no-store`, so there is no
   cache to bust. Small parse-time blocks (<200 lines) stay inline. This keeps the `.html` file
   readable — before the split, `messages.html` was 4,253 lines, 3,341 of them one `<script>`.
10. **Vault media goes through `_shared/vault-media.js`** (`window.FasttVault`; load it after
    fastt.js, before the page script). It owns the OnlyFans vault-row PAYLOAD CONTRACT —
    `progressiveVideoSrc` / `isDrmVideo` / `videoPosterFrames` / `scrubUrl` — plus `hoverScrub`,
    the hover-to-preview session. Never re-derive "is this playable" or "which source do I use"
    in a page: four pages each had their own copy and they had already started to drift.
    What is NOT shared, deliberately: the pickers and managers themselves. They have opposite
    contracts (attach to a draft vs. write to OnlyFans) and their own grid markup, empty-state
    copy and DOM ids. One configurable mega-picker would trade duplication for option flags,
    which is the worse trade. `growth-vault-pro` also keeps its own hover session — it paints
    the tile's `backgroundImage`, the pickers paint `img.src`, so it is not the same code.
10. **Danger rail**: wiring may SAVE config and toggle `is_enabled` freely (this lane's DB ships
    with every rule disabled), but must never call an endpoint that immediately messages fans
    (`/send`, `/blast`, `run-now`, queue posts) without an explicit user click on an obviously
    labeled button in the UI.

## Where truth lives

| area | API module (all under `service/`) | key routes |
|---|---|---|
| accounts / picker | `server.py` | `GET /admin/accounts` |
| automation rules hub | `automation_rules_api.py` | `GET/POST /admin/automation-rules`, `PATCH/DELETE /admin/automation-rules/{id}`, `POST …/{id}/run-now`, `GET /admin/automation-kinds` |
| AI Chatter / Seller | `scripts_api.py` | `GET/PUT /admin/ai-chatter-config` |
| Auto Convo | `autoreply_config_api.py` | `GET/PUT /admin/autoreply-config` |
| Sound human | `style_config_api.py` | `GET/PUT /admin/style-config` |
| Brain / persona / models | `account_config_api.py` | `GET/PUT /admin/account-config` |
| Tip rewards + tip-ask + image-reply | `tip_reward_config_api.py` | `GET/PUT /admin/tip-reward-config` |
| Make It Right | `make_right_config_api.py` | `GET/PUT /admin/make-right-config` |
| Broadcast / nudges | `nudge_config_api.py` | `GET/PUT /admin/nudge-config`, `POST /admin/nudge-config/preview`, `PUT/POST /admin/mass-nudge/*` |
| Funnels | `funnels_api.py` + `funnel_stats_api.py` | `GET/POST/PUT/DELETE /admin/funnels*` |
| PPV library | `ppv_library_config_api.py` | `/admin/ppv-library*` |
| Vault AI | `vault_ai_api.py` + `scripts_api.py` | `/admin/vault-ai/*`, `GET/PATCH /admin/account-ai-config/vault-ai` |
| Smart lists | `smart_lists_api.py` | `/admin/smart-lists*` |
| Trial links | `trial_links_api.py` | `/admin/trial-links*` |
| Tracking links | `tracking_links_api.py` | `/admin/tracking-links*`, public `GET /t/{slug}` |
| Profile promotions | `promotions_api.py` | `/admin/promotions*` |
| Banned words | `banned_words_api.py` | `/admin/banned-words*` |
| Translate | `translate_api.py` | `/admin/translate*` |
| Settings transfer | `settings_transfer_api.py` | `/admin/settings-transfer*` |
| OF mirror (chats, fans, posts, vault, stories, queue, trials, promos…) | `server.py` + routers | `/api/of/v2/*` (445-route inventory: see `route_inventory.json` in the session scratchpad) |
| Revenue / ingest health | `server.py` | `/admin/rev/live`, `/admin/rev/drift`, `/admin/ingest/transactions/*` |
| Live events | `server.py` | `GET /events` (SSE) — `Fastt.sse(handler)` |

## Verification bar (what "wired" means)

A page counts as wired only if, against the lane relay with the lane DB:
- it loads with **zero console errors** and every wired card shows real data (or an honest empty state);
- every save round-trips: PUT/PATCH → re-GET shows the new value;
- every endpoint it references exists in the live route table (`/openapi.json`);
- controls with no backend are visibly badged STATIC DEMO, not silently fake.
