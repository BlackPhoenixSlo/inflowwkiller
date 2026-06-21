# Telegram Vault — Spec

> Status: design / placeholder-tile branch. v1 deliverables are **layout + pick + send-highlight** only — tiles are simple placeholders (no hover-scrub, no real media render). This document is the single source of truth and supersedes the earlier draft.
> **Hard build floor: Telegram Bot API ≥ 7.x** (`copyMessages` plural, `sendPaidMedia`).

---

## 1. Overview & Goal

Re-imagine the existing OnlyFans **VaultPicker** (`app/components/chat/VaultPicker.tsx`, 1488 lines) as a Telegram-native content picker so a chatter's muscle memory carries over 1:1. A chatter opens a right slide-over, browses a grid of content tiles grouped by topic/type, multi-selects, and fires a **Send Free** or **Send PPV** in two clicks — exactly the rhythm of the OF vault, but delivering through a Telegram bot and charging in Telegram **Stars** (or an external paywall link as an escape hatch).

**Core inversion (the design's load-bearing premise):** *Source of truth is YOURS, not Telegram.* You own the media files in your own infrastructure and you own the payment record of truth. Telegram (bot + Stars + `copyMessage`) is a **delivery + teaser funnel**, never the system of record. Every `SENT`/`SOLD` state is reconciled from your ledger/provider, never trusted from a single Telegram event. A Telegram ban therefore degrades to "lose a funnel," not "lose the business."

**Premise correction vs. the earlier draft:** Telegram **does** have a native in-chat pay-to-unlock overlay — `sendPaidMedia`. The fan taps a blurred message, pays Stars, and the **same** message unblurs in place. This single fact collapses the draft's 3-way mechanism selector, its fictional `LOCKED` interim ring, and the `sendInvoice → hold → successful_payment-webhook → copyMessage` choreography. `sendPaidMedia` is the spine.

---

## 2. How it Maps to Telegram

| OF concept | Telegram equivalent | Notes |
|---|---|---|
| Vault media | A **catalog item** you authored at ingestion | One file you own + reusable Telegram `file_id` reference |
| Folder | **Forum topic** (`message_thread_id`) in a private staging supergroup | Topic is *mutable metadata, never identity* |
| Media library | A **bot-owned private staging channel/supergroup** | Bot is admin/member, privacy mode OFF |
| Send free | `copyMessage` (singleton) / `copyMessages` (album, ≥7.0) | Origin hidden |
| Send PPV | `sendPaidMedia(currency=XTR, star_count, media=[InputPaidMedia…])` | Native blurred unlock overlay |
| Fan paid (SOLD) | `successful_payment` webhook + reconciler | **Deterministic & near-instant — the one place Telegram beats OF** (OF's ring lags a 5-min payouts ingest) |
| "Posted to wall" (blue) | *(no equivalent — CUT from v1)* | Private supergroup has no public feed to be "also on" |
| Fan exists / reachable | Fan has `/start`ed the bot | First-class gating state (a bot cannot DM cold) |

### 2.1 Catalog is AUTHORED at ingestion — there is no indexer

A bot **cannot** enumerate a channel/topic's existing media (Bot API has no `getChatHistory`) and is delivered **no** historical messages. So a bot-only "index/crawl" yields an empty grid on day one for any pre-existing library, and a userbot/MTProto backfill is a ToS/ban vector. Therefore:

- When content is **added** through the ingestion path, we write a catalog row directly. No crawl. No "Re-index" button on the hot path (a quiet admin-only resync lives in settings).
- Because we author at ingestion, the draft's **NOT-INDEXED / stale-tile** state has no producer and is **CUT**.
- You own the `file_id`s forever — references survive even a banned supergroup (ban-resilience).

### 2.2 The reference asymmetry (important)

- **Free path** consumes `message_id`(s) → `copyMessage`/`copyMessages`.
- **Paid path** consumes `file_id`(s) → `sendPaidMedia` takes `InputPaidMedia` by `file_id`/URL, **not** by `(chat_id, message_id)`.

Both references live in the same catalog row, so either path is always serviceable.

### 2.3 "Show as soon as the fan opens"

Reachability gates everything. A fan who never `/start`ed the bot (or blocked it) is **UNREACHABLE** — a send 403s. `fan_id` is defined as the Telegram `user_id` from **signed Mini App `initData`**, mapped to the OF id via a join table. Cold OF fans are onboarded via a `t.me/<bot>?start=<token>` deep-link. (Mini App storefront where fans self-serve 24/7 is a flagged **follow-up**, see §11.)

---

## 3. Layout Regions (in order)

Right slide-over, `max-w-3xl`, over a dark backdrop, portaled to `document.body`. Identical chrome/DNA to the OF VaultPicker. **Nested Esc:** first Esc closes the preview lightbox if open, else closes the picker.

1. **REGION 1 — HEADER.** Title **"Library"** + Telegram glyph · source label (`@staging · forum`) · close `X`. A **quiet health note** (not a babysit pill) surfaces *only when wrong*: `payment status stale · webhook unconfirmed Nm` or `send queue throttled · 40 queued`. **No chatter-facing Re-index button** (moved to settings/admin) — indexing is invisible/background.
2. **REGION 2 — TYPE FILTER + CONTROLS.** Chips **All / Photos / Videos / GIFs / Files / Audio** (Telegram document/audio types extend OF's set) + a **Topic dropdown** (each option = `topic name (item-count)`, includes an **"Uncategorized"** bucket for orphaned items) + **Sort** (Newest/Oldest by `message_id`/date). No Re-index button.
3. **REGION 3 — TOPIC QUICK-CHIPS (simplified).** Plain most-recently-used topic chips for *this fan*, most-recent-first, plus exactly **one** canonical **"All"** control. The OF three-tier ★/•/plain glyph legend is **dropped for v1** (operator-curated small topic list doesn't need ranking glyphs; add only if chatters ask).
4. **REGION 4 — MEDIA GRID.** Responsive `grid-cols-3 → grid-cols-7`, `aspect-square` placeholder tiles, `2px` gap, infinite scroll (24/page) paging the **local catalog** (offset by `message_id` within topic). Tiles are placeholders this branch — type glyph + **one** colored status ring + **one** corner pill; **no hover-scrub**. Album items show a stack/count glyph (e.g. `4`). Per-fan delivery history loads lazily behind a **`cosmeticReady`** gate so status decoration never blocks first paint.
5. **REGION 5 — SEND TRAY (2-button reflex).** Selected items → 64px chips with a remove `X`. Default controls = **two buttons: `Send Free` / `Send PPV`** (PPV pre-fills the last-used Stars price, one price for the whole set, account-default mechanism). A caption field sits above the buttons. A single **"Custom…"** expander reveals advanced surface only when needed: per-chip price, drag-reorder, previews stepper + free/paid divider, mechanism override (Stars | external Link), and the `protect_content` toggle (hidden on the Stars/paid path).
6. **REGION 6 — FOOTER ACTION BAR.** `{n} selected · {k} free / {p} paid · ⭐{stars} net` — a **single** figure, the **NET-of-platform-cut** payout for Stars (never the draft's misleading `⭐X or $Y` dual display). Cancel + primary action. **Primary is DISABLED when:** 0 selected, the target fan is **UNREACHABLE**, or a priced chip violates the mechanism's integer/min constraint.

---

## 4. Media Grid & Tile Anatomy

Each tile is `aspect-square`, rounded, `2px` border. State is layered via **border color (ring) + one corner pill** — exactly the OF tile model (tokens verified in `VaultPicker.tsx`):

- `border-border` (neutral) · `border-warn` (yellow) · `border-ok` (green) · `border-accent` + `ring-accent/40` (purple, selected).

**Non-competing decorations** (never a second badge in the status corner):
- **Video** → duration pill bottom-right.
- **GIF** → `GIF` label bottom-left.
- **File** → paperclip + filename strip.
- **Audio** → waveform/▶ glyph + duration.
- **Album (multi-message set)** → a small **stack glyph with the item count** (e.g. `4`) so the chatter knows the tile is a set, not a single photo.
- **PROTECTED** → faint full-tile tint *only* (no corner badge — would collide at `grid-cols-7`).

**One primary signal per tile** = the per-fan money/delivery state (§7). Reachability and protect are demoted to non-corner affordances.

---

## 5. Multi-Select & Pick Behavior

- **Single click** on the tile body → toggle in/out of the selection (purple ring + white check disc top-right).
- **Double click** → express path: add **and** open the lightweight tray.
- Selection is a `Set<item_id>`. A **`selectedMeta` Map** caches each picked row `{ topic_id, type, message_ids[], file_id, duration, thumb-placeholder }` so chips survive switching topic/type mid-pick.
- A small **play/expand affordance** (not the tile body) opens the preview lightbox.
- On a **SOLD / PENDING** tile, the **"already sold / pending — re-send?"** confirm resolves **first**; only then does the tray open — so a fast double-click can't race the dialog.
- `SELECTED` overrides every status ring (pick state is always unmistakable).

---

## 6. Send Flow & Action Bar

```
job = {
  fan_id,
  items: [{ source_chat_id, message_ids[], file_id, price_stars, is_free_preview }],
  caption,
  mechanism,        // "stars" (default) | "link"
  protect_content   // free/copy path only; ignored on stars
}
```

1. **PICK** in the grid (single = toggle, double = express).
2. **PRECHECK reachability.** If the fan is UNREACHABLE, the grid dims, the onboarding-link banner shows, the primary stays disabled, and **no send is constructed**.
3. **DEFAULT DECISION (2 buttons, no spreadsheet).** Chatter clicks `Send Free` **or** `Send PPV`. `Send PPV` applies the **last-used Stars price** (one price, whole set) using the account-default mechanism. This is the path **95%** of sends take. The previews divider, per-chip pricing, mechanism override, and protect toggle are *not touched* unless "Custom…" is opened.
4. **(OPTIONAL) Custom…** For a curated multi-price set: per-chip prices (**integer XTR** — the unit is fixed because the mechanism is Stars), drag-reorder, set how many **leading** chips are free teasers via the divider, optionally switch to external **Link**, and (free/copy path only) toggle `protect_content`. Validation blocks any chip violating Stars integer/min constraints; footer shows **net-of-cut**.
5. **QUEUE & SEND.** POST the job into the **per-bot serialized send queue** under a **per-account lock + fan-lease** (reuse the existing W3 machinery — no two chatters double-send the same fan).
6. **BOT EXECUTES (album-aware, reference-correct).** Free items → `copyMessage`/`copyMessages`; paid items → `sendPaidMedia(currency=XTR, star_count, media=[InputPaidMedia by file_id])` (native blurred unlock, no manual invoice/hold/copy dance). Throttle honors ~1/s per-chat + `retry_after` on 429 with backoff. A `copyMessage` **400 "message to copy not found"** hard-invalidates the catalog row (dangling-pointer guard) and pulls the tile.
7. **STATE WRITES (pessimistic + reconciled).** Free → write `SENT` ledger row **only** from the bot's real 200 (carrying the delivered `message_id`). Paid → `PENDING` (in-flight spinner) until `successful_payment` fires → handled **idempotently** (dedup on `telegram_payment_charge_id`), committed via the **outbox**, flipped to `SOLD`. The reconciler self-heals missed webhooks; refund flips `SOLD → REFUNDED`; `PENDING` auto-expires to `EXPIRED` on its TTL. **The grid never shows a confident color for an unconfirmed send.**

---

## 7. State Model — Badges, Colors & Rules

**Precedence (highest first):** `SELECTED` > `SOLD` > `PENDING` > `SENT-FREE` > `DEFAULT`. One ring + one corner pill per tile.

| State | Trigger | Ring | Corner pill | Rule |
|---|---|---|---|---|
| **SELECTED** | In current pick set | `border-accent` + `ring-accent/40` (purple) | white check disc, top-right | Overrides every status ring. Click=toggle, dbl-click=add+tray. |
| **SENT-FREE** | Bot `copyMessage(s)` returned **200** at price 0 | `border-warn` (yellow) | `SENT` (or `FREE` for an explicit preview) | "This fan already got this free." **Written only on real 200**, never optimistically. |
| **PENDING** | `sendPaidMedia`/invoice awaiting payment | **neutral-cautious hollow/dashed** ring + hourglass | `⭐{n}` (asking price) | *Not* a confident red — an unconfirmed invoice is **not** a refusal. Operator-set TTL → `EXPIRED` (gray, actionable again), since Telegram emits no "declined/expired" signal. On Stars this is usually a brief spinner. |
| **SOLD** | Reconciled payment (webhook **or** reconciler) | `border-ok` (green) | `⭐{n} PAID`, bright | Dominant signal — **do not re-charge**. Shown only on reconciled state. If health note is active, SOLD tiles gain a `?` + "do not re-charge — status stale." `SOLD → REFUNDED` (loses green, reverts toward neutral) on `refundStarPayment`/provider refund. |
| **DEFAULT** | No ledger row for this fan+item | `border-border` (neutral) | none | Dark square, centered type glyph + faint topic tag + type-specific decoration (§4). |
| **UNREACHABLE** *(panel-level, not a ring)* | Fan never `/start`ed / blocked | — faint disabled tint over the **whole grid** | inline banner | "Fan must /start the bot first" + copyable `t.me/<bot>?start=<token>`. **REGION 6 primary DISABLED.** Per-tile rings still render history, but no send is possible. |
| **PROTECTED** *(affordance, not a state)* | `protect_content` on the free/copy path | faint full-tile tint OR tray-chip text `no-save` | none | Honestly labelled **screenshot-permeable deterrent**, never "DRM." Hidden entirely on the `sendPaidMedia` path. |
| **PREVIEW** *(lightbox)* | Click play/expand affordance | — | — | Centered **placeholder card**: type glyph, topic, `message_id(s)` (+ album count), kind+duration, current per-fan status (SENT/SOLD/PENDING + price), reachability, protect/no-save flag. Pixels deliberately stubbed ("media renders on the fan's device"). No `<video>`, no frame slideshow, no scrub. First Esc closes preview, second Esc closes picker. |

**CUT from v1 (no data producer / overclaim removed):**
- **WALL / POSTED-PUBLICLY** (blue ring) — a private supergroup has no public feed; it would be decoration with no backing query (the "check artifacts before theorizing" anti-pattern). Returns only when a real producer exists (a second indexed public channel correlated by `file_unique_id`).
- **NOT-INDEXED / stale** — catalog is authored at ingestion; there is no crawl latency to surface.
- **Generic Bot-Payments provider mechanism** — tripled payment/webhook/reconciliation surface for a placeholder branch.

---

## 8. Data Model (what we store locally, since Telegram won't tell us)

### 8.1 Catalog (authored at ingestion)

```
catalog_item {
  item_id            PK
  topic_id           nullable      -- MUTABLE metadata, never identity
  type               photo | video | gif | file | audio
  duration           nullable
  media_group_id     nullable      -- null = singleton
  message_ids[]      ordered       -- album-aware; FREE path delivers via these
  source_chat_id                   -- the staging supergroup
  file_id                          -- delivery-bot-scoped; PAID path delivers via this
  file_unique_id                   -- stable cross-bot/cross-chat dedupe identity
  created_at / sort_key (message_id)
}
```
> The single column change that fixes **both** the album-cardinality bug (one tile = an ordered set, not one `message_id`) **and** the paid-path file-reference gap (`sendPaidMedia` needs `file_id`).

### 8.2 Per-fan ledger (the OF `FanVaultEntry` analog)

```
fan_item_ledger {
  PK / ON CONFLICT key = (fan_id, source_chat_id, source_message_id)
  fan_id                           -- Telegram user_id (from signed initData)
  state              DEFAULT | SENT | PENDING | SOLD | EXPIRED | REFUNDED
  price_stars
  delivered_message_id  nullable   -- from the bot's real 200
  telegram_payment_charge_id nullable  -- idempotency key for webhook dedupe
  is_free_preview    bool
  sent_at / paid_at / refunded_at
  net_stars                        -- after platform cut, for footer
}
```
- **Conditional upsert** (`INSERT … ON CONFLICT (fan_id, source_chat_id, source_message_id)`) so concurrent SENT/SOLD writes converge deterministically (same pattern `llm_client` needed). No last-writer-wins.
- **`SENT` only on a real 200; `SOLD` only on a reconciled payment.**
- **"Seen" is dropped** entirely — Bot API has no read receipts; it is unobservable.

### 8.3 Identity join

```
fan_identity { telegram_user_id  ↔  of_fan_id }   -- one row per linked fan
fan_reachability { fan_id, state: STARTED | NOT_STARTED | BLOCKED, last_403_at }
```
On any 403, durably mark the fan unreachable/blocked (reuse `skip_unreachable_fan` / quarantine).

---

## 9. Payment & Unlock Mechanism — Decision

**Decision: `sendPaidMedia` (Stars) is the DEFAULT and primary paid-send path.** The manual `sendInvoice → hold → webhook → copyMessage` choreography is **deleted** from the core flow. The external paywall **LINK** (fiat) survives **only** as an opt-in escape hatch behind "Custom…". The generic Bot-Payments provider is **cut** from v1.

| | **Stars (`sendPaidMedia`) — default** | **External paywall LINK — escape hatch** |
|---|---|---|
| Fan UX | Native in-chat blurred unlock, same message unblurs in place | Fan leaves chat to a fiat checkout |
| SOLD signal | Deterministic `successful_payment` webhook (**beats OF**) | Poll the paywall order API |
| Currency | Integer XTR only (no cents) → lets the price collapse to one pre-filled field | Fiat, full control |
| Platform cut | Telegram takes a cut; payout in Stars | Yours |
| Ban exposure | Most exposed rail | Off-Telegram → survives a Telegram ban |
| Forwarding | Non-forwardable by construction → `protect_content` redundant | Needs explicit protect on copy path |

**Payment integrity (both rails):**
1. Unique invoice **payload** = `(fan_id, source_message_id, nonce)`.
2. `pre_checkout_query` answered **within 10s** (mandatory or the payment fails).
3. **Idempotent** payment handler keyed on `telegram_payment_charge_id` (dedupes Telegram's webhook retries).
4. **Durable outbox** so "mark SOLD" commits atomically.
5. **Per-mechanism reconciler** — Stars: periodic `getStarTransactions`; external: poll order API — so a missed webhook self-heals (mirrors the OF fan-drawer 5-min reconcile lesson).
6. `SOLD → REFUNDED` via `refundStarPayment` / provider refund.

**Throughput:** single per-bot send queue, token-bucket throttle **well under ~30/s global, ~1/s per-chat**, honor `429 retry_after` with backoff, per-account/per-bot lock + fan-lease reuse to block multi-chatter double-sends.

---

## 10. Leak / Privacy Protection

- `protect_content` blocks **save/forward** but **NOT screenshots** — a **weak deterrent, not DRM**. The UI labels it honestly; the operator must not over-trust it.
- On the **`sendPaidMedia` path it is redundant** (paid media is already non-forwardable by construction), so the toggle is **hidden** there.
- It is exposed only on the **free/copy** path, **default OFF** for free teasers (you *want* teasers shareable).
- Defense in depth lives at the model layer (§ Telegram Model #1): you own the files, Telegram holds reusable `file_id` references, so a leak/ban is a funnel loss, not a content loss.

---

## 11. The 5-Hats Review

**⚪ White (facts/gaps) — responded.** Flagged the draft's *factually false* "no native overlay" claim → adopted `sendPaidMedia`. Flagged "one tile = one `message_id`" silently dropping every album photo but the first, **and** that `sendPaidMedia` needs `file_id` not `(chat_id, message_id)` → catalog now stores `message_ids[]` + `file_id` + `file_unique_id`. Flagged that `fan_id` was undefined → defined as signed Mini App `user_id`. Flagged the WALL state has **no producer** → cut.

**🔴 Red (gut/squint) — responded.** The draft's REGION 5 made the chatter operate a 6-control pricing spreadsheet **while a fan waits** → collapsed to a **2-button reflex** (`Send Free` / `Send PPV`), advanced controls behind "Custom…", matching the real OF footer (one line + one Attach). Squint test: 8 states + a PROTECTED lock in the same corner as the status pill was unreadable at `grid-cols-7` → status model back to OF's 4-state precedence + non-competing affordances. "Don't make the chatter babysit infrastructure mid-sale" → no hot-path Re-index, quiet health note only when wrong.

**⚫ Black (risk/failure) — responded.** ToS/ban blast radius → **own the files + payment record**, external-link escape hatch, file references survive a banned supergroup. Optimistic green could show GREEN for a copy that flood-waited/403'd → **pessimistic + reconciled** state (SENT only on real 200, SOLD only on reconciled payment). No idempotency → handler keyed on `telegram_payment_charge_id`. No per-account lock / dangling-pointer guard → reuse fan-lease + spend-lock machinery + a `copyMessage` 400 invalidation. Userbot backfill is a ban vector → **killed** in favor of author-at-ingestion. Raised the **GO/NO-GO** product question (§12).

**🟡 Yellow (value/optimism) — responded.** Identified the one place Telegram is structurally **better** than OF: the Stars webhook gives **deterministic, near-instant SOLD** (OF's ring lags a 5-min ingest). Gold-plating findings (generic provider, dual `⭐X or $Y` display, ranking-glyph legend, REGION 5 spreadsheet, WALL/PROTECTED states) all trimmed for a placeholder-tile scope. Noted reachability is a hard opt-in wall OF doesn't have → surfaced as a real state.

**🟢 Green (alternatives/upside) — responded.** Strongest alt: a bot structurally **cannot** index history → adopted **author-at-ingestion**. Demanded `sendPaidMedia` as the default UX (native Stars unlock) → adopted, with the tension against Black resolved by keeping file-ownership + reconciler + external-link so the *business* isn't Telegram-dependent though the *happy path* is. Second alt: a **fan-facing Mini App storefront** (same grid, role-flagged, fans buy 24/7 with no chatter online) — genuinely the highest-leverage lever, **captured as a flagged follow-up** (§ below), not built in v1.

> **Flagged upside (NOT v1):** the same grid component, role-flagged, renders a fan-facing Mini App storefront. Deep-link `t.me/<bot>/app?startapp=topic_<id>`. Turns the chatter from a rate-limited manual sender into a curator and sidesteps per-fan flood limits. Recorded as the strongest growth lever for a follow-up branch.

---

## 12. Open Questions for the Human

1. **GO / NO-GO on the whole product (answer first).** Telegram's Bot + Stars/Payments ToS broadly prohibit pornographic/adult content via bots and Stars, and a ban vaporizes the staging supergroup (dangling `message_id`s), every fan DM thread, **and** any held Stars balance. This is a migration of an OF (adult) operation onto a hostile-to-this-use-case platform — **and this codebase already SCRUBS the word "telegram" as a contact-leak keyword**, so the org's own anti-platform-leak logic fights this product. Do you have a legal read for the target regions? Is the intent adult or SFW? If adult, are you accepting the ban risk and shipping with the external-paywall LINK as the surviving rail?
2. **Payment default — Stars vs external LINK.** Stars = best fan UX + deterministic SOLD, but platform cut, Stars-denominated payout, integer-XTR only, most ban-exposed. External LINK = fiat, full control, ban-resilient (Black-hat's recommended default) but the fan leaves the chat and you own that webhook+reconciler. Spec defaults to **Stars for UX**; the business-risk answer may be **external link**. Which is the account default?
3. **Content backfill scope.** The indexer is killed in favor of author-at-ingestion, so v1 only knows content that flows through the new ingestion path. Is the library being (re)ingested through the bot going forward, or is there an **existing** library that must be backfilled? Backfill has no ToS-safe bot path — it requires either a one-time userbot/MTProto pass (ban risk) or manual re-ingest.
4. **Fan identity source.** Confirm `fan_id` = Telegram `user_id` from signed Mini App `initData`, joined to the OF id via a mapping table. If fans are only ever plain bot DMs (no Mini App), where does the OF↔Telegram mapping come from, and how do cold (never-`/start`ed) OF fans get onboarded? This gates the entire addressable audience.
5. **Source chat type.** Is the source a **forum-enabled supergroup** (topics = `message_thread_id`, the "folder" mapping the whole picker assumes)? If it's a plain channel/non-forum group, confirm the hashtag/caption fallback taxonomy is acceptable (the topic dimension otherwise collapses to one bucket).
6. **Fan-facing Mini App storefront (deferred).** Confirm the self-serve fan storefront is a **follow-up** branch after this placeholder one ships, not a v1 expectation.

---

The full spec is above. To save it to the repo as `telegram-vault-spec.md`, that file does not yet exist on this branch — say the word and I'll write it.
