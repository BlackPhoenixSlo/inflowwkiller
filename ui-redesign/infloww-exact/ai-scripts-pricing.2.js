Fastt.ready(async () => {
  if (!Fastt.account()) return;
  const $ = Fastt.$;

  // ============ Test the catalog — dry run (POST /admin/scripts/simulate) ======
  // Sends NOTHING to any fan: it builds the same prompt the seller would, makes one
  // capped model call, and returns her would-be bubbles + which rung she'd offer.
  Fastt.liveBadge($('#sim-h'));
  const simIn = $('#sim-input'), simRun = $('#sim-run'), simOut = $('#sim-out');
  async function runSim() {
    const says = (simIn.value || '').trim();
    if (!says) { simIn.focus(); return; }
    simRun.disabled = true; simRun.textContent = '▶ Running…';
    simOut.style.display = ''; simOut.innerHTML = '<div class="sim-none">Thinking…</div>';
    try {
      const r = await Fastt.post('/admin/scripts/simulate',
        { account_id: String(Fastt.account()), fan_says: says });
      const bubbles = r.bubbles || [];
      let html = '<div class="sim-bubbles">' + (bubbles.length
        ? bubbles.map(b => '<div class="sim-bub">' + Fastt.esc(b) + '</div>').join('')
        : '<div class="sim-none">She stayed silent — no reply generated for that line.</div>') + '</div>';
      if (r.offer) {
        const o = r.offer;
        html += '<div class="sim-offer">💌 would offer <b>' + Fastt.esc(o.label || ('rung #' + o.item_id)) + '</b>'
          + (o.is_free_teaser ? ' · free teaser'
             : ' · ' + Fastt.fmtCents(o.price_cents) + ' PPV / ' + Fastt.fmtCents(o.tip_unlock_cents) + ' tip') + '</div>';
      } else if (!r.manifest_present) {
        html += '<div class="sim-none">⚠ No sellable rung reached the prompt — give the ladder above rows that have media and a price.</div>';
      } else {
        const n = r.offerable_count || 0;
        html += '<div class="sim-none">No offer this turn — she is reading the room ('
          + n + ' rung' + (n === 1 ? '' : 's') + ' were on the table).</div>';
      }
      simOut.innerHTML = html;
    } catch (e) {
      // A real backend that errored — most often the lane account's saved model is a
      // stale wire name the provider now rejects. Say so plainly instead of a bare toast.
      const detail = (e && e.body && (e.body.detail || e.body.error)) || (e && e.message) || 'Simulate failed';
      simOut.innerHTML = '<div class="fx-note warn" style="margin:0">'
        + '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" style="flex:0 0 auto;margin-top:1px"><path d="M12 3l9.5 17h-19z" stroke-linejoin="round"/><path d="M12 10v4" stroke-linecap="round"/><circle cx="12" cy="17" r=".9" fill="currentColor" stroke="none"/></svg>'
        + '<span>Dry run couldn’t complete — the model call failed: ' + Fastt.esc(String(detail).slice(0, 180)) + '</span></div>';
      console.error(e);
    } finally { simRun.disabled = false; simRun.textContent = '▶ Run dry run'; }
  }
  simRun.addEventListener('click', runSim);
  simIn.addEventListener('keydown', (e) => {
    if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') { e.preventDefault(); runSim(); }
  });

  // ============ Live monitor (GET /admin/ai-chatter/sessions) ==================
  Fastt.liveBadge($('#mon-h'));
  function offerRow(o, cancellable) {
    const age = o.offered_at ? Fastt.fmtAgo(o.offered_at) : '';
    const tips = o.tips_accum_cents ? ' · ' + Fastt.fmtCents(o.tips_accum_cents) + ' tipped' : '';
    return '<div class="mon-row" data-offer="' + o.id + '">'
      + '<span class="who">' + Fastt.esc(o.fan_name || ('#' + o.fan_id)) + '</span>'
      + '<span class="what">' + Fastt.esc(o.item_label || ('item #' + o.id)) + '</span>'
      + '<span class="amt">' + Fastt.fmtCents(o.price_cents) + tips + '</span>'
      + (age ? '<span class="age">' + age + '</span>' : '')
      + (cancellable
          ? '<button class="mon-cancel" title="Cancel offer — hand this fan back to your team (sends nothing)">✕</button>'
          : '<span class="mon-badge ' + Fastt.esc(o.status) + '">' + Fastt.esc(o.status) + '</span>')
      + '</div>';
  }
  function renderMonitor(d) {
    const offers = d.offers || [], prog = d.progress || [];
    const open = offers.filter(o => o.status === 'open');
    const closed = offers.filter(o => o.status !== 'open').slice(0, 8);
    const chip = $('#mon-open-chip');
    chip.style.display = ''; chip.textContent = open.length + ' open';
    chip.className = 'pchip ' + (open.length ? 'ok' : 'warn');
    if (!open.length) chip.className = 'pchip';
    $('#mon-open-n').textContent = open.length ? '(' + open.length + ')' : '';
    $('#mon-pin-n').textContent = prog.length ? '(' + prog.length + ')' : '';
    $('#mon-offers').innerHTML = open.length
      ? open.map(o => offerRow(o, true)).join('')
      : '<div class="mon-empty">No open offers right now — she is between sales.</div>';
    $('#mon-pins').innerHTML = prog.length
      ? prog.map(p => '<div class="mon-row"><span class="who">' + Fastt.esc(p.fan_name || ('#' + p.fan_id))
          + '</span><span class="what">🎬 ' + Fastt.esc(p.script) + ' · step ' + p.position + '</span>'
          + '<span class="mon-badge ' + (p.status === 'active' ? 'active' : 'expired')
          + '" style="margin-left:auto">' + Fastt.esc(p.status) + '</span></div>').join('')
      : '<div class="mon-empty">No fan is mid-script — singles are a flat pool, so most selling never pins a script.</div>';
    if (closed.length) {
      $('#mon-recent-wrap').style.display = '';
      $('#mon-recent').innerHTML = closed.map(o => offerRow(o, false)).join('');
    } else { $('#mon-recent-wrap').style.display = 'none'; }
    $('#mon-refreshed').textContent = 'updated ' + new Date().toLocaleTimeString() + ' · every 15s';
  }
  async function loadMonitor() {
    try { renderMonitor(await Fastt.get('/admin/ai-chatter/sessions')); }
    catch (e) {
      $('#mon-offers').innerHTML = '<div class="mon-empty">Couldn’t load offers: '
        + Fastt.esc((e && e.message) || 'error') + '</div>';
      $('#mon-pins').innerHTML = '';
    }
  }
  // Cancel = hand-to-human. Explicit click + confirm; the endpoint only flips the
  // offer's DB status to "cancelled" — it never messages the fan.
  $('#mon-offers').addEventListener('click', async (e) => {
    const btn = e.target.closest('.mon-cancel'); if (!btn) return;
    const row = btn.closest('.mon-row'), id = row && row.dataset.offer;
    if (!id) return;
    if (!confirm('Cancel this open offer?\n\nThe fan is handed back to your team and the AI stops '
      + 'chasing this sale. Nothing is sent to the fan.')) return;
    btn.disabled = true;
    try {
      await Fastt.post('/admin/ai-chatter/offers/' + id + '/cancel',
        { account_id: String(Fastt.account()) });
      Fastt.saved('Offer cancelled — handed to your team');
      loadMonitor();
    } catch (err) { Fastt.oops(err); btn.disabled = false; }
  });
  loadMonitor();
  setInterval(loadMonitor, 15000);
});
