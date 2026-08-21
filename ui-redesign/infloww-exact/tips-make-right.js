Fastt.ready(async function () {
  var $ = Fastt.$, esc = Fastt.esc;
  // No creator → the markup keeps its SAMPLE badges (inbox rows AND status
  // strip) and the disabled Approve/Skip buttons; fastt.js adds the global
  // placeholder banner.
  if (!Fastt.account()) return;

  // A real creator is in scope: drop the mock-up SAMPLE rows/badges right away
  // (before any await) so fabricated incidents never show next to live data.
  ['mr-sample-badge', 'mr-sample-strip'].forEach(function (id) {
    var b = $('#' + id); if (b) b.remove();
  });
  inboxRows('<div class="irow"><div class="c-what" style="flex:1"><div class="wt" style="color:var(--muted)">Scanning for incidents…</div></div></div>');
  $('#mr-inbox-n').textContent = '…';
  chip('mr-st-sug', 'Scanning…');
  chip('mr-st-scan', 'Scanning…');
  // these two are baked mock-up values until render() lands (mr-st-mode keeps
  // its "Preview-only — nothing sends" default: true until proven otherwise)
  chip('mr-st-look', 'Loading…');
  chip('mr-st-cap', 'Loading…');

  // A failed config GET must not leave the page wearing "Scanning…" forever:
  // render()/runPreview() never run, so say so before letting boot() toast.
  var out;
  try {
    out = await Fastt.get('/admin/make-right-config');
  } catch (e) {
    chip('mr-st-mode', 'Settings unavailable', 'err');
    chip('mr-st-sug', 'Not scanned', 'err');
    chip('mr-st-scan', 'Config failed to load', 'err');
    chip('mr-st-look', 'Lookback unknown');
    chip('mr-st-cap', 'Per-fan cap unknown');
    $('#mr-inbox-n').textContent = '—';
    $('#mr-sweep-note').textContent = 'Not scanned';
    $('#mr-run-why').textContent = 'Settings could not be loaded — the sweep button stays disabled.';
    inboxRows('<div class="irow"><div class="c-what" style="flex:1"><div class="wt" style="color:var(--muted)">'
      + 'Could not load this creator’s Make It Right settings — see the toast for the error.</div></div></div>');
    throw e; // the boot loop in fastt.js catches this and calls Fastt.oops
  }
  var stored = out.config || {};
  var defaults = out.defaults || {};
  function M() { var m = {}; Object.assign(m, defaults, stored); return m; }

  var saving = false;
  async function save() {
    if (saving) return; saving = true;
    try {
      var r = await Fastt.put('/admin/make-right-config',
        { account_id: Fastt.account(), config: stored });
      if (r && r.config) stored = r.config;
      Fastt.saved();
    } catch (e) { Fastt.oops(e); }
    finally { saving = false; }
    render();
    rescanDeb(); // keep the incident inbox truthful to the just-saved settings (dry-run only)
  }
  var saveDeb = Fastt.debounce(save, 700);
  var rescanDeb = Fastt.debounce(function () { runPreview(false); }, 900);

  // folder / tier names for the two pickers (best-effort reads)
  var vaultNames = [], vaultMeta = {}, tierNames = [];
  try {
    var vl = await Fastt.get('/api/of/v2/vault/lists', { limit: 50 }); // route caps at le=50 — 100 422s
    (vl.list || []).forEach(function (x) {
      if (!x || !x.name) return;
      vaultNames.push(x.name);
      vaultMeta[x.name] = (x.photosCount || 0) + (x.videosCount || 0) + (x.gifsCount || 0) + (x.audiosCount || 0);
    });
  } catch (e) {}
  try {
    var trc = await Fastt.get('/admin/tip-reward-config');
    var tiers = (trc.config && trc.config.tiers) || (trc.defaults && trc.defaults.tiers) || [];
    tierNames = tiers.map(function (t) { return t.name; }).filter(Boolean);
  } catch (e) {}
  function folderNote(n) {
    if (!n) return '';
    var t = vaultMeta[n];
    if (t === undefined) return ' (not in this vault)';
    return t === 0 ? ' (empty — nothing will send)' : ' (' + t + ' items)';
  }

  function chip(id, text, cls) {
    var el = $('#' + id); if (!el) return;
    el.innerHTML = '<i></i>' + esc(text);
    if (cls !== undefined) {
      el.classList.remove('ok', 'warn', 'err');
      if (cls) el.classList.add(cls);
    }
  }
  function setCheck(el, on) { if (el) el.classList.toggle('on', on); }

  function render() {
    var m = M();
    var sw = $('#mr-card .fx-switch[data-k]');
    sw.classList.toggle('on', !!m.enabled);
    $('#mr-card').classList.toggle('on', !!m.enabled);
    $('#mr-card .fx-tc-state').textContent = m.enabled ? 'On' : 'Off';
    setCheck($('#mr-autosend'), !!m.auto_send);
    setCheck($('#mr-undeliv'), !!m.detect_undelivered);
    setCheck($('#mr-match'), !!m.gift_value_match);
    setCheck($('#mr-open'), !!m.open_with_gift);
    setCheck($('#mr-flag'), !!m.flag_refund);

    var live = m.enabled && m.auto_send;
    chip('mr-st-mode', live ? 'Live — approved fixes send' : 'Preview-only — nothing sends', live ? 'ok' : 'warn');
    chip('mr-st-look', 'Looks back ' + m.lookback_days + ' days');
    chip('mr-st-cap', 'Max ' + m.per_fan_cap + ' make-rights per fan');
    $('#mr-kv').innerHTML = 'fires up to <b>' + esc(m.per_fan_cap) + '×</b> per fan, then hands him to you';

    $('#mr-look').value = m.lookback_days;
    $('#mr-grace').value = m.undelivered_grace_hours;
    $('#mr-gmin').value = m.gift_min_count;
    $('#mr-gmax').value = m.gift_max_count;
    $('#mr-gval').value = ((m.gift_piece_value_cents || 0) / 100).toFixed(2);
    $('#mr-acap').value = m.apology_caption || '';
    $('#mr-ppvcap').value = m.ppv_caption || '';
    $('#mr-steps').value = m.free_steps;
    $('#mr-pps').value = m.gift_pieces_per_step;
    $('#mr-ppvprice').value = ((m.ppv_price_cents || 0) / 100).toFixed(2);
    $('#mr-nudge').value = m.nudge_hours;
    $('#mr-close').value = m.close_hours;
    $('#mr-cap2').value = m.per_fan_cap;
    $('#mr-guard').value = m.guard_hours;
    // on_hard_decline ships in the automation defaults but make_right_config_api
    // ._validate does NOT allowlist it — a PUT would drop it, so show the truth.
    $('#mr-hd').value = (m.on_hard_decline === false)
      ? 'Off' : 'Always on — a hard decline gets an apology, never a bot stop';

    // gift tier — real tip-reward tier names
    var gsel = $('#mr-gtier');
    var gnames = tierNames.slice();
    if (m.gift_tier && gnames.indexOf(m.gift_tier) < 0) gnames.push(m.gift_tier);
    gsel.innerHTML = '<option value="">Auto-pick by over-charge</option>' + gnames.map(function (n) {
      return '<option value="' + esc(n) + '">' + esc(n) + '</option>';
    }).join('');
    gsel.value = m.gift_tier || '';

    // PPV folder — real vault folder names, with the item count that decides
    // whether the pivot can send anything at all
    var psel = $('#mr-ppvfolder');
    var pnames = vaultNames.slice();
    if (m.ppv_folder && pnames.indexOf(m.ppv_folder) < 0) pnames.unshift(m.ppv_folder);
    psel.innerHTML = '<option value="">No PPV — just close warmly</option>' + pnames.map(function (n) {
      return '<option value="' + esc(n) + '">' + esc(n) + esc(folderNote(n)) + '</option>';
    }).join('');
    psel.value = m.ppv_folder || '';

    paintRunBar();
  }

  // ── incident inbox ← dry-run preview (nothing sends, nothing is logged) ──
  var AVG = ['g1', 'g2', 'g3', 'g4'];
  function inboxRows(html) {
    var card = $('#mr-inbox');
    card.querySelectorAll('.irow').forEach(function (r) { r.remove(); });
    var bar = document.getElementById('mr-runbar');
    if (bar) bar.insertAdjacentHTML('beforebegin', html);
    else card.insertAdjacentHTML('beforeend', html);
  }
  // mirrors service/automations/make_right.py: every action it can emit.
  var ACTION_LABEL = {
    would_open: 'suggested', in_progress: 'in progress', operator_only: 'operator-only',
    excluded: 'skipped', already_handled: 'already handled'
  };
  var ACTION_CLASS = {
    would_open: 'suggested', in_progress: 'running', operator_only: 'yours',
    excluded: 'sent', already_handled: 'done'
  };
  var ACTION_ACT = {
    would_open: 'sends only once<br>enabled + auto-send are on',
    in_progress: 'live exchange —<br>the bot is working it',
    operator_only: 'handed to an operator',
    excluded: 'skipped by policy',
    already_handled: 'already made right'
  };
  var ACTION_FIX = {
    in_progress: 'Exchange already running',
    operator_only: 'Handed to an operator',
    excluded: 'Skipped by policy',
    already_handled: 'Already apologised for'
  };
  function stepLine(r) {
    var steps = r.steps || [];
    if (!steps.length) return '';
    var pretty = steps.map(function (s) { return String(s).replace('apology_gift', 'apology+gift'); });
    var at = (typeof r.step === 'number' && r.step > 0) ? ' · at step <b>' + esc(r.step + 1) + '</b>' : '';
    return '<div class="steps">' + esc(pretty.join(' → ')) + at + '</div>';
  }

  var lastPreview = null;
  function paintRunBar() {
    var m = M(), btn = $('#mr-run'), why = $('#mr-run-why');
    if (!btn) return;
    var n = lastPreview ? (lastPreview.would_open || 0) : 0;
    var reason = lastPreview ? lastPreview.preview_only_reason : undefined;
    var ready = !!(m.enabled && m.auto_send) && n > 0;
    btn.disabled = !ready;
    btn.style.opacity = ready ? '' : .5;
    btn.style.cursor = ready ? 'pointer' : 'not-allowed';
    if (!lastPreview) { why.textContent = 'Checking whether anything is safe to send…'; return; }
    // `preview_only_reason` is null on this route (the preview endpoint is always
    // a dry run), so quote it only when the relay actually returned one.
    var relaySays = reason ? ' Relay: <code>preview_only_reason: ' + esc(reason) + '</code>.' : '';
    if (!m.enabled) {
      why.innerHTML = 'Make It Right is <b>off</b> — a real sweep would run preview-only and send nothing. '
        + 'Turn the switch on to unlock this button.' + relaySays;
    } else if (!m.auto_send) {
      why.innerHTML = 'Auto-send is <b>off</b> — a real sweep would run preview-only and send nothing. '
        + 'Tick “Auto-send approved fixes” to unlock this button.' + relaySays;
    } else if (n === 0) {
      why.innerHTML = 'Nothing to send: <b>0</b> of the '
        + esc(lastPreview.candidates || 0) + ' incident(s) would start an exchange right now.';
    } else {
      why.innerHTML = 'Sends a real apology + free content to <b>' + esc(n)
        + '</b> fan(s) and flags their refunds for you. One click, then a confirmation.';
    }
  }

  async function runPreview(fromClick) {
    var note = $('#mr-sweep-note');
    note.textContent = 'Scanning…';
    chip('mr-st-scan', 'Scanning…');
    try {
      var p = await Fastt.post('/admin/make-right-preview', { account_id: Fastt.account() });
      lastPreview = p;
      var rows = p.preview || [];
      var cand = p.candidates || 0, wouldOpen = p.would_open || 0;
      var opOnly = p.operator_only || 0, inProg = p.in_progress || 0;
      var excl = p.excluded || 0, handled = p.already_handled || 0;
      var skipped = opOnly + excl + handled;

      $('#mr-inbox-n').textContent = rows.length;
      // "N incidents · M actionable (K skipped)" — no more "0 suggested" next to a badge of 2
      chip('mr-st-sug', cand
        ? cand + (cand === 1 ? ' incident · ' : ' incidents · ') + wouldOpen + ' actionable'
          + (skipped ? ' (' + skipped + ' skipped)' : '')
        : 'No incidents found',
        wouldOpen ? 'ok' : (cand ? 'warn' : ''));
      chip('mr-st-scan', 'Scanned just now');
      note.textContent = 'Dry-run scan · preview only — nothing was sent';

      // the full breakdown the relay returns, not just `preview`
      $('#mr-pv-sum').innerHTML =
        '<span class="fx-st"><i></i>' + cand + ' candidate' + (cand === 1 ? '' : 's') + ' detected</span>'
        + '<span class="fx-st' + (wouldOpen ? ' ok' : '') + '"><i></i>' + wouldOpen + ' would start an exchange</span>'
        + '<span class="fx-st' + (inProg ? ' ok' : '') + '"><i></i>' + inProg + ' already in progress</span>'
        + '<span class="fx-st' + (opOnly ? ' warn' : '') + '"><i></i>' + opOnly + ' operator-only</span>'
        + '<span class="fx-st"><i></i>' + excl + ' skipped by a guard</span>'
        + '<span class="fx-st"><i></i>' + handled + ' already handled</span>'
        + (p.preview_only_reason
            ? '<span class="fx-st warn"><i></i>preview-only: ' + esc(p.preview_only_reason) + '</span>'
            : '<span class="fx-st ok"><i></i>no preview-only block</span>');

      paintRunBar();

      if (!rows.length) {
        inboxRows('<div class="irow"><div class="c-what" style="flex:1"><div class="wt" style="color:var(--muted)">No incidents found in the last ' + esc(M().lookback_days) + ' days — nothing to make right.</div></div></div>');
        if (fromClick) Fastt.toast('Scan done — no incidents found', 'ok');
        return;
      }
      inboxRows(rows.map(function (r, i) {
        var name = r.name || ('fan #' + r.fan_id);
        var initials = String(name).split(/\s+/).map(function (w) { return w[0] || ''; }).join('').slice(0, 2).toUpperCase();
        var kind = r.kind === 'dup_charge' ? 'Charged twice for the same content' : (r.kind || 'incident');
        var gift = (r.gift_media_ids || []).length;
        var fix = r.apology
          ? '“' + r.apology.slice(0, 70) + (r.apology.length > 70 ? '…' : '') + '”'
          : (ACTION_FIX[r.action] || 'No fix suggested');
        // make_right.py emits `reason` only on operator_only (per_fan_cap); the
        // excluded/in_progress/already_handled lanes carry none, so name the gate.
        var noteFor = {
          excluded: 'blocked by the contact guard / stop lists (blacklist, paused, muted, bot)',
          in_progress: 'the exchange is already open with him',
          already_handled: 'this exact incident was already made right'
        };
        var fs = (gift ? gift + (gift === 1 ? ' free piece' : ' free pieces') + ' · ' : '')
          + (r.refund_flagged ? '<span class="rf">refund flagged for you</span>'
             : (r.action === 'would_open' ? 'no refund flag'
                : esc(r.reason ? 'reason: ' + r.reason : (noteFor[r.action] || 'no fix queued'))));
        var act = ACTION_ACT[r.action] || esc(r.action || '');
        return '<div class="irow">'
          + '<div class="c-fan"><span class="iav ' + AVG[i % 4] + '">' + esc(initials) + '</span>'
          + '<span><span class="fn">' + esc(name) + '</span><span class="fa">fan #' + esc(r.fan_id) + '</span></span></div>'
          + '<div class="c-what"><div class="wt">' + esc(kind) + '</div>'
          + '<div class="ws">' + esc(Fastt.fmtCents(r.wrongful_cents || 0)) + ' wrongly charged · media overlap confirmed</div></div>'
          + '<div class="c-fix"><div class="ft">' + esc(fix).replace(/&quot;/g, '"') + '</div>'
          + '<div class="fs">' + fs + '</div>' + stepLine(r) + '</div>'
          + '<div class="c-status"><span class="stchip ' + (ACTION_CLASS[r.action] || 'suggested') + '"><i></i>'
          + esc(ACTION_LABEL[r.action] || r.action || 'suggested') + '</span></div>'
          + '<div class="c-act"><span class="act-note">' + act + '</span></div>'
          + '</div>';
      }).join(''));
    } catch (e) {
      lastPreview = null;
      note.textContent = 'Scan failed';
      chip('mr-st-scan', 'Scan failed');
      $('#mr-pv-sum').innerHTML = '<span class="fx-st err"><i></i>Dry-run scan failed — no breakdown available</span>';
      inboxRows('<div class="irow"><div class="c-what" style="flex:1"><div class="wt" style="color:var(--muted)">Could not scan — see the toast for the error.</div></div></div>');
      paintRunBar();
      Fastt.oops(e);
    }
  }
  $('#mr-rescan').addEventListener('click', function () { runPreview(true); });

  // ── the ONE outbound action: enqueue the whole sweep (explicit click + confirm) ──
  // There is no per-incident approve/skip route on the relay (see /openapi.json),
  // so this mirrors the real app: gated on enabled && auto_send, and it re-runs
  // the dry run first so the confirmation names a number that is still true.
  $('#mr-run').addEventListener('click', async function () {
    var m = M();
    if (!m.enabled || !m.auto_send) {
      Fastt.toast('Turn Make It Right on AND tick auto-send first', 'err'); return;
    }
    this.disabled = true;
    await runPreview(false);                 // refuse to send against a stale count
    var n = lastPreview ? (lastPreview.would_open || 0) : 0;
    if (!n) { Fastt.toast('Nothing to send — 0 incidents would start an exchange', 'ok'); paintRunBar(); return; }
    if (!window.confirm('Send a real apology + free content to ' + n + ' fan(s) who were wrongly charged?\n\n'
        + 'This sends real DMs and free media, and flags any refunds for you to action in OF.')) {
      paintRunBar(); return;
    }
    try {
      var resp = await Fastt.post('/admin/automation/enqueue',
        { account_id: Fastt.account(), kind: 'make_right', payload: {} });
      Fastt.toast('Queued (job #' + ((resp && resp.enqueued_job_id) || '?') + ') — sends within ~30s', 'ok');
    } catch (e) { Fastt.oops(e); }
    finally { paintRunBar(); }
  });

  // ── bindings ──
  // Bounds mirror service/make_right_config_api.py _INT_KNOBS. Clamp on the
  // client too so a typo SNAPS visibly to the allowed range instead of the
  // server silently clamping it (which reads as "my number didn't save").
  var INT_BOUNDS = {
    lookback_days: [1, 365], per_fan_cap: [1, 10],
    gift_min_count: [1, 20], gift_max_count: [1, 20],
    free_steps: [0, 10], gift_pieces_per_step: [1, 10],
    nudge_hours: [1, 8760], close_hours: [1, 8760],
    guard_hours: [0, 8760], undelivered_grace_hours: [0, 8760]
  };
  function bindInt(id, key) {
    var b = INT_BOUNDS[key] || [0, 1e9];
    $('#' + id).addEventListener('change', function () {
      var n = parseInt(this.value, 10);
      if (isNaN(n)) { render(); return; }              // junk → revert to stored
      stored[key] = Math.max(b[0], Math.min(n, b[1]));  // clamp to server range
      // Keep the server's two cross-field rules valid so a save never 422s.
      // The field the operator just edited wins; its sibling is nudged to match.
      var e = M();
      if (e.gift_max_count < e.gift_min_count) {
        if (key === 'gift_min_count') stored.gift_max_count = e.gift_min_count;
        else stored.gift_min_count = e.gift_max_count;
      }
      if (e.close_hours < e.nudge_hours) {
        if (key === 'nudge_hours') stored.close_hours = e.nudge_hours;
        else stored.nudge_hours = e.close_hours;
      }
      saveDeb(); render(); // reflect the clamp + any sibling bump immediately
    });
  }
  bindInt('mr-look', 'lookback_days');
  bindInt('mr-grace', 'undelivered_grace_hours');
  bindInt('mr-gmin', 'gift_min_count');
  bindInt('mr-gmax', 'gift_max_count');
  bindInt('mr-steps', 'free_steps');
  bindInt('mr-pps', 'gift_pieces_per_step');
  bindInt('mr-nudge', 'nudge_hours');
  bindInt('mr-close', 'close_hours');
  bindInt('mr-cap2', 'per_fan_cap');
  bindInt('mr-guard', 'guard_hours');
  // loCents/hiCents mirror _INT_KNOBS — clamp both ends so the input snaps to
  // the storable range instead of re-rendering as the server's silent clamp.
  function bindCents(id, key, loCents, hiCents) {
    $('#' + id).addEventListener('change', function () {
      var d = parseFloat(this.value);
      if (isNaN(d)) { render(); return; }
      stored[key] = Math.max(loCents, Math.min(Math.round(d * 100), hiCents));
      saveDeb(); render();
    });
  }
  bindCents('mr-gval', 'gift_piece_value_cents', 1, 100000);   // 1¢–$1,000
  bindCents('mr-ppvprice', 'ppv_price_cents', 0, 1000000);     // 0 (no PPV pivot)–$10,000
  $('#mr-acap').addEventListener('change', function () { stored.apology_caption = this.value; saveDeb(); });
  $('#mr-ppvcap').addEventListener('change', function () { stored.ppv_caption = this.value; saveDeb(); });
  $('#mr-gtier').addEventListener('change', function () { stored.gift_tier = this.value; saveDeb(); });
  $('#mr-ppvfolder').addEventListener('change', function () { stored.ppv_folder = this.value; saveDeb(); });

  var CHECK_KEYS = { 'mr-autosend': 'auto_send', 'mr-undeliv': 'detect_undelivered',
                     'mr-match': 'gift_value_match', 'mr-open': 'open_with_gift', 'mr-flag': 'flag_refund' };
  document.addEventListener('click', function (e) {
    var sw = e.target.closest('#mr-card .fx-switch[data-k]');
    if (sw) { stored.enabled = sw.classList.contains('on'); save(); return; }
    var ck = e.target.closest('.fx-check[id]');
    if (ck && CHECK_KEYS[ck.id]) { stored[CHECK_KEYS[ck.id]] = ck.classList.contains('on'); save(); }
  });

  Fastt.liveBadge($('#mr-card .fx-tc-title'));
  Fastt.liveBadge($('#mr-inbox .fx-card-h'));
  // no backend key exists for these — visibly static, inert
  Fastt.staticBadge($('#mr-sens-field > label'), 'STATIC');
  Fastt.staticBadge($('#mr-tone-field > label'), 'STATIC');
  Fastt.staticBadge($('#mr-hd-field > label'), 'READ-ONLY — no save key');
  $('#mr-sens').disabled = true; $('#mr-sens').style.opacity = .55;
  $('#mr-tone').disabled = true; $('#mr-tone').style.opacity = .55;

  render();
  runPreview(false); // dry-run only: the endpoint never sends or logs anything
});
