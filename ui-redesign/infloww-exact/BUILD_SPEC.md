# Infloww interlinked-clone BUILD SPEC

Goal: turn each captured screen into a pixel-exact **standalone** HTML page that reuses ONE shared
"shell" (black sidebar + top bar) and links to its siblings, so the whole thing clicks through like
the real app. Dark mode. All self-contained (inline CSS/SVG, Google Fonts Inter ok, no other network).

## The shell (COPY IT — do not reinvent)
`dashboard.html` in this folder is the reference shell. For every new page:
1. Start from dashboard.html's markup: keep the **left sidebar** and **top bar** verbatim (same
   CSS, same structure, same icons).
2. Replace ONLY the main content area with the new screen's body (built from its screenshot).
3. Set the **active** nav item to the current page; expand the parent submenu if it has one.
4. Set every nav href per the MAP below so links actually navigate.

Palette (see PALETTE.md): accent `#4166f6`, sidebar `#000`, canvas/cards `#262626`, borders `#333`,
panels `#232323`, thread `#151515`, text `#fff`, muted `#8a8a8a`, green `#67d1ae`. Font Inter.

Window frame target 1440×900. Content uses placeholder/SFW data only (never real handles/emails).

## Sidebar structure + hrefs (top → bottom)
- Creator selector "OnlyFans ▾"  (not a link; static)
- **Dashboard** → `dashboard.html`
- **OF Manager** ▾ → `ofmanager.html`   (submenu items Home/New post/Notifications/Messages Basic/Vault/Queue/Collections/Statements/Statistics/Bank/My profile/OF settings all → `ofmanager.html`)
- **Analytics** ▾ → `analytics-creator-reports.html`
    - Creator reports → `analytics-creator-reports.html`
    - Employee reports → `analytics-employee-reports.html`
    - Fan reports → `analytics-fan-reports.html`
    - Message dashboard → `analytics-message-dashboard.html`
- **Messages Pro** (pink badge + ↗) → `messages.html`
- **Growth** ▾ → `growth-smart-messages.html`
    - Smart Messages → `growth-smart-messages.html`
    - Smart lists → `growth-smart-lists.html`
    - Auto-follow → `growth-auto-follow.html`
    - Vault Pro → `growth-vault-pro.html`
    - Scripts → `growth-scripts.html`
    - Profile promotion → `growth-profile-promotion.html`
    - Free trial links → `growth-free-trial-links.html`
    - Tracking links → `growth-tracking-links.html`
- **Share for Share** ▾ → `s4s-discover-creators.html`
    - Discover creators → `s4s-discover-creators.html`
    - Requests → `s4s-requests.html`
    - S4S schedule → `s4s-schedule.html`
- (divider)
- **Creators** ▾ → `creators-manage.html`
    - Manage creators → `creators-manage.html`
    - Custom proxy → `creators-proxy.html`
- **Employees** ▾ → `employees-manage.html`
    - Manage employees → `employees-manage.html`
    - Shift schedule → `employees-shift.html`
- (bottom) **Settings** → `settings.html`
- **Help center** → `#`
- muted "Version 5.7.14"

## Top bar hrefs
Operational · UTC-10:00 · **Referrals** → `referrals.html` · **Leaderboard** → `leaderboard.html`
· SFW toggle · **bell** → `notifications.html` · avatar (static menu on click: Add account / Log out).

## Screen → file → source screenshot (in scratchpad/assets/infloww/, read for pixel accuracy)
| file | screenshot |
|---|---|
| dashboard.html (exists) | infloww__dashboard__default.png |
| messages.html (exists) | infloww__messages__thread.png |
| ofmanager.html | infloww__ofmgr-of-embed.png  (this tab EMBEDS OnlyFans — build a simple placeholder page: the shell + a centered card "OF Manager embeds the live OnlyFans site" + the OnlyFans splash look; do NOT reproduce real OF content) |
| analytics-creator-reports.html | infloww__analytics-creator-reports.png |
| analytics-employee-reports.html | infloww__analytics-employee-reports.png |
| analytics-fan-reports.html | infloww__analytics-fan-reports.png |
| analytics-message-dashboard.html | infloww__analytics-message-dashboard.png |
| growth-smart-messages.html | infloww__growth-smart-messages.png |
| growth-smart-lists.html | infloww__growth-smart-lists.png |
| growth-auto-follow.html | infloww__growth-auto-follow.png |
| growth-vault-pro.html | infloww__growth-vault-pro.png |
| growth-scripts.html | infloww__growth-scripts.png |
| growth-profile-promotion.html | infloww__growth-profile-promotion.png |
| growth-free-trial-links.html | infloww__growth-free-trial-links.png |
| growth-tracking-links.html | infloww__growth-tracking-links.png |
| s4s-discover-creators.html | infloww__s4s-discover-creators.png |
| s4s-requests.html | infloww__s4s-requests.png |
| s4s-schedule.html | infloww__s4s-s4s-schedule.png |
| creators-manage.html | infloww__creators-manage-creators.png |
| creators-proxy.html | infloww__creators-custom-proxy.png |
| employees-manage.html | infloww__employees-manage-employees.png |
| employees-shift.html | infloww__employees-shift-schedule.png |
| settings.html | infloww__settings-*.png (11 tabs — build as ONE page with a left tab-list + JS switching the 11 panels; tabs: Your Account, Your Preferences, Security (organization), Security (personal), Billing, Your Balance, Role Settings, Sales Settings, Time tracking, Messaging settings, About Infloww) |
| referrals.html | infloww__topbar-referrals.png |
| leaderboard.html | infloww__topbar-leaderboard.png |
| notifications.html | infloww__topbar-notifications.png |
