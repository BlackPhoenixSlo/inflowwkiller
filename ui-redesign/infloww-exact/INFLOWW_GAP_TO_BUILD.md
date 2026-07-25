# What Infloww has that fastt doesn't — the build list

Gap analysis, 2026-07-22. Left column = an Infloww surface (there's a clone page for most of them in this
folder, so you can see the target UI). Right column = what fastt actually has today (verified against
`app/app/**` routes + `app/components/**` + `service/automations/*`). "Build" = net-new work.

**How to read verdicts:** 🔴 nothing in fastt · 🟡 partial (extend what exists) · ✅ fastt already has an
equal-or-better version (do NOT rebuild). S4S was **dropped on purpose** (you removed it) — listed last for
the record, not to build.

---

## 🔴 Build — Infloww has it, fastt has nothing

### 1. Shifts & Time Tracking  *(biggest gap — the Dashboard already shows empty widgets for it)*
Infloww: chatters clock in/out, a shift calendar, timesheets, "hours worked," and the Dashboard's
**"My shifts"** + **"Current clocked-in employees"** cards. fastt tracks *who sent what* (X-Employee-Id →
`PerEmployeeTable`) but has **no concept of a shift or a clock**.
- Clone to match: `employees-shift.html`, plus the Dashboard shift cards.
- Build: a `shifts` table (employee, account, start/end, status), clock-in/out actions, a week calendar UI,
  an hours/timesheet rollup, and wire the two Dashboard cards to it.
- Why it matters: agencies pay chatters by the hour — this is table-stakes for team management.

### 2. Smart Lists  *(saved dynamic fan segments)*
Infloww: build a segment once ("spent >$100 AND active in 7d AND not messaged in 3d"), save it, reuse it as a
mass-message audience. fastt only *consumes* OF's native lists (`MassMessageComposer` include/exclude) — there's
**no UI to define/save a segment**.
- Clone: `growth-smart-lists.html`.
- Build: a segment builder (spend / recency / status / tag rules) → saved lists → surface them in the shared
  AudiencePicker used by mass/broadcast/funnels. High leverage: every blast gets sharper targeting.

### 3. Auto-follow / Auto-like
Infloww: automatically follow/like fans (or lists) to trigger OF's re-engagement. fastt has **no follow/like
automation** at all (`service/automations/` has none).
- Clone: `growth-auto-follow.html`.
- Build: an `auto_follow` automation (targets + daily cap + schedule) hitting OF's follow endpoint.

### 4. Free-Trial-Link generator
Infloww: mint OF free-trial links, name them, track redemptions/conversions. fastt: **not found** ("free trial"
only appears as a KPI qualifier in `PerModelKpiGrid`).
- Clone: `growth-free-trial-links.html`.
- Build: create/label trial links + a redemptions table.

### 5. Tracking / UTM Links + click analytics
Infloww: create trackable promo links, see clicks → subs → revenue per source. fastt: **not found**.
- Clone: `growth-tracking-links.html`.
- Build: link shortener/tagger + a click/conversion analytics table (source → clicks → subs → $).

### 6. Profile Promotion campaigns
Infloww: run promo campaigns for the creator's profile (cross-promo slots, discounts, campaign tracking).
fastt: **not found**.
- Clone: `growth-profile-promotion.html`.
- Build: campaign objects (offer + audience + window) + performance readout. Lower priority.


---

## 🟡 Extend — fastt has part of it

### 10. Fan Reports / fan analytics page
Have: per-fan lifetime spend + activity in `FanDrawer`, top-tippers in `TopTippersCard`. Missing: a **dedicated
fan-analytics page** (cohorts, spend distribution, churn, LTV by segment) and fan-list analytics.
- Clone: `analytics-fan-reports.html`. Build: a fan-reports route reusing existing spend data.

### 11. Role-based permissions (named roles)
Have: account-scoped + vault-folder-scoped access (`ChattersTab` `AccessEditor`) + founder impersonation
(`/admin/manage`). Missing: named roles (Owner/Manager/Chatter/VA) with permission sets.
- Build: a role layer over the existing access model. Medium effort.

---

## ✅ Already covered — do NOT rebuild (fastt equals or beats Infloww)

| Infloww surface | fastt equivalent |
|---|---|
| Creator earnings dashboard (subs/posts/msgs/tips/streams) | `CompetitorReports` on `/` (real data; styled as a parody of the competitor) + `TotalRevenueChart` |
| Message / creator reports | `/messages` (PPV·tips·posts·feed·top-posts + CSV) and `/stats` |
| Employee sales report | `PerEmployeeTable` on `/stats` (table today → see #8 to gamify) |
| Creator / account management + proxy config | `/setup`: `AccountsTable`, `ProxiesTable`, `PasteCurlCard`, `KeysCard` |
| Growth → **Smart Messages** (auto-message rules) | the whole Automations engine (`/automations`, 14 modules) — far beyond Infloww |
| Growth → **Vault Pro** | **Vault AI** (`/vault`: describe/flags/dupes/disputes, PPV Library) — superior |
| Growth → **Scripts** | AI Chatter + `script_packs` + Scripts & Pricing |
| OF Manager (embedded OF) | fastt embeds the OF web app too |
| Notifications | `NotificationBell` / `NotificationToaster` (real-time SSE) |

**fastt-only (no Infloww equivalent):** AI 1:1 chatting/upselling, Auto Convo, Vault-AI describe/PPV pipeline,
Mass Funnels reply-walker, Tip Rewards/Ladder, gen_info profiling, Make-It-Right resolution, human-rhythm layer,
unified Inbox + Roster, Group chat (8 panes), MoneyRail, attribution + ingest-health, audit log, cURL capture.

---

## ⛔ Dropped on purpose
**Share for Share (S4S)** — creator cross-promo (discover/requests/schedule). Removed from the clone at your
request; not on the build list. (Infloww *does* have it, if you ever reconsider: `git`-recover the deleted
`s4s-*.html` from the scratchpad backup `pre_s4s_backup/`.)

---

### Suggested build order
1. **Shifts & Time Tracking** (#1) — most-requested agency feature; the Dashboard already has holes for it.
2. **Smart Lists** (#2) — compounds every blast you already send.
3. **Leaderboard** (#8) — cheap, data already exists.
4. **Fan Reports** (#10) + **Auto-follow** (#3).
5. Trial/Tracking links (#4/#5), Roles (#11), Promotion/Referrals/Billing as the product demands.
