---
kinds: [ai_chatter, apply_profiles, arc_tease, audience_sync, auto_follow, auto_posts, auto_stories, autoreply, customs_watch, describe_media, gen_info, make_right, mass_nudge, mass_premade, nudge_online, nudge_online_fire, online_blast, ppv_send, process_old_fans, promo_reactivate, push_to_sheets, reengage_buyers, reply_mass_funnel, scheduled_send, scrape_chats, send_followup, send_mass_message, send_welcome, tip_request, tip_reward, unsend_messages, vault_ai_consume, vault_daily_reminder, welcome_chatter_for_info]
verified: 2026-08-29
---

# Fastt — how the product works

This is the reference for agency staff. Everything below was checked against the
running product on 2026-08-29. If something is not described here, it is not a
feature — say so instead of guessing.

Fastt works **only with OnlyFans**. It does not post to, read from, or connect to
any other platform.

---

## The top navigation

The nav strip, left to right:

**Home · Inbox · Stuff · Stats · Automations · Growth · Vault ·
Setup · Settings**

**Stuff** is the money-and-posts page: PPV, Tips, All, Customs, Posts, My Feed
and Top Posts as tabs across the top. Customs used to be its own nav entry and
is now **Stuff → Customs**; the old address still works and lands on the tab.

Four of those — **Automations, Growth, Vault, Setup** — are owner/admin
surfaces and are not shown to a chatter-only login. **Stuff is deliberately
visible to chatters**, Customs tab included, because a chatter needs to know
when a fan is owed a custom.

To the left of the links sit the ≡ menu, the ⧉ switch-to-Infloww-view button,
the image-blur toggle and the Fastt wordmark. To the right: **+ New ▾** (📝 New
post · 📣 Mass message · 🟢 Mass Online · ♻️ Premade Mass · 🔔 Nudge Online),
the scope switcher, the notification bell, the Desktop app link, the theme
toggle, the employee chip and logout.

## Roles — who can see what

There are two kinds of login: an **owner/admin** (a User account) and a
**chatter**.

A chatter sees: Home, Inbox, Stuff, Stats, Settings — and inside Stuff, the
Customs tab.

A chatter does **not** see Automations, Growth, Vault or Setup in the nav. On
Settings a chatter sees only four tabs: **Templates, Scheduled, Mass messages,
Restrictions**. Employees, Chatters, Auto stories, Transfer model, Audit log and
Concurrency are owner-only.

**Rule for answering a chatter:** if the thing they are asking about is
configured on Automations, Growth, Vault, Setup, or an owner-only Settings tab,
tell them the feature exists, say what it does, and tell them to **ask an owner
or admin to set it up** — do not hand them a click path they cannot follow.

## Where the automations actually live

**/automations is not a tabbed page.** It is one page of stacked cards, in this
order on a desktop screen:

1. **Brain** — the per-account voice, clock, spend cap, model, plus the Welcome,
   Follow-ups and Audience switches.
2. **Automation rules** — the raw rule list, with **+ New rule** and per-rule
   **Run now / Edit / Delete**.
3. **Ready-made posts & broadcasts** — a strip of **14 tabs** (below).
4. **Automation runs** — read-only run history. This is the authoritative log.

On a phone the Brain card moves *below* the rules list, and the 14-tab strip
scrolls sideways — the later tabs are off-screen until you swipe.

The 14 ready-made tabs, in their exact on-screen order:

🗓️ Auto Posts · ♻️ Mass Premade · 🎯 Mass funnels · 👋 Nudge Online ·
📣 Mass Nudge · 📡 Blast Online · ⚡ Reply Instant · 💬 Auto Convo ·
🤖 AI Chatter · 💰 AI Upseller · 🎁 Tip Reward · 💸 PPV Library · 🧠 Vault AI ·
🆕 Onboard old fans

**/settings** has 10 tabs: Employees · Chatters · Templates · Scheduled ·
Mass messages · Auto stories · Restrictions · Transfer model · Audit log ·
Concurrency. Only **Auto stories** is an automation tab there; **Mass messages**
also holds the Auto-unsend automation card.

**/growth** has 6 tabs: 🎯 Smart Lists · 🎟️ Trial Links · 🔗 Tracking Links ·
📣 Promotion · ❤️ Auto-follow · 📊 Overview. Growth owns its own two
automations; neither appears under Automations.

Two surfaces people hunt for and cannot find, because they are **sections inside
the 🤖 AI Chatter tab**, not tabs of their own: **Make It Right** and
**Re-engage buyers**. Scroll to the bottom of 🤖 AI Chatter.

## "Enabled" means three different things

This is the single most common cause of "I turned it on and nothing happened."
An automation only runs when there is an **enabled rule** for it. A tab's
Enabled box may or may not create that rule.

**1. Rule-backed — the box IS the automation.** Saving creates or wakes the
rule: 🗓️ Auto Posts, ♻️ Mass Premade, 👋 Nudge Online, 📣 Mass Nudge,
📡 Blast Online, 💸 PPV Library, 🤖 AI Chatter, the Mass funnels *Reply-walking*
banner, Settings → Auto stories, Settings → Mass messages → Auto-unsend, and the
Brain's Welcome / Follow-ups / Audience sections.

**2. Config-only — the box changes behaviour but starts nothing:**
💬 Auto Convo, ⚡ Reply Instant, 🧠 Vault AI. **Auto Convo in particular still
needs an `autoreply` rule added by hand** in Automations → Automation rules →
+ New rule.

💰 **AI Upseller is the exception in that group.** Its knobs are tuning, but the
"Enable Upseller (recommended)" button writes `enabled` onto the *Seller's*
config, which creates or wakes the `ai_chatter` rule — clicking it starts the
seller on that account.

**3. Event-driven — no rule and no schedule at all:** 🎁 Tip Reward fires off
the tip itself.

One more: the **"Get to know fans (AI info-gather)"** switch on the 🤖 AI Chatter
tab writes **immediately** on click — there is no Save behind it.

## Adding a rule by hand

Automations → Automation rules → **+ New rule**. Fields: Automation (a dropdown
grouped Core / Advanced; it cannot be changed later), Name, **Run every**
(number + seconds/minutes/hours), an Enabled checkbox, and optional **Quiet
hours** (two creator-local hours; blank = 24/7).

The dropdown does **not** list every automation — only these:

- **Core:** Get to know fans (AI info-gather), AI Seller, Auto Convo, Follow up
  quiet fans, Generate fan profiles, Apply profiles, Customs owed.
- **Advanced:** Backfill chat history, Export to Google Sheet, Onboard pre-AI
  fans, Walk mass-funnel replies.

Everything else is created from its own tab. If an automation is not in this
dropdown, do not tell anyone to add it there.

Cadence limits: minimum 30 seconds (the engine ticks every 30s), maximum 30
days. The unit selector offers seconds, minutes and hours — there is no days
option, so use hours.

The Brain card, the rules card and the ready-made card each keep their **own**
account selection. Switching the model on one does not switch it on the others.

## The AI key — nothing writes without it

Every AI-written message resolves the agency's own API key. **Setup → Your AI
keys**, one password field per provider in this order: **DeepInfra · DeepSeek ·
Grok (x.ai) · Z.ai (GLM)**, each with a set / not set badge. Leave a field blank
to keep the stored key.

- DeepSeek powers chat replies and most automations — this is the one that
  matters. The system default model is `deepseek-v4-flash`.
- DeepInfra powers vision: vault image descriptions and replies to photos a fan
  sends.
- Grok and Z.ai are optional second providers.

**It fails closed.** With no key for the provider a model belongs to, the call is
refused with "add it in Setup → Your AI keys" — there is no fallback house key.
An agency holding only a DeepInfra key still has every chat reply fail. If two
owners are linked to one model, AI stops on that model entirely and the card
shows a red "AI is stopped on N models — two owners" panel.

Setup is **desktop-only**: on a phone the key cards are hidden and the page says
"Session capture, proxies and keys are desktop-only."

## Which autochat is which

**Get to know fans (`welcome_chatter_for_info`)** is the warm-up. It talks to
fans who have not bought anything, asking one gentle question at a time to build
a profile. It does not pitch — but if a fan asks outright to see something it
answers with a priced vault PPV.

**AI Seller (`ai_chatter`)** is the seller. Out of the box it only answers fans
who have **already bought content** — a tip or a PPV unlock; a subscription does
not count — and it leaves fans over $1,000 lifetime spend to human chatters. It
pitches pieces from your content library and delivers the unlock.

They hand off: a fan leaves the warm-up lane for good once he buys content, or
once the lane is finished with him — every topic filled or asked to its cap, and
the follow-up questions it mined all used up — and the Seller picks him up from
there. The 10-message cutoff only bites while he is still being interviewed; a
fan who has finished answering keeps getting replies past 10. A complete profile
on its own is not a hand-off. **If AI Seller is set to cover every fan (the payer floor
turned off), the warm-up chatter stands down for the whole account** even though
its own switch still reads on.

**AI Upseller is not a separate engine.** It is a second settings page over the
same Seller — the "sell harder" tuning — and `ai_upseller` in the message log is
just the label on a Seller reply that carried a priced offer. There is no
upseller rule to look for. Pressing "Enable Upseller (recommended)" also turns
the Seller itself on.

**Auto Convo (`autoreply`)** is neither. It is a keep-warm safety net: when
nobody answered a fan in time it sends one casual line — or, if he asked outright
to see something and AI Seller is off on that account, a priced PPV from the
vault instead. Never a profile question.

## The online-message family

Three different things, easy to confuse:

- **👋 Nudge Online (`nudge_online`)** — the real "message someone when they come
  online". Every ~60 seconds it checks who just appeared, then sends that fan a
  **personalised** DM after a short delay. This is the answer to "can I message
  people when they log in".
- **📣 Mass Nudge (`mass_nudge`)** — on a timer, **one generic message** to
  everyone online right now. No names, no personalisation. It builds the list
  itself, so it knows exactly who got it.
- **📡 Blast Online (`online_blast`)** — one OnlyFans list-broadcast to the whole
  online set, resolved by OnlyFans server-side. Built for very large accounts.
  Its per-fan memory is capped: it skips fans touched in the last 8 hours, but
  remembers only the first 5,000 fans it hits, so on a very large account the
  cadence you set is the real brake. Run it hourly, not every few minutes.

All three share one "recently contacted" ledger, so a fan caught by one will not
be caught by another inside the cooldown window.

**When someone asks about messaging "everyone online" or "all the people who log
in", name all three and say which one they want**, because the phrasing does not
settle it: one personalised DM per fan as he appears is Nudge Online; one generic
message to everyone online right now is Mass Nudge; a single list-broadcast for a
very large account is Blast Online.

None of them is a true login event. Detection is polling, roughly once a minute.

---

# Setting up

## Connecting an OnlyFans account

**Where:** **Setup → Paste cURL.** Hidden from chatters, and **desktop only** — on a phone this card is not rendered and the page just says "Session capture, proxies and keys are desktop-only."

**How to do it:**
1. Install the **Fastt Login Capture** Chrome extension. The card shows an
   **Install Chrome Extension** button, and once it is there the button is
   replaced by a green line: "Extension installed — click the puzzle icon in
   your Chrome toolbar to open it."
2. Sign in to OnlyFans as that creator **in Chrome**, open the extension from
   the puzzle icon, and press **Copy cURL**.
3. Paste it into the big box on Setup → Paste cURL. It must start with `curl` —
   anything else is refused with "Must start with `curl ...`".
4. Optional, before you submit:
   - **Nickname** — the name shown in the accounts table and the model picker.
     You can rename it later by clicking the name in the accounts table.
   - **Attach proxy** — pick one of your registered proxies (default is "none",
     which egresses on the server WAN IP).
   - **Flip this account to the relay's default after bootstrap** — **ticked by
     default**. Untick it if you do not want this new model to become the
     relay's default account.
   - **Onboard the 100 most-recent subscribers after connecting** — **off by
     default**. It runs profiles and writes nicknames/notes to OnlyFans, sends
     no messages, and spends LLM.
5. Press **Bootstrap from cURL**. Success prints the new account id, the OF
   user_id, the session file it wrote and the x-of-rev, and the model appears
   in the accounts table and the model picker.

**Re-capturing.** The session is a copy of that browser login, so it goes stale.
When OnlyFans ships a new front-end, Setup shows an amber **"Session stale —
re-capture required"** banner naming the models affected and the new rev. (The
banner calls it the "Fastt Login extension" — same extension. Its **Dismiss**
button only hides it for that one rev; a new rev brings it back.) Fix is the
same steps: sign in again, Copy cURL, paste, Bootstrap. That also immediately
clears a paused account (see below).

**Caveats:**
- Picking a proxy shows an amber teleport warning on the card, and pressing
  Bootstrap asks you to confirm it: the cookies were minted on your own IP while
  every call afterwards leaves from the proxy's. Only do it when the proxy sits
  close to where you signed in. If the session captures but the proxy attach
  fails, the result panel says so and tells you to attach it manually under
  Setup → Proxies.
- Nothing else on Setup connects an account. Proxies, keys and the accounts
  table are all after-the-fact management.

## Filling in the Brain

**Where:** Automations → the **Brain** card — the first card on the page at desktop (on a phone it sits below the rules list). Pick the model with the account chips under the header. Everything on the card saves with **Save brain**. The two exceptions: the Welcome and Follow-up sections further down have their own **Save welcome** / **Save follow-ups** buttons, and the "Answer 'what are you up to?'" tick saves the moment you click it.

**What to fill in, top to bottom:**
- **Persona** — free text: "Who this model is — name, vibe, how they talk…". Write this first; Enrich and the creator facts are built out of it.
- **Location** — free text, e.g. "Vancouver Island". It becomes the "You are based in X." line in the chat prompt. This is *not* the clock.
- **Creator** — **Female (default)** or **Male**. Male rewrites the texting frame, the FaceTime refusal, the creator-fact labels, the off-platform deflections and the script pack. Nothing else on the account changes, and no other account is touched.
- **Language** — what the account writes in *and* which safety-word list runs. Changing it re-generates each fan's saved lines on their next profile pass.
- **Creator clock (place & time)** — pick the creator's place from the dropdown. This one value drives every clock: the time line in the welcome, the "right now" line in every chat prompt, the sleep window and quiet hours. Left on "— not set (no clock in chat) —" there is no clock, no sleep window and no quiet hours. It is a fixed offset, so move it by hand at the daylight-saving changeover.
- **Daily cap (¢)** — this account's AI spend ceiling for the day, in **cents**; a fresh brain starts at 100 (= $1.00). At the cap the sweep stops generating, but the run still finishes and reports ok with `cap_hit` on the run row — nothing errors, it just does less.
- **Creator facts** — nine boxes, in the order fans actually ask: Age, Work, Lives in, Relationship, Kids, Family, Tattoos, What she's into, What she won't do (the last two read "What he's into" / "What he won't do" on a male account). These are pinned into every chat prompt as facts the creator may never contradict. **A blank box is not neutral — it is a box the model improvises, differently each time.**
- **Model** — **Default (all purposes)**, which you leave on "— system default —" unless you have a reason. **Thinking effort** only appears for models that have one. **Per-purpose overrides** — Fan profiles, Get to know fans (info-gather), Welcomes, Follow-ups, **Help chatbot** — each sits on "inherit default" until you change it, with one exception. **Help chatbot** reads **"always deepseek-v4-flash"** instead, because that route does *not* inherit the Default above: ~28k tokens of manual ride on every one of its calls at a ~98.6% cache hit, so it is priced on the cached-input rate and is held on DeepSeek unless you pin it yourself. Pin it here and your pin wins.
- **Time-of-day lines & images** — six slots (Morning early/late, Afternoon early/late, Evening, Night): what the creator is doing, plus the image the welcome/follow-up attaches for that slot. **Answer "what are you up to?"** (on by default) uses those lines to write a fresh day each morning.
- Below that, **Co-performer tag** and the **Audience** fence — both saved with Save brain.

**🪄 Enrich** fills the *empty* fact boxes from the persona and resolves the **Creator clock** from the city it lands on (the offset is worked out in code, never guessed by the model). It saves nothing: the proposals land in the boxes marked with an amber dot for you to read and correct, then you press **Save brain**. It never overwrites a box that already has text.

**Caveats:**
- Enrich never touches the three 🔒 boxes — **What she's into**, **What she won't do** and **Tattoos**. The first two are a business decision — a guessed limit either costs a sale or promises something the creator does not do — and tattoos are visible in her photos, so a confident wrong answer is worse than a blank one. Fill those three yourself.
- The button is dead until the Persona box has text. It also reads the **saved** brain, not what is on screen, so write the persona and press **Save brain** before pressing it — and because it replaces the fact boxes with what the server hands back, unsaved edits in those boxes are lost.
- If the place is ambiguous (or a half-hour zone) it reports "clock unresolved — pick the creator's place below" and leaves the clock alone.
- **Reset to defaults** refills the persona, location, clock, daily cap, model and time-of-day lines from the starter brain for that Creator lane. It keeps this account's images, its creator facts, its Creator setting and its audience / co-performer settings. Nothing is written until you press Save brain.
- A brand-new account opens **pre-filled with that starter brain** instead of blank — it is a worked example to overwrite, not this creator.

## Adding a chatter

**Where:** **Settings → Chatters.** Owner only — a chatter signed in on their
own login does not see this tab at all.

**A brand-new chatter:** in **Invite a new chatter**, press **Mint invite URL**,
then **Copy** and send them the link. On that page they pick their own username
and password; that creates their login and links them to you right then — they
land in the app already signed in as a chatter. The link is **single use** (a
second person opening it gets "This invite link is no longer valid") and
**expires 24 hours** after you mint it; the card prints the exact expiry under
the URL. **New** mints a fresh link — it does *not* cancel the old one, which
keeps working until it is used or runs out.

**Someone who already chats for another owner:** use **Link an existing
chatter**, type their username, press **Link**. They see your models in their
picker on next sign-in (instantly if they are currently online). If the username
is not found the card says so and tells you to send an invite link instead.

**Limiting what a chatter sees:** each row has an **Access** link that opens
**Access limits** under it.
- **Limit which models this chatter sees** — off (the default) means they see
  all of your models. Tick it and pick the accounts.
- **Folder limits (per model)** — under each model they can see, tick
  *"— limit folders"* and pick folders. Only **custom** vault folders are
  listable; a model with none says so.
Press **Save access**. A limit that is switched on with nothing picked will not
save ("Pick at least one model, or turn the limit off") — to take someone off
completely, **Unlink** them instead of building an empty list.

**Unlink** sits in the same row and fires immediately — no confirmation. It only
removes *your* link; other owners they work for keep theirs, and re-linking them
later reuses their existing record so the audit history stays joined up.

**Mirror employee column.** Each chatter shows the employee row their work is
credited to, or "not yet (auto-created on first action)" until they do
something. You do not create that yourself.

**Employees is not this.** Settings → **Employees** is the roster of names and
colours used to credit work in the picker and the audit log — those rows have no
password and give nobody a login. Chatters are the logins.

**Caveat:** don't open an invite link in the browser you are signed in as owner
on — registering there logs the owner out of that browser.

## Stopping the bot on one fan

Everything here is done from the **⋯ menu in the chat header** ("More actions"),
on that fan's thread. **Settings → Restrictions** is the list of who is already
stopped, not where you add someone — it is visible to chatters as well as
owners, and it shows one model at a time (pick the model in the Account
selector when you have more than one).

- **Restrict from automations** — the normal answer. No automation ever messages
  him again: welcome, AI chat, follow-ups, nudges, tip reward, mass blasts, all
  of it. Humans and chatters can still write to him normally. The same menu
  line then reads **Allow automations** to lift it. Every fan you have
  restricted this way is listed on **Settings → Restrictions** under
  **Restricted from automations**, where you can lift one (the bin icon on the
  row) or lift the lot with **Unrestrict all**.
- **Add to MASSppvEXCLUDE** / **Add to MASSdmEXCLUDE** — narrower. PPV keeps him
  out of *priced* broadcasts; DM keeps him out of the unpriced ones — mass DM,
  online blast and nudge. Both leave welcomes, AI replies and 1:1 chat alone.
  These are the two opt-out lists the mass sends subtract, and each is mirrored
  to a pinned OnlyFans list of the same name. The menu lines flip to **Remove
  from MASSppvEXCLUDE** / **Remove from MASSdmEXCLUDE** once he is on one.
- **Restrict on OnlyFans** — the heavy one, and it is OnlyFans-side. In order it
  sends one "." (so our message is the last one and the thread stops counting as
  owe-reply), marks the chat read, restricts him on OnlyFans, and finally hides
  the chat so the dead thread leaves the inbox. From then on he is out of every
  unread and owe-reply count and every automation hard-skips him. The hide is
  not permanent — the thread comes back if he messages again. Clicking it a
  second time on someone already restricted does not send another ".".
  **Lift OnlyFans restrict** in the same menu un-restricts him on OnlyFans and
  un-hides the chat; the second card on **Settings → Restrictions**
  (**Restricted on OnlyFans**) lifts him too, one row at a time.

**Caveats:**
- A peer creator whose chat you **muted** on OnlyFans is restricted
  automatically and does **not** appear on Settings → Restrictions. The menu
  shows him as a greyed-out **"Auto-restricted (chat muted)"** instead of the
  restrict line — use **Unmute notifications** in the same menu to free him.
- Someone already restricted on OnlyFans shows the same way, as
  **"Auto-restricted (OF restrict)"**. Lift it with **Lift OnlyFans restrict**,
  not with the automations line.

## Building and sending a funnel

**Where:** Automations → Ready-made posts & broadcasts → **🎯 Mass funnels**. Pick
the model in the account chips above the tab strip first — Start, Media and
reply-walking all act on the selected model.

**Funnels are global.** One funnel is shared across every model you own. What is
*not* shared is its media — vault ids do not carry between models, so each model
maps its own.

**Build it:** press **+ New funnel**. A funnel is a **Name**, an optional
**Description**, an **Opening message**, and an ordered list of steps added with
**+ Reply step** and **+ PPV step**. Save with **Create funnel** (**Save
changes** when editing). The editor is text only.
- A **reply step** polls for the fan's answer on the minutes you list ("Check for
  a reply every …", comma-separated; blank = 2, 4, 10), then sends what you typed
  in its variant boxes — **only the first two boxes are used**. Or tick
  **Generate the reply with AI** and it writes the message, steered by the
  optional prompt box.
- A **PPV step** is sales copy at a **Price (USD, whole)**, with an optional
  **Lock the text too (fan must pay to read the copy)**. Same AI tick if you want
  the sales copy generated.

**Map its media, per model:** press **🖼 Media** on the funnel's row, with the
model you want selected. You attach vault media to the **Opening message media**
(optional) and to each PPV step, then press **Save media**. Per PPV step,
"Free preview — first N of M" sets how many of the picked items go out unlocked
as the teaser; the strip is in send order and the free ones are always the first
N, so reorder with ◀ ▶ to choose them. A PPV step only shows up here once it is
saved in the editor. Do this once for every model that will run the funnel — a
funnel that works on one model and sends nothing on another is almost always
this.

**Send it:** press **▶ Start** on the funnel's row, then set the audience:
- **Send to (include)** — **All fans** (ticked by default), **Following**, and
  the account's own OnlyFans lists.
- **Exclude** — the same chips. If that account has a list named
  **MASSPPVEXCLUDE**, it is ticked into the exclude set for you; untick it for a
  one-off.
- **Online only**.
- **Skip fans I messaged in the last … hours**.
- **Auto-unsend opener after … hours** — pre-filled with 4; blank means no
  auto-unsend. Once the opener is unsent, new repliers stop enrolling, but anyone
  already being walked keeps going.

Then press **▶ Start funnel**. There is a confirm box because it is a real blast.
The opener goes out free — the priced step comes later in the funnel — and fans
who already answered this funnel are skipped automatically.

**Turn on reply-walking, or nothing advances.** The banner at the top of the tab
reads **Reply-walking: ON · every 2 min** or **Reply-walking: OFF**. Off means
every funnel sends its opener and stops there. Press **Enable (every 2 min)**.
It is **per model**, so switch the account chip and enable it on every model that
runs funnels. The Start panel warns you too — "⚠ Reply-walking is off — this
funnel will send the opener but won't advance through its reply / PPV steps" —
with an **Enable reply-walking** button right there. See `reply_mass_funnel`
below for the rest.

## Growth — Smart Lists, Trial Links, Tracking Links, Promotion

**🎯 Smart Lists** — a saved fan segment you build once and reuse. **+ New
segment**, name it, then add rules over **Lifetime spend ($)**, **Recent spend
($)** (with its own "in last N days" box), **Days since active**, **Subscription
status** (active / expired) and **Tag**, joined by **ALL rules (AND)** or **ANY
rule (OR)**. Press **Preview count** to see how many of the account's fans match
and a sample of the names, then **Create segment**. A saved list is then pickable
as an audience — include or exclude — in the mass composer, and as a **Target**
on Growth → ❤️ Auto-follow for the follow and like-messages actions. Lists are
per model, and the composer only offers them when you have a single model
selected. They live in Fastt and are never pushed to OnlyFans as a list. On a send
they are resolved to fan ids against live spend at that moment — and for an
*exclude*, those ids are written into a hidden per-account OnlyFans list called
`Auto_Exclude`, because OnlyFans has no per-fan exclusion on a broadcast.

**🎟️ Trial Links** — mint OnlyFans free-trial links. Set a **Name**, **Free
days** and **Max claims (0 = unlimited)**, press **Create trial link**; the table
shows each link with its claims and clicks, a **live** or **spent** badge, and
copy and delete buttons. OnlyFans owns these — when it is unreachable the tab
says so and shows the saved links only.

**🔗 Tracking Links** — two different kinds on one tab, and the difference
matters:
- **Tracking Links** (top, the OnlyFans kind) point at the creator's profile.
  Give it a **Channel name**, press **Create link**, and OnlyFans reports real
  clicks **and how many of those clicks subscribed** — true attribution. First
  time, click one of the links yourself to confirm the address works: OnlyFans
  does not hand out the share URL, so the app constructs it and says so until you
  have checked.
- **Custom redirect links** (the collapsed section below — click the heading to
  open it) are for a target that is not the profile: a Linktree, a landing page,
  an X profile. Give a **Name** and a **Target URL**. These are Fastt's own short
  links and they count **clicks only** — the visitor never lands on OnlyFans, so
  subscribers cannot be attributed, and the per-link **Analytics** panel says so.

Use the OnlyFans kind whenever you are sending people to the profile.

**📣 Promotion** — discounted subscription campaigns on the creator's profile.
Set a **Name**, a **Discount (% off)** (1–100), **Max claims (0 = unlimited)**, a
**Days** value and an optional **Message**, then **Create promotion** — it asks
you to confirm, because it is a real campaign on OnlyFans. **100% off is
OnlyFans' free trial and is the only case where the days box does anything** —
for any smaller discount OnlyFans ignores the duration and the promo runs with no
time limit until the claim cap fills or you end it. The field labels itself
**"Days (ignored by OF)"** when you are under 100%.

To keep an offer alive, tick **Keep running** on the campaign's own row — that is
the opt-in. The card at the bottom of the tab, **♻️ Keep promos running**, is
only the engine behind it (how often it checks, a max number of re-arms, and a
**Dry run** box that is **on by default** — untick it or it will only report).
See below.

---

# Recipes — wiring several automations together

Each of these is per model. Work down the steps in order.

## "Welcome them, chat, then sell — but leave my big spenders out of the mass sends"

The full shape: welcome every new fan → warm-up chatter for fans who have not
bought → AI Seller takes over the moment they buy → mass sends skip anyone over
your spend line.

**First, build the segment.** Growth → 🎯 Smart Lists → **+ New segment**. Name
it "Over $400". Match **ALL rules (AND)**. One rule: field **Lifetime spend
($)** · operator **>** · value **400** (the box is dollars). Press **Preview
count** and read the sample before saving — **lifetime spend counts
subscriptions and tips, not just content**, so a fan on $10/month for 40 months
crosses $400 without ever buying a PPV. Then **Create segment**.

**The three lanes.**
1. Automations → **Brain** card → **Welcome**: tick Enabled, Check every 5 min,
   press **Enable welcome**.
2. Automations → Ready-made → **🤖 AI Chatter** → flip the switch card **"Get to
   know fans (AI info-gather)"**. It saves on click, no Save button. This is the
   lane for fans who have not bought.
3. Same tab header → tick **Enabled** → **Save config**. Leave **"Only chat fans
   who have bought before"** ticked. Then **💰 AI Upseller** → **Sell in the
   chat (1:1)** → Save.

**The spend line, surface by surface — they do not behave the same.**
- **Mass message** (+ New ▾ → Mass message) — the only mass surface that can do
  it. Select **one** model (the Smart List chips vanish in all-models mode and
  the pick is silently dropped), then under **Smart Lists — exclude** click the
  "Over $400" chip. Works the same for 🟢 Mass Online.
- **💸 PPV Library** — has its own box: "Never mass-PPV a fan who has spent over
  $___ lifetime". It drops fans **at or above** the number, so type **401** for
  "more than $400".
- **👋 Nudge Online** — **Max lifetime spend ($)**. This one cuts **strictly
  above**, so type **400**. Yes, that is a different boundary from the PPV box.

**THE OTHER DIRECTION — "only fans UNDER $400" — is not this recipe backwards.**
Asked for the mirror image, it is tempting to build an "Under $400" segment and
exclude it. That messages exactly the wrong half: exclude removes what the
segment matches, so excluding "Under $400" leaves you sending to the big
spenders. **Build the segment for the people you do NOT want and exclude that,
always.** So for "only fans under $400" you still build **"Over $400"**
(operator **>**, value **400**) and exclude it — the same chip as above.

And the two number boxes flip, because their boundaries are not the same:
- **💸 PPV Library** drops **at or above**, so "under $400" is **400** (not 399,
  which would also drop the fan sitting on exactly $399).
- **👋 Nudge Online** cuts **strictly above**, so "under $400" is **399** (400
  would keep the fan on exactly $400, who is not under it).

A useful check: the two boxes should differ by one whichever direction you are
going. If you have typed the same number into both, one of them is wrong.

- **📣 Mass Nudge, 📡 Blast Online, ♻️ Mass Premade — cannot do it at all.**
  None has a spend control and none can read a Smart List. If the carve-out
  matters, switch these three off. The only workaround is adding each fan to
  **MASSdmEXCLUDE** by hand from the chat ⋯ menu, one at a time, re-checked as
  people cross the line. Mass Nudge's raw-JSON `excluded_users` is *not* a
  workaround: the ids never update, and the next Save on the form deletes them.
- Anything a **human chatter** sends is never gated by any of this.

**Then stop the non-spenders being chatted.**
- **💬 Auto Convo** — turn off *both* halves (untick Enabled + Save, then switch
  its rule off in the Automation rules list). It is a **low**-spender lane: its
  controls are ceilings, it has no minimum, and a fan who never paid passes it.
  Leaving it on is the most common reason "spenders only" does not hold.
- Decide about the warm-up lane. If "spenders only" means *the AI should only
  ever reply to people who bought*, untick the "Get to know fans" card too.

**Three things to remember**
1. **"Only chat fans who have bought before" is a yes/no test, not a dollar
   bar** — a tip or a PPV unlock counts, a subscription does not. There is no
   minimum-spend control on the chat engines at all.
2. **Never type 0** into the AI Chatter **Whale gate** or Nudge Online's **Max
   lifetime spend**. Neither reads 0 as "off": the whale gate at 0 makes every
   fan a whale and the seller talks to nobody; Nudge Online at 0 mutes every fan
   who ever spent a cent. Leave the box untouched to mean off.
3. **Some lanes can never be spend-gated.** The welcome (a new subscriber has
   bought nothing by definition) and **Follow-ups**, which has a hard-coded $1
   lifetime floor with no operator control — and subscription money clears $1.

## "Brand new model — what do I turn on first?"

1. **Setup → Your AI keys** — paste the **DeepSeek** key. Nothing AI-written
   works without it, and there is no fallback.
2. **Setup → Paste cURL** — connect the account (see Setting up).
3. **Automations → Brain** — Persona, Location, Creator, Language, Creator
   clock, **Daily cap (¢)** (a fresh brain starts at 100 = $1.00 — raise it),
   and all nine Creator facts. **Save brain**. Optionally press **🪄 Enrich**
   *after* saving, then Save again.
4. **Brain → Welcome** — tick Enabled, **Enable welcome**.
5. **🤖 AI Chatter** — flip **"Get to know fans (AI info-gather)"**.
6. Same tab header — tick **Enabled**, **Save config**.
7. Only then add selling: **💸 PPV Library** or **💰 AI Upseller**.

There is no one-click onboarding wizard — every step is its own Save button.

## "Make money from the vault without chatting"

1. **💸 PPV Library** → **Library ON** (cards stay greyed out until you do).
2. Build PPVs: **+ Add PPV**, a preset (🍑 Weekly tease $15 3×/wk, 🐳 Whale drop
   $60 1×/wk, 💔 Win-back $9 2×/wk), or **Duplicate**. On each: **Pick content**,
   tap **⭐** for the free teaser, pick a **Caption style**, set **Base price**
   ($3–$200) and **How often**.
3. Leave the account caps at **2/day · 14/week · 60/month**. Blank is *not* "no
   limit"; only an explicit 0 removes the spacing.
4. Set **Quiet hours**. Leave **AI caption at send** off and this lane spends no
   AI credits at all.
5. **Save** — that upserts the hourly rotator rule. If every individual PPV is
   unticked the rule saves *disabled* and nothing sends.
6. Silence the chat lanes: 🤖 AI Chatter → untick Enabled + Save, and flip the
   "Get to know fans" card off.

OnlyFans rejects any priced message under $3.00.

## "Message everyone who comes online, but not twice a day"

Broadcast version: **📣 Mass Nudge** → tick Enabled → **Send every** 60 min →
**Re-nudge cooldown** **24** hrs (this is the "not twice a day" control) →
**Skip repliers within** 12 hrs → check the six time slots have lines →
**Preview**, then **Save**.

Personal version: **👋 Nudge Online** → tick Enabled → **Min hours between**
**24** → Save. The first pass after switching on sends nothing on purpose — it
records who is already online so you do not stampede everyone.

Know three limits: one Mass Nudge run only scans the first **500** online fans;
the two share **one** recently-contacted ledger, so a fan caught by one is
skipped by the other; and neither is a real login event — detection is polling,
about once a minute.

## "Win back fans who expired"

1. **Growth → ❤️ Auto-follow** → **Action** = "Follow fans back (win-back)".
2. **Set Target = "Recently-expired fans (win-back)" yourself** — changing the
   Action does *not* change the Target, and left on "Recently-active fans" a
   follow notifies nobody, forever.
3. Max actions 50 · Run every 240 min · **untick Dry run** · tick Enabled ·
   **Create automation**. Dry run is ticked by default; Enabled + Dry run is
   scheduled and doing nothing.
4. **Growth → 📣 Promotion** → create the discount campaign (100% off is a free
   trial and the only case where the Days box does anything), then tick **Keep
   running** on that campaign and set up the **♻️ Keep promos running** card
   with **Dry run unticked**.

**A mass message to expired fans is not supported.** The composer's only system
audiences are All fans and Following; there is no expired audience, and a Smart
List can never become an OnlyFans list. Auto-follow and the promo are the
win-back tools.

---

# Editing the raw JSON

Some settings have no checkbox. They are reached by opening a JSON editor and
typing the key yourself. There are **two different kinds** of JSON in this app
and they behave in opposite ways, so read this before pasting anything.

**All raw-JSON editors are desktop-only.** The buttons are hidden below a
tablet-width screen — on a phone there is no raw-JSON path at all.

## The two kinds of JSON

**1. A rule payload** — the per-run settings of one automation.
Open it at **Automations → Automation rules → Edit** on the rule → in the
**Settings** block, the link on the right reading **"Edit raw JSON →"** (it
flips back with **"← Typed fields"**). Three tabs also carry their own
**{ } Edit raw JSON** button that edits the same thing for their rule:
📣 Mass Nudge, 📡 Blast Online, and Settings → Auto stories.

It takes one JSON object — the whole payload, e.g.

    { "limit": 200, "max_replies": 25, "dry_run": true }

**Unknown keys are kept.** The server stores whatever you type. That is
deliberate, but it means **a typo is a silent no-op**: `"limt": 40` saves fine,
reports success, and does nothing forever. Check your spelling against the
field names shown in the typed view.

**2. A config blob** — the settings behind a whole tab. Open it with the
**{ } Edit raw JSON** link in that tab's header. Eight surfaces have one:

| Tab | What the editor is called |
|---|---|
| Automations → Brain | `{ } Edit raw JSON` (the account's Brain) |
| 🤖 AI Chatter / 💰 AI Upseller | `Edit raw JSON — AI Seller (ai-chatter) config` |
| 💬 Auto Convo (first card) | `Edit raw JSON — Auto-convo (autoreply) config` |
| 💬 Auto Convo (style card) | `Edit raw JSON — Style config` |
| 👋 Nudge Online | `Edit raw JSON — Nudge online config` |
| 🎁 Tip Reward | `Edit raw JSON — Tip Reward config` |
| 💸 PPV Library | `Edit raw JSON — PPV Library config` |
| ⚡ Reply Instant | `Edit raw JSON — Instant-reply (webhook) config` |

**Unknown keys are thrown away.** Every one of these except the Brain runs the
JSON through a list of keys it recognises and **silently drops anything else**,
then replaces the whole stored blob. You get a success message either way, and
re-opening the editor shows the default — so a dropped key looks exactly like a
key that saved. If a setting does not stick, it is almost always this.

**Three surfaces have no raw-JSON editor at all:** 🧠 Vault AI, the Make It
Right section, and the banned-words filter (which has no screen anywhere).

## Setting a time of day instead of an interval

The rules editor only offers "Run every N minutes/hours". Clock times exist but
have no control — they are set on the rule's `trigger`:

    { "daily_at": ["09:00", "20:30"], "tz_offset_minutes": 120 }

`daily_at` is a list of "HH:MM" in the creator's local time;
`tz_offset_minutes` is minutes to add to UTC. You can add `"max_runs": 5` to
either form to switch the rule off automatically after N runs.

**Auto stories is the exception** — its tab has an "At set times" mode that
writes this for you. For every other automation it is JSON-only.

Unlike a payload, an unrecognised key inside `trigger` is **dropped**.

## Automations with no form at all

Eleven automations have no settings screen, and the + New rule dropdown does not
list them: **arc_tease, auto_follow, describe_media, ppv_send, promo_reactivate,
reengage_buyers, scheduled_send, tip_request, tip_reward, vault_ai_consume,
vault_daily_reminder**.

Most are driven from their own tab instead (PPV Library, Tip Reward, Growth →
Auto-follow, Growth → Promotion). But if a rule of one of these already exists,
opening **Edit** on it shows a bare JSON box with no field names, no defaults
and no validation — whatever you type is stored as-is. Creating one from
scratch is not possible from the UI.

## Settings that exist but have no control

These are honoured by the engine and can only be set as JSON. The most-asked:

- **Turning a nudge cooldown off** — `"exclude_replied_hours": 0` and
  `"exclude_inbound_hours": 0` in the 📣 Mass Nudge or 📡 Blast Online rule
  payload. Typing 0 into the form saves as blank, which means the default.
- **Nudge Online spend targeting** — `min_lifetime_spend_cents`,
  `max_lifetime_spend_cents`, `min_recent_spend_cents`,
  `max_recent_spend_cents`, `recent_spend_days` in the Nudge Online config.
- **Nudge Online weekday/weekend copy** — the friendly editor only writes the
  `default` bucket; other day buckets are JSON-only.
- **PPV Library** — `owner_second_leg`, `ai_caption_at_send`.
- **Ghost cycle** — `rhythm_ghost_enabled` (default **off**) and
  `rhythm_ghost_cycle` in the 🤖 AI Chatter config. Whole DAYS on which the bot
  does not answer a fan at all, on a schedule that repeats: chat 3 days → dark 1,
  chat 4 → dark 2, chat 5 → dark 2.5, then back to the top. The point is that a
  fan who has her attention every single day stops noticing it. It is the one
  silence in the product that fires even on a fan who is owed an answer —
  deliberately — and a live sale is exempt. The PPV lane reads the same switch
  and holds its blasts back from a fan who is dark that day.
- **AI Seller pricing depth** — `pack_price_ladder`, `value_caps_price`,
  `msg_limits_by_spend`, `daily_quota_by_spend`, the quota backoff hours, and
  the gather-close PPV set (`gather_close_folder`, `gather_close_price_cents`,
  `gather_close_count`).
- **Tip Reward** — `context_pick_max`, `context_pick_messages`, and the whole
  `tip_request` block.
- **Vault AI** — everything: `models.describe`, `describe.max_items_per_run`,
  `describe.describe_all_cap_percent`, and the daily-reminder settings. This is
  the one blob that keeps every key you write.

## Backing it up before you edit

**Automations → the Automation rules card header → ⬇ Export settings** downloads
the account's whole configuration, and **⬆ Import settings** restores it exactly
as saved — including keys the per-tab editors would drop. Export before hand-
editing anything, so a dropped key is one restore away.

---

# The automations

## kind: welcome_chatter_for_info — Get to know fans (AI info-gather)

**What it does:** Opens and continues conversations with fans who have not
bought anything, asking one gentle question at a time to learn about them. It
feeds Generate fan profiles. It does not pitch — but if the fan asks outright to
see something it answers with a priced vault PPV, and it can send a parting PPV
when it finishes with him. Both live on the 💰 AI Upseller tab ("sell on ask"
ships on; the gather-close set is off until you pick a folder).

**Where:** Automations → Ready-made posts & broadcasts → 🤖 AI Chatter → the
"Get to know fans (AI info-gather)" switch card. Owner/admin only.

**How to enable:** Flip that switch — it writes **immediately**, no Save button.
It can also be added in Automations → Automation rules → + New rule ("Core").

**Key knobs (rule payload):** run every 60s · `limit` 200 chats scanned per run ·
`max_replies` 25 per run · `history_tail` how many recent messages the AI reads
(engine default 20) · `dry_run` · `only_fan_ids` scope with gates on ·
`force_ids` target fans ignoring gates. Its selling permission and the
gather-close PPV live on the 💰 AI Upseller tab.

**Prerequisites:** an agency AI key; chat history already in the system; an
enabled rule of this kind.

**Caveats:**
- Graduation is permanent, but a finished profile is not graduation. He leaves
  for good when he buys content, or when the interview is over **and** the
  follow-up questions it mined have all been used. The 10-message cutoff applies
  only while he is still being interviewed. Once he does leave, this lane never
  talks to him again — if AI Seller is off, no bot does.
- If AI Seller is on and covers every fan, this engine stands down for the whole
  account while still reading "on".
- Dry run still calls the AI and still costs money; only the send is skipped.

## kind: ai_chatter — AI Seller (chatter + catalog selling)

**What it does:** Chats freestyle with fans who have already bought something,
pitches pieces from your content library as PPVs, and delivers the unlock when
they pay.

**Where:** Automations → Ready-made posts & broadcasts → 🤖 AI Chatter. The
sell-harder tuning is on 💰 AI Upseller; both tabs edit the same settings.

**How to enable:** Tick **Enabled** in the 🤖 AI Chatter header and Save. That one
box does everything — it writes the config and creates or wakes the rule. The
badge beside it is only a status light.

**Key knobs:** "Only chat fans who have bought before" (**on** by default) ·
big-spender ceiling $1,000 lifetime · mode always / backup · backup SLA 10 min ·
8 fans per run · stay out 1h after a human chatter · max 2 offers per fan per day
· 4 fan messages between offers · unpaid offer expires after 6h and is unsent ·
reply-burst caps by how hot the fan is (10 normal / 20 buying signal / 5 cold /
25 he sent a photo) · daily quota 10 replies per fan after 31 free ones.

**Prerequisites:** an agency AI key; a content library with media actually
attached (an empty shelf means it can chat but has nothing to pitch); fans who
have bought.

**The bubble window is two minutes.** A reply is typed out as several bubbles a
few seconds apart, and consecutive outbound messages closer together than two
minutes are **one reply**, not several. That is why the reply-burst caps above
count replies rather than rows, and why the chat shows a burst as a single turn.
It is not how long a reply waits before it lands — that is the reply-latency
budget, a different number.

**Caveats:**
- Turning it on alone leaves every never-bought fan silent — those belong to the
  warm-up lane, which is a separate switch on the same tab.
- Fans at or above $1,000 lifetime spend are never touched by the bot.
- "Enable Upseller (recommended)" on the 💰 AI Upseller tab also switches this
  engine on.

## kind: autoreply — Auto Convo (reply when the team is slow)

**What it does:** When a fan messages and nobody answers in time, it sends one
casual line matched to the tone of the recent messages, to keep the conversation
alive. It never pitches. It can still answer one thing: if the fan explicitly
asks to see something it sends a priced vault PPV instead of the casual line —
the "Auto Convo may sell on ask" switch on the 💰 AI Upseller tab, which ships
**on**. That only happens on accounts where AI Seller is off; with AI Seller
enabled Auto Convo stays purely keep-warm.

**Where:** Automations → Ready-made posts & broadcasts → 💬 Auto Convo.

**How to enable — TWO steps, and this is the trap:**
1. Tick Enabled on the 💬 Auto Convo tab and Save. **This only saves settings.**
2. Add an **Auto Convo** rule in Automations → Automation rules → + New rule and
   enable it. Without step 2 the tab reads on and nothing ever sends.

**Key knobs:** wait 24 minutes of silence before stepping in · give up after
~18.5 hours · one reply per waiting message · 5 minutes minimum between its
replies to a fan · only fans under $20,000 lifetime and under $500 in the last
day · reads the last 16 messages · quiet hours · 200 candidates and 25 replies
per run.

**Prerequisites:** an agency AI key, plus both halves of the switch above.

**Caveats:**
- When AI Seller is enabled, Auto Convo stays purely keep-warm even if a fan
  asks to see content.
- Enable it only on accounts with real fans, never on promo-spam accounts.

## kind: send_welcome — Welcome new subscribers

**What it does:** Watches OnlyFans' new-subscriber feed and sends each new
subscriber a welcome: a greeting with a time-of-day photo, then a line saying the
day/time and where the creator is, then optionally a question you wrote
word-for-word, then optionally a GIF. Once per fan, ever.

**Where:** Automations → Brain card → the **Welcome** section.

**How to enable:** In the Welcome section tick Enabled, set "Check every N min"
(default 5), press **Enable welcome** / **Save welcome**. That button — not the
card's "Save brain" button — is what creates the rule. It is not offered in the
+ New rule dropdown.

**Key knobs:** `time_only` short 2nd bubble (on) · `question` 3rd bubble, sent
exactly as typed, default "what's yours?" (blank = off) · `gif_id` 4th bubble
(blank = off) · `with_image` · `restyle` let the AI rewrite the 2nd bubble (on) ·
`max_welcomes` 25 per run · `guard_hours` 12.

**Prerequisites:** a working OnlyFans session; the Brain filled in (location and
creator clock drive the time line); for a photo, a per-slot image in the Brain's
time-of-day slots or a vault folder named "bot", "welcome script" or "welcome".
The greeting itself is written by the app, so it still goes out with no AI key —
only the optional restyle needs one.

**Caveats:**
- It will **not** back-fill your existing roster. Anyone with a real conversation
  already (more than 2 messages sent to him, or more than 8 from him) is treated
  as established and skipped — the OF feed also carries renewals.
- One welcome per fan for life; a fan who resubscribes months later gets nothing.
- Preview / ↻ Regenerate with "AI restyle" on is a real, billed AI call.

## kind: send_followup — Follow up quiet fans

**What it does:** A slow drip that re-opens chats with paying fans who went
quiet: up to three AI-written nudges as the silence grows, each optionally with a
time-of-day photo.

**Where:** Automations → Brain card → the **Follow-ups** section. Also creatable
in Automations → Automation rules → + New rule ("Core").

**How to enable:** In the Follow-ups section tick Enabled, set the check interval
(default 45 min) and the three nudge delays, press **Save follow-ups**.

**Key knobs:** the three silence thresholds in hours (real defaults **26 / 64 /
120**) · `with_image` · `limit` 200 fans per run, highest lifetime spend first ·
`exclude_replied_hours` 12.

**Prerequisites:** an agency AI key (every nudge is AI-written); fans with at
least $1 lifetime spend; the Brain filled in.

**Caveats:**
- **The Brain shows 26 / 64 / 256 hours, but 256 is thrown away.** Anything at or
  over 168 hours (a week) is ignored and that step falls back to its default, so
  the third nudge really fires at 120 hours. Saving without touching the boxes
  stores a 256 that can never fire.
- Free and never-spent fans are never followed up — this cannot wake a cold free
  list.

## kind: reengage_buyers — Re-engage buyers who went cold

**What it does:** Finds fans who bought in the last few days but have not been
messaged since, and sends each one warm two-line opener built from his own stored
profile lines. No AI call — the lines are already written.

**Where:** Automations → Ready-made posts & broadcasts → 🤖 AI Chatter → scroll
to the **Re-engage buyers who went cold** block, just above Make It Right.

**How to enable:** There is no switch and no schedule. Set the numbers, press
**Preview** to see the exact openers, then **Send now** — it fires once. It
cannot be added to the rules list.

**Key knobs:** bought within 3 days · cold after 24 hours · max 25 this run ·
skip if messaged within 12 hours · tone soft (warm greeting, then one of his
stored questions) or flirty (a tease, then one of his stored teases) — either
falls back to the other kind of line, then to a generic one.

**Prerequisites:** purchase history in the system; generated fan profiles for the
personal second line (without one the fan still gets a generic opener).

**Caveats:**
- "Cold" means *we* have not messaged him, not that he has not replied.
- There is no cooldown table — sending is itself the brake. Repeat clicks with a
  low "cold after" produce repeat DMs.

## kind: nudge_online — Nudge Online (message a fan when they come online)

**What it does:** Every ~60 seconds it spots fans who just came online and
schedules a short-delayed personal DM — a time-of-day greeting, optionally
followed by a question or tease drawn from his profile. It queues a separate job
that re-checks he is still around before sending.

**Where:** Automations → Ready-made posts & broadcasts → 👋 Nudge Online. Also
reachable from **+ New ▾ → Nudge Online**, which opens the same panel in a
window.

**How to enable:** Tick Enabled and Save. That creates the rule and pins the 60s
detector cadence. "Roll out to models" applies one shared config across many
accounts at once.

**Key knobs:** delay 4 min + up to 3 min random jitter · a fan counts as newly
online after 5 min away · never nudge the same fan twice inside 12 hours · 25
fans per tick, scanning up to 200 online · quiet hours 00:00–07:00 · only fans
already welcomed (on) · skip anyone with a message in either direction in the
last 6 hours · stop after 3 unanswered nudges · optional lifetime and recent
spend floors and ceilings · six time-of-day text and image pools.

**Prerequisites:** a working OnlyFans session; fans who have already been
welcomed; vault images if you want pictures. **No AI key needed** — every line
comes from your pools.

**Caveats:**
- The very first pass on an account sends nothing; it records who is already
  online so switching on does not stampede everyone. Real nudges start on the
  second pass.
- On an account your team chats all day, most nudges never fire: any message in
  either direction inside 6 hours disqualifies the fan. Nudges land on quiet
  fans by design.
- Pressing Save rewrites the rule payload and forces the cadence back to 60s, so
  anything set through the rules list's **Edit raw JSON →** hatch (Automations →
  Automation rules → Edit) is wiped. This tab has no raw-JSON button of its own.
- "Roll out to models" force-disables images on every target account and
  overwrites each one's whole nudge config.

## kind: nudge_online_fire — Nudge Online, fire (internal)

**What it does:** The delayed half of Nudge Online. Nobody schedules it; the
detector creates one per fan, timed a few minutes out. It re-checks the fan is
still online and still eligible, then sends.

**Where:** No screen of its own. It appears in run logs under this name.

**How to enable:** Nothing to enable — it exists only because Nudge Online
created it.

**Caveats:** a `nudge_online_fire` run showing nothing sent is **normal**, not an
error — the fan went offline, a real conversation started during the delay, or
another automation held him. If the picture fails to attach, the text still goes.

## kind: mass_nudge — Mass Nudge (broadcast to everyone online)

**What it does:** On a timer, sends one generic time-of-day message plus one
picture to every fan online at that moment. No personalisation and no names. It
works out the list itself, so it remembers exactly who received it.

**Where:** Automations → Ready-made posts & broadcasts → 📣 Mass Nudge.

**How to enable:** Tick Enabled, set "Send every (min)", Save. "Apply to N
models" rolls the same payload out across accounts.

**Key knobs:** send every 60 min (60-second floor) · with image (on) · skip fans
contacted by anything in the last 12 hours · skip fans who messaged us in the
last 12 hours · scan at most 500 online fans · optional auto-unsend after N hours
· the per-slot text and image pools · dry run.

**Prerequisites:** a working OnlyFans session and lines configured for the time
slot the run lands in. No AI key needed.

**Caveats:**
- Pressing Save rebuilds the payload from the five form fields, silently deleting
  anything set through the tab's **{ } Edit raw JSON** link.
- Leaving a cooldown box **empty** — or typing 0 into it — is not "off": both
  save as blank, and blank means the 12-hour default, so the box reads 12 again
  next time you open the tab. The only way to actually switch the guard off is
  the **{ } Edit raw JSON** link in the tab's header, with
  `"exclude_replied_hours": 0`.
- It only ever sees the first 500 online fans per run; running it more often
  reaches the same slice again, not more people.
- Text is sent exactly as written — placeholders like {name} are never filled in.
- If the slot has no lines configured the run ends quietly with nothing sent.

## kind: online_blast — Blast Online (scale broadcast to all online)

**What it does:** The big-account version of Mass Nudge. Instead of listing
online fans one by one, it hands OnlyFans a single broadcast aimed at the whole
"online now" audience, so it scales to accounts with tens of thousands online.

**Where:** Automations → Ready-made posts & broadcasts → 📡 Blast Online. (The
on-screen label is "Blast Online", not "Online Blast".)

**How to enable:** Tick Enabled, set "Send every (min)", Save. There is no
roll-out across models — switch each account on individually.

**Key knobs:** send every 60 min · with image · skip fans DMed in the last 8
hours · skip fans who messaged us in the last 8 hours · explicit excluded fans
and excluded OnlyFans lists · auto-unsend after 1 hour · record up to 5,000
recipients so the other nudges know not to hit them.

**Prerequisites:** a working OnlyFans session and lines for the current slot. The
bot must be able to maintain an OnlyFans custom list called `Auto_Exclude` — that
list is how "don't hit these people" is enforced.

**Caveats:**
- **Its per-fan cooldown is thinner than Mass Nudge's, so cadence is the
  backstop.** Each run does skip anyone DMed or replying inside the 8-hour
  windows, and it stamps up to 5,000 of the fans it just hit so the next run
  skips them too — but OnlyFans resolves the audience, so anyone past that 5,000
  leaves no trace. Run it hourly, not every few minutes.
- If the exclude-list sync fails the blast is **skipped entirely** rather than
  sent unprotected. A run saying "exclude_list_sync_failed" means nothing went
  out, deliberately.
- Because OnlyFans resolves the audience, the run log cannot say how many people
  received it — read the real number on Settings → Mass messages.
- With no lines configured it falls back to the female generic pool — on a male
  account you must write your own.
- Save rebuilds the payload from the form, deleting raw-JSON extras.

## kind: send_mass_message — Mass message

**What it does:** Sends one message to a whole audience through OnlyFans'
broadcast endpoint, and writes a copy into each known fan's chat so the blast
shows up in your chat view. It is also the engine every other broadcast feature
calls underneath.

**Where:** For a one-off blast use **+ New ▾ → Mass message** — that composer
sends straight away and creates no rule. This *automation kind* is **not** offered
in + New rule; a saved, repeating blast of this kind has to already exist before
you can open it and press **Compose**. In practice you reach this engine through
the features that call it (Premade Mass, PPV Library, funnel openers), not
directly.

**Key knobs:** text · **price in DOLLARS** · media and free previews · audience
(fan ids, saved lists, OnlyFans audiences like "fans"/"following", online only) ·
exclusions · skip fans messaged in the last 6 hours (on by default) · skip fans
who messaged us in the last 2 hours (on by default) · optional funnel to attach.

**Prerequisites:** a working OnlyFans session; text (or an attached funnel with an
opener); at least one audience source.

**Caveats:**
- **The price is in dollars, but the editor hint says "cents".** Typing 500
  meaning $5 sends a $500 PPV.
- Two audience guards are on even if you never touch them (6 hours outbound, 2
  hours inbound), so a blast can reach far fewer people than the list size
  suggests. Only an explicit 0 turns each off.
- Free and priced blasts respect **different** opt-out lists — a priced send
  subtracts MASSppvEXCLUDE, a free one MASSdmEXCLUDE. The wrong list will not
  stop the blast you meant to stop.
- One banned word can end the whole broadcast with nothing sent.

## kind: mass_premade — Premade mass (send / resend / unsend)

**What it does:** Fires ready-made broadcasts on timers. You give it a list of
messages; it sends the first, optionally schedules it to auto-unsend, optionally
re-sends it a set number of times, and drips the rest forward.

**Where:** Automations → Ready-made posts & broadcasts → ♻️ Mass Premade →
**+ New premade mass**. A one-shot version is at **+ New ▾ → Premade Mass**,
which saves nothing.

**How to enable:** The saved definitions **are** the rules — the per-row checkbox
in the list is the switch.

**Key knobs, per message:** one text or a pool of texts · media ids or a vault
folder · how many images per fire · price in dollars · online only · audience and
exclusions · auto-unsend after N hours · resend after N hours · how many extra
resends · delay before the next item in the list.

**Caveats:**
- Text and images are **re-randomised on every fire** — a resend is a different
  message with different pictures, not a repeat.
- A vault-folder image pool only ever sees the folder's **48 most recent non-DRM
  photos**. Videos are never picked.
- The rule's own cadence fires **on top of** the payload's resend timers, which
  multiplies the sends.

## kind: reply_mass_funnel — Walk mass-funnel replies

**What it does:** Watches fans who replied to a funnel broadcast and walks each
through the funnel's follow-up and PPV steps.

**Where:** Automations → Ready-made posts & broadcasts → 🎯 Mass funnels → the
**Reply-walking** banner at the top of the tab.

**How to enable:** Press **Enable (every 2 min)** on that banner. Also creatable
in + New rule ("Advanced").

**Key knobs:** leave `mass_run_id` blank so one rule walks every active funnel on
the account — that is the normal setting · 40 fans advanced per tick · run every
2 minutes · dry run · `test_fan` to try a funnel on yourself.

**Prerequisites:** a funnel with steps saved and an opener actually sent; for any
paid PPV step, vault media mapped for **that funnel on that model** — vault ids
do not carry between models. Only steps that generate their text need an AI key.

**Caveats:**
- **Without an enabled rule of this kind, every funnel sends its opener and then
  nothing.** Fans reply and the funnel looks broken. This is the single most
  common funnel complaint.
- Fans who never reply are never walked — a reply is what enrols someone.
- New fans stop being enrolled 14 days after the broadcast went out.
- A paid step whose vault media was never mapped for that model is skipped, so a
  funnel that works on one model can stall on another.

## kind: auto_posts — Auto posts (self-cleaning feed)

**What it does:** Publishes a saved list of ready-made feed posts one at a time,
waiting the gap you set. Each post can delete itself after N hours, so the feed
cleans itself up.

**Where:** Automations → Ready-made posts & broadcasts → 🗓️ Auto Posts →
**+ New auto post**.

**How to enable:** Build it, tick Enabled, Save. The saved definitions are the
rules; the per-row checkbox is the switch.

**Key knobs, per post:** one caption or a pool · media ids and/or a vault folder ·
how many images per fire · **price in dollars** for a paid post · free teaser
images · `hours_to_live` auto-delete · `delay_minutes` before the next post ·
whole-list `resend_after_hours` + `resend_count` · dry run.

**Prerequisites:** a working OnlyFans session; vault media for images. For a
**paid** post the account must be a free page — a paid-subscription page has no
paid-post lane on OnlyFans.

**Caveats:**
- Price is in **dollars**. Typing 1000 meaning $10 posts a $1,000 post.
- On a paid-subscription page a priced item is silently **dropped**, not posted
  free — you get a partial drop with no error.
- The saved rule's timer and the payload's resend cycle both run, so a list can
  repost on two clocks at once. The editor defaults the timer to every 24 hours.
- The resend cycle needs **both** `resend_after_hours` and `resend_count` above
  zero; setting only one does nothing.

## kind: auto_stories — Auto stories (from a vault folder)

**What it does:** On a schedule, posts random photos from a vault folder as
OnlyFans stories, each optionally deleting itself after a set number of hours. It
also hides the duplicate copy OnlyFans files in the vault when it re-uploads the
photo.

**Where:** **Settings → Auto stories.** Owner/admin only — this is the one
automation tab that genuinely lives under Settings.

**How to enable:** Pick a folder, choose a schedule, tick Enabled, press **Create
automation** / **Save changes**.

**Key knobs:** vault folder (and/or hand-picked images — they combine into one
pool) · stories per fire (1) · `hours_to_live` (the tab offers 6; 0 = keep) ·
remove the re-uploaded vault duplicate (on) · optional watermark · schedule
either **"At set times each day"** (clock times, default 09:00) or **"Every few
hours"** (default 6) · optional total run limit.

**Prerequisites:** a working OnlyFans session and a vault folder containing
photos.

**Caveats:**
- **Only photos are ever posted.** Videos in the folder are invisible, and only
  the folder's 48 most recent non-DRM photos are ever in the pool.
- **"Total runs" counts every auto-stories run this account has ever had** — from
  any rule and every "Run now" — not this rule's runs. A number below the
  account's history disables the rule on the very next tick.
- Clock times are frozen with the time-zone offset of the browser you saved from.
  Save from another zone, or ride through a daylight-saving change, and the
  stories move by an hour until you re-save.
- "Every few hours" posts its first story right away — within about 30 seconds
  of saving — and then every N hours after that. Do not also press Run now
  straight after saving, or you post two stories back to back. (The tab's own
  helper text says otherwise; the tab is wrong.)

## kind: unsend_messages — Unsend messages

**What it does:** Deletes messages already sent — a whole broadcast pulled back
from every recipient, individual chat bubbles, a feed post or a story, or a sweep
that cleans up anything older than the hours you set.

**Where:** **Settings → Mass messages → the "Auto-unsend (automation)" card.**
Tick the model, set the hours per class, Save. To pull back one broadcast by
hand, use the cancel button in the Mass messages list on the same tab.

**How to enable:** Saving that card creates or updates one hourly rule per
account named "Auto-unsend mass". Ticking alone does nothing until you Save.

**Key knobs:** auto-unsend free text-only broadcasts after N hours (the card
offers 4, on by default) · broadcasts with image/video after N hours (the card
offers 24) · **priced PPV broadcasts after N hours (the card offers 48)** · a
class left unticked is kept forever.

**Caveats:**
- **A priced broadcast is never auto-unsent before 24 hours**, whatever you type
  — your number is silently raised. About half of PPV revenue lands more than 4
  hours after the send.
- An unsend job with a blank policy does **not** do nothing: it falls through to
  the per-chat sweep and deletes your own text-only bubbles older than 9 hours
  across every chat.
- Each sweep removes at most 200 items per run **in total** — the ticked classes
  share that one allowance, oldest first — so a big backlog clears gradually over
  many hourly runs, and one large class can use up the whole run's budget before
  the others are reached.
- Unsending a funnel's opening broadcast permanently stops new fans enrolling
  into that funnel.
- Every removal is irreversible and is recorded against "Automation", not a named
  chatter.

## kind: scheduled_send — Scheduled 1:1 send

**What it does:** Delivers a direct message a chatter wrote earlier, at the exact
minute they picked. Because it lives on the server it survives closing the tab,
every chatter on the model sees it queued, any of them can cancel it, and the
delivered message is credited to the chatter who scheduled it.

**Where:** The **schedule picker in the chat composer**. It then shows as a
pending bubble in that fan's chat thread — every chatter on the model sees it
there, and any of them can cancel it from that bubble before it fires.
(Settings → Scheduled is a *different* list: it shows OnlyFans' own queue, i.e.
the sends further than 15 minutes out.)

**How to enable:** Nothing to enable — it is created automatically whenever a
chatter schedules a message.

**Caveats:**
- Only sends **15 minutes or less** in the future use this. Anything further out
  is handed to OnlyFans' own queue — that is what Settings → Scheduled lists and
  cancels. The near-term ones are cancelled from the pending bubble in the chat
  thread instead, not from that page.
- Cancelling only works while it is still waiting; once the worker picks it up it
  is already on its way.

## kind: ppv_send — PPV Library sender

**What it does:** Sends the premade PPVs you built in the PPV Library. One saved
PPV goes to every fan, but each group pays a different price — big spenders more,
never-paid fans less, active fans more than cold ones. The free preview picture
and the caption change every send, so a resend never looks like a repeat.

**Where:** Automations → Ready-made posts & broadcasts → 💸 PPV Library.

**Building the PPVs:** the cards are greyed out until the master switch reads
**Library ON** — turn it on first, then build. At the bottom of the list,
**+ Add PPV** starts a blank one, or start from a preset — **🍑 Weekly tease**
($15, 3× a week), **🐳 Whale drop** ($60, once a week) or **💔 Win-back** ($9,
twice a week). **Duplicate** copies a PPV you already built, which is how you get
to twenty quickly. On each card: press **Pick content** for the photos/video the
fan pays to unlock, tap **⭐** on any of those thumbnails to show that one free as
a teaser (no star = fans see only a locked message), pick a **Caption style** or
write your own lines in the caption boxes under it, then set the **Base price**
($3–$200) and **How often**. A PPV with no content picked will not send, and the
card says so.

**✨ Build a week from vault** reads your described vault and proposes a week of
bundles — content, ⭐ teasers, price, cadence and caption style. It sits outside
the greyed-out list, so it works with the library still off. It only proposes:
you tick the ones you want, press **Add N to library**, then **Save**.
**Everything it adds arrives switched OFF** — both *Send as PPV messages* and
*Post to feed* — so nothing can send until you turn it on one by one. Running it
again refreshes the content of bundles it made before instead of duplicating
them, and keeps any price or caption you changed. (A PPV you add by hand or from
a preset arrives with both boxes **ticked** — the opposite default.)

**How to enable:** Tick the master switch ("Library ON"), tick at least one PPV,
Save. Saving upserts a single hourly rotator rule for the whole account. It
cannot be created from the rules list.

**Key knobs:** per PPV — vault media, caption pool or your own lines, base price
($3–$200), free teaser picks, sends per week (1–14), resend monthly, exclude
fans who already own it, also post to feed. Account-wide — "send to everyone"
broadcast, PPV caps (2/day, 14/week, 60/month), quiet hours, price floor and
ceiling, whale gate by lifetime spend, AI caption at send.

**Caveats:**
- Ticking the master switch is **not enough** — if every individual PPV is
  unticked the rule is saved disabled and nothing sends.
- Leaving the caps blank is **not** "no limit": the house default 2/day forces a
  minimum 12-hour gap between any two PPV sends. Only explicit zeros turn spacing
  off.
- The whale gate counts **everything** a fan ever spent, subscriptions included —
  $10/month for 40 months trips a $400 gate without a single PPV purchase.
- OnlyFans rejects any priced message under $3.00, so every price is floored
  there.
- The tab's save rebuilds the config from a fixed list of keys, so anything set
  outside the tab is dropped on the next save.

## kind: tip_reward — Tip Reward

**What it does:** The moment a fan tips, sends him a free bundle of vault media
he has never received. The bigger the tip, the more items; which folder they come
from depends on how much he has tipped recently.

**Where:** Automations → Ready-made posts & broadcasts → 🎁 Tip Reward → the
REWARD section.

**How to enable:** Turn on that section's switch, fill in at least one tier
folder, Save. **There is no rule and no schedule** — it is event-driven off the
tip itself. Do not go looking for a `tip_reward` rule; there isn't one.

**Key knobs:** three tier folders with thresholds ($0 / $10 / $100) · $5 buys one
item · at least 2, at most 12 items · tips added up over the last 72 hours decide
the tier · allow videos (off) · optional thank-you caption · fire even when a PPV
offer is open (off) · match some items to what he actually asked for in chat (on).

**Prerequisites:** at least one tier folder filled; a working OnlyFans session and
the live event feed; unseen media for that fan. The chat-matching option needs an
AI key.

**Caveats:**
- Tier is chosen by his **running total over the window, not just this tip** —
  his tips in the last 72 hours are added up, and the bigger of that total and
  this single tip picks the folder. A fan who tipped $150 two days ago gets
  premium folders for a $5 follow-up, and three $40 tips in three days reach the
  $100 tier even though no single tip did.
- Only tiers that actually have folders count. With just "basic" filled, a $200
  whale is served from basic, silently.
- A fan who has seen everything in his tier gets a reward of zero items, and that
  counts as done — it is never retried.
- With videos allowed, one clip can eat several of the items the tip paid for.

## kind: tip_request — Ask a quiet mass-buyer for a tip

**What it does:** Finds fans who bought a mass PPV and then went silent, and
sends each one free vault teaser picture with a caption asking for a tip.

**Where:** Automations → Ready-made posts & broadcasts → 🎁 Tip Reward → scroll
to the bottom section, **"Ask a quiet mass-buyer for a tip"**. Do not confuse it
with **"Ask fans to tip for content"** at the top of the same tab — that is a
different switch, for how the chatbot answers "can i see…".

**How to enable:** Turn the section on, pick teaser images, Save — **and** an
enabled `tip_request` rule must exist. That kind is not in the + New rule
dropdown, so an owner cannot finish the setup from the UI today; it has to be
created through the API. **Tell staff this is a known gap** rather than sending
them hunting.

**Key knobs:** the teaser image pool (one picked at random per fan) · the caption ·
wait 2 hours after the purchase · do not chase a purchase older than 48 hours ·
one request per fan per week · skip anyone contacted in the last 6 hours · 200
fans per run.

**Caveats:**
- With an empty image pool nothing sends even when enabled.
- A fan owed an undelivered custom is never asked for a tip — nothing overrides
  that.

## kind: make_right — Make It Right (resolution agent)

**What it does:** Catches a fan who got the wrong outcome — above all one charged
twice for the same content — and makes him whole: an apology plus a few free
unseen pieces, up to twice per fan, then hands him to a human. **Money is never
refunded automatically**, only flagged for review.

**Where:** Automations → Ready-made posts & broadcasts → 🤖 AI Chatter → scroll
to the **Make It Right** section. (There is no "🤝 Make It Right" tab, despite
what some in-app text says.)

**How to enable:** It is **on by default for every account**. Saving that section
both stores the config and owns the rule.

**Key knobs:** enabled and auto-send (both on) · look back 30 days · only
apologise while at most 3 messages have passed since the mistake · at most 2
make-rights per fan · 1 free piece per turn, 1 free turn · optional custom apology
wording · flag a refund for review (on) · nudge after 24h silence, close after
48h · optional priced pivot after the free turns · also catch "paid but got
nothing" (off).

**Prerequisites:** Tip Reward tier folders filled — the free pieces are pulled
from that library, so an account with empty tiers has nothing to gift.

**Caveats:**
- It is on by default, so an account nobody ever opened this tab for is still
  apologising and gifting free content.
- Gift size **multiplies**: free turns × pieces per turn. Three and three is nine
  free pieces across three messages.
- Two apologies per fan and it stops — the third incident goes to an operator
  with no send.

## kind: customs_watch — Customs owed (tip → voice note)

**What it does:** Tags fans who tipped for a custom and have not received it, and
**stops the AI selling to them** until it ships. It sends the fan nothing.

**Where:** Add it in Automations → Automation rules → + New rule ("Core",
"Customs owed"). The work queue is **Stuff → Customs** — visible to
chatters. A debt clears two ways: someone clears it there, or the watch's next
sweep sees the voice note actually go out to that fan and clears it itself.

**Clearing a debt:** open **Stuff → Customs**. It is one cross-account queue,
split into two lists: **Owed** (he paid, the voice note has not gone out) and
**Worth a look** (a tip big enough to be an order that nothing tagged — it may
just have been generosity). Each row shows how long he has been
waiting — red past two days — the fan, the model, what he paid and his lifetime
spend. **Read** opens the messages around that tip without leaving the page: the
tip says he paid, the conversation says what he paid *for*. Once the voice note
has actually gone out, press **Sent**. That settles the row in Fastt and lifts
the "he already paid, stop selling" brake so the AI can sell to him again;
nothing is pushed to OnlyFans. On a *Worth a look* row the same button reads
**Dismiss** and just settles it so it stops coming back.

`customs_watch` also clears a row by itself if it sees a voice note go out in
that thread after he paid. If an account has been taking custom-sized tips with
no `customs_watch` enabled, an amber banner at the top of the tab names it.

**Key knobs:** `min_cents` how big a tip burst counts as an order (default
**$50**) · look back 72 hours · 200 tips per run · dry run.

**Caveats:**
- **Set `min_cents` above $100 and the trap is silent:** a chatter can quote a
  price this watch refuses to book, so a fan pays and is never marked as owed.
- Stacked tips inside one burst are **one** order, not several — the amount
  encodes how *long* the voice note is, not how many.
- It marks a fan owed with **no timeout** — nothing expires on its own. The bot
  will not sell to him until the debt clears, either by someone clearing him on
  Stuff → Customs or by the watch seeing the voice note go out. If the custom is
  never delivered and nobody clears it, he stays blocked forever. That is
  deliberate.
- An account that takes custom-sized tips with no enabled rule looks exactly like
  a healthy empty queue — Stuff → Customs prints an amber warning listing those
  accounts.

## kind: arc_tease — Arc free-DM drop

**What it does:** One free teaser DM that belongs to an approved Vault AI content
arc. On its booked day it reads the tease line from the live arc and broadcasts
it free.

**Where:** No surface of its own. It is booked when an operator approves a PPV
week or month on Automations → 🧠 Vault AI.

**How to enable:** Nothing to enable — approving an arc books each free-tease day
as a one-off job. To stop it, cancel the arc.

**Caveats:** it goes to everyone (fans + following) with the usual "don't
re-contact recently" guards deliberately off; it is free so it never counts
toward the PPV caps; an arc that has played out stops and waits for a human
rather than regenerating itself.

## kind: describe_media — Describe vault media (Vault AI)

**What it does:** A background sweep that looks at vault photos and videos with an
image-reading AI and writes down what is in them, plus a suggested caption and
script. It is what makes vault media searchable and sellable by the other
automations. It sends nothing to fans.

**Where:** No tab configures it. The Vault AI master switch is on Automations →
🧠 Vault AI; the on-demand "Describe all" button is on the Vault page.

**How to enable:** There is no click path — its rule has to be created through the
API, and the Vault AI master switch must be on. Say exactly that rather than
inventing a path.

**Caveats:**
- The "Vault AI ON" toggle only writes settings; it does not create the rule. A
  tab can read ON while the sweep has never run.
- One sweep may spend up to **80% of the account's whole daily AI budget** by
  default, so the first runs on a big vault can starve every other automation.
- Descriptions are applied straight to the vault item — there is no review step.
- Needs a DeepInfra key for the vision model and a DeepSeek key for the copy.

## kind: vault_ai_consume — Apply approved Vault-AI items

**What it does:** Takes the Vault AI suggestions a human already approved on the
Vault review screen and applies them: a folder proposal becomes a real internal
vault folder, a PPV proposal becomes a switched-off draft in the PPV Library.

**Where:** Approvals happen on the **Vault** page → Vault review. The Vault AI
master switch is on Automations → 🧠 Vault AI.

**How to enable:** No click path — the rule must be created through the API, and
the Vault AI master switch must be on.

**Caveats:** an approved PPV lands in the Library **switched off** — someone must
arm it or it never sends; an approved folder is internal to Fastt only and does
not appear in the OnlyFans vault; if the world moved after approval the item is
skipped silently and stays "approved".

## kind: vault_daily_reminder — Daily vault reminder (Vault AI)

**What it does:** Once a day it picks a few unseen photos from a named internal
Vault AI folder, pairs them with one of your written lines, and puts it up as a
card for a human to approve. When approved, the *next* run sends it as a free
broadcast to the fan list.

**Where:** Approval is on the **Vault** page → Vault review → Reminders.

**How to enable:** There is no configuration screen at all — its folder, lines and
counts can only be set through the API, and both the Vault AI master switch and
the reminder's own switch must be on. Treat it as not operator-configurable
today.

**Caveats:** approval is not a send — the card goes out on the next tick, so with
no rule it sits approved forever; only one card can be open at a time, so an
un-reviewed card stalls the whole feature; when it sends it is a **free broadcast
to the whole fan list**.

## kind: auto_follow — Auto-follow / Auto-like

**What it does:** Pokes fans with an OnlyFans **notification** instead of a DM. It
can like a fan's most recent message, follow lapsed fans back, or "ping" a quiet
fan by unfollowing and instantly re-following so a fresh "started following you"
push fires. It never sends a message and costs no AI credits.

**Where:** **Growth → ❤️ Auto-follow.** Owner/admin only. None of its knobs are
on the Automations page and it is not in the + New rule dropdown — but once
saved, its rule row *does* appear in the Automations rules list as "Auto-follow /
Auto-like". Leave that row alone; deleting it stops the schedule.

**How to enable:** Set the options, **untick Dry run**, tick Enabled, press
**Create automation** / **Save changes**. Both boxes matter — see below.

**Key knobs:** Action — "Like latest message (re-engage)" / "Follow fans back
(win-back)" / "Re-follow ping (quiet fans)" · Max actions per run (50) · Target —
for Follow: "Recently-expired fans (win-back)" (pick it yourself — the box does
not switch to it for you), "Recently-active
fans", "A Smart List"; for Like: recently-active or a Smart List only · Active
within (7 days) · for Ping: quiet after 7 days, min 14 days between pings · Run
every (240 minutes).

**"Can I follow fans back after they expire?"** Yes. Action = **Follow fans back
(win-back)**, Target = **Recently-expired fans (win-back)** — that is OnlyFans'
own lapsed-subscriber list. **Changing the Action does not change the Target for
you** — after you pick "Follow fans back" the Target box still reads
"Recently-active fans", so set it to "Recently-expired fans (win-back)" yourself
before saving. A fan with a lapsed relationship is re-armed and reported as
"refollowed".

**Caveats:**
- **Dry run is ticked by default**, and so is any rule whose payload never set it.
  Enabled + Dry run = scheduled and doing nothing. The Growth → 📊 Overview badge
  says "DRY RUN" when that is the case — read it.
- **Run now uses the last SAVED settings, not what is on screen.** Save first.
- Setting "Max actions / run" to 0 switches it off — 0 is a stop, not unlimited.
- **Follow aimed at fans you already follow fires nothing forever** — they
  classify as "already followed". To re-notify an existing follower use the
  **ping** action instead.
- Follow and ping only ever touch **free** profiles. A page with a subscription
  price, or one whose price cannot be read, is skipped — following a paid page
  would spend money. A run that notifies nobody is normal on a roster of paid
  pages.
- "Re-follow ping" ignores the Target setting entirely; it builds its own pool of
  quiet fans.
- "Like latest message" re-likes the same message every run until the fan writes
  again, so later runs on an unchanged pool notify nobody.

## kind: promo_reactivate — Keep promos running

**What it does:** OnlyFans ends a profile promotion when its clock runs out or its
claim cap fills. This checks the campaigns you marked "Keep running" and, if
nothing is on offer any more, re-creates the same campaign with the same terms.

**Where:** **Growth → 📣 Promotion** → the **♻️ Keep promos running** card at the
bottom of that tab. It is a card, not a tab, and none of its knobs are on the
Automations page — though once saved its rule row shows up in the Automations
rules list as "Keep promos running". Leave that row alone.

**How to enable — three things, all required:** tick **"Keep running"** on the
individual campaign higher up the tab; then in the card tick Enabled, **untick
Dry run**, and Save.

**Key knobs:** check every 60 minutes · dry run (**on** by default) · max re-arms
per campaign (0 = unlimited).

**Caveats:**
- Dry run is on by default, so an operator who ticks Enabled and saves gets an
  automation that reports forever and re-creates nothing.
- **Only one campaign per account can be set to "Keep running."** Arming a second
  is refused.
- If any promo is live on the profile — including one made by hand — it stands
  down and re-creates nothing.
- "Check now" runs the last saved config; with dry run off it can create a real
  public promo immediately.

## kind: gen_info — Generate fan profiles

**What it does:** Reads each fan's stored chat history and has the AI write him a
profile — a nickname tag, a one-line bio, a fact sheet, three personal questions
and three teases — plus the facts it found (age, city, job, hobbies, language).
That is what the fan drawer, the chat Lines picker and the sheet export read.

**Where:** Automations → Automation rules → + New rule → "Generate fan profiles"
("Core"). Suggested cadence: daily. Its model can be overridden per purpose in
the Brain.

**Key knobs:** `limit` 200 fans per run, highest spend first · `model` ·
`force_ids` profile these now ignoring every gate · `refill_ids` narrow the pass
with gates on.

**Prerequisites:** an AI key; chat history already in the system (it reads that
from our own database, never from OnlyFans); daily AI cap headroom. Note: after
it writes a profile it re-pushes the structured nickname onto OnlyFans, so an
OnlyFans nickname a human typed can be overwritten even with Apply profiles'
"push to OF" left off.

**Caveats:**
- A fan is never profiled until he has sent **8 new messages** since his last
  profile. Raising the limit or the cadence will not profile a quiet fan — only
  `force_ids` will.
- Refresh frequency is set by his spending, not your cadence: every 2 days for
  anyone who spent in the last 7 days, is a $500+ lifetime whale, subscribed in
  the last 7 days, or has ever spent anything at all; every 7 days only for fans
  with $0 lifetime who are not new subscribers. Your cadence only controls how
  often it looks.

## kind: apply_profiles — Apply profiles (nick + notes)

**What it does:** Stamps the generated profiles onto the fan — the cleaned
nickname and a 200-character fact-sheet note. By default into Fastt only; tick
"push to OF" and it also writes them onto OnlyFans where the whole team sees them.

**Where:** Automations → Automation rules → + New rule → "Apply profiles"
("Core"). Suggested cadence: hourly. It also runs automatically as the second
half of Onboard pre-AI fans.

**Key knobs:** `limit` 500 per run · `force_ids` bypass the 24-hour cooldown ·
`push_to_of` (off).

**Caveats:**
- The sweep is ordered by fan id, lowest first, and cut at the limit — on a big
  account the same low ids are re-checked every run and higher ids are never
  reached. Raise the limit, not the cadence.
- The note only lands on OnlyFans for **current subscribers**; an expired fan gets
  the nickname and no note, and the run still says success.
- `push_to_of` **overwrites** whatever a human typed in those OnlyFans fields.

## kind: process_old_fans — Onboard pre-AI fans

**What it does:** A one-time catch-up over fans who subscribed before the AI was
running. It flags them so the warm-up chatter never opens a conversation with
them cold, then builds their profile and applies the nickname and note. **It
sends no messages.**

**Where:** Automations → Ready-made posts & broadcasts → 🆕 Onboard old fans. It
is also offered as a tick box when connecting a brand-new account.

**How to enable:** It is a button, not a switch. Set the count, press **Onboard
recent fans**, confirm.

**Key knobs:** how many recent fans (the card pre-fills 100) · order by newest
subscribers or most recently messaged · flag only, no profiling (costs no AI) ·
push nicknames and notes to OnlyFans.

**Caveats:**
- **It cannot be undone from this screen** — every fan in the batch gets a
  permanent pre-AI flag.
- It is an action, not a loop. Built as a recurring rule it re-flags your newest
  subscribers as pre-AI fans on every tick, which is the opposite of onboarding.

## kind: scrape_chats — Backfill chat history

**What it does:** Pulls conversation history down from OnlyFans to fill gaps the
live message feed missed.

**Where:** Automations → Automation rules → + New rule → "Backfill chat history",
under **Advanced**. Suggested cadence: daily.

**Key knobs:** `limit` chats per sweep (the editor suggests 50; a blank value
actually covers 100) · `max_pages` 40 · `fan_ids` to backfill specific chats.

**Caveats:**
- It does **not** fetch a whole conversation: per chat it keeps roughly the newest
  100 messages plus, once only, about 100 from the very beginning. The middle of a
  long history is skipped on purpose.
- The sweep walks the inbox most-recently-active first, so quiet old chats sit
  past whatever limit you set — name the fan in `fan_ids` to reach him.
- Every connected account gets one automatic sweep when the relay restarts, so
  you will see backfill runs nobody scheduled.

## kind: push_to_sheets — Export to Google Sheet

**What it does:** Exports every profiled fan to one tab of a Google Sheet — name,
nickname, bio, bullets, the questions and teases, lifetime spend, message count
and the note.

**Where:** Automations → Automation rules → + New rule → "Export to Google
Sheet", under **Advanced**. Paste the sheet's link into the spreadsheet field.

**Key knobs:** spreadsheet link · tab name (default "Main") · `limit` 5000 fans ·
create the tab if missing (on).

**Caveats:**
- **Every run clears columns A–Z of the target tab** before writing. Anything a
  human added there is wiped.
- With the sheet and tab left blank the export goes to a built-in shared sheet on
  a tab called "Main" — two accounts left blank overwrite each other.
- The Google authorisation has to be set up on the server by hand; if the token
  expires every run just errors until someone re-mints it.
- You do not need this to read the data — the same columns are browsable in
  Stats → User data.

## kind: audience_sync — Audience folder sync (include-only mode)

**What it does:** Keeps a local copy of one OnlyFans chat folder that decides
which fans the automations may touch. Every 15 minutes it re-reads the folder,
can add brand-new subscribers to it for you, and maintains the exclude list that
broadcasts subtract so they stay inside the fence too.

**Where:** Automations → Brain card → the **Audience** section.

**How to enable:** Set **Mode** to Shadow or Enforce, pick a **Folder**, and save
the Brain. The rule is created for you on that save.

**Key knobs:** Mode — "Off — all fans" / "Shadow — measure only" / "Enforce —
folder members" · the folder · "auto-add new subscribers" (off) · a "Backfill
existing fans…" button.

**Caveats:**
- Going straight from Off to **Enforce** is refused unless you accept a warning
  showing how many active subscribers would fall outside the fence. Run Shadow
  first.
- Enforce does not enforce immediately — until the first sync lands it behaves as
  Shadow, and the status line says so.
- If the local copy goes stale or the folder exceeds 20,000 members, Enforce
  **halts**: the gated automations send nothing rather than falling back to your
  whole fan list. Silence is the deliberate failure mode.
- Taking a confirmed fan **out** of the folder is final — the sync will never
  re-add him.
- Some sends deliberately escape the fence: anything a human or chatter sends,
  tip-reward thank-yous, Make It Right repairs, unsend cleanup, posts and
  stories, OnlyFans' native welcome, and the PPV house broadcast.
- If someone deletes the "Audience folder sync" rule, re-saving the Brain with the
  same settings will **not** bring it back. Set Mode to Off, save, set it back,
  save.

---

# NOT SUPPORTED

These come up often and genuinely do not exist. Say so plainly, then point at the
nearest real thing.

**Posting to Instagram, X/Twitter, TikTok, Reddit or any other platform.**
OnlyFans only. The bot is in fact actively *blocked* from mentioning off-platform
contact, because that is flaggable on OnlyFans. Nearest real thing: Growth →
🔗 Tracking Links builds an OnlyFans link you paste on the other platform
yourself, and OnlyFans counts the clicks and the subscribers it produced.

**Unsending a 1:1 chat message older than about 24 hours.** That is OnlyFans'
own edit window; past it OnlyFans refuses. Important near-miss: a **mass
broadcast has no window at all** — it can be pulled back from every recipient
basically forever, via the chat prompt "also unsend from everyone who received
it?" or the Auto-unsend sweep.

**Editing a message you already sent.** There is no edit anywhere, in Fastt or
via OnlyFans' API. Unsend and resend instead. (Feed *posts* can be edited.)

**A true "the moment he logs in" trigger.** Login detection is polling, about
once a minute — that is Nudge Online, and it is the closest thing that exists.
Real-time dispatch exists only for an inbound **message**: Automations →
⚡ Reply Instant makes the bot answer in seconds instead of waiting for the 30
second tick. It is off by default per account.

**Automatic delivery of a custom.** Deliberate product ruling, not a gap. The
product is a **voice note**, a person records and sends it, and the amount tipped
encodes how *long* it is, not how many. What Fastt does: notice a $50+ tip burst,
mark the fan owed, **stop the bot selling to him**, and put the debt on the
Stuff → Customs for a human to clear.

**Sending a story to specific fans.** A story has no audience — OnlyFans' story
endpoint takes no recipient list, and Auto stories has no targeting knob. If the
point is reaching specific people, send a message instead.

**Permanently deleting media from the OnlyFans vault.** OnlyFans has no
hard-delete for vault media. What "remove" does — in Fastt and in OnlyFans' own
web app — is **hide** it: anything already attached to a sent PPV or a live post
keeps working, and there is no unhide, so treat it as one-way. Folders *can* be
deleted for real.

**AI-generated voice notes or audio of any kind.** There is no text-to-speech in
the product. (Careful: the "voice" settings mean the *writing* voice — whether
the account writes as a woman or a man.)

**Replying to comments on posts.** No screen for it — nothing in Fastt opens a
comment thread or answers one, and comments are not part of any automation lane.
Comment *counts* do show on post cards, and Stuff → Top posts can sort by
comments, but that is the whole of it. Reply in OnlyFans.

**Running or scheduling OnlyFans live streams.** Streams are read-only in the
API and are not surfaced anywhere in the app. Run them in OnlyFans.

**Requesting a payout or withdrawing earnings.** Money is read-only here —
balances and payout eligibility are shown, but nothing moves money. Withdraw in
OnlyFans.

**Scheduling the *Auto posts automation* to a specific clock time.** Auto posts
runs on an interval with per-post delays, not at a wall-clock time. Everything
else timed *is* supported: a **one-off feed post** (+ New ▾ → 📝 New post, fill
the Schedule field — the button becomes "Schedule post"), a **1:1 DM** (the
schedule picker in the chat composer), a **mass message** (from the Mass message
composer), and **Auto stories** at set times each day.

---

# When someone says "I turned it on and nothing happened"

Work down this list:

1. **Is the account's OnlyFans session still alive?** Check **Setup → the
   accounts table**. A model showing a red **"automations paused"** badge with
   **"unlinked N ago"** next to it has lost its OnlyFans login — either OnlyFans
   rejected the login (the creator logged in elsewhere or unlinked it) or there
   is no captured session for that account on this host. Hover the badge and it
   tells you which. The model picker marks the same account with a small red
   **"paused"**. While that badge is up **every** automation on that account is
   skipped: nothing sends, queued jobs are left parked, and — this is the part
   that misleads people — **no run rows are written at all**, so Automation runs
   looks empty rather than failing. The rules all still read "enabled."
   **Fix:** re-capture the session (Setup → Paste cURL). That clears the pause
   immediately and the parked jobs resume on the next tick. The relay also
   re-tests a paused account on its own and un-pauses it the first time the
   session works — every 10 min for the first hour, every 30 min for the first
   day, hourly for the first week, then every 6 hours.
2. **Is there an enabled rule?** Many tabs only save settings. Check Automations
   → Automation rules for a rule of that kind with its box ticked. The classic
   offender is 💬 Auto Convo, which needs the rule added by hand.
3. **Is dry run still on?** Growth → ❤️ Auto-follow and the ♻️ Keep promos
   running card both default to dry run. Growth → 📊 Overview shows "DRY RUN"
   when a rule is scheduled but inert.
4. **Is the agency AI key set?** Setup → Your AI keys. Without it every
   AI-written message fails closed — chat replies, follow-ups, profiles. The
   welcome and the nudges still send, because their text is not AI-written.
5. **Is the daily AI cap used up?** The cap is on the Brain card. Runs at the cap
   still report "ok" and just do less.
6. **Is a cooldown eating it?** Most senders skip a fan any automation or chatter
   touched recently — commonly 6 or 12 hours. An empty cooldown box means the
   default, not off — and on the nudge tabs typing 0 saves as empty too, so the
   guard stays on. Turning one off takes the **{ } Edit raw JSON** link in that
   tab's header.
7. **Is the fan restricted?** Settings → Restrictions lists fans restricted from
   automations and fans restricted on OnlyFans. Both are hard skips.
8. **Is he owed a custom?** A fan on the Stuff → Customs queue is deliberately
   withheld from selling until the debt is cleared.
9. **Is the Audience fence on and Enforce halted?** Check the status line in the
   Brain's Audience section.
10. **Did a tab Save wipe the setting?** Several tabs rebuild their whole payload
    on Save and drop anything set through raw JSON. Re-check after saving.
11. **Read the run log,** not the tab: Automations → Automation runs at the
    bottom of the page is the authoritative history. The one exception is a
    paused account, which writes no runs at all — check its session badge on
    Setup first.
