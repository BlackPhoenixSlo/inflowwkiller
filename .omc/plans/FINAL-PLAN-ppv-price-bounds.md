# FINAL-PLAN: ppv-price-bounds — global Min/Max price clamp for PPV Library

**Task**: Add top-level Min/Max price fields to the PPV Library settings section. Defaults min $3 / max $200. Every price the library produces — per-tier cell prices, the reach_all broadcast base price, and feed-post prices (manual post-now + auto also_post_to_feed) — is clamped into [min, max] before sending.

**Scope tier**: Standard (5 files). Visionary stream: SKIPPED (self-decided — solution space for a config-driven clamp is tiny; user request fully specified). Iron Rule #6 not triggered.

## Prior institutional context (PRECEDENT)
- ppv_send double-fire gate + per-cell containment just deployed (a9777fd→b1c5e98) — do NOT touch the containment/ledger logic while editing run().
- Relay runs WITHOUT --reload — service edits need a relay restart locally; prod deploy (inflowwkiller) is NOT in scope.
- Tests are plain-assert, no pytest, temp-DB harness + FakeOF.
- JsonConfigModal allows RAW blob writes bypassing UI clamps → send-time clamping in run() is mandatory.
- MEMORY: OF rejects any priced message below $3.00 (verified live 2026-06-20) — hard floor 300¢ regardless of user input. OF PPV max $200.

## CHALLENGE (codex, read-only, 2026-07-09) — findings incorporated
1. CONFIRMED FLAW (fixed in this plan): clamping `base_price_cents` into USER bounds at save time is destructive — max $50 would permanently rewrite a $100 authored base; raising max later can't restore it. → Min/Max is a RUNTIME clamp only; authored bases keep the hard OF clamp [300, 20000].
2. Unsaved-UI drift: preview endpoints use STORED bounds while the grid shows draft bounds. → `/preview` body gains optional draft `price_min_cents/price_max_cents` (parity with how it already takes a draft `base_price_cents`). Post-now stays stored-config (same semantic as unsaved captions/media today).
3. Inconsistent threading risk: broadcast raw base leaks at ppv_send.py:740 (dry_run broadcast_price), :797-800 (send), :814/:820 (results rows); auto feed at :833 passes no config context. → ONE `bounds = price_bounds(cfg)` computed in run(), one clamped `bcast_cents` used for send+caption+dry_run+results; `post_to_feed(bounds=...)` passed from BOTH call sites.

## SECOND OPINION (codex, stacked, 2026-07-09) — P1s folded in
- P1.2 → Step 2: `/post-now` stamped `last_posted_ppv_id` onto the PRE-post blob (:396 load, :411-424 write-back) — a concurrent config save (e.g. new min/max) landing during the OF post gets clobbered. Fix: RE-load the latest blob after the post and stamp ONLY the marker.
- P1.3 → Step 1: `_defer_capped` payload (:587-589) drops `is_resend` — a capped monthly-resend fire comes back as a non-resend and enqueues a SECOND monthly one-shot (:840-846). Fix: thread `is_resend` through the deferred payload.
- P1.1 (deploy procedure, NOT this change): relay restart mid-run bypasses the double-fire gate (ledger finalizes post-run) — drain/disable ppv_send before any relay restart/rollback. Recorded for the deploy step; no code here.
- P2 noted: new bounds retroactively apply to already-pending jobs (config reloaded at execution — this is the desired "across all" semantic); roll back app+relay together; post-deploy canaries must use dry_run/force_ids (the dup-gate skips real re-sends inside the gap).

## Design decisions (CLARIFY — self-decided)
1. **Wire keys**: top-level `price_min_cents` / `price_max_cents` in `ppv_library_config_json` (cents, like `base_price_cents`). Absent → defaults 300 / 20000 (old blobs keep working).
2. **Hard bounds**: user min clamped into [300, 20000]; user max clamped into [min, 20000] (ordering enforced by lifting max). Silent clamp matches house style (pause_hours, ppv_caps).
3. **Runtime clamp only** (codex finding 1): stored `base_price_cents` never rewritten by user bounds — clamp applies where prices are COMPUTED.
4. **Clamped price sits EXACTLY on the bound** (no .99 restyle). Operator sets $3 → fan sees $3.00.
5. **Deliberate behavior change**: default floor 399→300 (user: "by default min is 3"). Only cells whose rounded price was < $3.50 change ($3.99 → $3.00). `case_price_floor` assert updated.
6. **Deploy**: laptop only; VPS deploy is a separate explicit step.

## Execution order
Step 1 first (helper is imported by 2). Step 3 parallel-safe. Step 4 after 1–2. Step 5 last.

---

### Step 1: ppv_send.py — bounds helper + clamp every computed-price site
Do: `_PRICE_FLOOR_CENTS` 399→300 (comment updated: exact-bound clamp, no .99 restyle at the floor). Add `price_bounds(cfg) -> (min_c, max_c)`: reads the two keys, defaults 300/20000, min→[300, 20000], max→[min, 20000]. `round_to_99(amount_cents, bounds=None)` (None → module constants). In `run()`: `bounds = price_bounds(cfg)` once, right after `_load_ppv`; per-cell price (:754) and dry_run plan (:730) via `round_to_99(..., bounds)`; single `bcast_cents = max(bounds[0], min(base_cents, bounds[1]))` used in the broadcast payload price AND caption tokens AND dry_run `broadcast_price` (:740) AND both results rows (:814, :820). `post_to_feed()` gains kwarg `bounds: tuple[int,int] | None = None` (None → module constants) clamping base (:360) — so `Post.price_cents` records the clamped value; run()'s also_post_to_feed (:833) passes bounds. `segment_preview(account_id, base_price_cents, bounds=None)` gains optional bounds; None → loads the account's cfg row and derives them (keeps old callers correct).
ALSO (2nd-opinion P1.3): `_defer_capped(account_id, ppv_id, release_at, now, *, is_resend=False)` — include `"is_resend": True` in the deferred payload when set, and pass `is_resend` at the call site (:679) so a capped resend never re-enqueues a second monthly one-shot when it finally runs.
Files: service/automations/ppv_send.py
Complexity: complex
Claims:
  - fact: hard constants at service/automations/ppv_send.py:86-87; round_to_99 clamp at :254-259
  - fact: per-cell send price at :754; dry_run plan at :730; broadcast raw `base_cents / 100` at :800 with results rows at :814/:820 and dry_run broadcast_price at :740; base_cents parsed unclamped at :656
  - fact: post_to_feed base clamp at :360; segment_preview clamp at :597; also_post_to_feed call `post_to_feed(account_id, ppv)` at :833
  - fact: `run()` loads cfg via `_load_ppv` at :637 — bounds derivable with zero extra queries
  - design_bet: optional kwargs on post_to_feed/segment_preview break no callers
    assumption: only callers are run() (:833), ppv_library_config_api (:328, :405), tests; _drive_ppv_post_sakai.py drives the endpoint handler, not post_to_feed directly
    validation_plan: `grep -rn "post_to_feed\|segment_preview" service/ app/` before edit; full test file after
    blast_radius: PPV-library pricing only; send_mass_message untouched
Acceptance criteria:
  - static: `cd service && python -c "import automations.ppv_send"`
  - unit: new bounds cases + ALL existing cases green (with the deliberate case_price_floor update)
Failure mode: a missed raw-base site (dry_run broadcast_price, results rows, Post.price_cents) reports/sends an out-of-bounds price.
Fallback: git checkout service/automations/ppv_send.py
Checkpoint: true
Depends on: —

### Step 2: ppv_library_config_api.py — validate/persist keys + thread bounds
Do: `_validate` parses both keys via `price_bounds` and ALWAYS emits them into the clean blob. `_validate_ppv` UNCHANGED semantics (hard clamp [300, 20000] only — now via the updated `_PRICE_FLOOR_CENTS`; authored bases never rewritten by user bounds). `/preview`: `_PreviewBody` gains optional `price_min_cents`/`price_max_cents` (draft-aware); handler builds bounds from body-if-present else stored blob, passes to `segment_preview`. `/post-now` (:405): derive bounds from stored blob, pass into `post_to_feed`. `/post-now/preview` (:461): clamp displayed base with stored bounds. Extend the existing import from automations.ppv_send (:29-39) with `price_bounds`. ALSO (2nd-opinion P1.2): the `last_posted_ppv_id` stamp (:411-424) must RE-load the latest blob via `_load_stored_config` AFTER the OF post and stamp ONLY the marker onto it — never write back the pre-post `stored` snapshot (a concurrent save during the post would be clobbered).
Files: service/ppv_library_config_api.py
Complexity: simple
Claims:
  - fact: _validate_ppv base clamp at service/ppv_library_config_api.py:81; top-level parse pattern (pause_hours/ppv_caps) at :151-180; _PreviewBody at :315-317; preview handler clamp at :327; post-now post_to_feed call at :405-407; post-now/preview clamp at :461
Acceptance criteria:
  - static: `cd service && python -c "import ppv_library_config_api"`
  - unit: case_bounds_validation green (absent→300/20000 emitted; min 100→300; max 50000→20000; min 5000 + max 1000 → max lifted to 5000; base_price_cents NOT rewritten by user bounds)
Failure mode: saved blob missing keys → run() silently uses defaults while UI showed custom bounds.
Fallback: git checkout service/ppv_library_config_api.py
Checkpoint: true
Depends on: 1

### Step 3: Frontend — fields at top of section + mirrored runtime clamp
Do: usePpvLibraryConfig.ts: add `price_min_cents?`/`price_max_cents?` to `PpvLibraryConfig`; `usePpvPreview` mutation arg becomes `{ basePriceCents, priceMinCents?, priceMaxCents? }` (draft bounds threaded). PPVLibraryTab.tsx: `priceMin`/`priceMax` state (dollars, defaults 3/200), hydrated in the effect (:199-220); new bordered block DIRECTLY under Library ON/OFF (:353): "Price limits — every PPV & post" with Min $/Max $ inputs (input clamp 3–200, save-side ordering by lifting max) + help line ("applies when sending: tier prices, the everyone-broadcast, and feed posts — your Base prices are kept as typed"). `buildConfig()` emits both keys; base_price_cents clamp stays HARD-bounds only (not user bounds) with `PRICE_FLOOR` moved 399→300 to mirror the backend, and the Base-price input's `min={4}` → `min={3}`. `roundTo99(cents, minC, maxC)` gains bounds params and EVERY call site passes the draft bounds — all FOUR: `priceRange` (:114), per-card range line (:645), `PriceGrid` (:1134/:1154), and `CaptionPreview.fill()` (:1099, the `{now}` token example price; thread bounds through its props at both call sites :707 and :751). Integration-gate finding: :1099 was the silent-drift site — an unclamped display price that tsc would never catch.
Files: app/components/settings/PPVLibraryTab.tsx, app/hooks/usePpvLibraryConfig.ts
Complexity: simple
Claims:
  - fact: mirror constants/functions at PPVLibraryTab.tsx:89-120; buildConfig base clamp at :324; hydration effect :199-220; ON/OFF toggle ends :353; range line :645; preview mutate :787; PriceGrid :1134; CaptionPreview fill at :1099 with call sites :707/:751; Base-price input min={4} at :591-601
  - fact: PpvLibraryConfig interface at usePpvLibraryConfig.ts:38-49; usePpvPreview at :108-116 (single mutate call site :787, grep-verified by Integration gate)
Acceptance criteria:
  - static: `cd app && npx tsc --noEmit` (no new errors)
  - static: `grep -n "roundTo99(" PPVLibraryTab.tsx` → every call site passes bounds (no default-arg fallbacks left)
  - e2e (needs Step 2, verified in Step 5): fields render 3/200 defaults; Save round-trips keys (GET blob shows them)
Failure mode: a roundTo99 call site compiles with default bounds and silently displays unclamped prices.
Fallback: git checkout app/
Checkpoint: false
Depends on: — for code (different files); the round-trip criterion is checked in Step 5 after Step 2

### Step 4: Tests — new cases + deliberate floor update
Do: (a) UPDATE case_price_floor → cheapest cell $3.00 with a comment on the deliberate default change; (b) case_price_bounds_clamp — 4-cell seed (mults from SPEND_BANDS/RECENCY_BANDS), base 2500, min 1000/max 3000 → whale:warm 4999→30.0, free:quiet 699→10.0, mid:hot 28.99 & low:cool 13.99 untouched; (c) case_broadcast_price_clamped — reach_all, base 2500, max 2000 → broadcast row price 20.0 (payload AND results row); (d) case_bounds_validation — api._validate: defaults emitted, hard clamps, ordering, base NOT rewritten; (e) case_feed_post_price_clamped — FakeFeedClient path with bounds → create_post price + Post.price_cents clamped; (f) dry_run bounds — plan prices + broadcast_price within bounds; (g) case_capped_resend_keeps_flag — ppv_caps hit on an `is_resend=True` fire → deferred ScheduledJob payload carries `is_resend: true` (P1.3 regression); (h) case_post_now_stamp_preserves_concurrent_save — write a modified blob (e.g. new price_min_cents) between config load and the stamp (monkeypatch the fake client's create_post to do the write) → after post-now, blob has BOTH the new key AND last_posted_ppv_id (P1.2 regression). Register all in the runner list (:907).
Files: service/tests/test_ppv_send.py
Complexity: simple
Claims:
  - fact: harness helpers at tests/test_ppv_send.py:96-147; runner list :907; floor assert 3.99 :191; broadcast case asserts 25.0 :577; FakeFeedClient :696
  - fact: multipliers (whale 2.0 / hot 1.15 / free 0.5 / quiet 0.55) at ppv_send.py:71-83
Acceptance criteria:
  - unit: `cd service && python tests/test_ppv_send.py` → ALL green
Failure mode: expected values mis-derived from the matrix.
Fallback: recompute from bands.
Checkpoint: true
Depends on: 1, 2

### Step 5: E2E verification scenario (the ONE flow)
Do: In-process scratch script (pattern of _drive_ppv_post_sakai.py, temp DB — never the prod fallback): PUT handler with min $10/max $30 + one PPV base $25 → blob carries both keys AND base stays 2500; run_once dry_run → all plan prices in [10, 30], broadcast_price == 25.0; preview handler with draft bounds → cells bounded. Then `cd app && npx tsc --noEmit`.
Files: — (scratch, not committed)
Complexity: simple
Claims:
  - strategic: in-process handler E2E instead of the running relay
    rationale: relay runs without --reload (memory) — the live process wouldn't have new code; in-process is deterministic
    alternatives_considered: restart relay + curl (slower, stateful); manual UI click-through (unrepeatable)
Acceptance criteria:
  - e2e: script exits 0; tsc clean
Failure mode: DB-URL footgun → accidentally touching prod DB (memory: CHATTERLY_DB_URL fallback).
Fallback: reuse the test harness temp-DB setup verbatim.
Checkpoint: true
Depends on: 1, 2, 3

## Rollback strategy
Single commit; `git revert` restores prior behavior. Keys are additive; blobs saved with them are harmless to reverted code.

## Blast radius
- PPV-library pricing only (per-cell, broadcast, feed posts). Only consumers of round_to_99/post_to_feed/segment_preview are ppv_send + config API + tests (grep-verified).
- Cheapest-cell default $3.99 → $3.00 (deliberate).
- No DB migration; no OF wire change (price stays dollars).

## E2E verification scenario
Step 5: save bounds → dry_run bounded + base preserved → preview bounded → tsc clean.
