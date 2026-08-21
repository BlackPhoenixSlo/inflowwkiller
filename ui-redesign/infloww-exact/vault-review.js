Fastt.ready(async function () {
  'use strict';
  var acct = Fastt.account();
  var esc = Fastt.esc;

  function stripSet(id, txt, cls) {
    var el = document.getElementById(id);
    if (!el) return;
    el.innerHTML = '<i></i>' + esc(txt);
    if (cls !== undefined) el.className = 'fx-st' + (cls ? ' ' + cls : '');
  }

  // Advanced knobs that are server-fixed carry their badge on BOTH paths —
  // the no-account return below must not leave them looking settable.
  Fastt.staticBadge(document.getElementById('rv-orig-field'), 'FIXED: EARLIEST');
  Fastt.staticBadge(document.getElementById('rv-rescan-field'), 'STATIC');
  Fastt.staticBadge(document.getElementById('rv-disp-group'), 'ALWAYS ON SERVER-SIDE');
  Fastt.staticBadge(document.getElementById('rv-vmodel-field'), 'FIXED SERVER-SIDE');

  if (!acct) {
    // No creator selected — strip the hardcoded demo rows so their
    // visual-only Keep/Confirm buttons can't be mistaken for the real thing.
    ['rv-st-fresh', 'rv-st-wait', 'rv-st-acc', 'rv-st-model'].forEach(function (id) {
      stripSet(id, 'No creator selected', '');
    });
    var note0 = '<div class="fx-note" style="margin-bottom:12px;color:#8a8a8a">No creator selected — click the creator name (top-left) to sign in and pick one.</div>';
    var pd0 = document.getElementById('pane-dupes');
    if (pd0) {
      pd0.querySelectorAll('.pair').forEach(function (p) { p.remove(); });
      var f0 = pd0.querySelector('.rv-foot');
      if (f0) f0.insertAdjacentHTML('beforebegin', note0);
    }
    var pi0 = document.getElementById('pane-disputes');
    if (pi0) {
      pi0.querySelectorAll('.drow').forEach(function (d) { d.remove(); });
      var f1 = pi0.querySelector('.rv-foot');
      if (f1) f1.insertAdjacentHTML('beforebegin', note0);
    }
    var fg0 = document.getElementById('rv-fgrid');
    if (fg0) fg0.innerHTML = note0;
    var pq0 = document.getElementById('rv-queue');
    if (pq0) pq0.innerHTML = note0;
    ['rv-ct-queue', 'rv-ct-dupes', 'rv-ct-disputes', 'rv-ct-flags'].forEach(function (id) {
      var el = document.getElementById(id);
      if (el) el.textContent = '—';
    });
    var hs0 = document.getElementById('rv-hs');
    if (hs0) hs0.textContent = 'No creator selected.';
    var pct0 = document.getElementById('rv-pct');
    if (pct0) pct0.textContent = '—';
    var bar0 = document.getElementById('rv-bar-i');
    if (bar0) bar0.style.width = '0%';
    return;
  }
  function thumbUrl(mid) {
    return '/admin/vault-ai/thumb?account_id=' + encodeURIComponent(acct) + '&media_id=' + encodeURIComponent(mid);
  }
  function thumbStyle(mid) {
    return 'background-image:url(\'' + esc(thumbUrl(mid)) + '\');background-size:cover;background-position:center';
  }
  function emptyRow(txt) {
    return '<div class="fx-note" style="margin-bottom:12px;color:#8a8a8a">' + esc(txt) + '</div>';
  }
  // created_at comes back tz-NAIVE UTC (`.isoformat()` with no Z) — a bare
  // new Date() would read it as local time and can show the wrong day.
  function dayTxt(s) { var d = Fastt.parseUtc(s); return d ? d.toLocaleDateString() : ''; }

  var state = { threshold: 2, allowSent: false, order: 'iffy-first', batch: 24, keep: {}, dupes: null, flags: null };

  var results = await Promise.all([
    Fastt.get('/admin/vault-ai/cache/summary'),
    Fastt.get('/admin/vault-ai/duplicates', { threshold: state.threshold }),
    Fastt.get('/admin/vault-ai/disputes'),
    Fastt.get('/admin/vault-ai/flags-review', { limit: state.batch }),
    Fastt.get('/admin/vault-ai/flags-accuracy'),
    Fastt.get('/admin/account-ai-config/vault-ai'),
    Fastt.get('/admin/vault-ai/review'),
  ]);
  var sum = results[0];
  state.dupes = results[1];
  var disputes = results[2];
  state.flags = results[3];
  var acc = results[4];
  var cfg = (results[5] && results[5].config) || {};
  var review0 = results[6] || { folder: [], ppv: [], reminder: [] };

  // ── status strip (LIVE) ──────────────────────────────────────
  if (sum.last_run && sum.last_run.finished_at)
    stripSet('rv-st-fresh', 'Mirror synced ' + Fastt.fmtAgo(sum.last_run.finished_at), 'ok');
  else stripSet('rv-st-fresh', 'Mirror never synced', 'warn');
  var waiting = (state.dupes.removable || 0) + (disputes.count || 0) + (state.flags.total || 0);
  stripSet('rv-st-wait', waiting ? Fastt.fmtInt(waiting) + ' items waiting' : 'Nothing waiting', waiting ? 'warn' : '');
  stripSet('rv-st-acc', acc.accuracy == null
    ? 'Accuracy unmeasured — no grades at prompt v' + (acc.prompt_version || 0)
    : 'Model agrees with you ' + (Math.round(acc.accuracy * 1000) / 10) + '% (' + Fastt.fmtInt(acc.answers || 0) + ' answers)');
  stripSet('rv-st-model', String((cfg.models && cfg.models.describe) || 'qwen3-vl-30b'));
  Fastt.liveBadge(document.getElementById('rv-status'));

  function setCt(id, n) { var el = document.getElementById(id); if (el) el.textContent = Fastt.fmtInt(n); }

  // ── duplicates pane (LIVE) ───────────────────────────────────
  var paneDupes = document.getElementById('pane-dupes');
  function renderDupes() {
    var r = state.dupes || {};
    setCt('rv-ct-dupes', r.sets || 0);
    var pctEl = document.getElementById('rv-pct');
    var pct = r.scanned ? Math.round(1000 * (r.removable || 0) / r.scanned) / 10 : 0;
    if (pctEl) pctEl.textContent = pct + '%';
    var hs = document.getElementById('rv-hs');
    if (hs) hs.textContent = r.scanned
      ? Fastt.fmtInt(r.removable || 0) + ' of ' + Fastt.fmtInt(r.scanned) + ' items are copies of something already in the vault (' +
        Fastt.fmtInt(r.sets || 0) + ' sets at strictness ' + state.threshold + ', ' + Fastt.fmtInt(r.unhashed || 0) + ' unhashed). Keep the original, remove the rest.'
      : 'The mirror has no hashed items yet — sync the vault first.';
    var bar = document.getElementById('rv-bar-i');
    if (bar) bar.style.width = Math.min(100, pct) + '%';

    paneDupes.querySelectorAll('.pair').forEach(function (p) { p.remove(); });
    var foot = paneDupes.querySelector('.rv-foot');
    var html = '';
    (r.clusters || []).slice(0, 10).forEach(function (c) {
      var o = c.original || {};
      (c.dupes || []).forEach(function (d) {
        var key = String(d.media_id);
        if (!(key in state.keep)) state.keep[key] = true; // true = remove this copy
        var dist = Math.max(d.dhash_dist || 0, d.ahash_dist || 0);
        html += '<div class="pair" data-mid="' + esc(d.media_id) + '">' +
          '<div class="pcol"><div class="pthumb orig" style="' + thumbStyle(o.media_id) + '"><span class="tag">Original</span></div>' +
          '<div class="pmeta"><b>#' + esc(o.media_id) + '</b> · ' + esc(o.kind || '') + '<br>' + esc(dayTxt(o.created_at)) + '</div></div>' +
          '<div class="sim"><span class="pc">' + (d.exact ? '100%' : 'd' + dist) + '</span>' +
          '<span class="band ' + (d.band === 'identical' || d.exact ? 'identical' : 'near') + '">' + esc(d.band || (d.exact ? 'identical' : 'near')) + '</span></div>' +
          '<div class="pcol"><div class="pthumb" style="' + thumbStyle(d.media_id) + '"><span class="tag">Re-upload</span></div>' +
          '<div class="pmeta"><b>#' + esc(d.media_id) + '</b> · ' + esc(d.kind || '') + '<br>' + esc(dayTxt(d.created_at)) + '</div></div>' +
          '<div class="pinfo">' + (d.exact ? 'Exact same bytes uploaded twice.' : 'Near-duplicate at hash distance ' + dist + '.') + '<br>' +
          (d.send_count > 0 ? '<span class="warn">Copy already sent ' + d.send_count + '× — protected unless you allow it in Advanced.</span>' : 'Neither copy has ever been sent.') + '</div>' +
          '<div class="acts">' +
          '<button class="pact' + (state.keep[key] ? ' sel' : '') + '" data-k="remove">Keep left</button>' +
          // "Keep right" can never do anything: the hide endpoint refuses
          // originals (earliest upload is always the keeper), so it ships
          // disabled rather than as a button that only ever says no.
          '<button class="pact" data-k="right" disabled style="opacity:.4;cursor:not-allowed"' +
          ' title="The earliest upload is always the keeper — OF has no unhide, so the original can never be removed.">Keep right</button>' +
          '<button class="pact' + (!state.keep[key] ? ' sel' : '') + '" data-k="both">Keep both</button></div></div>';
      });
    });
    if (!html) html = emptyRow(r.scanned ? 'No duplicate sets at strictness ' + state.threshold + '.' : 'Nothing scanned yet — the local mirror is empty.');
    if (foot) foot.insertAdjacentHTML('beforebegin', html);
    updateHideFoot();
  }
  function selectedIds() {
    return Object.keys(state.keep).filter(function (k) { return state.keep[k]; }).map(Number);
  }
  function updateHideFoot() {
    var ids = selectedIds();
    var btn = document.getElementById('rv-hide');
    if (btn) btn.textContent = 'Remove ' + ids.length + ' cop' + (ids.length === 1 ? 'y' : 'ies') + ' from OnlyFans…';
    var fine = document.getElementById('rv-dupes-fine');
    if (fine) fine.textContent = Fastt.fmtInt((state.dupes && state.dupes.sets) || 0) + ' sets · ' + ids.length +
      ' copies ticked · originals are never touched · asks you to confirm first';
  }
  renderDupes();
  Fastt.liveBadge(document.getElementById('rv-hero'));

  paneDupes.addEventListener('click', function (ev) {
    var b = ev.target.closest('.pact');
    if (!b || b.disabled) return;
    var pair = b.closest('.pair');
    var mid = pair && pair.dataset.mid;
    if (!mid) return;
    if (b.dataset.k === 'right') return; // disabled above; never selectable
    pair.querySelectorAll('.pact').forEach(function (x) { x.classList.remove('sel'); });
    b.classList.add('sel');
    state.keep[mid] = (b.dataset.k === 'remove');
    updateHideFoot();
  });
  var hideBtn = document.getElementById('rv-hide');
  if (hideBtn) hideBtn.addEventListener('click', async function () {
    var ids = selectedIds();
    if (!ids.length) { Fastt.toast('No copies ticked.'); return; }
    if (!confirm('Hide ' + ids.length + ' duplicate cop' + (ids.length === 1 ? 'y' : 'ies') +
      ' in the REAL OnlyFans vault? OF has no unhide. Originals are never touched.')) return;
    try {
      var out = await Fastt.post('/admin/vault-ai/duplicates/hide',
        { account_id: acct, media_ids: ids, threshold: state.threshold, allow_sent: state.allowSent });
      var refusedN = Object.keys(out.refused || {}).reduce(function (n, k) { return n + out.refused[k].length; }, 0);
      Fastt.saved('Hidden ' + (out.hidden || 0) + ' cop' + (out.hidden === 1 ? 'y' : 'ies') + (refusedN ? ' · ' + refusedN + ' refused' : ''));
      state.dupes = await Fastt.get('/admin/vault-ai/duplicates', { threshold: state.threshold });
      state.keep = {};
      renderDupes();
    } catch (e) { Fastt.oops(e); }
  });

  // ── disputes pane (LIVE) ─────────────────────────────────────
  var paneDisp = document.getElementById('pane-disputes');
  function regionsSummary(regions) {
    var bare = Object.keys(regions || {}).filter(function (k) { return regions[k] === 'bare'; })
      .map(function (k) { return k.replace('_vis', ''); });
    return bare.length ? 'bare: ' + bare.join(', ') : 'nothing bare';
  }
  function renderDisputes() {
    setCt('rv-ct-disputes', disputes.count || 0);
    paneDisp.querySelectorAll('.drow').forEach(function (d) { d.remove(); });
    var foot = paneDisp.querySelector('.rv-foot');
    var html = '';
    (disputes.disputes || []).slice(0, 12).forEach(function (d) {
      var pv = d.propose || {};
      var canFlags = pv.flags && Object.keys(pv.flags).length;
      var canDesc = pv.describe && Object.keys(pv.describe).length;
      html += '<div class="drow" data-mid="' + esc(d.media_id) + '">' +
        '<div class="dthumb" style="' + thumbStyle(d.media_id) + '"></div>' +
        '<div class="dinfo"><div class="dn">' + esc(d.kind || 'item') + ' · #' + esc(d.media_id) + '</div>' +
        '<div class="dm">' + esc((d.reasons || d.codes || []).join(' · ') || 'contradiction') + '</div></div>' +
        '<div class="dsaid">' +
        '<div class="dside"><div class="who">Describe said</div><span class="dchip ai">' + esc(d.clothing_state || d.explicitness || '—') + '</span></div>' +
        '<div class="dside"><div class="who">Flags said</div><span class="dchip you">' + esc(regionsSummary(d.regions)) + '</span></div></div>' +
        '<div class="acts">' +
        '<button class="pact" data-side="describe"' + (canDesc ? '' : ' disabled style="opacity:.4"') + '>Trust describe</button>' +
        '<button class="pact" data-side="flags"' + (canFlags ? '' : ' disabled style="opacity:.4"') + '>Trust flags</button></div></div>';
    });
    if (!html) html = emptyRow((disputes.checked || 0)
      ? 'No contradictions between the describe pass and the flags pass — nothing to settle.'
      : 'No flagged items yet — run the flags sweep first, disputes appear when the two passes disagree.');
    if (foot) foot.insertAdjacentHTML('beforebegin', html);
    var fine = document.getElementById('rv-disp-fine');
    if (fine) fine.textContent = (disputes.count || 0) + ' open · checked ' + Fastt.fmtInt(disputes.checked || 0) +
      ' items · a fix is locked — no re-run can quietly revert it';
  }
  renderDisputes();

  paneDisp.addEventListener('click', async function (ev) {
    var b = ev.target.closest('.pact');
    if (!b || b.disabled) return;
    var row = b.closest('.drow');
    var mid = row && Number(row.dataset.mid);
    if (!mid) return;
    var d = (disputes.disputes || []).find(function (x) { return x.media_id === mid; });
    if (!d) return;
    var values = (d.propose || {})[b.dataset.side === 'flags' ? 'flags' : 'describe'] || {};
    if (!Object.keys(values).length) return;
    if (!confirm('Apply this correction and lock it? Fields: ' + Object.keys(values).join(', '))) return;
    try {
      await Fastt.post('/admin/vault-ai/disputes/resolve', { account_id: acct, media_id: mid, values: values });
      Fastt.saved('Corrected + locked #' + mid);
      disputes = await Fastt.get('/admin/vault-ai/disputes');
      renderDisputes();
    } catch (e) { Fastt.oops(e); }
  });

  // ── flags pane (LIVE) ────────────────────────────────────────
  // The vision model answers, per region of her body, whether she's bare /
  // covered / not in frame — those three answers pick the folder, the price
  // band and whether an item is safe for a whole list. It has been wrong in a
  // DIFFERENT direction after every prompt change, so grading is part of the
  // product: tap only what's wrong, or press "Looks right" to CONFIRM (which
  // is not a no-op — it's the evidence the accuracy number runs on). Mirrors
  // app/components/vault/VaultFlagsReviewModal.tsx (grade carries corrections).
  var fgrid = document.getElementById('rv-fgrid');
  var REGION_ORDER = ['breasts_vis', 'vulva_vis', 'anus_vis'];
  var REGION_LBL = { breasts_vis: 'breasts', vulva_vis: 'vulva', anus_vis: 'anus' };
  var STATE_ORDER = ['not_in_frame', 'covered', 'bare'];
  var STATE_LBL = { not_in_frame: 'not in', covered: 'covered', bare: 'bare' };
  var STATE_CLS = { not_in_frame: 'st-not', covered: 'st-covered', bare: 'st-bare' };

  function headerLabel(it) {
    var over = it.over || {};
    var g = Object.keys(over).map(function (k) { return over[k]; }).filter(Boolean);
    return it.clothing_state || g[0] || '—';
  }
  function garmentBlock(it) {
    var over = it.over || {};
    var parts = Object.keys(over).filter(function (k) { return over[k]; }).map(function (k) {
      return (REGION_LBL[k] || k) + ': <b>' + esc(over[k]) + '</b>';
    });
    return parts.length ? '<div class="fgar">' + parts.join(' · ') + '</div>' : '';
  }
  function regionRows(it, graded) {
    var regions = it.regions || {};
    var locked = it.locked || [];
    return '<div class="fregions">' + REGION_ORDER.map(function (rk) {
      var cur = regions[rk] || 'not_in_frame';
      var isLk = locked.indexOf(rk) >= 0;
      var btns = STATE_ORDER.map(function (st) {
        return '<button type="button" class="fsb ' + STATE_CLS[st] + (cur === st ? ' on' : '') +
          '" data-state="' + st + '"' + (isLk || graded ? ' disabled' : '') + '>' + STATE_LBL[st] + '</button>';
      }).join('');
      return '<div class="freg" data-region="' + rk + '" data-orig="' + esc(cur) + '">' +
        '<span class="frn' + (isLk ? ' lk' : '') + '" title="' + (isLk ? 'locked by an earlier correction — a re-run cannot revert it' : '') + '">' +
        (isLk ? '🔒' : '') + (REGION_LBL[rk] || rk) + '</span><div class="fstates">' + btns + '</div></div>';
    }).join('') + '</div>';
  }

  function updateFlagsFine() {
    var f = state.flags || {};
    var fine = document.getElementById('rv-flags-fine');
    if (fine) fine.textContent = Fastt.fmtInt(f.total || 0) + ' to check · ' + Fastt.fmtInt(f.graded || 0) + ' graded · ' +
      Fastt.fmtInt(f.iffy || 0) + ' nominated iffy · corrections are locked';
    var kvEl = document.getElementById('rv-flags-kv');
    if (kvEl) kvEl.innerHTML = 'prompt <b>v' + esc(f.prompt_version || 0) + '</b> · model agrees with you <b>' +
      (acc.accuracy == null ? '—' : (Math.round(acc.accuracy * 1000) / 10) + '%') + '</b> (' + Fastt.fmtInt(acc.answers || 0) +
      ' answers) · confirms count too — they’re what the accuracy is measured from';
  }

  function renderFlags() {
    var f = state.flags || {};
    setCt('rv-ct-flags', f.total || 0);
    var items = (f.items || []).slice();
    if (state.order === 'newest') items.sort(function (a, b) { return b.media_id - a.media_id; });
    var html = '';
    items.forEach(function (it) {
      var graded = it.graded && Number(it.graded.v || 0) >= Number(f.prompt_version || 0) && Number(f.prompt_version || 0) > 0;
      var warns = (it.iffy_why || []).map(function (w) { return '<div class="fwarn">⚠ ' + esc(w) + '</div>'; }).join('');
      html += '<div class="fcard' + (graded ? ' done' : '') + '" data-mid="' + esc(it.media_id) + '">' +
        '<div class="fthumb zoomable" style="' + thumbStyle(it.media_id) + '" title="Click to view the full frame"></div>' +
        '<div class="fbody"><div class="frow1"><span class="flabel">' + esc(headerLabel(it)) + '</span>' +
        '<span class="fm">' + esc(it.kind || '') + ' · #' + esc(it.media_id) + (it.lanes && it.lanes.length ? ' · ' + esc(it.lanes.join(', ')) : '') + '</span></div>' +
        regionRows(it, graded) +
        garmentBlock(it) +
        (it.description ? '<div class="fgar">' + esc(it.description) + '</div>' : '') +
        warns +
        '<input class="fnote" placeholder="anything odd? (optional)"' + (graded ? ' disabled' : '') + '>' +
        '<div class="facts"><button type="button" class="fbtn ok">' + (graded ? 'Confirmed ✓' : 'Looks right') + '</button></div>' +
        '</div></div>';
    });
    if (!html) html = emptyRow('No flagged items to check — sync + run the flags sweep on the Vault Manage page first.');
    fgrid.innerHTML = html;
    updateFlagsFine();
  }
  renderFlags();
  // Badge the pane's intro note, not the grid — fgrid is display:grid, so a
  // badge appended there would stretch into a full empty cell.
  Fastt.liveBadge(document.querySelector('#pane-flags .fx-note'));

  // full-frame lightbox — the square crop hides an edge-of-frame waistband,
  // which is exactly what a reviewer is here to judge.
  var lb = document.getElementById('rv-lb'), lbImg = document.getElementById('rv-lb-img');
  function fullImgUrl(mid) {
    return '/admin/vault-ai/image?account_id=' + encodeURIComponent(acct) + '&media_id=' + encodeURIComponent(mid);
  }
  function openLb(mid) { if (!mid || !lb) return; lbImg.src = fullImgUrl(mid); lb.classList.add('open'); }
  function closeLb() { if (lb) { lb.classList.remove('open'); lbImg.src = ''; } }
  var lbX = document.getElementById('rv-lb-x');
  if (lbX) lbX.addEventListener('click', closeLb);
  if (lb) lb.addEventListener('click', function (e) { if (e.target === lb) closeLb(); });
  document.addEventListener('keydown', function (e) { if (e.key === 'Escape') closeLb(); });

  function collectCorrections(cardEl) {
    var out = {};
    cardEl.querySelectorAll('.freg').forEach(function (fr) {
      var on = fr.querySelector('.fsb.on');
      var cur = on ? on.getAttribute('data-state') : null;
      if (cur && cur !== fr.getAttribute('data-orig')) out[fr.getAttribute('data-region')] = cur;
    });
    return out;
  }
  function refreshDirty(cardEl) {
    if (!cardEl || cardEl.classList.contains('done')) return;
    var n = Object.keys(collectCorrections(cardEl)).length;
    cardEl.classList.toggle('dirty', n > 0);
    var ok = cardEl.querySelector('.fbtn.ok');
    if (ok) { ok.classList.toggle('fix', n > 0); ok.textContent = n ? 'Fix ' + n + ' & continue' : 'Looks right'; }
  }

  fgrid.addEventListener('click', async function (ev) {
    var th = ev.target.closest('.fthumb.zoomable');
    if (th) { var cz = th.closest('.fcard'); openLb(cz && cz.dataset.mid); return; }
    var sb = ev.target.closest('.fsb');
    if (sb) {
      if (sb.disabled) return;
      var fr = sb.closest('.freg');
      fr.querySelectorAll('.fsb').forEach(function (x) { x.classList.remove('on'); });
      sb.classList.add('on');
      refreshDirty(sb.closest('.fcard'));
      return;
    }
    var okBtn = ev.target.closest('.fbtn.ok');
    var cardEl = ev.target.closest('.fcard');
    var mid = cardEl && Number(cardEl.dataset.mid);
    if (!okBtn || !mid || cardEl.classList.contains('done')) return;
    var corrections = collectCorrections(cardEl);
    var n = Object.keys(corrections).length;
    var msg = n
      ? 'Apply ' + n + ' correction' + (n === 1 ? '' : 's') + ' to #' + mid + ' and LOCK ' + (n === 1 ? 'it' : 'them') +
        '?\n\n' + Object.keys(corrections).map(function (k) { return (REGION_LBL[k] || k) + ' → ' + corrections[k]; }).join('\n') +
        '\n\nNo re-run can revert a locked field. Nothing is sent to OnlyFans.'
      : 'Confirm the flags on #' + mid + ' as correct? This is recorded as a grade — it is what the accuracy number is measured from.';
    if (!confirm(msg)) return;
    var note = (cardEl.querySelector('.fnote') || {}).value || '';
    try {
      await Fastt.post('/admin/vault-ai/flags-review/grade', { account_id: acct, media_id: mid, corrections: corrections, note: note });
      Fastt.saved(n ? 'Fixed + locked ' + n + ' region' + (n === 1 ? '' : 's') + ' on #' + mid : 'Confirmed #' + mid);
      cardEl.classList.add('done'); cardEl.classList.remove('dirty');
      okBtn.textContent = 'Confirmed ✓'; okBtn.classList.remove('fix');
      cardEl.querySelectorAll('.fsb, .fnote').forEach(function (x) { x.disabled = true; });
      state.flags.graded = (state.flags.graded || 0) + 1;
      try { acc = await Fastt.get('/admin/vault-ai/flags-accuracy'); } catch (e) {}
      updateFlagsFine();
      // A correction re-cuts lanes + re-bands prices and can open/close disputes.
      if (n) { try { disputes = await Fastt.get('/admin/vault-ai/disputes'); renderDisputes(); } catch (e) {} }
    } catch (e) { Fastt.oops(e); }
  });

  // ── advanced (live where the API has the knob) ───────────────
  var thresh = document.getElementById('rv-thresh');
  if (thresh) thresh.addEventListener('change', async function () {
    state.threshold = Number(thresh.value) || 2;
    try {
      state.dupes = await Fastt.get('/admin/vault-ai/duplicates', { threshold: state.threshold });
      state.keep = {};
      renderDupes();
      Fastt.toast('Re-clustered at strictness ' + state.threshold, 'ok');
    } catch (e) { Fastt.oops(e); }
  });
  var allowSent = document.getElementById('rv-allowsent');
  if (allowSent) allowSent.addEventListener('click', function () {
    state.allowSent = !allowSent.classList.contains('on'); // fx kit toggles after us
  });
  var order = document.getElementById('rv-order');
  if (order) order.addEventListener('change', async function () {
    state.order = order.value;
    try {
      state.flags = await Fastt.get('/admin/vault-ai/flags-review',
        { limit: state.batch, only_iffy: state.order === 'only' });
      renderFlags();
    } catch (e) { Fastt.oops(e); }
  });
  var batch = document.getElementById('rv-batch');
  if (batch) batch.addEventListener('change', async function () {
    state.batch = Math.max(1, Math.round(Number(batch.value) || 24));
    batch.value = String(state.batch);
    try {
      state.flags = await Fastt.get('/admin/vault-ai/flags-review',
        { limit: state.batch, only_iffy: state.order === 'only' });
      renderFlags();
    } catch (e) { Fastt.oops(e); }
  });

  // ══════════════════════════════════════════════════════════════
  // TAB 0 · WAITING FOR APPROVAL — the queue this page is named for.
  //
  // /admin/vault-ai/review returns three pending buckets (folder / ppv /
  // reminder). Only `reminder` had a home anywhere in this UI; `folder` and
  // `ppv` proposals were invisible. Mirrors app/components/vault/
  // VaultReviewTab.tsx: per-section Approve all + per-item Approve/Reject,
  // with the `stale` id list badged and left pending.
  // ══════════════════════════════════════════════════════════════
  var QSECTIONS = [
    { kind: 'folder', title: 'Folders', icon: '📁', empty: 'No folder proposals waiting.' },
    { kind: 'ppv', title: 'PPVs', icon: '💰', empty: 'No PPV drafts waiting.' },
    { kind: 'reminder', title: 'Reminders', icon: '🔔', empty: 'No reminder cards waiting.' },
  ];
  var qState = { data: null, stale: {}, busy: false };
  var qWrap = document.getElementById('rv-queue');

  function qIds(v) { return Array.isArray(v) ? v.filter(function (x) { return typeof x === 'number'; }) : []; }
  function qStr(v) { return (typeof v === 'string' && v) ? v : null; }
  function qNum(v) { return (typeof v === 'number' && isFinite(v)) ? v : null; }
  function qThumbs(ids) {
    // Every proposal's media lives in the mirror; with a cold mirror the thumb
    // route 404s, so don't request images that cannot exist.
    if (!sum.count || !ids.length) return '';
    return '<div class="rq-thumbs">' + ids.slice(0, 6).map(function (m) {
      return '<div class="rq-th" style="background-image:url(\'' + esc(thumbUrl(m)) + '\')"></div>';
    }).join('') + (ids.length > 6 ? '<div class="rq-th" style="display:flex;align-items:center;justify-content:center;font-size:11px;color:#8a8a8a">+' + (ids.length - 6) + '</div>' : '') + '</div>';
  }
  function qBody(kind, p) {
    p = p || {};
    if (kind === 'folder') {
      var name = qStr(p.folder_name) || qStr(p.folder_key) || '(unnamed)';
      var media = qIds(p.media_ids);
      var why = qStr(p.rationale);
      return '<div class="rq-t">📁 ' + esc(name) + '</div>' +
        '<div class="rq-m"><span>' + Fastt.fmtInt(media.length) + ' media</span>' +
        (qStr(p.source) ? '<span>' + esc(p.source) + '</span>' : '') + '</div>' +
        (why ? '<div class="rq-q">' + esc(why) + '</div>' : '') + qThumbs(media);
    }
    if (kind === 'ppv') {
      var nm = qStr(p.name) || '(unnamed)';
      var md = qIds(p.media_ids), pv = qIds(p.preview_media_ids);
      var cents = qNum(p.price_cents), tier = qStr(p.tier);
      var cap = qStr(p.caption), scr = qStr(p.script);
      return '<div class="rq-t">💰 ' + esc(nm) + '</div>' +
        '<div class="rq-m"><span>' + Fastt.fmtInt(md.length) + ' media</span><span>' +
        Fastt.fmtInt(pv.length) + ' preview</span>' +
        (cents == null ? '' : '<span>' + Fastt.fmtCents(cents) + '</span>') +
        (tier ? '<span>tier: ' + esc(tier) + '</span>' : '') + '</div>' +
        (cap ? '<div class="rq-q">“' + esc(cap) + '”</div>' : '') +
        (scr ? '<div class="rq-m"><span>script: ' + esc(scr) + '</span></div>' : '') +
        qThumbs(md.concat(pv));
    }
    var fn = qStr(p.folder_name) || '(no folder)';
    var mi = qIds(p.media_ids);
    var line = qStr(p.line), aud = qStr(p.audience), per = qNum(p.images_per_day);
    return '<div class="rq-t">🔔 ' + esc(fn) + '</div>' +
      '<div class="rq-m"><span>' + Fastt.fmtInt(mi.length) + ' media</span>' +
      (per == null ? '' : '<span>' + per + '/day</span>') +
      (aud ? '<span>' + esc(aud) + '</span>' : '') + '</div>' +
      (line ? '<div class="rq-q">“' + esc(line) + '”</div>' : '') + qThumbs(mi);
  }

  function renderQueue() {
    if (!qWrap) return;
    var d = qState.data || {};
    var total = 0;
    QSECTIONS.forEach(function (sec) { total += (d[sec.kind] || []).length; });
    setCt('rv-ct-queue', total);
    qWrap.innerHTML = QSECTIONS.map(function (sec) {
      var items = d[sec.kind] || [];
      var rows = items.length ? items.map(function (it) {
        var isStale = !!qState.stale[it.id];
        return '<div class="rq-item" data-rid="' + esc(it.id) + '">' +
          '<div class="rq-b"><div class="rq-m" style="margin:0 0 3px"><span>#' + esc(it.id) + '</span>' +
          '<span>' + esc(it.status || 'pending') + '</span>' +
          '<span>' + esc(dayTxt(it.created_at)) + '</span>' +
          (isStale ? '<span class="rq-stale" title="The vault moved under this proposal — re-review before approving.">stale</span>' : '') +
          '</div>' + qBody(sec.kind, it.payload) + '</div>' +
          '<div class="rq-acts"><button class="btn-ok" data-act="approve">Approve</button>' +
          '<button class="btn-no" data-act="reject">Reject</button></div></div>';
      }).join('') : '<div class="rq-empty">' + esc(sec.empty) +
        (sum.count ? '' : ' The local mirror is empty — sync and describe the vault on Vault Manage and the producers start proposing.') + '</div>';
      return '<div class="rq-sec" data-kind="' + sec.kind + '">' +
        '<div class="rq-h"><span class="t">' + esc(sec.title) + '</span>' +
        '<span class="c">' + Fastt.fmtInt(items.length) + ' pending</span>' +
        '<span class="sp"></span>' +
        '<button class="btn-ok" data-act="approve-section"' + (items.length ? '' : ' disabled') + '>Approve all</button></div>' +
        rows + '</div>';
    }).join('');
  }

  async function reloadQueue() {
    try { qState.data = await Fastt.get('/admin/vault-ai/review'); }
    catch (e) { Fastt.oops(e); qState.data = { folder: [], ppv: [], reminder: [] }; }
    renderQueue();
    paintWaiting();
  }

  function pendingTotal() {
    var d = qState.data || {};
    return (d.folder || []).length + (d.ppv || []).length + (d.reminder || []).length;
  }
  function paintWaiting() {
    var waiting = (state.dupes.removable || 0) + (disputes.count || 0) +
      (state.flags.total || 0) + pendingTotal();
    stripSet('rv-st-wait', waiting ? Fastt.fmtInt(waiting) + ' items waiting' : 'Nothing waiting', waiting ? 'warn' : '');
  }

  qState.data = review0;
  renderQueue();
  paintWaiting();
  Fastt.liveBadge(document.querySelector('#pane-queue .fx-note'));

  if (qWrap) qWrap.addEventListener('click', async function (ev) {
    var btn = ev.target.closest('button[data-act]');
    if (!btn || btn.disabled || qState.busy) return;
    var act = btn.dataset.act;
    var sec = btn.closest('.rq-sec');
    var kind = sec && sec.dataset.kind;
    var row = btn.closest('.rq-item');
    var rid = row ? Number(row.dataset.rid) : null;

    if (act === 'approve-section') {
      var n = ((qState.data || {})[kind] || []).length;
      if (!confirm('Approve all ' + n + ' pending ' + kind + ' proposal' + (n === 1 ? '' : 's') + '?\n\n' +
        'This flips them to approved on the review ledger. Nothing is created on OnlyFans and nothing is sent — ' +
        'a downstream automation applies approved rows later.')) return;
      qState.busy = true;
      try {
        var r = await Fastt.post('/admin/vault-ai/review/approve', { account_id: acct, kind: kind });
        (r.stale || []).forEach(function (id) { qState.stale[id] = true; });
        Fastt.saved('Approved ' + (r.approved || []).length + ((r.stale || []).length ? ' · ' + r.stale.length + ' stale (still pending)' : ''));
        await reloadQueue();
      } catch (e) { Fastt.oops(e); } finally { qState.busy = false; }
      return;
    }
    if (!rid) return;
    if (act === 'approve') {
      if (!confirm('Approve proposal #' + rid + '?\n\nLedger-only: nothing is created on OnlyFans and nothing is sent.')) return;
      qState.busy = true;
      try {
        var r2 = await Fastt.post('/admin/vault-ai/review/approve', { account_id: acct, ids: [rid] });
        if ((r2.stale || []).indexOf(rid) >= 0) {
          qState.stale[rid] = true;
          Fastt.toast('#' + rid + ' is stale — the vault moved since it was drafted. It stays pending.', 'err');
        } else {
          delete qState.stale[rid];
          Fastt.saved('Approved #' + rid);
        }
        await reloadQueue();
      } catch (e) { Fastt.oops(e); } finally { qState.busy = false; }
      return;
    }
    if (act === 'reject') {
      if (!confirm('Reject proposal #' + rid + '? The suggestion is discarded and the queue clears.')) return;
      qState.busy = true;
      try {
        var r3 = await Fastt.post('/admin/vault-ai/review/reject', { account_id: acct, ids: [rid] });
        (r3.rejected || []).forEach(function (id) { delete qState.stale[id]; });
        Fastt.saved('Rejected #' + rid);
        await reloadQueue();
      } catch (e) { Fastt.oops(e); } finally { qState.busy = false; }
    }
  });

  // (the server-fixed advanced knobs were badged at the top, before the
  // no-account return — Fastt.staticBadge is idempotent either way)
});
