/* palette.js — a global command palette (⌘K / Ctrl-K) for fast navigation.
 *
 * The skin has ~54 pages and 16 creators; jumping between them by mouse is
 * slow. This gives every page an instant fuzzy launcher: type to filter pages,
 * switch creator, or fire a quick action, then Enter. The real app has no such
 * thing — it's a pure friendliness win. Auto-loaded by _shared/fastt.js.
 */
(function () {
  "use strict";
  if (!window.Fastt) return;
  const F = window.Fastt;
  const esc = F.esc;
  const dec = (s) => s.replace(/&amp;/g, "&").replace(/&lt;/g, "<").replace(/&gt;/g, ">");

  // name → file, grouped by section (from the canonical sidebar + the extras).
  const PAGES = [
    ["Dashboard", "dashboard.html", "Home"],
    ["Messages Pro — inbox", "messages.html", "Chat"],
    ["Group chat — multi-pane", "group.html", "Chat"],
    ["Notifications", "notifications.html", "Home"],
    ["Leaderboard", "leaderboard.html", "Home"],
    ["Referrals", "referrals.html", "Home"],
    ["OF Manager", "ofmanager.html", "Home"],
    ["Creator reports", "analytics-creator-reports.html", "Analytics"],
    ["Employee reports", "analytics-employee-reports.html", "Analytics"],
    ["Fan reports", "analytics-fan-reports.html", "Analytics"],
    ["Message dashboard", "analytics-message-dashboard.html", "Analytics"],
    ["Automations — overview", "automations.html", "AI Selling"],
    ["AI Chatter / Seller", "ai-chatter.html", "AI Selling"],
    ["Auto Convo", "ai-auto-convo.html", "AI Selling"],
    ["Re-engage Buyers", "ai-reengage.html", "AI Selling"],
    ["Brain & Persona", "ai-brain.html", "AI Selling"],
    ["Scripts & Pricing", "ai-scripts-pricing.html", "AI Selling"],
    ["Reply Timing", "ai-reply-timing.html", "AI Selling"],
    ["Mass Message", "outreach-mass-message.html", "Outreach"],
    ["Broadcast", "outreach-broadcast.html", "Outreach"],
    ["Funnels", "funnels.html", "Outreach"],
    ["Follow-ups", "outreach-followups.html", "Outreach"],
    ["Scheduled Sends", "outreach-scheduled.html", "Outreach"],
    ["Welcome new subs", "onboarding-welcome.html", "Onboarding"],
    ["Follow-up Sequence", "onboarding-sequence.html", "Onboarding"],
    ["Templates & snippets", "onboarding-templates.html", "Onboarding"],
    ["Import Old Fans", "onboarding-import.html", "Onboarding"],
    ["Tip Rewards", "tips-rewards.html", "Tips"],
    ["Tip Ask", "tips-ask.html", "Tips"],
    ["Image Reply & Teasers", "tips-image-reply.html", "Tips"],
    ["Make It Right", "tips-make-right.html", "Tips"],
    ["Auto-learn Fans", "fans-auto-learn.html", "Fan Intelligence"],
    ["Sheets Export", "fans-sheets-export.html", "Fan Intelligence"],
    ["Restrictions", "fans-restrictions.html", "Fan Intelligence"],
    ["Auto Posts", "content-auto-posts.html", "Content"],
    ["Auto Stories", "auto-stories.html", "Content"],
    ["Create Post", "content-create-post.html", "Content"],
    ["Smart Messages", "growth-smart-messages.html", "Growth"],
    ["Smart Lists", "growth-smart-lists.html", "Growth"],
    ["Auto-follow", "growth-auto-follow.html", "Growth"],
    ["Vault Pro — manager", "growth-vault-pro.html", "Growth"],
    ["Scripts library", "growth-scripts.html", "Growth"],
    ["Profile Promotion", "growth-profile-promotion.html", "Growth"],
    ["Free Trial Links", "growth-free-trial-links.html", "Growth"],
    ["Tracking Links", "growth-tracking-links.html", "Growth"],
    ["Vault AI — describe/organize", "vault-ai.html", "Vault AI"],
    ["Auto-describe", "vault-describe.html", "Vault AI"],
    ["Reminder Cards", "vault-reminders.html", "Vault AI"],
    ["Vault Review — dupes/flags", "vault-review.html", "Vault AI"],
    ["Manage Creators", "creators-manage.html", "Ops"],
    ["Custom Proxy", "creators-proxy.html", "Ops"],
    ["Manage Employees", "employees-manage.html", "Ops"],
    ["Shift Schedule", "employees-shift.html", "Ops"],
    ["Settings", "settings.html", "Ops"],
  ];

  const CSS = `
  .ft-cmdk-back{position:fixed;inset:0;background:rgba(0,0,0,.55);z-index:10000;display:flex;
    align-items:flex-start;justify-content:center;padding-top:12vh}
  .ft-cmdk{width:min(620px,92vw);max-height:70vh;background:#1c1c1c;border:1px solid #3a3a3a;
    border-radius:14px;box-shadow:0 24px 70px rgba(0,0,0,.6);font:14px Inter,sans-serif;color:#fff;
    display:flex;flex-direction:column;overflow:hidden}
  .ft-cmdk input{background:transparent;border:0;outline:0;color:#fff;font:15px Inter;padding:15px 18px;
    border-bottom:1px solid #333}
  .ft-cmdk input::placeholder{color:#7a7a7a}
  .ft-cmdk-list{overflow:auto;padding:6px}
  .ft-cmdk-sec{color:#7a7a7a;font:600 10.5px Inter;text-transform:uppercase;letter-spacing:.06em;
    padding:9px 12px 4px}
  .ft-cmdk-row{display:flex;align-items:center;gap:10px;padding:9px 12px;border-radius:8px;cursor:pointer}
  .ft-cmdk-row .ic{width:22px;height:22px;border-radius:6px;background:#2a2a2a;display:flex;
    align-items:center;justify-content:center;font-size:12px;flex:none}
  .ft-cmdk-row .ic.cre{border-radius:50%;background:#4166f6;font-weight:700;font-size:11px}
  .ft-cmdk-row .nm{flex:1;min-width:0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .ft-cmdk-row .sec{color:#7a7a7a;font-size:11px}
  .ft-cmdk-row.on{background:#2b3550;outline:1px solid #4166f6}
  .ft-cmdk-empty{padding:22px;text-align:center;color:#8a8a8a}
  .ft-cmdk-foot{border-top:1px solid #333;padding:7px 14px;color:#7a7a7a;font-size:11px;display:flex;gap:14px}
  .ft-cmdk-foot b{color:#aaa;font-weight:600}`;

  let styled = false;
  function css() {
    if (styled) return; styled = true;
    const s = document.createElement("style");
    s.textContent = CSS; document.head.appendChild(s);
  }

  const here = () => (location.pathname.split("/").pop() || "dashboard.html");

  function items(q) {
    q = q.trim().toLowerCase();
    const out = [];
    // creators (switch)
    (F.accounts() || []).forEach((a) => {
      const name = a.nickname || String(a.id);
      if (!q || name.toLowerCase().includes(q) || "creator switch".includes(q)) {
        out.push({ kind: "creator", id: a.id, label: name, sec: "Switch creator" });
      }
    });
    // pages (navigate)
    PAGES.forEach(([name, href, sec]) => {
      const hay = (name + " " + sec).toLowerCase();
      if (!q || hay.includes(q)) out.push({ kind: "page", href, label: name, sec });
    });
    // scored: exact prefix first
    if (q) out.sort((a, b) => (b.label.toLowerCase().startsWith(q) ? 1 : 0) - (a.label.toLowerCase().startsWith(q) ? 1 : 0));
    return out.slice(0, 60);
  }

  let back, listEl, inputEl, rows = [], sel = 0;

  function render(q) {
    const list = items(q);
    rows = list;
    if (!list.length) { listEl.innerHTML = '<div class="ft-cmdk-empty">No match</div>'; return; }
    let html = "", lastSec = null;
    list.forEach((it, i) => {
      if (it.sec !== lastSec) { html += '<div class="ft-cmdk-sec">' + esc(it.sec) + "</div>"; lastSec = it.sec; }
      const ic = it.kind === "creator"
        ? '<span class="ic cre">' + esc(it.label[0].toUpperCase()) + "</span>"
        : '<span class="ic">' + (it.href === here() ? "•" : "›") + "</span>";
      html += '<div class="ft-cmdk-row' + (i === sel ? " on" : "") + '" data-i="' + i + '">' + ic +
        '<span class="nm">' + esc(it.label) + "</span>" +
        (it.kind === "page" ? '<span class="sec">' + esc(it.sec) + "</span>" : '<span class="sec">↩ switch</span>') +
        "</div>";
    });
    listEl.innerHTML = html;
    const on = listEl.querySelector(".ft-cmdk-row.on");
    if (on) on.scrollIntoView({ block: "nearest" });
  }

  function choose(i) {
    const it = rows[i]; if (!it) return;
    close();
    if (it.kind === "creator") F.setAccount(it.id);           // reloads, re-scoped
    else if (it.href !== here()) location.href = it.href;
  }

  function open() {
    if (back) return;
    css();
    back = document.createElement("div");
    back.className = "ft-cmdk-back";
    back.innerHTML =
      '<div class="ft-cmdk" role="dialog" aria-label="Command palette">' +
        '<input placeholder="Jump to a page, switch creator…" autocomplete="off" spellcheck="false">' +
        '<div class="ft-cmdk-list"></div>' +
        '<div class="ft-cmdk-foot"><span><b>↑↓</b> move</span><span><b>↩</b> open</span><span><b>esc</b> close</span></div>' +
      "</div>";
    document.body.appendChild(back);
    listEl = back.querySelector(".ft-cmdk-list");
    inputEl = back.querySelector("input");
    sel = 0; render("");
    inputEl.focus();
    inputEl.addEventListener("input", () => { sel = 0; render(inputEl.value); });
    back.addEventListener("click", (e) => {
      if (e.target === back) return close();
      const row = e.target.closest(".ft-cmdk-row");
      if (row) choose(Number(row.dataset.i));
    });
    inputEl.addEventListener("keydown", (e) => {
      if (e.key === "ArrowDown") { e.preventDefault(); sel = Math.min(rows.length - 1, sel + 1); render(inputEl.value); }
      else if (e.key === "ArrowUp") { e.preventDefault(); sel = Math.max(0, sel - 1); render(inputEl.value); }
      else if (e.key === "Enter") { e.preventDefault(); choose(sel); }
      else if (e.key === "Escape") { e.preventDefault(); close(); }
    });
  }
  function close() { if (back) { back.remove(); back = null; } }

  document.addEventListener("keydown", (e) => {
    if ((e.metaKey || e.ctrlKey) && (e.key === "k" || e.key === "K")) {
      e.preventDefault(); back ? close() : open();
    }
  });

  window.FasttPalette = { open, close };
})();
