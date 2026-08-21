/* ==== Re-engage Buyers wiring — automation rule kind=reengage_buyers
   payload {lookback_days, cold_hours, max_per_run, guard_hours, tone} ==== */
Fastt.ready(async () => {
  const $ = Fastt.$;
  if (!Fastt.account()) return;

  const DEFAULTS = { lookback_days: 3, cold_hours: 24, max_per_run: 25, guard_hours: 12, tone: 'soft' };
  const CREATE = { name: 'Re-engage buyers', every_seconds: 3600, is_enabled: false };

  let rule = await Fastt.rule('reengage_buyers');
  let pay = Object.assign({}, DEFAULTS, (rule && rule.payload) || {});

  var chipText = Fastt.chipText;
  // reengage_buyers reads every knob as `int(cfg.get(k) or DEFAULT)` — a stored 0
  // (or a blank) silently RUNS at the default (max_per_run 0→25, lookback 0→3,
  // cold 0→24, guard 0→12). So 0 is not a settable state: reject it and snap the
  // field back rather than displaying a number the sweep will never use.
  const KNOB_MIN = 1;
  const KNOB_LABEL = {
    lookback_days: 'Bought within the last (days)',
    cold_hours: 'Cold after (hours with no reply)',
    max_per_run: 'Max openers per run',
    guard_hours: 'Skip if messaged within (hours)',
  };
  function knob(key, raw) {
    const n = parseInt(String(raw).replace(/[,\s]/g, ''), 10);
    if (!isFinite(n) || n < KNOB_MIN) {
      Fastt.toast('"' + KNOB_LABEL[key] + '" must be ' + KNOB_MIN + ' or more — the engine '
        + 'treats 0 (and blank) as "use the default ' + DEFAULTS[key] + '".', 'err');
      paint();                       // restore the value that is actually stored
      return;
    }
    if (n !== Number(pay[key])) savePayload({ [key]: n });
    else paint();                    // normalize formatting, no pointless PATCH
  }
  const minsUntil = (iso) => {
    // next_due_at is a tz-NAIVE UTC stamp — a bare new Date() reads it as local
    // and lands hours in the past, pinning every readout to 0. parseUtc stamps the Z.
    const d = Fastt.parseUtc(iso);
    if (!d) return null;
    const m = Math.round((d.getTime() - Date.now()) / 60000);
    return isFinite(m) ? Math.max(0, m) : null;
  };

  function paintTone() {
    $('#chips-tone').querySelectorAll('.fx-chip').forEach((c) =>
      c.classList.toggle('on', c.dataset.tone === pay.tone));
  }

  function paint() {
    const on = !!(rule && rule.is_enabled);
    const sw = $('#sw-re'), tc = $('#tc-re');
    sw.classList.toggle('on', on);
    tc.classList.toggle('on', on);
    tc.querySelector('.fx-tc-state').textContent = on ? 'Running' : 'Off';
    const st = $('#st-run');
    st.classList.toggle('ok', on);
    chipText(st, on ? 'Running' : (rule ? 'Off' : 'Not set up yet'));

    const lr = rule && rule.last_run;
    const sent = lr && lr.stats && (lr.stats.sent ?? lr.stats.picked);
    chipText($('#st-warm'), lr ? ((sent ?? 0) + ' warmed last sweep') : 'no sweeps yet');
    chipText($('#st-tone'), 'tone ' + (pay.tone === 'flirty' ? 'flirty' : 'soft'));
    chipText($('#st-sweep'), lr && lr.started_at
      ? ('Last sweep ' + Fastt.fmtAgo(lr.started_at)) : 'Never swept');
    const nm = on ? minsUntil(rule.next_due_at) : null;
    chipText($('#st-next'), on ? ('Next in ' + (nm === null ? '—' : nm + ' min')) : 'Paused');

    paintTone();
    $('#f-lookback').value = pay.lookback_days;
    $('#f-cold').value = pay.cold_hours;
    $('#f-max').value = pay.max_per_run;
    $('#f-guard').value = pay.guard_hours;
    $('#kv-next').innerHTML = 'next sweep in <b>' +
      (on && nm !== null ? nm + ' min' : 'paused') +
      '</b> · up to <b>' + Number(pay.max_per_run) + '</b> fans per run';
  }

  let saving = false, dirty = false;
  async function savePayload(part) {
    Object.assign(pay, part);
    // An edit made while a PATCH is in flight must not be swallowed: mark it dirty
    // and re-send once the current request lands (same loop as Auto Convo's save()).
    if (saving) { dirty = true; return; }
    saving = true;
    try {
      do {
        dirty = false;
        rule = await Fastt.upsertRule('reengage_buyers', { payload: Object.assign({}, pay) }, CREATE);
      } while (dirty);
      Fastt.saved();
    } catch (e) {
      Fastt.oops(e);
      try { rule = await Fastt.rule('reengage_buyers'); pay = Object.assign({}, DEFAULTS, (rule && rule.payload) || {}); } catch (_) {}
    } finally { saving = false; dirty = false; paint(); schedulePreview(); }
  }
  // Knobs decide WHO is cold, so the opener list is stale the moment one moves.
  // Debounced because the dry run is a real sweep query (cheap, but not free).
  const schedulePreview = Fastt.debounce(() => { runPreview('preview', true); }, 450);

  paint();
  Fastt.liveBadge($('#tc-re .fx-tc-title'));
  Fastt.liveBadge($('#h-samples'));

  // ── dry-run preview: the REAL openers for the REAL cold buyers ──────────
  // POST /admin/reengage-preview runs automations/reengage_buyers with dry_run:true —
  // it composes each opener from that fan's stored gen_info lines (no LLM call, no
  // OF traffic) and returns them WITHOUT sending or writing anything. Shapes:
  //   with candidates → {dry_run,candidates,would_send,tone,preview:[{fan_id,name,opener}]}
  //   none            → {sent:0,skipped:"no_cold_buyers",candidates:0}  (the run's own
  //                     early-exit shape — note there is no `preview` key at all)
  const AV = ['#7d97f8,#4166f6', '#67d1ae,#3d8f76', '#e0679b,#a8306a',
              '#e5a35b,#b06a1f', '#a78bfa,#6d43d6'];
  const initials = (nm) => {
    // gen_info names arrive as "Witcher/Tsawwassen,Canada/34/BC Ferries/Spender" —
    // the display name is the first path segment, same split the engine uses.
    const first = String(nm || '').split('/')[0].trim();
    const parts = first.split(/\s+/).filter(Boolean);
    if (!parts.length) return '?';
    return (parts[0][0] + (parts[1] ? parts[1][0] : '')).toUpperCase();
  };
  const shortName = (nm, fid) => String(nm || '').split('/')[0].trim() || ('fan ' + fid);

  let previewing = false;
  function pvBusy(on, which) {
    previewing = on;
    const bp = $('#btn-preview'), bs = $('#btn-sendnow');
    bp.disabled = on; bs.disabled = on;
    bp.textContent = (on && which === 'preview') ? 'Checking…' : 'Preview (dry run)';
    bs.textContent = (on && which === 'send') ? 'Queuing…' : 'Send now';
  }
  function renderPreview(resp, note) {
    const rows = $('#pv-rows'), sum = $('#pv-summary');
    rows.innerHTML = '';
    const list = Array.isArray(resp && resp.preview) ? resp.preview : [];
    const cand = Number((resp && resp.candidates) || 0);
    const would = Number((resp && (resp.would_send != null ? resp.would_send : resp.sent)) || 0);
    if (!list.length) {
      const why = (resp && resp.skipped) === 'no_cold_buyers'
        ? 'Nobody bought in the last <b>' + Number(pay.lookback_days) + ' day'
          + (Number(pay.lookback_days) === 1 ? '' : 's') + '</b> and then went quiet for <b>'
          + Number(pay.cold_hours) + ' h</b> — so there is nothing to win back right now.'
        : (resp && resp.skipped
            ? 'The sweep stopped early: <b>' + Fastt.esc(String(resp.skipped)) + '</b>.'
            : 'No fan cleared the cold-buyer test with the knobs above.');
      sum.innerHTML = '<b style="color:#ddd">No cold buyers right now</b>' +
        (cand ? ' — ' + cand + ' candidate' + (cand === 1 ? '' : 's') + ' found, all excluded' : '');
      rows.innerHTML = '<div class="pv-empty"><span>' + why +
        ' Widen "bought within" or shorten "cold after" in Advanced mode to cast wider.</span></div>';
      return;
    }
    sum.innerHTML = '<b style="color:#ddd">' + would + '</b> cold buyer' + (would === 1 ? '' : 's')
      + ' would get one opener' +
      (cand > would ? ' <span style="color:var(--muted2)">(' + cand + ' found, capped/excluded to '
        + would + ')</span>' : '') + ' · tone <b style="color:#ddd">'
      + Fastt.esc(String((resp && resp.tone) || pay.tone)) + '</b>'
      + (note ? ' · <span style="color:var(--muted2)">' + Fastt.esc(note) + '</span>' : '');
    rows.innerHTML = list.map((r, i) => {
      const bubbles = String(r.opener || '').split('\n').map((s) => s.trim()).filter(Boolean);
      return '<div class="demo-row' + (i === 0 ? ' first' : '') + '">'
        + '<div class="demo-av" style="background:linear-gradient(135deg,' + AV[i % AV.length] + ')">'
        + Fastt.esc(initials(r.name)) + '</div><div style="min-width:0">'
        + '<div class="demo-meta"><b>' + Fastt.esc(shortName(r.name, r.fan_id)) + '</b>'
        + '<span class="pv-fid">fan #' + Fastt.esc(String(r.fan_id)) + '</span>'
        + '<span class="demo-tag">cold buyer</span></div>'
        + bubbles.map((b) => '<span class="demo-bub">' + Fastt.esc(b) + '</span>').join('')
        + '</div></div>';
    }).join('');
  }
  function pvSettings() {
    return {
      account_id: String(Fastt.account()),
      lookback_days: Number(pay.lookback_days), cold_hours: Number(pay.cold_hours),
      max_per_run: Number(pay.max_per_run), guard_hours: Number(pay.guard_hours),
      tone: pay.tone === 'flirty' ? 'flirty' : 'soft',
    };
  }
  async function runPreview(which, silent) {
    if (previewing) return null;
    pvBusy(true, which || 'preview');
    try {
      const resp = await Fastt.post('/admin/reengage-preview', pvSettings());
      renderPreview(resp);
      return resp;
    } catch (e) {
      $('#pv-summary').innerHTML = '<b style="color:#e8cf9a">Preview unavailable</b>';
      $('#pv-rows').innerHTML = '<div class="pv-empty"><span>The relay refused the dry run ('
        + Fastt.esc(String((e && e.body && (e.body.detail || e.body.error)) || (e && e.message) || 'error'))
        + '). Nothing was sent.</span></div>';
      if (!silent) Fastt.oops(e);
      return null;
    } finally { pvBusy(false); }
  }

  $('#btn-preview').addEventListener('click', () => { runPreview('preview'); });
  // The ONE outbound button on this page. It re-runs the dry run first so the
  // confirm always names the real count, then enqueues the normal automation job
  // (dry_run absent → the sweep really sends). Explicit click + confirm, always.
  $('#btn-sendnow').addEventListener('click', async () => {
    const pv = await runPreview('send');
    if (!pv) return;
    const n = Number(pv.would_send != null ? pv.would_send : pv.sent) || 0;
    if (!n) { Fastt.toast('No cold buyers right now — nothing queued.'); return; }
    if (!confirm('Send a personal re-engage DM to ' + n + ' cold buyer(s) on this creator NOW?\n\n'
      + 'These are REAL messages (' + pvSettings().tone + ' tone) and cannot be recalled once OF '
      + 'delivers them. Cancel to review the preview first.')) return;
    pvBusy(true, 'send');
    try {
      const resp = await Fastt.post('/admin/automation/enqueue',
        { account_id: String(Fastt.account()), kind: 'reengage_buyers', payload: pvSettings() });
      Fastt.saved('Queued job #' + (resp && resp.enqueued_job_id) + ' for ' + n + ' fan(s)');
    } catch (e) { Fastt.oops(e); }
    finally { pvBusy(false); }
  });

  // First paint: the openers are real, so show them instead of a promise. Silent —
  // a preview failure must not fire a toast the operator never asked for.
  runPreview('preview', true);

  // listeners registered AFTER the fx kit — we read post-toggle state and can revert it
  document.addEventListener('click', async (e) => {
    // BUG FIX — the "Advanced mode" switch lives INSIDE #tc-re (.fx-togglecard), and the
    // shared fx kit's switch handler unconditionally does
    //   sw.closest('.fx-togglecard').classList.toggle('on')  +  state text → 'Running'.
    // So opening the drawer painted this automation as RUNNING while the rule row is OFF.
    // The kit is canonical (identical on every page) and must not be forked, so re-assert
    // the card from the real rule state in the same click — synchronous, so no flicker.
    if (e.target.closest('.fx-adv-head [data-adv]')) { paint(); return; }
    const chip = e.target.closest('#chips-tone .fx-chip');
    if (chip) {
      const t = chip.dataset.tone;
      if (!t) {  // "Direct" has no backend value — honest no-op
        paintTone();
        Fastt.toast('Direct tone is not supported by the engine yet — pick Soft or Flirty', 'err');
        return;
      }
      if (t !== pay.tone) savePayload({ tone: t });
      return;
    }
    if (e.target.closest('#sw-re')) {
      const on = $('#sw-re').classList.contains('on');   // state AFTER the kit toggled it
      if (on && !confirm('Turn ON Re-engage Buyers?\n\nOn its next sweep it will send REAL DMs to recent buyers who went quiet on this account.')) {
        paint();   // revert the visual toggle
        return;
      }
      try {
        rule = await Fastt.upsertRule('reengage_buyers',
          { is_enabled: on, payload: Object.assign({}, pay) }, CREATE);
        Fastt.saved(on ? 'Re-engage is ON' : 'Re-engage turned off');
      } catch (err) { Fastt.oops(err); }
      paint();
    }
  });

  $('#f-lookback').addEventListener('change', (e) => knob('lookback_days', e.target.value));
  $('#f-cold').addEventListener('change', (e) => knob('cold_hours', e.target.value));
  $('#f-max').addEventListener('change', (e) => knob('max_per_run', e.target.value));
  $('#f-guard').addEventListener('change', (e) => knob('guard_hours', e.target.value));
});
