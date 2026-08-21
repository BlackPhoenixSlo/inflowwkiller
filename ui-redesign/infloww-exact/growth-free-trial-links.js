/* ── live wiring: /admin/trial-links (list ∪ live OF / create / delete) ──────
 *
 * created_at gap: trial_links_api._from_live() hardcodes created_at=None for
 * every OF-sourced row, and on this account 100% of rows are OF-sourced — so
 * the column was a dash on every line. OnlyFans DOES return the date, on its own
 * /trials route, so we fetch that directly and join on of_trial_id.
 * ⚠️ /api/of/v2/* stamps are tz-AWARE ("…+00:00"); parse them with new Date()
 * as-is. Do NOT push them through the tz-naive Z-stamping path Fastt.parseUtc
 * applies to relay stamps, or they get shifted twice.
 *
 * Dashboard tab = OF profile funnel (/api/of/v2/users/me/stats/overview) plus
 * aggregates computed off the links already loaded.
 * ───────────────────────────────────────────────────────────────────────── */
Fastt.ready(async () => {
  const $ = Fastt.$, esc = Fastt.esc;

  // Filters/controls with no backend stay inert AND badged.
  Fastt.$$('.fbar .inp.sel').forEach((el) => Fastt.staticBadge(el, 'STATIC'));
  Fastt.staticBadge($('#tl-net-note'), 'NO NET/GROSS SPLIT — NO EARNINGS ATTRIBUTION');
  Fastt.staticBadge($('#tl-nodata-note'), 'NO SPEND ATTRIBUTION');

  const rowsEl = $('#tl-rows');
  const acct = Fastt.account();
  if (!acct) {
    rowsEl.innerHTML = '<div class="empty"><div>No creator selected — click the creator name (top-left) to pick one.</div></div>';
    $('#tl-fresh-txt').textContent = 'No creator selected.';
    return;
  }
  Fastt.liveBadge($('#tl-title'));
  const acctEl = $('#tl-acct'), row0 = Fastt.accountRow();
  if (acctEl) acctEl.textContent = (row0 && row0.nickname) || acct;

  const COPY  = '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><rect x="9" y="9" width="11" height="11" rx="2"/><path d="M5 15V6a2 2 0 0 1 2-2h9" stroke-linecap="round"/></svg>';
  const TRASH = '<svg width="19" height="19" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M5 7h14M10 7V5h4v2M6 7l1 13h10l1-13" stroke-linecap="round" stroke-linejoin="round"/></svg>';
  const NA = (why) => '<span class="nodata" title="' + esc(why) + '">not tracked</span>';
  const NO_SPEND = 'No trial→fan join exists anywhere in the relay, so no spend can be attributed to this link';
  const NO_COST  = 'Nothing records what a campaign cost to run — there is no ad-spend input in fastt';
  const NO_EARN  = 'Earnings are attributed to fans, never to the link that brought them in';

  let links = [];
  let ofAvailable = true;
  let showDead = false;
  let ofDates = null;   // Map of_trial_id → {createdAt, expiredAt}

  const statusPill = (st) =>
    st === 'active'   ? '<span class="pill-in">Active</span>' :
    st === 'finished' ? '<span class="pill-ex">Finished</span>' :
                        '<span style="color:#9a9a9a;font-size:13px">unknown</span>';

  /** OF-proxied stamps already carry an offset — plain Date is correct here. */
  const ofDate = (s) => { const d = s ? new Date(s) : null; return d && !isNaN(d.getTime()) ? d : null; };
  const createdOf = (l) => {
    if (l.created_at) return l.created_at;                     // fastt-created mirror rows
    const j = ofDates && ofDates.get(l.of_trial_id);
    return (j && j.createdAt) || null;
  };

  function inRange(created) {
    const from = $('#tl-from').value, to = $('#tl-to').value;
    if (!from && !to) return true;
    const d = ofDate(created);
    if (!d) return false;                                      // undated rows drop out of a date filter
    const iso = d.toISOString().slice(0, 10);
    if (from && iso < from) return false;
    if (to && iso > to) return false;
    return true;
  }

  function visible() {
    const q = ($('#tl-search').value || '').toLowerCase();
    return links.filter((l) => {
      if (!showDead && l.status === 'finished') return false;
      if (!inRange(createdOf(l))) return false;
      if (q && !((l.name || '').toLowerCase().includes(q) || (l.url || '').toLowerCase().includes(q))) return false;
      return true;
    });
  }

  function renderTotals() {
    const live = links.filter((l) => l.status === 'active').length;
    const claims = links.reduce((a, l) => a + (l.claim_counts || 0), 0);
    const clicks = links.reduce((a, l) => a + (l.clicks_count || 0), 0);
    const t = (label, value, sub) =>
      `<div class="t"><span class="tl">${esc(label)}</span><span class="tv">${value}</span>${sub ? '<span class="tl">' + esc(sub) + '</span>' : ''}</div>`;
    $('#tl-totals').innerHTML =
      t('Trial links', Fastt.fmtInt(links.length), live + ' live · ' + (links.length - live) + ' finished')
      + t('Clicks', Fastt.fmtInt(clicks), 'counted by OnlyFans')
      + t('Claims', Fastt.fmtInt(claims), 'trials actually redeemed')
      + t('Click→claim rate',
          clicks > 0 ? ((claims / clicks) * 100).toFixed(0) + '%'
                     : '<span style="font-size:14px;font-weight:500;color:#8a8a8a">no clicks yet</span>',
          clicks > 0 ? 'OnlyFans counters only' : 'OnlyFans has counted no clicks to divide by')
      + t('Showing', Fastt.fmtInt(visible().length), showDead ? 'all links' : 'live links only');
  }

  function render() {
    const vis = visible();
    const warn = ofAvailable ? '' :
      '<div class="trow" style="min-height:44px;color:#e2a94e;font-size:13.5px"><div class="td">OnlyFans not reachable right now — showing mirrored rows only (claim counters may be stale).</div></div>';
    renderTotals();
    if (!vis.length) {
      rowsEl.innerHTML = warn + '<div class="empty"><div>' + (links.length ? 'Nothing matches the current filters' : 'No free trial links yet') + '</div></div>';
      return;
    }
    rowsEl.innerHTML = warn + vis.map((l, i) => {
      const cap = (l.subscribe_counts === 0 || l.subscribe_counts == null) ? '∞' : l.subscribe_counts;
      const canDel = l.id != null;
      const created = createdOf(l), cd = ofDate(created);
      const clicks = l.clicks_count || 0;
      const bits = [(l.subscribe_days || 0) + '-day trial', esc(l.source),
                    Fastt.fmtInt(clicks) + ' click' + (clicks === 1 ? '' : 's')];
      if (l.expired_at) bits.push('expires ' + esc(ofDate(l.expired_at) ? ofDate(l.expired_at).toLocaleDateString() : l.expired_at));
      return `
      <div class="trow" data-i="${i}">
        <div class="td" style="width:56px"><span class="cbx"></span></div>
        <div class="td" style="flex:1;min-width:0">
          <div style="display:flex;align-items:center;gap:10px">${esc(l.name || '(unnamed)')} ${statusPill(l.status)}</div>
          <div style="color:#8a8a8a;font-size:12.5px;margin-top:5px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(l.url || '')}</div>
          <div style="color:#6f6f6f;font-size:12px;margin-top:3px">${bits.join(' · ')}</div>
        </div>
        <div class="td" style="width:160px">${cd
          ? esc(cd.toLocaleDateString() + ' ' + cd.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }))
          : '<span class="nodata" title="OnlyFans did not return a creation date for this trial">no date from OF</span>'}</div>
        <div class="td" style="width:120px">${Fastt.fmtInt(l.claim_counts || 0)} / ${cap}</div>
        <div class="td" style="width:180px">${NA(NO_SPEND)}</div>
        <div class="td" style="width:160px">${NA(NO_COST)}</div>
        <div class="td" style="width:110px">${NA(NO_EARN)}</div>
        <div class="td" style="width:130px;display:flex;justify-content:flex-end;gap:6px">
          <span class="iconbtn2" data-act="copy" title="Copy link URL">${COPY}</span>
          ${canDel ? `<span class="iconbtn2" data-act="del" title="Delete on OnlyFans">${TRASH}</span>`
                   : `<span class="iconbtn2" style="opacity:.3;cursor:default" title="Created outside fastt — delete it on OnlyFans directly">${TRASH}</span>`}
        </div>
      </div>`;
    }).join('');
  }

  /** OF's own /trials payload carries createdAt (the relay's mirror drops it). */
  async function loadOfDates() {
    try {
      const raw = await Fastt.get('/api/of/v2/trials');
      const arr = Array.isArray(raw) ? raw : (raw && raw.list) || [];
      ofDates = new Map(arr.filter((t) => t && t.id != null)
        .map((t) => [t.id, { createdAt: t.createdAt || null, expiredAt: t.expiredAt || null }]));
    } catch (e) { ofDates = new Map(); }   // dateless rows say so; nothing is invented
  }

  async function refresh() {
    const [out] = await Promise.all([Fastt.get('/admin/trial-links'), loadOfDates()]);
    links = out.links || [];
    ofAvailable = out.of_available !== false;
    // Newest first once the real dates are joined in (the relay returns OF order).
    links.sort((a, b) => {
      const da = ofDate(createdOf(a)), db = ofDate(createdOf(b));
      if (da && db) return db - da;
      return (db ? 1 : 0) - (da ? 1 : 0);
    });
    render();
    if (dashLoaded) renderLinkStats();
    const dated = links.filter((l) => createdOf(l)).length;
    $('#tl-fresh-txt').textContent = 'Read live on every load — OnlyFans is queried on each open, not cached for 2 hours. '
      + dated + '/' + links.length + ' links carry a creation date from OnlyFans. '
      + 'Last refreshed ' + new Date().toLocaleTimeString() + '.';
  }

  // ── Dashboard tab ───────────────────────────────────────────────────────
  let dashLoaded = false;
  const pct = (n) => (n == null ? '' : (n > 0 ? '▲ ' : n < 0 ? '▼ ' : '') + Math.abs(Number(n)).toFixed(1) + '% vs previous period');
  const cls = (n) => (n == null || n === 0 ? '' : n > 0 ? ' up' : ' dn');

  function bars(series, label, valueOf) {
    const max = Math.max(1, ...series.map(valueOf));
    return '<div class="barsrow">' + series.map((d) => {
      const v = valueOf(d);
      const h = Math.max(3, Math.round((v / max) * 100));
      return `<div class="barcol" title="${esc(label(d))}: ${Fastt.fmtInt(v)}">
        <div class="barfill${v ? '' : ' dim'}" style="height:${h}%"></div>
        <div class="barlab">${esc(label(d))}</div></div>`;
    }).join('') + '</div>';
  }

  async function loadDash() {
    if (dashLoaded) return;
    dashLoaded = true;
    let ov = null;
    try { ov = await Fastt.get('/api/of/v2/users/me/stats/overview'); }
    catch (e) {
      $('#tl-dash-sub').textContent = 'OnlyFans stats unavailable';
      $('#tl-dash-stats').innerHTML = '';
      $('#tl-visitors-plot').innerHTML = '<div class="noplot">OnlyFans did not return profile stats for this account — nothing to plot.</div>';
      $('#tl-visitors-sub').textContent = 'Source: OnlyFans profile statistics (unreachable).';
      renderLinkStats();
      return;
    }
    const v = (ov && ov.visitors) || {};
    const vis = v.visitors || {}, earn = v.earnings || {}, subs = v.subscriptions || {};
    const series = v.chartData || [];
    $('#tl-dash-sub').textContent = 'Live from OnlyFans profile statistics'
      + (series.length ? ' · last ' + series.length + ' days' : '');
    Fastt.liveBadge($('#tl-dash-head .sectitle'));

    const stat = (l, val, delta, note) => `<div class="stat"><div class="sl">${esc(l)}</div>`
      + `<div class="sv">${val}</div>`
      + `<div class="sd${cls(delta)}">${esc(delta != null ? pct(delta) : (note || ''))}</div></div>`;
    $('#tl-dash-stats').innerHTML =
      stat('Profile visitors', Fastt.fmtInt(vis.total || 0), vis.delta)
      + stat('Net earnings', Fastt.fmtMoney(earn.total || 0), earn.delta, 'gross ' + Fastt.fmtMoney(earn.gross || 0))
      + stat('New subscriptions', Fastt.fmtInt((subs.new || {}).total || 0), (subs.new || {}).delta)
      + stat('Renewals', Fastt.fmtInt((subs.renew || {}).total || 0), (subs.renew || {}).delta);

    if (series.length) {
      $('#tl-visitors-plot').innerHTML = bars(series, (d) => String(d.date).slice(5, 10), (d) => d.count || 0);
      const peak = series.reduce((a, d) => Math.max(a, d.count || 0), 0);
      $('#tl-visitors-sub').textContent = 'Daily profile visits straight from OnlyFans · peak ' + peak
        + ' · total ' + series.reduce((a, d) => a + (d.count || 0), 0) + ' over the plotted window.';
    } else {
      $('#tl-visitors-plot').innerHTML = '<div class="noplot">OnlyFans returned no daily series for this profile.</div>';
      $('#tl-visitors-sub').textContent = 'Source: OnlyFans profile statistics.';
    }
    renderLinkStats();
  }

  function renderLinkStats() {
    if (!links.length) {
      $('#tl-linkstats-body').innerHTML = '<div class="noplot">No trial links on this account yet.</div>';
      return;
    }
    const live = links.filter((l) => l.status === 'active').length;
    const claims = links.reduce((a, l) => a + (l.claim_counts || 0), 0);
    const clicks = links.reduce((a, l) => a + (l.clicks_count || 0), 0);
    const used = links.filter((l) => (l.claim_counts || 0) > 0 || (l.clicks_count || 0) > 0)
      .sort((a, b) => (b.claim_counts || 0) - (a.claim_counts || 0));
    const max = Math.max(1, ...used.map((l) => Math.max(l.claim_counts || 0, l.clicks_count || 0)));
    $('#tl-linkstats-body').innerHTML =
      '<div class="totstrip" style="margin:0 0 16px">'
      + '<div class="t"><span class="tl">Links</span><span class="tv">' + links.length + '</span></div>'
      + '<div class="t"><span class="tl">Active</span><span class="tv">' + live + '</span></div>'
      + '<div class="t"><span class="tl">Claims</span><span class="tv">' + Fastt.fmtInt(claims) + '</span></div>'
      + '<div class="t"><span class="tl">Clicks</span><span class="tv">' + Fastt.fmtInt(clicks) + '</span></div>'
      + '<div class="t"><span class="tl">Never used</span><span class="tv">' + (links.length - used.length) + '</span></div>'
      + '</div>'
      + (used.length
        ? used.map((l) => `
          <div style="display:flex;align-items:center;gap:14px;padding:9px 0;border-bottom:1px solid #1a1a1a">
            <div style="width:240px;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:14px">${esc(l.name || '(unnamed)')}</div>
            <div style="flex:1;min-width:0;height:12px;background:#1a1a1a;border-radius:6px;overflow:hidden">
              <div class="barfill" style="height:12px;width:${Math.max(2, Math.round(((l.claim_counts || 0) / max) * 100))}%;border-radius:6px"></div></div>
            <div style="width:110px;text-align:right;font-size:13.5px">${Fastt.fmtInt(l.claim_counts || 0)} claim${(l.claim_counts || 0) === 1 ? '' : 's'}</div>
            <div style="width:100px;text-align:right;font-size:13.5px;color:#9a9a9a">${Fastt.fmtInt(l.clicks_count || 0)} click${(l.clicks_count || 0) === 1 ? '' : 's'}</div>
            <div style="width:96px;text-align:right;font-size:12px;color:#6f6f6f">${esc(l.status)}</div>
          </div>`).join('')
        : '<div class="noplot">OnlyFans has recorded no clicks and no claims on any of these links.</div>');
  }

  // ── create ──────────────────────────────────────────────────────────────
  function openCreate() {
    const acctName = (Fastt.accountRow() && Fastt.accountRow().nickname) || acct;
    const back = document.createElement('div');
    back.className = 'ft-modal-back';
    back.innerHTML = `
      <div class="ft-modal tlm-wide">
        <h3>Create free trial link</h3>
        <div class="tlm-sub">A real link is minted on <b style="color:#e8e8e8">${esc(acctName)}</b>'s OnlyFans. Anyone who opens it gets free access for the trial window.</div>
        <div class="tlm-field">
          <label for="tlm-name">Link name <span style="color:#8a8a8a;font-weight:400">(internal label — fans never see it)</span></label>
          <input type="text" id="tlm-name" placeholder="e.g. Twitter 7-day" autocomplete="off">
        </div>
        <div class="tlm-row">
          <div class="tlm-field">
            <label for="tlm-days">Free days</label>
            <input type="number" id="tlm-days" value="7" min="1" max="365" inputmode="numeric">
          </div>
          <div class="tlm-field">
            <label for="tlm-counts">Max claims</label>
            <input type="number" id="tlm-counts" value="1" min="0" inputmode="numeric">
            <div class="tlm-hint">0 = unlimited</div>
          </div>
        </div>
        <div class="tlm-summary">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><circle cx="12" cy="12" r="9"/><path d="M12 11v5" stroke-linecap="round"/><circle cx="12" cy="8" r=".9" fill="currentColor" stroke="none"/></svg>
          <span id="tlm-summary-txt"></span>
        </div>
        <div class="ft-err" id="tlm-err"></div>
        <div class="tlm-actions">
          <button type="button" class="tlm-cancel" id="tlm-cancel">Cancel</button>
          <button type="button" id="tlm-go" disabled>Create on OnlyFans</button>
        </div>
      </div>`;
    document.body.appendChild(back);

    const nameEl = $('#tlm-name', back), daysEl = $('#tlm-days', back),
          countsEl = $('#tlm-counts', back), goEl = $('#tlm-go', back),
          errEl = $('#tlm-err', back), sumEl = $('#tlm-summary-txt', back);

    const close = () => { document.removeEventListener('keydown', onKey); back.remove(); };
    function syncSummary() {
      const nm = nameEl.value.trim();
      const d = parseInt(daysEl.value, 10);
      const c = parseInt(countsEl.value, 10);
      const days = isNaN(d) || d < 1 ? 7 : Math.min(d, 365);
      const cap = isNaN(c) || c <= 0 ? null : Math.min(c, 100000);
      sumEl.innerHTML = 'Gives fans a <b style="color:#fff">' + days + '-day</b> free trial, '
        + (cap ? 'redeemable by up to <b style="color:#fff">' + cap + '</b> ' + (cap === 1 ? 'person' : 'people') + '.'
               : '<b style="color:#fff">unlimited</b> claims.');
      goEl.disabled = !nm;
    }
    ['input', 'change'].forEach((ev) => {
      nameEl.addEventListener(ev, syncSummary);
      daysEl.addEventListener(ev, syncSummary);
      countsEl.addEventListener(ev, syncSummary);
    });
    syncSummary();

    async function submit() {
      const name = nameEl.value.trim();
      if (!name) { nameEl.focus(); return; }
      const days = parseInt(daysEl.value, 10);
      const counts = parseInt(countsEl.value, 10);
      // Outbound action on OnlyFans — explicit confirm gate.
      if (!confirm('Create this free-trial link on ' + acctName + '’s OnlyFans now?')) return;
      errEl.style.display = 'none';
      goEl.disabled = true; goEl.textContent = 'Creating…';
      try {
        await Fastt.post('/admin/trial-links', {
          account_id: acct, name,
          subscribe_days: isNaN(days) ? 7 : days,
          subscribe_counts: isNaN(counts) ? 1 : counts,
        });
        close();
        Fastt.saved('Trial link created');
        await refresh();
      } catch (e) {
        goEl.disabled = false; goEl.textContent = 'Create on OnlyFans';
        errEl.style.display = 'block';
        errEl.textContent = (e.body && e.body.detail) || (e && e.message) || 'Create failed';
      }
    }

    function onKey(e) {
      if (e.key === 'Escape') { e.preventDefault(); close(); }
      else if (e.key === 'Enter' && !e.isComposing) { e.preventDefault(); if (!goEl.disabled) submit(); }
    }
    document.addEventListener('keydown', onKey);
    back.addEventListener('click', (e) => { if (e.target === back) close(); });
    $('#tlm-cancel', back).addEventListener('click', close);
    goEl.addEventListener('click', submit);
    setTimeout(() => nameEl.focus(), 0);
  }

  // ── events ──────────────────────────────────────────────────────────────
  $('#tl-create').addEventListener('click', openCreate);
  $('#tl-search').addEventListener('input', Fastt.debounce(render, 150));
  $('#tl-from').addEventListener('change', render);
  $('#tl-to').addEventListener('change', render);
  $('#tl-datclear').addEventListener('click', () => {
    $('#tl-from').value = ''; $('#tl-to').value = ''; render();
  });
  $('#tl-showdead').addEventListener('click', function () {
    showDead = !showDead;
    this.classList.toggle('on', showDead);
    render();
  });
  $('#tl-ptabs').addEventListener('click', (e) => {
    const t = e.target.closest('.ptab');
    if (!t) return;
    const dash = t.dataset.view === 'dash';
    $('#tl-view-list').style.display = dash ? 'none' : 'block';
    $('#tl-view-dash').style.display = dash ? 'block' : 'none';
    if (dash) loadDash();
  });
  $('#tl-export').addEventListener('click', () => {
    const head = ['name', 'url', 'trial_days', 'claims', 'max_claims', 'clicks', 'status', 'created_at'];
    const csv = [head.join(',')].concat(visible().map((l) => [
      l.name, l.url, l.subscribe_days, l.claim_counts, l.subscribe_counts, l.clicks_count, l.status, createdOf(l),
    ].map((v) => '"' + String(v == null ? '' : v).replace(/"/g, '""') + '"').join(','))).join('\n');
    const a = document.createElement('a');
    a.href = URL.createObjectURL(new Blob([csv], { type: 'text/csv' }));
    a.download = 'trial-links.csv';
    a.click();
    URL.revokeObjectURL(a.href);
  });
  rowsEl.addEventListener('click', async (e) => {
    const btn = e.target.closest('[data-act]');
    if (!btn) return;
    const l = visible()[Number(btn.closest('.trow').dataset.i)];
    if (!l) return;
    if (btn.dataset.act === 'copy') {
      try { await navigator.clipboard.writeText(l.url || ''); Fastt.toast('Link copied', 'ok'); }
      catch (err) { Fastt.toast(l.url || 'no url', ''); }
      return;
    }
    if (btn.dataset.act === 'del') {
      if (!confirm('Delete trial link "' + (l.name || l.url) + '" on OnlyFans? Fans can no longer claim it.')) return;
      try { await Fastt.del('/admin/trial-links/' + l.id); Fastt.saved('Deleted'); await refresh(); }
      catch (err) { Fastt.oops(err); }
    }
  });

  await refresh();
});
