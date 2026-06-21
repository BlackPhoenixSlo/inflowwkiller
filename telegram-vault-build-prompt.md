# Telegram Vault Picker — Build Prompt

> Hand this entire file to the coding agent in the Telegram project. Companion files: **`telegram-vault-mockup.html`** (the visual target — pixel/state reference) and **`telegram-vault-spec.md`** (the full hardened design + rationale). Read both before writing code; this prompt is the executive build order, the spec is the source of truth for any detail not spelled out here.

## Goal

Build an operator-facing **Telegram Vault Picker**: a right slide-over panel inside the operator control panel where a chatter picks media from a curated library and sends it to one fan — **free** (copy) or **paid** (Telegram Stars native blurred unlock). It mirrors the muscle-memory of the existing OnlyFans VaultPicker (grid of placeholder tiles → select → send tray) but is re-grounded on Telegram's real primitives. Tiles are **simple placeholders** this branch — no real media render, no hover-scrub.

## Tech context

- **Delivery actor:** a Telegram **bot** (Bot API **≥ 7.x — hard floor**, required for `copyMessages` and `sendPaidMedia`), admin/member of a bot-owned **private forum supergroup** ("staging"), privacy mode **off**.
- **Content origin:** the catalog is **authored at ingestion**, not crawled. When content is added to staging, write a catalog row directly. **No userbot/MTProto backfill, no bot history crawl** — the Bot API cannot enumerate or fetch historical messages, so a crawl-based index renders an empty grid day one.
- **Operator UI:** a web / Telegram Mini-App control panel (the chatter's workspace). React + Tailwind, dark theme, Telegram-flavored. Reuse the OF VaultPicker DNA: right slide-over, `max-w-3xl`, portaled to `body`, dark backdrop.
- **State store:** a **local DB you own** holds the catalog and the per-fan sent/sold ledger. Telegram is a delivery + teaser funnel, **never the system of record** — every SENT/SOLD is reconciled from your ledger/provider, not trusted from a single Telegram event. You also own the media files in your own infrastructure.

### Catalog data model (author at ingestion)

```
catalog_item {
  item_id            -- one grid tile
  topic_id           -- MUTABLE metadata (forum message_thread_id); NEVER identity
  type               -- photo | video | gif | file | audio
  duration           -- nullable
  media_group_id     -- nullable; non-null = album
  message_ids[]      -- ORDERED list of source message_ids (album-aware; singleton = 1)
  file_id            -- delivery-bot-scoped; required for the PAID path
  file_unique_id     -- stable across bots/chats; dedupe + future correlation
}
```

```
fan_ledger {
  PRIMARY KEY (fan_id, source_chat_id, source_message_id)   -- conditional upsert (ON CONFLICT)
  state              -- SENT_FREE | PENDING | SOLD | REFUNDED | EXPIRED
  price_stars
  delivered_message_id
  telegram_payment_charge_id   -- idempotency key for the payment webhook
  ...timestamps
}
fan {
  fan_id             -- Telegram user_id from SIGNED Mini App initData
  of_id              -- via join/mapping table
  reachability       -- REACHABLE | UNREACHABLE (never /started or blocked)
}
```

**Two critical reference facts** (the load-bearing corrections — do not regress these):
1. One tile = a **set** of `message_ids` (album-aware). `copyMessage` copies exactly one message, so "one tile = one message_id" silently drops every album photo but the first.
2. `sendPaidMedia` takes `InputPaidMedia` **by `file_id`** (not by `chat_id`+`message_id`). The free path consumes `message_ids[]`; the paid path consumes `file_id`. The catalog row must carry **both**.

## Features in priority order

### P0 — Layout + pick + send (the whole point of this branch)
- Build the slide-over shell and all 6 regions (see Layout below).
- Grid of placeholder tiles, type filter, topic dropdown + topic quick-chips, infinite scroll (24/page, paged from the **local catalog** by message_id within topic).
- Selection as a `Set` of `item_id`; a `selectedMeta` Map caches each picked row (`topic_id, type, message_ids[], file_id, duration, placeholder`) so chips survive switching topic/type mid-pick.
- Send tray collapsed to **two buttons**: `Send Free` and `Send PPV`. `Send PPV` pre-fills last-used Stars price, one price for the whole set, account-default mechanism.
- `Custom…` expander holds the advanced surface (per-chip price, drag-reorder, previews stepper + free/paid divider, mechanism override, `protect_content` toggle). The common send **never opens it**.
- POST a send job; wire the bot execution path (copyMessage/copyMessages free; sendPaidMedia paid). Album-aware. Honor throttle + 429 backoff.

### P1 — Sent/Sold highlight + local DB
- Per-fan ledger writes (conditional upsert). Lazy-load per-fan delivery history behind a `cosmeticReady` gate so status decoration never blocks first paint.
- Render status rings + corner pills from the ledger (rules below).
- **SENT_FREE is written ONLY on the bot's real 200** (carrying the delivered `message_id`) — never optimistically on POST-accept.

### P2 — Payment / unlock
- `sendPaidMedia(currency=XTR, star_count, media=[InputPaidMedia by file_id])` as the **default and primary** paid path. No manual `sendInvoice → hold → successful_payment → copyMessage` choreography in the core flow.
- `pre_checkout_query` handler answered **within 10s** (mandatory or the payment fails).
- Idempotent `successful_payment` handler keyed on `telegram_payment_charge_id` (Telegram retries webhooks). Durable **outbox** so "mark SOLD" commits atomically.
- PENDING → SOLD on reconciled payment. PENDING → EXPIRED on operator-set TTL (Telegram emits no invoice-declined/expired signal). SOLD → REFUNDED on `refundStarPayment`.
- **Reconciler** (per mechanism): Stars = periodic `getStarTransactions`; external link = poll paywall order API. Self-heals missed webhooks — a single webhook is not enough.
- **External paywall LINK** survives only as an opt-in escape hatch behind `Custom…` (fiat, off-Telegram rail, ban-resilience). The generic Bot-Payments provider mechanism is **cut from v1**.

### P3 — Fan-side view (flagged, NOT built v1)
- Same grid component, role-flagged, can render a fan-facing Mini App storefront (fan taps a locked tile, `sendPaidMedia`/invoice unlocks 24/7 without a chatter online). Deep-link `t.me/bot/app?startapp=topic_<id>`. Strongest growth lever — captured as a follow-up branch, do **not** build now.

## Layout regions

1. **PANEL SHELL** — right slide-over (`max-w-3xl`) over a dark backdrop, portaled to `body`. Nested Esc: first Esc closes the preview lightbox if open, else closes the picker.
2. **REGION 1 HEADER** — `Library` title + Telegram glyph, source label (`@staging · forum`), close X. A **quiet health note** that surfaces only when something is wrong (`payment status stale · webhook unconfirmed Nm` or `send queue throttled · 40 queued`). **No chatter-facing Re-index button** (a quiet admin-only resync lives in settings).
3. **REGION 2 FILTER + CONTROLS** — chips `All / Photos / Videos / GIFs / Files / Audio` + **Topic dropdown** (each option = `topic name (count)`, includes an `Uncategorized` bucket for orphaned items) + Sort (Newest/Oldest by message_id/date).
4. **REGION 3 TOPIC QUICK-CHIPS** — plain most-recently-used topic chips for this fan (most-recent-first) + exactly **one** canonical `All` control. The OF three-tier ★/•/plain glyph legend is **dropped** for v1.
5. **REGION 4 MEDIA GRID** — responsive `grid-cols-3 sm:4 md:5 lg:6 xl:7`, `aspect-square` placeholder tiles, `2px` gap, infinite scroll. Tiles: a type glyph + **one** colored status ring + **one** corner pill; **no hover-scrub**. Album tiles show a stack/count glyph (e.g. `4`).
6. **REGION 5 SEND TRAY** — selected items become 64px chips with a remove X. Default = two buttons `Send Free` / `Send PPV` (PPV pre-fills last-used Stars price, one price/whole set). Caption field above the buttons. `Custom…` expander = per-chip price, drag-reorder, previews stepper + divider, mechanism override (Stars default | external Link), `protect_content` toggle (**hidden on the Stars/paid path**).
7. **REGION 6 FOOTER ACTION BAR** — `{n} selected · {k} free / {p} paid · ⭐{stars} net` (show **NET-of-platform-cut**, single unit — never a misleading `⭐X or $Y` dual display). Cancel + primary. Primary **disabled** when: 0 selected, target fan UNREACHABLE/not-started, or a priced chip violates the mechanism's integer/min constraint.

## State → badge visual rules (match the mockup exactly)

**ONE primary signal per tile** = the per-fan money/delivery state = ring color + one corner pill, OF-style precedence. Demote everything else.

| State | Ring | Corner pill / affordance | Notes |
|---|---|---|---|
| **DEFAULT** (no ledger row) | neutral `border-border` | none | dark square + centered type glyph + faint topic tag; video → duration pill bottom-right; GIF → `GIF` bottom-left; file → paperclip+filename strip; album → stack glyph w/ count |
| **SELECTED** | purple `border-accent` + `ring-accent/40` | white check in purple disc top-right | **overrides every status ring**. Click = toggle, double-click = add + open tray. On a SOLD/PENDING tile the `already sold / pending — re-send?` confirm resolves **first**, then the tray opens |
| **SENT_FREE** | yellow `border-warn` | `SENT` (or `FREE` for explicit preview) | written **only** on the bot's real 200 |
| **PENDING** | neutral-cautious **hollow/dashed** ring | hourglass + `⭐{n}` price | deliberately **closer to neutral than to a confident red** — an unconfirmed invoice is **not** a refusal. Has an operator-set TTL → EXPIRED. On the Stars path usually a brief in-flight spinner |
| **SOLD** | green `border-ok` | `⭐{n} PAID`, bright | dominant signal — chatter must not re-charge. Reconciled state only. If health note is active, SOLD tiles gain a `?` + `do not re-charge — status stale`. SOLD → REFUNDED reverts toward neutral |
| **EXPIRED** | gray | actionable again | PENDING aged past TTL |
| **UNREACHABLE FAN** | — (panel-level, not a tile ring) | faint disabled tint over the whole grid + inline banner `Fan must /start the bot first` + copyable `t.me/bot?start=<token>` link; REGION 6 primary **disabled** | first-class gate, more important than half the status rings. Tiles still render ledger history; no send possible |
| **PROTECTED** | — (demoted, **no corner badge**) | faint full-tile tint **or** tray-chip text `no-save` | honest label: a **screenshot-permeable deterrent, not DRM**. Hidden entirely on the sendPaidMedia path (paid media already non-forwardable) |
| **PREVIEW** | — | placeholder-only lightbox | click a small play/expand affordance (**not** the tile body) → centered placeholder card: type glyph, topic, message_id(s)+album count, kind+duration, current per-fan status w/ price, reachability, protect flag. Pixels stubbed (`media renders on the fan's device`). No `<video>`, no scrub calls. First Esc closes preview, second closes picker |

**CUT from v1** (no data producer — do not build): the OF **WALL / posted-publicly** blue-ring state (a private supergroup has no public feed; it would be decoration with no backing query); the **NOT-INDEXED / stale** tile state (catalog is authored at ingestion, so there is no crawl latency to surface). Either returns only when a real producer exists.

## Send flow

1. **PICK** — single click toggles; double-click = add + open lightweight tray. On a SOLD/PENDING tile the re-send confirm resolves before the tray opens (a fast double-click must not race the dialog).
2. **PRECHECK reachability** — if fan UNREACHABLE: dim grid, show onboarding-link banner, keep primary disabled. Build no send for an unreachable fan.
3. **DEFAULT decision** — `Send Free` **or** `Send PPV` (PPV applies last-used Stars price, one price, whole set, account-default mechanism). This is 95% of sends.
4. **(Optional) Custom…** — per-chip integer-XTR prices, drag-reorder, leading-free-teaser divider, optional external-LINK mechanism, `protect_content` (free/copy path only). Validation blocks integer/min violations; footer shows NET-of-cut.
5. **QUEUE & SEND** — POST `{ fan_id, items:[{ source_chat_id, message_ids[], file_id, price_stars, is_free_preview }], caption, mechanism, protect_content }` → per-bot serialized send queue under a **per-account lock + fan-lease** (no two chatters double-send the same fan).
6. **BOT EXECUTES** — FREE → `copyMessage` (singleton) / `copyMessages` (album, `message_ids[]`); PAID → `sendPaidMedia(currency=XTR, star_count, media=[InputPaidMedia by file_id])` (native blurred unlock, no manual invoice/copy dance). Throttle ~1/s per-chat + honor `retry_after` on 429 with backoff. A `copyMessage` 400 `message to copy not found` **hard-invalidates the catalog row** (dangling-pointer guard) and pulls the tile.
7. **STATE WRITES (pessimistic + reconciled)** — FREE writes SENT_FREE only from the real 200. PAID sits PENDING until `successful_payment` (idempotent on `telegram_payment_charge_id`, committed via outbox) → SOLD. Reconciler self-heals missed webhooks; refund flips SOLD → REFUNDED; PENDING auto-expires. **The grid never shows a confident color for an unconfirmed send.**

## Atomicity / idempotency / throttle / concurrency (do not skip — these are blockers)

1. Unique invoice payload encoding `(fan_id, source_message_id, nonce)`.
2. Idempotent payment handler keyed on `telegram_payment_charge_id` (dedupes webhook retries).
3. Durable **outbox** so "mark SOLD" commits atomically.
4. Single **per-bot send queue** with token-bucket throttle (well under **30/s global**, **~1/s per-chat**), honoring `429 retry_after` with backoff.
5. Per-account/per-bot send **lock** + **fan-lease** reuse so concurrent chatters can't double-send the same fan.
6. Ledger writes are conditional upserts (`INSERT … ON CONFLICT (fan_id, source_chat_id, source_message_id)`) → concurrent SENT/SOLD writes converge deterministically, not last-writer-wins.
7. Validate-on-send dangling-pointer guard (the 400 hard-invalidate above).

## Telegram API surface + caveats (Black/White-hat blockers baked in)

- **`copyMessage`** — copies exactly **one** message; origin hidden. Use for singletons.
- **`copyMessages`** (plural, Bot API ≥ 7.0) — for free albums via `message_ids[]`. **Without this, albums lose every photo but the first.**
- **`sendPaidMedia(currency=XTR, star_count, media=[InputPaidMedia])`** — the **native in-chat blurred pay-to-unlock overlay**; fan taps → pays Stars → the **same** message unblurs in place. (The premise that "Telegram has no native in-chat pay-to-unlock" is **false** — this is it.) `successful_payment` webhook = authoritative near-instant SOLD; this is the one place Telegram beats OF, whose ring lags a 5-min ingest. Takes media **by `file_id`/URL**, not by `(chat_id, message_id)`.
- **`pre_checkout_query`** — must be answered within **10s** or the payment fails.
- **`refundStarPayment`** — drives SOLD → REFUNDED.
- **`getStarTransactions`** — the Stars reconciler source.
- **`protect_content`** — blocks save/forward but **not screenshots**: a weak deterrent, **not DRM**. Redundant on the paid-media path (paid media is non-forwardable by construction) → hide the toggle there; expose only on the free/copy path, default **OFF**.
- **Forum topics / `message_thread_id`** — `topic_id` is **mutable metadata, never identity**. Every item stays addressable by `(chat_id, message_ids[])` even if its topic is renamed/closed/deleted; orphaned items fall into `Uncategorized`. A startup check confirms the source is a **forum** supergroup; else the taxonomy falls back to hashtag/caption parsing and the topic dimension collapses to one bucket.
- **Reachability** — a bot **cannot initiate** a chat. A fan who never `/started` (or blocked) the bot is **UNREACHABLE → 403**. Catch 403 on any send and **durably mark the fan unreachable** (reuse the existing `skip_unreachable_fan`/quarantine pattern). `fan_id` = the **signed Telegram `user_id` from Mini App `initData`** (verifiable), joined to the OF id.
- **No read receipts** — "Seen" is unobservable via Bot API and is **dropped from the product**.
- **No history access** — the Bot API cannot enumerate or fetch historical messages → the catalog must be authored at ingestion, never crawled.

## Open questions — confirm BEFORE coding

1. **GO / NO-GO on the whole product (answer first).** Telegram's Bot + Stars/Payments ToS broadly prohibit adult content via bots/Stars, and a ban vaporizes the staging supergroup (dangling message_ids), every fan DM thread, and any held Stars balance. This is migrating an OF (adult) operation onto a platform hostile to this use case. Is the intent **adult or SFW**? If adult, are you accepting the ban risk and shipping with the **external-paywall LINK as the surviving revenue rail**? Do you have a legal read for the target regions?
2. **Payment mechanism default — Stars vs external LINK.** Stars = best fan UX + deterministic SOLD, but platform cut, payout in Stars, integer-XTR only, most ban-exposed. External LINK = fiat, full control, ban-resilience (Black-hat's recommended default), but the fan leaves the chat and you own the webhook+reconciler. Spec defaults to **Stars** for UX; confirm the account default.
3. **Content backfill scope.** The indexer is killed in favor of authoring at ingestion → v1 only knows content flowing through the new ingestion path. Is the library being (re)ingested through the bot going forward, or is there an **existing library that needs backfill**? If backfill is required you must explicitly accept a one-time **userbot/MTProto pass (ToS/ban risk)** or re-ingest manually — there is no ToS-safe bot way to read history.
4. **Fan identity source.** Confirm `fan_id = Telegram user_id from signed Mini App initData`, joined to OF id via a mapping table. If fans are only ever plain bot DMs (no Mini App), where does the OF↔Telegram mapping come from, and how do cold (never-`/started`) OF fans get onboarded? This gates the entire addressable audience.
5. **Source chat type.** Is the source a **forum-enabled supergroup** (topics = `message_thread_id`, the folder mapping the whole picker assumes)? If a plain channel/non-forum group, confirm the hashtag/caption fallback taxonomy is acceptable (topic dimension collapses to one bucket otherwise).
6. **Fan-facing Mini App storefront (deferred).** Confirm the self-serve fan storefront (same grid, role-flagged, fans buy 24/7 without a chatter online) is a **follow-up branch, not a v1 expectation**.

---
*Build P0 → P1 → P2 in order; ship nothing in P3 this branch. Keep tiles as placeholders — no real media render, no hover-scrub. Match `telegram-vault-mockup.html` for pixels/states and defer to `telegram-vault-spec.md` for any unstated detail.*
