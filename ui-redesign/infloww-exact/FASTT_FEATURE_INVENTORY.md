# fastt (FasttExtension) — User-Facing Feature Inventory
**Source of truth for the beginner-first UI redesign.** Read from real code: 33 automation modules
(`service/automations/*.py`), Settings tabs, the Automations hub, composers, vault surfaces.

**Architecture note that shapes the UI:** every automation is one `automation_rules` row =
`kind` + `trigger_json` (`every_seconds`/`daily_at`/`max_runs`) + `steps_json` (config payload) +
optional `quiet_hours_json`/`frequency_caps_json`. There is **no per-feature schema** — the UI hand-builds
a form per kind. Two UI surfaces expose the same automations twice: **Settings tabs** (rich per-account
forms) and **ReadyMadePanel** (a 14-tab "+New rule" hub). That duplication is a core redesign target.

Complexity: `TOGGLE` = on/off · `SIMPLE` = a few knobs · `COMPLEX` = many knobs / overwhelming.

## Group A — AI Selling (in-chat AI engine)
- **AI Chatter / Seller** `ai_chatter` — freestyle AI that chats + sells to sub-whale fans; supersedes `of_ai_chat`. always-on / `backup` after `sla_minutes`. **COMPLEX (~50 keys)**: enabled, mode, intent_only, offer_mode, max_lifetime_spend_cents (whale gate), tip-ladder base/step/cut/floor/cap, cadence, upsell_takes_over, rhythm_enabled, qualification_gate_enabled, engage_old_fans.
- **AI Chat (original)** `of_ai_chat` — replies once to any fan who spoke last until they "graduate" (>$1 / ≥10 msgs). **SIMPLE**: limit, max_replies, model.
- **Auto Convo** `autoreply` — ONE casual never-PPV keep-alive to a known low-spender silent 30m–2h. **SIMPLE→COMPLEX (~13 gates)**.
- **Deep Convo** `deep_convo` — fixed 4-step drill on ≥75%-profiled fans. **SIMPLE**.
- **Re-engage Buyers** `reengage_buyers` — one warm opener to cold buyers, hands to ai_chatter. **SIMPLE**: lookback_days, cold_hours, tone.
- *Offer Engine* `upsell.py` (helper) — qualification gate + ladder amount. TOGGLE (`qualification_gate_enabled`).
- *Human Rhythm* `rhythm.py` (helper) — reply-wait + sleep calendar. Ships OFF. TOGGLE.
- *Tip Ladder math* `tip_ladder.py` — adaptive ask + bundle sizing. TOGGLE.
- *Script Pack* `script_packs.py` — canned 1:1 lines by slot. TOGGLE (data). `bump_no_reply` slot is DEAD.

**UX smells:** `of_ai_chat` and `ai_chatter` are two names for one "AI answers DMs" capability. The ~50-field
`ai_chatter` JSON (most DARK/OFF) is the biggest UX liability. upsell/rhythm/tip_ladder/script_packs knobs all
leak through that one blob. UI split across ScriptsTab (705 lines) + UpsellerTab + AutoreplyTab duplicates
style/pricing.
**Simplify:** collapse to a **3-preset ladder** (Chat only → Chat + Sell → Aggressive Seller) over ~4 real
sliders (whale cutoff $, opening ask $, pushiness, reply timing); retire `of_ai_chat` name; rest in Advanced drawer.

## Group B — Outreach & Blasts
- **Mass Message** `send_mass_message` — core broadcast → OF /messages/queue, optional media + PPV. **COMPLEX**.
- **Ready-Made / Scheduled Mass** `mass_premade` — mass message + timer resend + auto-unsend. **COMPLEX**.
- **Online Nudge (generic)** `mass_nudge` — time-of-day blast to everyone online; per-fan 12h cooldown. **SIMPLE**.
- **Online Blast (scale)** `online_blast` — one OF list-broadcast to whole online set (100k scale). **SIMPLE**.
- **Online Nudge (personalized)** `nudge_online` (+`nudge_online_fire`) — diffs online set, gates, delayed per-fan nudge. **COMPLEX**.
- **Reply Funnel** `reply_mass_funnel` — walks repliers through a multi-step sequence ending in PPV. **COMPLEX**.
- **Follow-Up Drip** `send_followup` — up to 3 AI nudges at 26h/64h/256h silence. **SIMPLE**.
- **Schedule a Message** `scheduled_send` — true 1:1 deferred DM, survives tab close. **SIMPLE**.

**UX smells:** `mass_nudge` ↔ `online_blast` are near-duplicates (differ only by scale strategy); `nudge_online`
is a third "nudge online" variant; MassNudgeTab & OnlineBlastTab are near-clone forms. `mass_premade` = mass +
timer. `reply_mass_funnel` overlaps `send_followup`. `nudge_online_fire` is plumbing (never in UI).
**Simplify:** ONE "Broadcast" surface with an audience toggle (Online now / Everyone / Repliers) + "auto-resend/
unsend after N hrs" checkbox — auto-picks id-list vs list-broadcast by account size. `scheduled_send` = a "send
later" clock in the composer, not an automation.

## Group C — Fan Onboarding & Messages
- **Welcome New Subs** `send_welcome` — polls subscribe feed ~5min, sends AI persona/time-aware welcome. **COMPLEX**.
- **Templates / Snippets** (TemplatesTab) — saved snippets + Welcome editor + Follow-up step editor. **SIMPLE**.
- **Onboard Old Fans** `process_old_fans` — flags pre-AI subs, runs gen_info+apply_profiles. **COMPLEX** (one-time migration masquerading as automation).

**Simplify:** an "Onboarding" section: Welcome (one preview + regenerate + image on/off), Follow-ups, and a
guided "Import my existing fans" one-shot wizard (paste list → Flag / Profile / Push).

## Group D — Tips & Rewards
- **Tip Rewards** `tip_reward` — on inbound tip, sends free unseen vault media scaling with tip. Bundles 4 sub-features (image-reply, hot-teaser, bundle-scaling, tip-ask). `on_inbound_tip`. **COMPLEX (~30 keys)**.
- **Nudge Cold Buyers for Tip** `tip_request` — one global teaser + ask-for-tip caption. **SIMPLE** (buried in tip_reward config).
- **Make It Right** `make_right` — detects wronged fan (double-charge), auto apology + free content + refund flag. Ships OFF. **COMPLEX**.

**Simplify:** default tip_reward to "Tip → send N free pics scaling with tip"; split image-reply / hot-teaser /
tip-request into their own small cards. make_right = one trust toggle + an inbox of detected incidents.

## Group E — Fan Intelligence
- **Auto-Profiles** `gen_info` — LLM writes each qualifying fan's profile (nickname, bio, Q/Tease lines). **COMPLEX** (mostly invisible).
- **Apply Nicknames/Notes** `apply_profiles` — commits profile → nickname + note, opt push to OF. **SIMPLE** (really gen_info's commit step).
- **Export to Google Sheets** `push_to_sheets` — read-only export to one Sheet tab. **SIMPLE** (OAuth cliff).

**Simplify:** one "Auto-learn fans" toggle + read-only "last profiled / next" status; fold apply_profiles in as a
"write nicknames/notes to OnlyFans" checkbox; one "Connect Google Sheets" OAuth button.

## Group F — Content & Stories
- **Auto Posts** `auto_posts` — drips ready-made feed posts one at a time, opt auto-delete + re-post. **COMPLEX**.
- **Auto Stories** `auto_stories` — schedule posts random vault-folder photos as OF stories, auto-delete. `daily_at`+`max_runs`. **SIMPLE**.
- **Create Post** (PostComposer) — manual public feed post. **SIMPLE**.

**Simplify:** one "Post these, one every X hrs, delete after Y" form with a folder picker; resend/pools in Advanced.

## Group G — Vault AI
- **Vault-AI Describe** `describe_media` — Qwen3-VL describes undescribed items, DeepSeek fills caption/script, auto-applies. `every_seconds`. **COMPLEX**.
- **Daily Reminder Cards** `vault_daily_reminder` — operator-approved reminder cards (unseen media + line) via broadcast. **COMPLEX**.
- **Vault-AI Apply** `vault_ai_consume` — materializes approved items (PPV drafts, AI folders). Sends nothing. **SIMPLE (invisible)**.
- **Vault Manage** (VaultManagePanel) — OF-style folder+grid manager; AI describe/flag/dedupe in background. **COMPLEX**.
- **Vault Review modals** — Review (Folders/PPVs/Reminders), AI Folders, Flags Review, Duplicates, Disputes.

**UX smells:** VaultManagePanel toolbar exposes the whole pipeline at once; "Approved ≠ applied ≠ sent" is confusing;
vault_ai_consume is fully invisible; VaultAiTab mixes describe-engine + reminder-sender.
**Simplify:** one "Organize with AI" primary action runs the pipeline; individual jobs in Advanced. Split VaultAiTab
into "Auto-describe" (on/off + cadence) and "Reminders." Fold vault_ai_consume in as an "applied" status line.

## Group H — Housekeeping & Team
- **Auto-Unsend / Cleanup** `unsend_messages` — unsend via OF API: explicit targets OR policy sweep. **COMPLEX** (3 transports × 5 hour-windows).
- **Restrictions** (RestrictionsTab) — view/lift internal + OF fan restrictions. **SIMPLE**.
- **Chatters / Team** (ChattersTab, EmployeesTab) — invite/link staff, per-model access. **SIMPLE**.

**Simplify:** two unsend presets ("Auto-unsend unsold blasts after 8h" / "Clean up old DMs") + targeted unsend as a
per-message chat action. One "Add chatter" box; one unified restriction list with a status badge.

---
## Summary 1 — Enable/disable-only → TOGGLE CARDS (not wizards)
`gen_info` (Auto-learn fans) + `apply_profiles` (fold as "push to OF" checkbox) · `of_ai_chat` (retire → "Chat only"
preset) · `rhythm` (Instant / Realistic / Realistic+sleeps) · `send_followup` (on/off + "how pushy" slider) ·
`reengage_buyers` (on/off + tone) · `tip_request` (on/off + image + caption) · `make_right` (trust toggle + incident
inbox) · `vault_ai_consume` (status line, not a control) · `describe_media` (one "Auto-describe my vault" toggle) ·
`script_packs` (read-only line library) · `scheduled_send` ("send later" clock in composer).

## Summary 2 — Overwhelming/redundant → SIMPLE default + "Advanced mode" (ranked by pain)
1. **AI Chatter/Upseller** (~50-key JSON across 705-line ScriptsTab + UpsellerTab + AutoreplyTab) → 3-preset ladder + Advanced.
2. **PPV Library** (`ppv_send`, PPVLibraryTab **1425 lines**) → one-PPV-at-a-time wizard; multi-PPV grid behind Advanced.
3. **Nudge Online** (`nudge_online`, NudgeOnlineTab **834 lines**) → Simple = one toggle + one message; targeting in Advanced.
4. **Tip Rewards** (`tip_reward`, TipRewardTab **974 lines**, 7+ behaviors) → default reward; split the other 6 into cards.
5. **Three broadcast tools** (mass_nudge/online_blast/nudge_online near-clones) → one Broadcast surface + audience toggle.
6. **Funnels** (reply_mass_funnel + FunnelEditor raw steps) → visual "if reply → send → PPV" timeline; drop raw-JSON.
7. **Brain/persona** (BrainPanel ~2000px always expanded) → Persona+Model+Cap first; slots/welcome under Advanced.
8. **ReadyMadePanel** (14-tab emoji ribbon) + **RuleEditor** (per-kind morphing form + raw JSON) → ~4 buckets + expert mode.
9. **Composers** (MassMessage/Premade/Post/FunnelLaunch re-implement audience + toolbars) → one shared AudiencePicker + toolbar.
10. **unsend_messages** (3 transports × 5 windows) → two presets + a slider.

**Cross-cutting to factor out:** shared "Sound human" style (dup in Autoreply/Scripts/Upseller/RuleEditor), shared
pricing/ladder (Upseller/Scripts/TipReward/PPV), shared audience picker (5 places). AiStatusStrip = great read-only
diagnostics (11 chips) → default to state+reason, rest behind "details."

## Summary 3 — Natural sidebar GROUPS (collapses ~35 features + 20 tabs → 7 sections)
1. **AI Selling** — AI Chatter/Seller, Auto Convo, Re-engage, Brain/Persona, Scripts & pricing, Reply-timing *(preset ladder + Advanced)*
2. **Outreach & Blasts** — Mass Message, Broadcast (online/everyone/repliers), Funnels, Follow-ups, Scheduled sends *(one Broadcast surface)*
3. **Onboarding** — Welcome, Follow-up sequence, Templates/snippets, Import old fans
4. **Tips & Rewards** — Tip Rewards, Tip-Ask, Image-reply/Hot-teaser, Make It Right
5. **Fan Intelligence** — Auto-Profiles (nicknames/notes), Sheets export, fan restrictions
6. **Content & Stories** — Auto Posts, Auto Stories, Create Post
7. **Vault AI** — Vault Manage, Auto-describe, Reminder cards, Review/Duplicates/Disputes/Flags
*(footer)* **Team & Settings** — Chatters/Employees, Auto-unsend policy, transfer/audit

**Thesis:** ~35 real capabilities exposed via ~40+ config surfaces, many duplicated or near-clones. Highest leverage:
(a) preset ladders over raw-knob JSON for the 5 COMPLEX engines, (b) merge duplicate broadcast/composer/style/audience
surfaces, (c) turn the ~11 invisible-plumbing automations into toggle cards with sane defaults.

---

## Summary 4 — PRODUCTION DEFAULTS (what you actually run) — prefill the UI with these
> Read live from the VPS on 2026-07-22 (`account_ai_config` + `automation_rules`, cross-referenced against
> the code defaults). **These are the values to PREFILL** in the beginner-first UI so the mockup mirrors the
> real setup — not the ship-disabled code defaults. "Code" = fresh-account fallback; "PROD" = what's set on the
> ~10 active seller accounts (names withheld).
>
> ⚠️ **CORRECTED same day after the DB recovery — reference accounts = the graded vault + Ava** (per the operator).
> Where 4b–4f below disagree with this block, THIS block wins:
> - AI seller on Aria/Ava = **no `intent_only`** (full proactive selling), `sla_minutes: 7`, offers/day 7–8,
>   `resume_after_manual_hours: 0`, `stop_after_unpaid_rungs: 2`, velocity **$600/7d**, `force_ask` ON after 9,
>   qualification gate ON, smart pricing ON, **rhythm ON (no-sleep)**, post_buy_rung + gift ON, engage_old_fans ON,
>   unsend_expired_offer **OFF**. (The `intent_only:true` rows in 4b were other/pre-recovery accounts.)
> - style: humanizer/typos/nonnative ON **including ai_chatter**, and **strip_emojis: true**.
> - tip_reward: also hot_teaser ON (3 free → $15) + **teaser_convo ON, rungs $10×1/$30×3/$50×5**, cooldowns 6h.
> - autoreply: lifetime cap **$2,000** (not $20), `info_not_required: true`, day-gates 0; silence Aria 30–120m, Ava 25m–13h.
> - **Ava runs nudge_online ON** (rule every 60s; delay 1m/jitter 1m, quiet OFF, max_no_reply 99, info-line+image, no nudge-line).
> - mass_premade every **2–5h**, `online_only: true`; **mass messages cannot carry {name}** (one broadcast).
> - auto_posts every 3–7h; auto_stories every 4–7h × 2/run, live 13–15h; unsend policy text 4h/media 24h/priced 48h.
> - cadences: ai_chatter+of_ai_chat 1m · deep_convo 2m · autoreply 3m · funnels 2m · followup 45m (26/64/256h+image) ·
>   welcome 2–7m · gen_info 24h (Ava 45m) · apply_profiles 1h (Ava 24h) · PPV rules 2.3–7d each.

### 4a — Which automations are ON by default (the real "starter kit")
Every active seller account runs this SAME set — so a new account's UI should default these **ON**:
`ai_chatter`, `of_ai_chat`, `autoreply` (Auto Convo), `deep_convo`, `apply_profiles`, `gen_info`,
`send_welcome`, `send_followup`, `reply_mass_funnel`, `ppv_send` (one rule per PPV), `unsend_messages`,
`auto_stories`, `auto_posts`, `mass_premade`.
Default **OFF** (present but disabled on nearly all accounts): `nudge_online`, `mass_nudge`, `process_old_fans`.
On **some** accounts: `reengage_buyers`, `push_to_sheets`, `scrape_chats` (Ava/brain-owner extras).
→ Toggle cards: the 14 above ship **checked**; the 3 broadcasts ship **unchecked**.

### 4b — AI Seller engine (`ai_chatter` / `of_ai_chat`) — PROD vs code
| knob | code default | **PROD default (prefill)** |
|---|---|---|
| `mode` | `backup` | **`always`** |
| `intent_only` (Closer mode) | False | **True** |
| `offer_mode` | `both` | **`ppv`** |
| `sla_minutes` | 10 | **15** (Ava 7) |
| `max_lifetime_spend_cents` (whale cutoff) | 100000 ($1k) | **200000 ($2,000)** |
| `max_offers_per_fan_per_day` | 2 | **7** (Ava 4) |
| `min_fan_msgs_between_offers` | 4 | **2** |
| `resume_after_manual_hours` | 6 | **2** (1–3) |
| `stall_ttl_hours` | 6 | **2** (1–4) |
| `cadence_enabled` / `nudge_enabled` | False | **True** on the tuned account (Ava) |
| `model` (resolved) | `deepseek-v4-flash` | **`deepseek-v4-flash`** (unchanged) |
→ **Preset ladder anchors:** "Chat + Sell" (default) = always + intent_only + ppv + whale $2000 + 7 offers/day.
  "Aggressive Seller" = + cadence/nudge on, min_fan_msgs=2. "Chat only" = intent_only off, offer_mode none.

### 4c — "Sound human" style (`style_config_json`) — PROD INVERTS the code default
Code ships realism ON only for `ai_chatter`; **PROD runs it ON for the chat engines and typically OFF on ai_chatter itself.** Prefill the shared "Sound human" control **ON** for of_ai_chat / autoreply / deep_convo:
| flag family | code default | **PROD default (prefill)** |
|---|---|---|
| humanizer (`of_ai_chat`,`autoreply`,`deep_convo`) | OFF | **ON** |
| `typos_*` (same three) | OFF | **ON** |
| `nonnative_*` (same three) | OFF | **ON** |
| humanizer/typos on `ai_chatter` | ON | often **OFF** in prod |
| `strip_emojis` | False | mixed (leave **off**) |
| `factground_of_ai_chat` | True | **True** |
| `painful_texting` | True | **True** |

### 4d — Tip Rewards (`tip_reward_config_json`) — PROD
`enabled: True`, `dollars_per_image: 10`, `min_images: 1`, `max_images: 5`, `window_hours: 72` (= code).
PROD turns these **ON** (code default OFF): `image_reply_enabled: True`, `image_closer_enabled: True`
(`image_reply_cooldown_hours: 2–6`); `always_reward: True` on some. `tiers` are **populated** with real vault
folders at bases `[0, 1000, 10000]` (basic/mid/premium). Tip-ask `ask_enabled: True`, `ask_amount_dollars: null`
(ask without naming a price). → Prefill the Tip Rewards card ON with $10/image, image-reply + closer ON.

### 4e — Re-engage Buyers (`reengage_buyers`) — PROD (identical across accounts)
`{lookback_days: 3, cold_hours: 24, max_per_run: 25, guard_hours: 12, tone: "soft"}` → prefill exactly this.

### 4f — Broadcast / Nudge-online (`nudge_config_json`) — configured but rule OFF
PROD blob ≈ code default: `content_mode: "tease"`, `delay_minutes: 4`, `jitter_minutes: 3`, `gap_minutes: 5`,
`min_hours_between_nudges: 12`, `online_recent_minutes: 5`, `quiet_hours: [0,7]`, `require_welcomed: True`,
`max_no_reply: 3`, `max_per_tick: 25`. Nudge spend gate seen at `max_spend_cents: 20000` ($200).
→ Broadcast page: prefill these values but the master toggle **OFF** (matches prod: built, not running).

### 4g — Pricing / PPV
`price_min_cents: 300` ($3 OF floor), `price_max_cents: 20000` ($200 wire max) — code = prod.
Real funnel/PPV step prices seen: **$7, $24, $78**; adaptive whale ladder spans **~$10 (quiet) → ~$180 (hot)**.
Spend bands ×mult: whale ≥$100→2.0, mid $25–100→1.0, low <$25→0.7, free→0.5. Recency: hot<3d→1.15 … quiet→0.55.
→ Prefill PPV composer price field at **$24** (the modal median), library min/max $3/$200.

### 4h — Models / cost
`model = deepseek-v4-flash` (chat + ai_chatter); `gen_info` profiling uses **`deepseek-chat`**; vault-describe uses
`qwen3-vl-30b` (escalation `qwen3-vl-235b`). `daily_cost_cap_cents` code default 100 (¢) — prod runs higher; UI
should show a per-account cap, prefill generous. → Model dropdown default: **DeepSeek V4 Flash** everywhere.

> ⚠️ Operational aside (not UI): the live `chatterly.db` main file is **corrupt on disk** for the
> `account_ai_config` b-tree (fresh readers get "database disk image is malformed"; the running relay serves fine
> off cached pages, `-wal` is 0/checkpointed). This is the auto_vacuum-ptrmap failure mode from prior incidents —
> a restart of `fastt-relay` risks it loading the corrupt table. Worth a clean rebuild/backup-restore soon.
