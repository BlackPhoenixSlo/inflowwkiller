# Ultracode task — build the full fastt UI on top of the Infloww clone (beginner-first)

> Paste this whole block into a fresh **Fable + ultracode** session in this repo. It is self-contained.
> Author and run **workflows** for each phase (fan-out builders → adversarial verify → synthesize).

## Mission
Extend the existing Infloww-style HTML clone at `ui-redesign/infloww-exact/` into the **fastt** product UI.
Add **every** fastt feature as pixel-consistent, Infloww-styled pages — but **redesigned beginner-first**, not
mirrored. **Frontend only. No backend changes. Purely additive** (never edit anything under `service/`, `app/`, etc.).

The end state: a chatter/agency-owner can open this UI and set up AI selling, blasts, tips, onboarding, stories and
vault-AI **without drowning in options** — while power users can still reach every real knob behind an "Advanced" drawer.

## Read first (context — do not skip)
- `ui-redesign/infloww-exact/dashboard.html` — the canonical **SHELL** (black `#000` sidebar + top bar + all `<head>` CSS). **Copy it verbatim** for every new page; only swap the main content + set the active nav item.
- `ui-redesign/infloww-exact/PALETTE.md` — exact colors (accent `#4166f6`, canvas/cards `#262626`, borders `#333`, panels `#232323`, text `#fff`, muted `#8a8a8a`, green `#67d1ae`; font **Inter**).
- `ui-redesign/infloww-exact/BUILD_SPEC.md` — nav/href + shell-reuse conventions.
- `ui-redesign/infloww-exact/FASTT_FEATURE_INVENTORY.md` — **THE feature map** (Groups A–H, complexity ratings, UX smells, per-feature simplifications, the toggle-card list, the ranked "needs simple+Advanced" list, and the 7-section sidebar). This is your source of truth for *what* to build and *how* to simplify. **Summary 4 = the live PRODUCTION defaults** (read off the VPS) — the exact values every default state / slider / dropdown must be prefilled to. Treat Summary 4 as the spec for initial values.
- The **real components** per page: `app/components/settings/*`, `app/components/automations/*`, `app/components/vault/*`, `app/components/compose/*`, and `service/automations/*.py`. Read the specific one(s) for each page to ground the controls — **do not invent, do not copy raw**; reimagine them under the principles below.
- Existing already-built pages (match their look): `ai-chatter.html`, `automations.html`, `auto-stories.html`, `vault-ai.html`, `mass-nudge.html`, `funnels.html`, plus the 20 Infloww pages.

## Design principles (non-negotiable)
1. **Beginner-first.** Each page opens on ONE clear primary action and sane defaults. A first-time user should
   grasp it in seconds. Litmus: *"could someone set up AI selling in under 60 seconds?"*
2. **Progressive disclosure.** Every advanced/rare control lives behind an **"Advanced mode"** toggle / collapsible
   drawer — hidden by default, one click to reveal. Never show a 50-field JSON blob.
3. **Preset ladders for COMPLEX engines.** Replace raw knob-walls with a short preset ladder + ~3–4 real sliders.
   E.g. AI Chatter = **Chat only → Chat + Sell → Aggressive Seller** over {whale cutoff $, opening ask $, pushiness, reply timing}.
4. **Toggle cards for enable/disable features** (see Inventory Summary 1). `gen_info`, `of_ai_chat`, `rhythm`,
   `send_followup`, `reengage_buyers`, `tip_request`, `make_right`, `describe_media`, `vault_ai_consume`,
   `script_packs`, `scheduled_send` — a card with a big on/off and at most 1–2 obvious knobs. **No "add new
   automation → pick a kind" wizard for these.**
5. **Merge duplicates** (see Inventory Summary 2). Build these **shared components once** and reuse everywhere:
   - **AudiencePicker** (Online now / Everyone / Repliers + spend tier + exclude lists) — used by all blast/broadcast/compose surfaces.
   - **Composer toolbar** (message, vault media, PPV price + lockedText, emoji/template/GIF, "send later" clock).
   - **"Sound human" control** (humanizer + 3-bubble + typos) — one component, not duplicated across 4 tabs.
   - **Pricing/tip ladder** control — one component (Upseller + Scripts + TipReward + PPV share it).
   Collapse the three near-clone broadcast tools (`mass_nudge`/`online_blast`/`nudge_online`) into ONE **Broadcast** page.
6. **Consistency.** Reuse the shell, palette, Inter, `#4166f6`. Match card/table/toggle/filter vocabulary of the
   existing pages. Self-contained (inline SVG, Google Fonts ok, **no other network**). 1440×900 frame; content scrolls.
7. **UI only.** Realistic placeholder data; controls flip/select but do nothing. Never real fan handles/emails/explicit copy.
8. **Prefill from PRODUCTION, not code defaults** (see Inventory **Summary 4**). Every toggle's default state,
   every slider/price/dropdown's starting value must mirror what the real accounts run — read live from the VPS.
   Concretely: the 14 starter automations render **ON**, the 3 broadcasts **OFF**; AI seller preset defaults to
   **"Chat + Sell"** = mode `always` + Closer (`intent_only`) on + PPV offers + whale cutoff **$2,000** + 7 offers/day;
   the shared **"Sound human"** control renders **ON** for AI chat/Auto-Convo/Deep-Convo (typos + non-native included);
   Tip Rewards card **ON** at **$10/image** with image-reply + closer on; model dropdowns default **DeepSeek V4 Flash**
   (profiling `deepseek-chat`); PPV price field prefilled **$24**, library min/max **$3 / $200**; Re-engage =
   `lookback 3d / cold 24h / max 25 / soft`. When code default and prod differ, **prod wins for the prefill.**

## Target sidebar
Keep Infloww's OF-operations nav (OF Manager, Analytics, Messages Pro, Growth, Share for Share, Creators, Employees,
Settings) as the "OnlyFans operations" layer. **Replace the current single "AI & Automation" group** with the **7 fastt
sections** from Inventory Summary 3, each with sub-items:
1. **AI Selling** — AI Chatter (preset ladder), Auto Convo, Re-engage Buyers, Brain / Persona, Scripts & Pricing, Reply Timing
2. **Outreach & Blasts** — Mass Message, **Broadcast** (the merged one), Funnels, Follow-ups, Scheduled sends
3. **Onboarding** — Welcome, Follow-up Sequence, Templates, Import Old Fans (wizard)
4. **Tips & Rewards** — Tip Rewards, Tip-Ask, Image-reply / Hot-teaser, Make It Right
5. **Fan Intelligence** — Auto-learn Fans (gen_info+apply_profiles), Google Sheets export, Restrictions
6. **Content & Stories** — Auto Posts, Auto Stories, Create Post
7. **Vault AI** — Vault Manage, Auto-describe, Reminder Cards, Review (Duplicates/Disputes/Flags)

Give each real page a `<section>-<name>.html` file; wire every sidebar item; keep one canonical sidebar and **unify it
across ALL pages** (existing + new). Add a small "NEW" accent on the fastt sections.

## Pages to build (each: beginner default + Advanced drawer)
Build one page per leaf item above (≈28 new pages). For each, read its real component + its Inventory row, then:
- **beginner view** = the simplification named in the inventory (preset ladder / toggle card / one composer / one form);
- **Advanced drawer** = the remaining real knobs, grouped and labeled, revealed by the "Advanced mode" toggle.
The 6 already-built AI pages (ai-chatter, automations, auto-stories, vault-ai, mass-nudge, funnels) should be
**upgraded** to this beginner+Advanced pattern and slotted into the new sections (don't discard them).

## Build methodology (run as ultracode workflows)
- **Phase 0 — Scaffold (1 workflow):** author the canonical extended sidebar + the shared components (AudiencePicker,
  composer toolbar, "Sound human", pricing ladder, toggle-card, preset-ladder, advanced-drawer) as copy-paste HTML/CSS/JS
  snippets in `ui-redesign/infloww-exact/_shared/`. Update `dashboard.html`'s sidebar to the 7-section version (canonical).
- **Phase 1 — Build (1 workflow, fan-out):** one builder agent per page. Each: reads shell + palette + its real
  component + its inventory row + the shared snippets; builds beginner+Advanced; sets active nav; self-renders headless
  at 1440×900. Use `isolation: "worktree"` if agents would edit shared files concurrently.
- **Phase 2 — Adversarial verify (1 workflow, pipeline):** per page, a skeptic checks: (a) renders clean, shell
  consistent, 0 broken links, no external deps; (b) **is it actually simpler than the real tab?** (advanced-only stuff is
  hidden by default); (c) grounded controls match the real component. Kill/redo pages that fail.
- **Phase 3 — Unify + finish:** propagate the canonical sidebar to every page; update `ui-redesign/index.html` gallery
  to feature the full fastt app; final whole-app contact sheet + link check.

## Acceptance criteria
- Every feature in the inventory is represented; the 11 toggle-only features are **toggle cards** (no wizard); the 10
  overwhelming flows each have a **simple default + Advanced drawer**; the 3 near-clone broadcasts are **one Broadcast page**.
- Shared AudiencePicker / composer / "Sound human" / pricing ladder are built once and reused (no duplication).
- One canonical sidebar on every page; **0 broken internal links**; **no external deps** (Google Fonts only); every page
  renders at 1440×900 with a consistent black-sidebar shell.
- Litmus: a beginner can (1) turn on AI selling via a preset in <60s, (2) send a broadcast to online fans without opening
  Advanced, (3) enable tip rewards with one toggle. Power users can still reach every real knob under Advanced.
- **Prefill parity:** every default state / starting value matches Inventory **Summary 4** (prod), not code defaults —
  starter automations ON, broadcasts OFF, AI preset "Chat + Sell" ($2k whale, PPV, 7/day), "Sound human" ON for the
  chat engines, Tip Rewards ON ($10/img), model = DeepSeek V4 Flash, PPV $24 / $3–$200.

## Guardrails
- Never modify backend or app source (`service/`, `app/`, etc.) — read-only for grounding. All output goes under
  `ui-redesign/infloww-exact/` (+ `_shared/`) and the gallery.
- Keep everything self-contained and offline-openable (file://). No fabricated real data.
