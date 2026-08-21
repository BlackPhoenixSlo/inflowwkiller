Fastt.ready(async function () {
  'use strict';
  var acct = Fastt.account();
  var esc = Fastt.esc, fmtInt = Fastt.fmtInt;
  var CAD_S = 3600;

  function stripSet(id, txt, cls) {
    var el = document.getElementById(id);
    if (!el) return;
    el.innerHTML = '<i></i>' + esc(txt);
    if (cls !== undefined) el.className = 'fx-st' + (cls ? ' ' + cls : '');
  }
  function untilTxt(iso) {
    // next_due_at is a tz-NAIVE UTC stamp — a bare new Date() would read it as
    // local time and be hours off. Fastt.parseUtc stamps the missing Z.
    var d = Fastt.parseUtc(iso);
    if (!d) return null;
    var ms = d.getTime() - Date.now();
    if (!isFinite(ms)) return null;
    if (ms <= 0) return 'due now';
    var h = Math.floor(ms / 3600000), m = Math.round((ms % 3600000) / 60000);
    return 'in ' + (h ? h + ' h ' : '') + m + ' m';
  }

  if (!acct) {
    // No creator selected: fastt.js already shows the global placeholder
    // banner, so no second banner here — but the strip must not sit on
    // "Checking…" forever and the baked knobs must not read as this
    // creator's real settings.
    ['dsc-st-run', 'dsc-st-count', 'dsc-st-last', 'dsc-st-next', 'dsc-st-model', 'dsc-st-cost']
      .forEach(function (id) { stripSet(id, 'No creator selected', ''); });
    var card0 = document.getElementById('dsc-card');
    if (card0) Fastt.staticBadge(card0.querySelector('.fx-tc-title'), 'NO CREATOR — INERT');
    var kv0 = document.getElementById('dsc-kv');
    if (kv0) kv0.textContent = 'Pick a creator (top-left) to load the real describe status.';
    var sug0 = document.getElementById('dsc-suggest-card');
    if (sug0) Fastt.staticBadge(sug0.querySelector('.fx-card-h'), 'STATIC DEMO');
    var exd0 = document.getElementById('dsc-ex-desc');
    if (exd0) exd0.textContent = 'No creator selected — pick one (top-left) to read a real described item.';
    var ext0 = document.getElementById('dsc-ex-tag');
    if (ext0) ext0.textContent = 'no creator';
    var adv0 = document.getElementById('adv-describe');
    if (adv0) {
      adv0.querySelectorAll('.fx-adv-group h4').forEach(function (h) { Fastt.staticBadge(h, 'STATIC DEMO'); });
      adv0.querySelectorAll('select, input').forEach(function (el) { el.disabled = true; });
    }
    ['dsc-sweep', 'dsc-restage', 'dsc-pv'].forEach(function (id) {
      var b = document.getElementById(id); if (b) b.disabled = true;   // spends budget — never a dead click
    });
    return;
  }

  var promptV = 'v2';

  var results = await Promise.all([
    Fastt.get('/admin/account-ai-config/vault-ai'),
    Fastt.get('/admin/vault-ai/describe-all/plan', { prompt_version: promptV }),
    Fastt.get('/admin/vault-ai/describe-all/status'),
    Fastt.rule('describe_media'),
    Fastt.get('/admin/vault-ai/cache/summary'),
  ]);
  var cfg = (results[0] && results[0].config) || {};
  var plan = results[1], dstat = results[2], rule = results[3], sum = results[4];
  var described = Math.max(0, (plan.total || 0) - (plan.undescribed || 0));
  var d = cfg.describe || {};
  var models = cfg.models || {};

  // ── status strip (LIVE) ──────────────────────────────────────
  var enabled = !!cfg.enabled;
  var ruleOn = !!(rule && rule.is_enabled);
  function paintRunChip(running) {
    if (running) stripSet('dsc-st-run', 'Sweep running now', 'ok');
    else if (enabled && ruleOn) stripSet('dsc-st-run', 'Running', 'ok');
    else stripSet('dsc-st-run', enabled ? 'On (no schedule row yet)' : 'Off', enabled ? 'warn' : '');
  }
  paintRunChip(dstat.running);
  function paintCountChip() {
    stripSet('dsc-st-count', fmtInt(described) + ' / ' + fmtInt(plan.total || 0) + ' items described');
  }
  paintCountChip();
  var lastRun = rule && rule.last_run && (rule.last_run.completed_at || rule.last_run.started_at);
  stripSet('dsc-st-last', lastRun ? 'Last sweep ' + Fastt.fmtAgo(lastRun) : 'No sweep has run yet');
  var due = rule && rule.next_due_at ? untilTxt(rule.next_due_at) : null;
  stripSet('dsc-st-next', due ? 'Next ' + due : 'Next — not scheduled');
  stripSet('dsc-st-model', String(models.describe || 'qwen3-vl-30b'));
  stripSet('dsc-st-cost', '0.038¢ per item · ≈ ' + Fastt.fmtMoney((plan.total || 0) * 0.00038) + ' full pass');
  Fastt.liveBadge(document.getElementById('dsc-status'));

  // ── master toggle card (LIVE) ────────────────────────────────
  var card = document.getElementById('dsc-card');
  var sw = document.getElementById('dsc-switch');
  function paintMaster(on) {
    if (!card || !sw) return;
    card.classList.toggle('on', on);
    sw.classList.toggle('on', on);
    var st = card.querySelector('.fx-tc-state');
    if (st) st.textContent = on ? 'Running' : 'Off';
  }
  paintMaster(enabled);
  var kv = document.getElementById('dsc-kv');
  function paintKv() {
    if (!kv) return;
    kv.innerHTML = '<b>' + fmtInt(described) + '</b> of <b>' + fmtInt(plan.total || 0) +
      '</b> described &middot; next sweep ' + esc(due || 'not scheduled');
  }
  paintKv();
  var cad = document.getElementById('dsc-cad');
  if (cad) cad.value = String(d.cadence_hours || 6);
  var per = document.getElementById('dsc-per');
  if (per) per.value = String(d.max_items_per_run || 40);
  if (card) Fastt.liveBadge(card.querySelector('.fx-tc-title'));

  async function patchCfg(partial, msg) {
    try {
      var out = await Fastt.patch('/admin/account-ai-config/vault-ai', { account_id: acct, config: partial });
      cfg = (out && out.config) || cfg;
      Fastt.saved(msg || 'Saved ✓');
    } catch (e) { Fastt.oops(e); }
  }

  if (sw) sw.addEventListener('click', async function () {
    // This element handler runs before the fx kit's document handler, which
    // does the visual toggle itself — so only compute the intent + save here.
    var on = !sw.classList.contains('on');
    enabled = on;
    await patchCfg({ enabled: on }, on ? 'Vault AI describing ON' : 'Vault AI describing OFF');
    try {
      await Fastt.upsertRule('describe_media', { is_enabled: on }, {
        name: 'Describe media (vault-AI sweep)',
        every_seconds: (Number(cad && cad.value) || 6) * CAD_S,
        payload: {}, is_enabled: on,
      });
      ruleOn = on;
    } catch (e) { Fastt.oops(e); }
    paintRunChip(false);
  });
  if (cad) cad.addEventListener('change', async function () {
    var h = Number(cad.value) || 6;
    // describe.cadence_hours is informational only — the schedule lives on the
    // rule's trigger.every_seconds. With no rule row yet, nothing is actually
    // scheduled, so don't let the toast claim a cadence that isn't running.
    var r = null, lookupOk = true;
    try { r = await Fastt.rule('describe_media'); } catch (e) { lookupOk = false; }
    await patchCfg({ describe: { cadence_hours: h } },
      (r || !lookupOk) ? 'Cadence: every ' + h + ' h'
        : 'Cadence saved (' + h + ' h) — nothing is scheduled until the switch above is on');
    if (!r) return;
    try {
      await Fastt.patch('/admin/automation-rules/' + r.id, { every_seconds: h * CAD_S, trigger: { every_seconds: h * CAD_S } });
    } catch (e) { Fastt.oops(e); }
  });
  if (per) per.addEventListener('change', function () {
    var n = Math.max(1, Math.round(Number(per.value) || 40));
    per.value = String(n);
    patchCfg({ describe: { max_items_per_run: n } }, 'Items per run: ' + n);
  });

  // ── suggest-only card (architecture, not a switch) ───────────
  // `suggest_only` has NO consumer anywhere in service/ (only the defaults
  // blobs + tests) — the review queue is suggest-only by construction. So this
  // is not a control: always on, inert, honestly badged. Never fake a save.
  var sug = document.getElementById('dsc-suggest');
  if (sug) {
    sug.classList.add('on');
    Fastt.staticBadge(document.querySelector('#dsc-suggest-card .fx-card-h'), 'ALWAYS ON SERVER-SIDE');
    sug.addEventListener('click', function (ev) {
      ev.stopPropagation(); // keep the fx kit from faking a visual toggle
      Fastt.toast('Suggest-only is how the pipeline is built — everything a fan could see waits in Review. There is no off switch.');
    });
  }

  // ══════════════════════════════════════════════════════════════
  // "What the AI sees" — the NEWEST actually-described item.
  // Was a hand-written sample (invented description, invented chips,
  // invented caption, CSS-gradient tile) under a subtitle that claimed
  // "one real pass on one item". Now it is one.
  // ══════════════════════════════════════════════════════════════
  var FLD_ORDER = ['clothing_state', 'primary_folder', 'setting', 'outfit', 'mood',
                   'framing', 'acts', 'beats', 'lane', 'shoot'];
  function fieldChip(label, value, on) {
    return '<span class="fx-chip' + (on ? ' on' : '') + '">' + esc(label) + ': ' + esc(value) + '</span>';
  }
  async function loadExample() {
    var tag = document.getElementById('dsc-ex-tag');
    var sub = document.getElementById('dsc-ex-sub');
    var tile = document.getElementById('dsc-ex-tile');
    var kind = document.getElementById('dsc-ex-kind');
    var descEl = document.getElementById('dsc-ex-desc');
    var chips = document.getElementById('dsc-ex-chips');
    var capEl = document.getElementById('dsc-ex-cap');
    var cardEl = document.getElementById('dsc-ex-card');
    function empty(why) {
      if (tag) tag.textContent = 'nothing described yet';
      if (descEl) descEl.textContent = why;
      if (chips) chips.innerHTML = '';
      if (capEl) capEl.innerHTML = '';
      if (kind) kind.textContent = 'no described item';
      if (tile) { tile.classList.add('blank'); tile.classList.remove('real'); tile.style.backgroundImage = ''; }
      if (cardEl) Fastt.staticBadge(cardEl.querySelector('.fx-card-h'), 'EMPTY — NO DESCRIBED ITEM');
    }
    if (!sum.count) {
      empty('The local mirror is empty for this creator, so nothing has been described yet. ' +
            'Run “Sync vault (Collect all)” on Vault Manage, then a describe sweep — this card then shows the real reading of your newest item.');
      return;
    }
    var out;
    try { out = await Fastt.get('/admin/vault-ai/items', { limit: 40, sort: 'newest' }); }
    catch (e) { empty('Could not read the mirror: ' + ((e.body && (e.body.detail || e.body.error)) || e.message)); return; }
    var row = (out.list || []).filter(function (m) {
      return m._ai && m._ai.describe_status === 'described' && (m._ai.description || m._ai.video_description);
    })[0];
    if (!row) {
      empty('Nothing in the newest 40 mirrored items has been described yet — run a sweep and this card fills with the real reading.');
      return;
    }
    var ai = row._ai || {}, f = ai.fields || {};
    if (tag) tag.textContent = 'newest described · #' + row.id;
    if (sub) sub.textContent = 'One real pass on one item — the AI’s own words for #' + row.id +
      '. The description is a private note on the item; fans never see it.';
    if (tile) {
      tile.classList.add('real');
      tile.style.backgroundImage = "url('" + (row._thumb || '') + "')";
    }
    if (kind) kind.innerHTML = kind.innerHTML.replace(/&mdash;|—|photo|video|gif|audio/i, esc(row.type || 'item'));
    if (descEl) descEl.textContent = '“' + (ai.description || ai.video_description) + '”';
    if (chips) {
      var out2 = [];
      if (ai.explicitness_tier)
        out2.push(fieldChip('Tier', (cfg.tier_labels && cfg.tier_labels[ai.explicitness_tier]) || ai.explicitness_tier, true));
      FLD_ORDER.forEach(function (k) {
        var v = f[k];
        if (v === null || v === undefined || v === '') return;
        if (Array.isArray(v)) { if (!v.length) return; v = v.slice(0, 3).join(', '); }
        else if (typeof v === 'object') return;
        out2.push(fieldChip(k.replace(/_/g, ' '), String(v), false));
      });
      (ai.tags || []).slice(0, 4).forEach(function (t) { out2.push('<span class="fx-chip">#' + esc(t) + '</span>'); });
      chips.innerHTML = out2.length ? out2.join('')
        : '<span class="fx-chip">no taxonomy fields on this item</span>';
      // These chips are a read-out, not a filter — keep the fx kit from
      // toggling them into something that looks like a selection.
      chips.addEventListener('click', function (ev) {
        var ch = ev.target.closest('.fx-chip');
        if (!ch) return;
        ev.stopPropagation();
        Fastt.toast('This is what the AI wrote for #' + row.id + ' — a read-out, not a filter.');
      });
    }
    if (capEl) capEl.innerHTML = ai.suggested_caption
      ? 'It also drafted a caption: <b>“' + esc(ai.suggested_caption) + '”</b>'
      : '<span style="color:#6a6a6a">No caption drafted for this item.</span>';
    if (cardEl) Fastt.liveBadge(cardEl.querySelector('.fx-card-h'));
  }
  await loadExample();

  var costSub = document.getElementById('dsc-cost-sub');
  if (costSub) costSub.innerHTML = 'A full ' + fmtInt(plan.total || 0) + '-item vault pass &asymp; <b class="vd-w">' +
    Fastt.fmtMoney((plan.total || 0) * 0.00038) + '</b>. Only new or changed items are re-read, so day-to-day cost is near zero.';

  // ── advanced knobs (LIVE where a config key exists) ──────────
  var imgs = document.getElementById('dsc-imgs');
  var vids = document.getElementById('dsc-vids');
  if (imgs) {
    imgs.classList.toggle('on', d.images !== false);
    imgs.addEventListener('click', function () {
      var on = !imgs.classList.contains('on');
      patchCfg({ describe: { images: on } }, 'Photos & GIFs ' + (on ? 'described' : 'skipped'));
    });
  }
  if (vids) {
    vids.classList.toggle('on', d.videos !== false);
    vids.addEventListener('click', function () {
      var on = !vids.classList.contains('on');
      patchCfg({ describe: { videos: on } }, 'Videos ' + (on ? 'described' : 'skipped'));
    });
  }
  Fastt.staticBadge(document.querySelector('#dsc-ladder > h4'), 'FIXED SERVER-SIDE');
  // The ladder is hardcoded in the describe pipeline — no config key, nothing
  // to save. Disable the selects (same treatment as dsc-esc) so they can't be
  // changed into a value that would never be honoured.
  var ladder = document.getElementById('dsc-ladder');
  if (ladder) ladder.querySelectorAll('select').forEach(function (s) { s.disabled = true; });

  var modelSel = document.getElementById('dsc-model');
  if (modelSel) {
    modelSel.value = String(models.describe || 'qwen3-vl-30b');
    modelSel.addEventListener('change', function () {
      patchCfg({ models: { describe: modelSel.value } }, 'Describe model: ' + modelSel.value);
    });
  }
  // Escalation knobs: NOT wired. The real escalation is hardcoded in
  // vault_ai_api._describe_one — any refused/blank answer retries ONCE on
  // qwen3-vl-235b, unconditionally. `models.escalation` and
  // `models.escalate_below_confidence` have NO consumer (defaults blobs +
  // tests only), so "Never escalate" / a threshold save would be a lie.
  // Inert + badged until a real gate exists server-side.
  var conf = document.getElementById('dsc-conf');
  var escSel = document.getElementById('dsc-esc');
  if (escSel) {
    escSel.value = 'qwen3-vl-235b'; // what the server actually does
    escSel.disabled = true;
    var escField = escSel.closest('.fx-field');
    if (escField) Fastt.staticBadge(escField.querySelector('label') || escField, 'NOT WIRED');
  }
  if (conf) {
    conf.disabled = true;
    var confHead = conf.previousElementSibling;
    var rv = confHead && confHead.querySelector('.fx-rv');
    if (rv) rv.textContent = '—';
    var hint = confHead && confHead.querySelector('.fx-sl-hint');
    if (hint) hint.textContent = 'not confidence-gated: the server retries any refused/blank item once on the bigger model, always';
    if (confHead) Fastt.staticBadge(confHead, 'NOT WIRED — RETRY IS AUTOMATIC');
  }
  var cap = document.getElementById('dsc-cap');
  if (cap) {
    var capVal = Number(d.describe_all_cap_percent) || 80;
    cap.value = String(capVal);
    var rv2 = cap.previousElementSibling && cap.previousElementSibling.querySelector('.fx-rv');
    if (rv2) rv2.textContent = capVal + '%';
    cap.addEventListener('change', function () {
      patchCfg({ describe: { describe_all_cap_percent: Math.round(Number(cap.value) || 80) } },
        'Budget cap: ' + cap.value + '%');
    });
  }

  // ══════════════════════════════════════════════════════════════
  // prompt version + restage + force  (POST /describe-all accepts all
  // three; the plan is served PER VERSION, so the numbers must re-fetch)
  // ══════════════════════════════════════════════════════════════
  var pvSel = document.getElementById('dsc-pv');
  var restageBtn = document.getElementById('dsc-restage');
  var forceChk = document.getElementById('dsc-force');
  var restageChk = document.getElementById('dsc-restage-opt');
  var modeKv = document.getElementById('dsc-mode-kv');

  function paintPlan() {
    described = Math.max(0, (plan.total || 0) - (plan.undescribed || 0));
    paintCountChip();
    paintKv();
    stripSet('dsc-st-cost', '0.038¢ per item · ≈ ' + Fastt.fmtMoney((plan.total || 0) * 0.00038) + ' full pass');
    if (restageBtn) {
      if (plan.restage > 0) {
        restageBtn.style.display = '';
        restageBtn.textContent = 'Re-scan old (' + fmtInt(plan.restage) + ')';
      } else {
        restageBtn.style.display = 'none';
      }
    }
    // Friendlier primary action: with an empty mirror a sweep is a no-op, so
    // point the user at the sync step instead of leaving a button that does
    // nothing. Re-fires on every paintPlan (prompt switch, post-sweep refresh).
    var sweepBtn = document.getElementById('dsc-sweep');
    if (sweepBtn) {
      var noItems = !(plan.total > 0);
      sweepBtn.disabled = noItems;
      sweepBtn.title = noItems
        ? 'Nothing mirrored yet — run “Sync vault (Collect all)” on Vault Manage first'
        : 'Describe new / changed items now';
    }
    if (modeKv) modeKv.innerHTML = 'Prompt <b>' + esc(plan.prompt_version || promptV) + '</b> &middot; <b>' +
      fmtInt(plan.total || 0) + '</b> items &middot; <b>' + fmtInt(plan.undescribed || 0) +
      '</b> never described &middot; <b>' + fmtInt(plan.restage || 0) + '</b> described under an older prompt' +
      (forceChk && forceChk.classList.contains('on')
        ? ' &middot; <b style="color:#e8aa46">force ON — the sweep re-reads all ' + fmtInt(plan.total || 0) + '</b>'
        : '');
  }
  paintPlan();
  Fastt.liveBadge(document.querySelector('#dsc-mode > h4'));

  if (pvSel) pvSel.addEventListener('change', async function () {
    promptV = pvSel.value;
    try {
      plan = await Fastt.get('/admin/vault-ai/describe-all/plan', { prompt_version: promptV });
      paintPlan();
      Fastt.toast('Prompt ' + promptV + ': ' + fmtInt(plan.undescribed || 0) + ' undescribed · ' +
        fmtInt(plan.restage || 0) + ' stale', 'ok');
    } catch (e) { Fastt.oops(e); }
  });
  if (forceChk) forceChk.addEventListener('click', function () { setTimeout(paintPlan, 0); });
  if (restageChk) restageChk.addEventListener('click', function () { setTimeout(paintPlan, 0); });

  // ── sweep progress: polled while running (was read once at load) ──
  var progEl = document.getElementById('dsc-prog');
  var progLb = document.getElementById('dsc-prog-lb');
  var pollTimer = null;
  function paintProgress(st) {
    var pr = st && st.progress;
    if (!progEl) return;
    if (!pr) { progEl.style.display = 'none'; return; }
    progEl.style.display = '';
    var frac = pr.total ? Math.max(0, Math.min(1, (pr.done || 0) / pr.total)) : 0;
    progEl.querySelector('i').style.width = (frac * 100) + '%';
    progEl.classList.toggle('ok', !st.running && frac >= 1);
    if (progLb) progLb.textContent = (st.running ? 'Sweep running — ' : 'Last sweep — ') +
      fmtInt(pr.done || 0) + ' / ' + fmtInt(pr.total || 0) + ' items' +
      (pr.needs_review ? ' · ' + fmtInt(pr.needs_review) + ' need review' : '') +
      (pr.capped ? ' · stopped at the daily budget cap' : '');
  }
  async function pollStatus() {
    try {
      var st = await Fastt.get('/admin/vault-ai/describe-all/status');
      paintProgress(st);
      paintRunChip(st.running);
      if (st.running) return true;
      if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
      // refresh the plan-derived counters once the sweep lands
      try {
        plan = await Fastt.get('/admin/vault-ai/describe-all/plan', { prompt_version: promptV });
        paintPlan();
        await loadExample();
      } catch (e) { /* leave the last good numbers */ }
      return false;
    } catch (e) { if (pollTimer) { clearInterval(pollTimer); pollTimer = null; } return false; }
  }
  function watch() { if (!pollTimer) pollTimer = setInterval(pollStatus, 3000); }
  paintProgress(dstat);
  if (dstat.running) watch();

  async function runSweep(opts) {
    var body = { account_id: acct, prompt_version: promptV, model: String(models.describe || 'qwen3-vl-30b') };
    body.force = !!(opts && opts.force);
    body.restage = !!(opts && opts.restage);
    var n = body.force ? (plan.total || 0) : (body.restage ? (plan.restage || 0) : (plan.undescribed || 0));
    if (!confirm('Run a describe sweep now?\n\n' +
      'Prompt ' + promptV + ' · ' + (body.force ? 'FORCE — re-reads every item' :
        (body.restage ? 'restage — re-reads items described under an older prompt' : 'new/changed items only')) +
      '\nAbout ' + n + ' items ≈ ' + Fastt.fmtMoney(n * 0.00038) + ' of vision budget.\n' +
      'No fan is messaged and nothing is posted.')) return;
    try {
      var r = await Fastt.post('/admin/vault-ai/describe-all', body);
      Fastt.saved('Sweep ' + (r.status || 'started') + ' · prompt ' + (r.prompt_version || promptV) +
        ' · model ' + (r.model || ''));
      watch();
      setTimeout(pollStatus, 800);
    } catch (e) { Fastt.oops(e); }
  }

  var sweep = document.getElementById('dsc-sweep');
  if (sweep) sweep.addEventListener('click', function () {
    runSweep({ force: !!(forceChk && forceChk.classList.contains('on')),
               restage: !!(restageChk && restageChk.classList.contains('on')) });
  });
  if (restageBtn) restageBtn.addEventListener('click', function () { runSweep({ restage: true }); });
});
