# fastt page BUILD GUIDE (read fully before writing a page)

Every fastt page = the Infloww shell + fx components + your page's content. Follow exactly.

## Assembly (mechanical)
1. Read `ui-redesign/infloww-exact/dashboard.html` — it is CANONICAL.
2. Your page = dashboard.html **verbatim** with ONLY these changes:
   - `<title>fastt — <Page name></title>`
   - Everything inside `<main class="main">…</main>` replaced by your content.
   - Dashboard-specific CSS (`.earn`, `.metrics`, `.grid2`, `.chart-wrap`, `.myshifts`, `.clocked`, `.metric`, `.mi`, `.yaxis`, `.plot`, `.gl`) may be dropped; keep everything else in `<head>` byte-identical, then append the whole `<style id="fx-css">` block from `_shared/components.html`.
   - Keep the CANONICAL SIDEBAR JS block untouched (it auto-highlights this page — do NOT hand-set `active` anywhere in the aside).
   - Drop the dashboard's `#seg` segmented-control script; append the whole `<script id="fx-js">` block from `_shared/components.html` before `</body>`.
3. The `<aside class="sidebar">…</aside>` block must be **byte-identical** to dashboard.html's. Never edit it.
4. Add `.main{overflow-y:auto}` for your page via an extra style AFTER fx-css if your content is taller than 848px: `<style>.main{overflow-y:auto;padding-bottom:40px}</style>` — wide tables scroll in their own container, never the body.

## Content pattern (beginner-first — the whole point)
- Open with `.sec-head` (title + one-line explainer in muted text). Optionally an `fx-status` strip.
- **Above the fold = ONE primary thing**: a preset ladder, a toggle card, or a composer. A first-timer must "get it" in seconds and be done in <60s.
- Everything else goes in the **Advanced drawer** (`fx-adv`) — closed by default. Group knobs with `fx-adv-group` + `fx-grid2/3`. Label with plain words, not config keys (`Whale cutoff`, not `max_lifetime_spend_cents`).
- Use the fx kit ONLY — no new component styles unless truly page-specific (tables, timelines, grids are fine; match the shell's card/border/radius vocabulary).
- Explain consequences inline (`fx-note`) instead of docs: "The AI never messages whales…".

## Prefill = PRODUCTION (reference accounts: **the graded vault + Ava**, read live 2026-07-22 post-recovery)
| Thing | Prefill |
|---|---|
| Starter automations (ai_chatter, of_ai_chat/auto-convo, deep_convo, gen_info+apply_profiles, welcome, follow-ups, funnels, ppv_send, unsend, auto_stories, auto_posts, mass_premade) | ON |
| Broadcast/nudge_online | **ON for Ava** (every 1 min), off elsewhere · mass-nudge/import-old-fans OFF |
| AI seller preset | **"Aggressive Seller"**: always-on, proactive (NO intent gate on Aria/Ava) · qualification gate ON · smart pricing ON · rhythm ON (no-sleep) · post-buy rung + gift ON · engage old fans ON · force-ask after 9 fan msgs |
| AI seller knobs | whale $2,000 · 7–8 offers/day · 2 msgs between offers · backup SLA 7 min · resume 0h after human · stop after 2 unpaid rungs · velocity brake $600/7d · unsend expired offers OFF |
| Sound human | ON everywhere incl ai_chatter (voice+3-bubble, typos, non-native) · **strip_emojis ON** |
| Tip Rewards | ON · always_reward ON · $10/image · 1–5 · 72h · image-reply + closer ON (cooldown 6h) · hot-teaser ON (3 free, then $15) · **teaser-convo ladder ON: $10×1 / $30×3 / $50×5** |
| Auto Convo (autoreply) | ON · silence 30–120m (Ava 25m–13h) · lifetime cap **$2,000** · info_not_required ON · day-gates 0 |
| Mass pool (mass_premade) | repeat every **2–5 h** · **online_only** · **NO {name} in mass** (impossible — one broadcast) · exclude chatted-1h |
| Auto Posts | every **3–7 h** (Aria 7h) · Auto Stories every **4–7 h**, 2/run, live ~14 h |
| Cadences | ai_chatter 1m · of_ai_chat 1m · deep_convo 2m · autoreply 3m · funnels 2m · followup 45m (26/64/256h + image) · welcome 2–7m · gen_info 24h (Ava 45m) · apply_profiles 1h · unsend 1h (text 4h / media 24h / priced 48h) · PPV per-item 2.3–7d |
| Models | DeepSeek V4 Flash everywhere · profiling deepseek-chat · vault Qwen3-VL 30B (escalate 235B) · cost cap 100¢ |
| PPV | composer price $24 · library min $3 / max $200 · steps seen $7/$24/$78 |
| Welcome slots | morning_1/2, afternoon_1/2, evening, night |

## Hard rules
- **SFW placeholder data only.** Fan names like "Mike R." / "Alex T.", folder names like "Teasers", "Beach set", "VIP set". Never real handles, emails, explicit copy, or real vault folder names beyond the generic ones above.
- Self-contained: no network except Google Fonts (already in shell). Inline SVG only. No libraries.
- Frame is 1440×900. Verify with:
  `"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" --headless=new --window-size=1440,950 --screenshot=<scratch>/<page>.png "file://<abs path>"`
  then LOOK at the screenshot (Read it). Broken = fix before returning.
- Links: only to pages that exist or are in this build's page list. No `href="#"` except the Help-center shell link.
- Never touch anything outside `ui-redesign/`.
