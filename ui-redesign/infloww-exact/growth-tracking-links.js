/* ── live wiring ──────────────────────────────────────────────────────────
 * Two DIFFERENT kinds of "tracking link" live on this page and the difference
 * is the whole point:
 *
 *   1. OnlyFans-native  GET/POST /admin/of-tracking-links (of_tracking_api.py)
 *      OF mints https://onlyfans.com/<user>/c<code> and reports countTransitions
 *      (clicks) AND countSubscribers — real click→subscriber attribution.
 *      Shown FIRST because it is the one with a funnel.
 *   2. Internal /t/<slug> GET/POST /admin/tracking-links (tracking_links_api.py)
 *      Our own redirector. It only ever sees the HTTP hit; the relay returns
 *      subscribers:null / revenue_cents:null BY DESIGN, so those columns stay
 *      blank there and say so instead of showing a number nobody can back.
 *
 * Dashboard tab = OF profile funnel (GET /api/of/v2/users/me/stats/overview)
 * plus aggregates computed from the links already loaded.
 * ⚠️ /api/of/v2/* stamps are already tz-AWARE — do NOT run them through the
 * tz-naive Z-stamping in Fastt.parseUtc's fixup path (new Date() is correct).
 * ───────────────────────────────────────────────────────────────────────── */
Fastt.ready(async () => {
  const $ = Fastt.$, esc = Fastt.esc;

  // Filters that genuinely have no backend stay inert and badged.
  Fastt.$$('.fbar .inp.sel').forEach((el) => Fastt.staticBadge(el, 'STATIC'));
  Fastt.staticBadge($('#tk-net-note'), 'NO NET/GROSS SPLIT — NO EARNINGS ATTRIBUTION');
  Fastt.staticBadge($('#tk-showdead-row'), 'RELAY DROPS DELETED LINKS SERVER-SIDE');
  Fastt.staticBadge($('#tk-int-note'), 'NO SUBSCRIBER ATTRIBUTION ON /t/ LINKS');

  const rowsEl = $('#tk-rows'), ofRowsEl = $('#tk-ofrows');
  const acct = Fastt.account();
  if (!acct) {
    const msg = '<div class="empty"><div>No creator selected — click the creator name (top-left) to pick one.</div></div>';
    rowsEl.innerHTML = msg; ofRowsEl.innerHTML = msg;
    $('#tk-fresh-txt').textContent = 'No creator selected.';
    return;
  }
  Fastt.liveBadge($('#tk-title'));
  Fastt.liveBadge($('#tk-of-head .sectitle'));
  const acctEl = $('#tk-acct'), row0 = Fastt.accountRow();
  if (acctEl) acctEl.textContent = (row0 && row0.nickname) || acct;

  const COPY  = '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><rect x="9" y="9" width="11" height="11" rx="2"/><path d="M5 15V6a2 2 0 0 1 2-2h9" stroke-linecap="round"/></svg>';
  const CHART = '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M6 20V10M12 20V4M18 20v-7" stroke-linecap="round"/></svg>';
  const TRASH = '<svg width="19" height="19" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M5 7h14M10 7V5h4v2M6 7l1 13h10l1-13" stroke-linecap="round" stroke-linejoin="round"/></svg>';
  const OPEN  = '<svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M9 6h9v9M18 6L7 17" stroke-linecap="round" stroke-linejoin="round"/></svg>';
  const NA = (why) => '<span class="nodata" title="' + esc(why) + '">not tracked</span>';

  let links = [];        // internal /t/ redirects
  let ofLinks = [];      // OnlyFans-native campaigns
  let ofMeta = { of_available: true, url_verified: false, username: null };

  // ── shared filters (search + created-at range) ──────────────────────────
  const dayOf = (s) => {
    const d = s ? new Date(s) : null;          // OF stamps carry an offset already
    return d && !isNaN(d.getTime()) ? d : null;
  };
  function inRange(created) {
    const from = $('#tk-from').value, to = $('#tk-to').value;
    if (!from && !to) return true;
    const d = dayOf(created);
    if (!d) return false;                      // undated rows drop out of a date filter
    const iso = d.toISOString().slice(0, 10);
    if (from && iso < from) return false;
    if (to && iso > to) return false;
    return true;
  }
  const q = () => ($('#tk-search').value || '').toLowerCase();

  function visible() {
    const s = q();
    return links.filter((l) => inRange(l.created_at) && (!s
      || (l.name || '').toLowerCase().includes(s)
      || (l.slug || '').toLowerCase().includes(s)
      || (l.target_url || '').toLowerCase().includes(s)));
  }
  function visibleOf() {
    const s = q();
    return ofLinks.filter((l) => inRange(l.created_at) && (!s
      || (l.name || '').toLowerCase().includes(s)
      || (l.url || '').toLowerCase().includes(s)));
  }
  const cvr = (subs, clicks) => (clicks > 0
    ? '<b style="color:#e8e8e8">' + ((subs / clicks) * 100).toFixed(1) + '%</b>'
      + ' <span style="color:#6f6f6f;font-size:12.5px">(' + Fastt.fmtInt(subs) + '/' + Fastt.fmtInt(clicks) + ')</span>'
    : '<span class="nodata" title="OnlyFans has recorded no clicks on this link yet, so there is no rate to compute">no clicks yet</span>');

  // ── OnlyFans-native table ───────────────────────────────────────────────
  function renderOf() {
    const sub = $('#tk-of-sub');
    if (!ofMeta.of_available) {
      ofRowsEl.innerHTML = '<div class="trow" style="min-height:52px;color:#e2a94e;font-size:13.5px"><div class="td">OnlyFans is not reachable right now — its tracking links could not be read. Nothing is shown rather than something stale.</div></div>';
      sub.textContent = 'OnlyFans unreachable';
      $('#tk-of-warn').style.display = 'none';
      return;
    }
    const vis = visibleOf();
    const tot = ofLinks.reduce((a, l) => ({ c: a.c + (l.clicks || 0), s: a.s + (l.subscribers || 0) }), { c: 0, s: 0 });
    sub.textContent = ofLinks.length
      ? ofLinks.length + ' link' + (ofLinks.length === 1 ? '' : 's') + ' · '
        + Fastt.fmtInt(tot.c) + ' clicks · ' + Fastt.fmtInt(tot.s) + ' subscribers'
        + (ofMeta.username ? ' · @' + ofMeta.username : '')
      : 'None yet';
    $('#tk-of-warn').style.display = (!ofMeta.url_verified && ofLinks.length) ? 'flex' : 'none';

    if (!vis.length) {
      ofRowsEl.innerHTML = '<div class="empty" style="flex:none;padding:40px 0"><div>'
        + (ofLinks.length ? 'No OnlyFans link matches the current filters'
                          : 'No OnlyFans tracking links yet — “Create link” mints one on the profile')
        + '</div></div>';
      return;
    }
    ofRowsEl.innerHTML = vis.map((l, i) => `
      <div class="trow" data-oi="${i}">
        <div class="td" style="width:56px"></div>
        <div class="td" style="flex:1;min-width:0">
          <div style="display:flex;align-items:center;gap:10px">${esc(l.name || '(unnamed)')}<span class="pill-in">Active</span></div>
          <div style="color:#5b8cff;font-size:12.5px;margin-top:5px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(l.url || '(url unavailable — OnlyFans did not return a username)')}</div>
          <div style="color:#6f6f6f;font-size:12px;margin-top:3px">code c${esc(l.code)}${l.end_date ? ' · ends ' + esc(new Date(l.end_date).toLocaleDateString()) : ''}</div>
        </div>
        <div class="td" style="width:160px">${l.created_at ? esc(new Date(l.created_at).toLocaleDateString() + ' ' + new Date(l.created_at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })) : '<span class="nodata">no date from OF</span>'}</div>
        <div class="td" style="width:110px">${Fastt.fmtInt(l.clicks || 0)}</div>
        <div class="td" style="width:110px">${Fastt.fmtInt(l.subscribers || 0)}</div>
        <div class="td" style="width:200px">${cvr(l.subscribers || 0, l.clicks || 0)}</div>
        <div class="td" style="width:130px;display:flex;justify-content:flex-end;gap:6px">
          ${l.url ? `<span class="iconbtn2" data-oact="copy" title="Copy the OnlyFans link">${COPY}</span>
                     <a class="iconbtn2" href="${esc(l.url)}" target="_blank" rel="noopener" title="Open — confirms the URL format">${OPEN}</a>` : ''}
          <span class="iconbtn2" data-oact="del" title="Delete this campaign on OnlyFans">${TRASH}</span>
        </div>
      </div>`).join('');
  }

  // ── internal /t/ table ──────────────────────────────────────────────────
  function render() {
    const vis = visible();
    $('#tk-int-sub').textContent = links.length
      ? links.length + ' redirect' + (links.length === 1 ? '' : 's') + ' · '
        + Fastt.fmtInt(links.reduce((a, l) => a + (l.click_count || 0), 0)) + ' clicks'
      : 'None yet';
    if (!vis.length) {
      rowsEl.innerHTML = '<div class="empty" style="flex:none;padding:40px 0"><div>'
        + (links.length ? 'No redirect matches the current filters'
                        : 'No internal redirect links yet — create one to shorten an off-profile campaign URL')
        + '</div></div>';
      return;
    }
    rowsEl.innerHTML = vis.map((l) => `
      <div class="trow" data-id="${l.id}">
        <div class="td" style="width:56px"><span class="cbx"></span></div>
        <div class="td" style="flex:1;min-width:0">
          <div>${esc(l.name)}</div>
          <div style="color:#8a8a8a;font-size:12.5px;margin-top:5px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(location.origin + l.path)} → ${esc(l.target_url)}</div>
        </div>
        <div class="td" style="width:160px">${l.created_at ? esc(Fastt.fmtDate(l.created_at)) : '<span class="nodata">no date</span>'}</div>
        <div class="td" style="width:110px">${Fastt.fmtInt(l.click_count || 0)}</div>
        <div class="td" style="width:110px">${NA('A /t/ redirect sees the click only — the relay returns subscribers:null for these by design')}</div>
        <div class="td" style="width:200px">${NA('No click→subscriber join exists for /t/ redirects, so no rate can be computed')}</div>
        <div class="td" style="width:160px">${NA('Nothing in the relay records ad spend against a link')}</div>
        <div class="td" style="width:130px;display:flex;justify-content:flex-end;gap:6px">
          <span class="iconbtn2" data-act="copy" title="Copy public link">${COPY}</span>
          <span class="iconbtn2" data-act="stats" title="Click analytics">${CHART}</span>
          <span class="iconbtn2" data-act="del" title="Delete link">${TRASH}</span>
        </div>
      </div>
      <div class="tk-detail" data-for="${l.id}" style="display:none;border-bottom:1px solid #1a1a1a;padding:14px 20px 18px 76px;color:#9a9a9a;font-size:13.5px"></div>`).join('');
  }

  // ── totals strip (real numbers, both families) ───────────────────────────
  function renderTotals() {
    const oc = ofLinks.reduce((a, l) => a + (l.clicks || 0), 0);
    const os = ofLinks.reduce((a, l) => a + (l.subscribers || 0), 0);
    const ic = links.reduce((a, l) => a + (l.click_count || 0), 0);
    const t = (label, value, sub) =>
      `<div class="t"><span class="tl">${esc(label)}</span><span class="tv">${value}</span>${sub ? '<span class="tl">' + esc(sub) + '</span>' : ''}</div>`;
    $('#tk-totals').innerHTML =
      t('Links', Fastt.fmtInt(ofLinks.length + links.length), ofLinks.length + ' OnlyFans · ' + links.length + ' internal')
      + t('Total clicks', Fastt.fmtInt(oc + ic), oc + ' OnlyFans · ' + ic + ' internal')
      + t('Subscribers attributed', Fastt.fmtInt(os), 'OnlyFans links only')
      + t('Subscription CVR',
          oc > 0 ? ((os / oc) * 100).toFixed(1) + '%'
                 : '<span style="font-size:14px;font-weight:500;color:#8a8a8a">no clicks yet</span>',
          oc > 0 ? 'across OnlyFans links' : 'OnlyFans has counted no clicks to divide by');
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
      $('#tk-dash-sub').textContent = 'OnlyFans stats unavailable';
      $('#tk-dash-stats').innerHTML = '';
      $('#tk-visitors-plot').innerHTML = '<div class="noplot">OnlyFans did not return profile stats for this account — nothing to plot.</div>';
      $('#tk-visitors-sub').textContent = 'Source: OnlyFans profile statistics (unreachable).';
      renderLinkStats();
      return;
    }
    const v = (ov && ov.visitors) || {};
    const vis = v.visitors || {}, earn = v.earnings || {}, subs = v.subscriptions || {};
    const series = v.chartData || [];
    $('#tk-dash-sub').textContent = 'Live from OnlyFans profile statistics'
      + (series.length ? ' · last ' + series.length + ' days' : '');
    Fastt.liveBadge($('#tk-dash-head .sectitle'));

    const stat = (l, val, delta, note) => `<div class="stat"><div class="sl">${esc(l)}</div>`
      + `<div class="sv">${val}</div>`
      + `<div class="sd${cls(delta)}">${esc(delta != null ? pct(delta) : (note || ''))}</div></div>`;
    $('#tk-dash-stats').innerHTML =
      stat('Profile visitors', Fastt.fmtInt(vis.total || 0), vis.delta)
      + stat('Net earnings', Fastt.fmtMoney(earn.total || 0), earn.delta, 'gross ' + Fastt.fmtMoney(earn.gross || 0))
      + stat('New subscriptions', Fastt.fmtInt((subs.new || {}).total || 0), (subs.new || {}).delta)
      + stat('Renewals', Fastt.fmtInt((subs.renew || {}).total || 0), (subs.renew || {}).delta);

    if (series.length) {
      $('#tk-visitors-plot').innerHTML = bars(series, (d) => String(d.date).slice(5, 10), (d) => d.count || 0);
      const peak = series.reduce((a, d) => Math.max(a, d.count || 0), 0);
      $('#tk-visitors-sub').textContent = 'Daily profile visits straight from OnlyFans · peak ' + peak
        + ' · total ' + series.reduce((a, d) => a + (d.count || 0), 0) + ' over the plotted window.';
    } else {
      $('#tk-visitors-plot').innerHTML = '<div class="noplot">OnlyFans returned no daily series for this profile.</div>';
      $('#tk-visitors-sub').textContent = 'Source: OnlyFans profile statistics.';
    }
    renderLinkStats();
  }

  function renderLinkStats() {
    const oc = ofLinks.reduce((a, l) => a + (l.clicks || 0), 0);
    const os = ofLinks.reduce((a, l) => a + (l.subscribers || 0), 0);
    const rows = ofLinks.map((l) => ({ n: l.name || 'c' + l.code, c: l.clicks || 0, s: l.subscribers || 0, k: 'OnlyFans' }))
      .concat(links.map((l) => ({ n: l.name, c: l.click_count || 0, s: null, k: 'internal /t/' })));
    if (!rows.length) {
      $('#tk-linkstats-body').innerHTML = '<div class="noplot">No links on this account yet — create one above and its clicks land here.</div>';
      return;
    }
    const max = Math.max(1, ...rows.map((r) => r.c));
    $('#tk-linkstats-body').innerHTML =
      '<div class="totstrip" style="margin:0 0 16px">'
      + '<div class="t"><span class="tl">OnlyFans links</span><span class="tv">' + ofLinks.length + '</span></div>'
      + '<div class="t"><span class="tl">OnlyFans clicks</span><span class="tv">' + Fastt.fmtInt(oc) + '</span></div>'
      + '<div class="t"><span class="tl">Subscribers attributed</span><span class="tv">' + Fastt.fmtInt(os) + '</span></div>'
      + '<div class="t"><span class="tl">Internal redirects</span><span class="tv">' + links.length + '</span></div>'
      + '<div class="t"><span class="tl">Internal clicks</span><span class="tv">' + Fastt.fmtInt(links.reduce((a, l) => a + (l.click_count || 0), 0)) + '</span></div>'
      + '</div>'
      + rows.map((r) => `
        <div style="display:flex;align-items:center;gap:14px;padding:9px 0;border-bottom:1px solid #1a1a1a">
          <div style="width:220px;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:14px">${esc(r.n)}</div>
          <div style="flex:1;min-width:0;height:12px;background:#1a1a1a;border-radius:6px;overflow:hidden">
            <div class="barfill" style="height:12px;width:${Math.max(2, Math.round((r.c / max) * 100))}%;border-radius:6px"></div></div>
          <div style="width:92px;text-align:right;font-size:13.5px">${Fastt.fmtInt(r.c)} clicks</div>
          <div style="width:140px;text-align:right;font-size:13.5px;color:${r.s == null ? '#5a5a5a' : '#e8e8e8'}">${r.s == null ? 'subs not tracked' : Fastt.fmtInt(r.s) + ' subs'}</div>
          <div style="width:96px;text-align:right;font-size:12px;color:#6f6f6f">${esc(r.k)}</div>
        </div>`).join('');
  }

  // ── loads ───────────────────────────────────────────────────────────────
  async function refreshInternal() {
    links = (await Fastt.get('/admin/tracking-links')).links || [];
  }
  async function refreshOf() {
    try {
      const out = await Fastt.get('/admin/of-tracking-links');
      ofLinks = out.links || [];
      ofMeta = { of_available: out.of_available !== false, url_verified: !!out.url_verified, username: out.username || null };
    } catch (e) {
      ofLinks = []; ofMeta = { of_available: false, url_verified: false, username: null };
    }
  }
  async function refresh() {
    await Promise.all([refreshInternal(), refreshOf()]);
    render(); renderOf(); renderTotals();
    if (dashLoaded) renderLinkStats();
    $('#tk-fresh-txt').textContent = 'Read live on every load — OnlyFans links + stats come straight from OnlyFans, '
      + '/t/ redirects from the relay. Last refreshed ' + new Date().toLocaleTimeString() + '.';
  }

  // ── create modals ───────────────────────────────────────────────────────
  function openCreateOf() {
    const back = document.createElement('div');
    back.className = 'ft-modal-back';
    back.innerHTML = `
      <div class="ft-modal" style="width:380px">
        <h3>Create OnlyFans tracking link</h3>
        <input type="text" id="tkof-name" placeholder="Channel name (e.g. TikTok bio)">
        <div style="font-size:11px;color:#8a8a8a;margin-bottom:10px">Creates a REAL campaign on this OnlyFans profile. OnlyFans mints the short URL and counts clicks <b>and</b> subscribers against it.</div>
        <div class="ft-err" id="tkof-err"></div>
        <button id="tkof-go">Create on OnlyFans</button>
      </div>`;
    document.body.appendChild(back);
    back.addEventListener('click', (e) => { if (e.target === back) back.remove(); });
    $('#tkof-go', back).addEventListener('click', async () => {
      const errEl = $('#tkof-err', back);
      const name = $('#tkof-name', back).value.trim();
      if (!name) { errEl.style.display = 'block'; errEl.textContent = 'Name is required.'; return; }
      if (!confirm('Create the tracking link “' + name + '” on OnlyFans now?')) return;
      try {
        await Fastt.post('/admin/of-tracking-links', { account_id: acct, name });
        back.remove(); Fastt.saved('Tracking link created on OnlyFans');
        await refresh();
      } catch (e) {
        errEl.style.display = 'block';
        errEl.textContent = (e.body && e.body.detail) || 'Create failed';
      }
    });
  }

  function openCreate() {
    const back = document.createElement('div');
    back.className = 'ft-modal-back';
    back.innerHTML = `
      <div class="ft-modal" style="width:380px">
        <h3>Create internal redirect</h3>
        <input type="text" id="tkm-name" placeholder="Link name (e.g. Reddit June)">
        <input type="text" id="tkm-target" placeholder="Target URL (https://onlyfans.com/…)">
        <input type="text" id="tkm-slug" placeholder="Custom slug (optional — auto from name)">
        <div style="font-size:11px;color:#8a8a8a;margin-bottom:10px">Public URL becomes ${esc(location.origin)}/t/&lt;slug&gt; — each hit records one click, then redirects. Clicks only: no subscriber attribution.</div>
        <div class="ft-err" id="tkm-err"></div>
        <button id="tkm-go">Create link</button>
      </div>`;
    document.body.appendChild(back);
    back.addEventListener('click', (e) => { if (e.target === back) back.remove(); });
    $('#tkm-go', back).addEventListener('click', async () => {
      const errEl = $('#tkm-err', back);
      try {
        await Fastt.post('/admin/tracking-links', {
          account_id: acct,
          name: $('#tkm-name', back).value.trim(),
          target_url: $('#tkm-target', back).value.trim(),
          slug: $('#tkm-slug', back).value.trim() || null,
        });
        back.remove(); Fastt.saved('Tracking link created');
        await refresh();
        if ($('#tk-int-wrap').style.display === 'none') toggleInternal();
      } catch (e) {
        errEl.style.display = 'block';
        errEl.textContent = (e.body && e.body.detail) || 'Create failed';
      }
    });
  }

  // ── events ──────────────────────────────────────────────────────────────
  function toggleInternal() {
    const w = $('#tk-int-wrap'), open = w.style.display === 'none';
    w.style.display = open ? 'block' : 'none';
    $('#tk-int-chev').classList.toggle('closed', !open);
  }
  $('#tk-int-chev').addEventListener('click', toggleInternal);
  $('#tk-int-title').addEventListener('click', toggleInternal);
  $('#tk-of-create').addEventListener('click', openCreateOf);
  $('#tk-create').addEventListener('click', openCreate);
  $('#tk-refresh').addEventListener('click', async () => {
    try { await refresh(); Fastt.toast('Refreshed', 'ok'); } catch (e) { Fastt.oops(e); }
  });
  const reFilter = () => { render(); renderOf(); };
  $('#tk-search').addEventListener('input', Fastt.debounce(reFilter, 150));
  $('#tk-from').addEventListener('change', reFilter);
  $('#tk-to').addEventListener('change', reFilter);
  $('#tk-datclear').addEventListener('click', () => {
    $('#tk-from').value = ''; $('#tk-to').value = ''; reFilter();
  });

  $('#tk-ptabs').addEventListener('click', (e) => {
    const t = e.target.closest('.ptab');
    if (!t) return;
    const dash = t.dataset.view === 'dash';
    $('#tk-view-list').style.display = dash ? 'none' : 'block';
    $('#tk-view-dash').style.display = dash ? 'block' : 'none';
    if (dash) loadDash();
  });

  $('#tk-export').addEventListener('click', () => {
    const head = ['kind', 'name', 'url', 'clicks', 'subscribers', 'created_at'];
    const rows = visibleOf().map((l) => ['onlyfans', l.name, l.url, l.clicks, l.subscribers, l.created_at])
      .concat(visible().map((l) => ['internal', l.name, location.origin + l.path, l.click_count, '', l.created_at]));
    const csv = [head.join(',')].concat(rows.map((r) => r
      .map((v) => '"' + String(v == null ? '' : v).replace(/"/g, '""') + '"').join(','))).join('\n');
    const a = document.createElement('a');
    a.href = URL.createObjectURL(new Blob([csv], { type: 'text/csv' }));
    a.download = 'tracking-links.csv';
    a.click();
    URL.revokeObjectURL(a.href);
  });

  ofRowsEl.addEventListener('click', async (e) => {
    const btn = e.target.closest('[data-oact]');
    if (!btn) return;
    const l = visibleOf()[Number(btn.closest('.trow').dataset.oi)];
    if (!l) return;
    if (btn.dataset.oact === 'copy') {
      try { await navigator.clipboard.writeText(l.url || ''); Fastt.toast('Link copied: ' + l.url, 'ok'); }
      catch (err) { Fastt.toast(l.url || 'no url', ''); }
      return;
    }
    if (btn.dataset.oact === 'del') {
      if (!confirm('Delete the OnlyFans tracking link "' + (l.name || ('c' + l.code)) + '"?\n\nIt is removed on OnlyFans and its click/subscriber counts go with it.')) return;
      try { await Fastt.del('/admin/of-tracking-links/' + l.id); Fastt.saved('Deleted on OnlyFans'); await refresh(); }
      catch (err) { Fastt.oops(err); }
    }
  });

  rowsEl.addEventListener('click', async (e) => {
    const btn = e.target.closest('[data-act]');
    if (!btn) return;
    const id = Number(btn.closest('.trow').dataset.id);
    const l = links.find((x) => x.id === id);
    if (!l) return;
    if (btn.dataset.act === 'copy') {
      const full = location.origin + l.path;
      try { await navigator.clipboard.writeText(full); Fastt.toast('Link copied: ' + full, 'ok'); }
      catch (err) { Fastt.toast(full, ''); }
      return;
    }
    if (btn.dataset.act === 'stats') {
      const det = rowsEl.querySelector('.tk-detail[data-for="' + id + '"]');
      if (!det) return;
      if (det.style.display !== 'none') { det.style.display = 'none'; return; }
      det.style.display = 'block';
      det.textContent = 'Loading analytics…';
      try {
        const a = await Fastt.get('/admin/tracking-links/' + id + '/analytics');
        const by = a.by_day || [];
        const plot = by.length
          ? '<div id="tk-day-chart-' + id + '" class="cbox" style="margin:12px 0 0;padding:14px 16px 10px">'
            + '<div style="font-size:12.5px;color:#8a8a8a;margin-bottom:10px">Clicks per day</div>'
            + bars(by, (d) => String(d.day).slice(5), (d) => d.clicks || 0)
            + '</div>'
          : '<div style="margin-top:10px;color:#8a8a8a">No clicks yet — nothing to plot.</div>';
        const recent = (a.recent || []).slice(0, 5)
          .map((r) => Fastt.fmtAgo(r.clicked_at) + (r.referer ? ' <span style="color:#6f6f6f">(' + esc(r.referer) + ')</span>' : '')).join(' &nbsp;·&nbsp; ');
        det.innerHTML = 'Total clicks: <b style="color:#e8e8e8">' + Fastt.fmtInt(a.total_clicks) + '</b>'
          + plot
          + (recent ? '<div style="margin-top:10px">Recent: ' + recent + '</div>' : '')
          + '<div style="margin-top:8px;color:#6f6f6f">Subscribers and revenue are null for /t/ redirects — the relay records the click only, with no fan on the other end of it.</div>';
      } catch (err) { det.textContent = 'Analytics failed to load'; Fastt.oops(err); }
      return;
    }
    if (btn.dataset.act === 'del') {
      if (!confirm('Delete tracking link "' + l.name + '"? Its public URL stops working and click history is removed.')) return;
      try { await Fastt.del('/admin/tracking-links/' + id); Fastt.saved('Deleted'); await refresh(); }
      catch (err) { Fastt.oops(err); }
    }
  });

  // ── filter-bar creator switcher (was a chevron that did nothing) ─────────
  // Reuse the shared roster + scope so switching here re-scopes the whole page,
  // exactly like the top-left creator picker. No new state, no fake list.
  const cselEl = $('#tk-acct') && $('#tk-acct').closest('.csel');
  if (cselEl) {
    cselEl.style.cursor = 'pointer';
    cselEl.title = 'Switch creator';
    cselEl.addEventListener('click', (e) => {
      e.stopPropagation();
      document.querySelectorAll('.tk-acct-menu').forEach((m) => m.remove());
      const roster = Fastt.accounts() || [];
      if (!roster.length) { Fastt.toast('Sign in (top-left creator) to list creators', ''); return; }
      const menu = document.createElement('div');
      menu.className = 'tk-acct-menu';
      const r = cselEl.getBoundingClientRect();
      menu.style.cssText = 'position:fixed;z-index:200;left:' + r.left + 'px;top:' + (r.bottom + 6)
        + 'px;min-width:230px;max-height:420px;overflow:auto;background:#1b1b1d;border:1px solid #333;'
        + 'border-radius:12px;padding:6px;box-shadow:0 12px 34px rgba(0,0,0,.55)';
      menu.innerHTML = '<div style="font-size:10.5px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;color:#6a6a6a;padding:8px 10px 6px">Switch creator</div>'
        + roster.map((a) => {
            const on = String(a.id) === String(acct);
            return '<div class="tk-acct-row" data-id="' + esc(a.id) + '" style="display:flex;align-items:center;gap:10px;'
              + 'height:38px;padding:0 10px;border-radius:9px;cursor:pointer;font-size:14px;color:#e6e6e6;'
              + (on ? 'background:#242424' : '') + '">'
              + '<span style="width:8px;height:8px;border-radius:50%;background:' + (on ? '#3ec46a' : '#3a3a3a') + ';flex:0 0 auto"></span>'
              + '<span style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">' + esc(a.nickname || a.id) + '</span>'
              + (on ? '<span style="color:#67d1ae;font-size:12px">current</span>' : '') + '</div>';
          }).join('');
      document.body.appendChild(menu);
      menu.querySelectorAll('.tk-acct-row').forEach((el) => {
        el.addEventListener('mouseenter', () => { if (String(el.dataset.id) !== String(acct)) el.style.background = '#242424'; });
        el.addEventListener('mouseleave', () => { if (String(el.dataset.id) !== String(acct)) el.style.background = ''; });
        el.addEventListener('click', () => {
          const id = el.dataset.id;
          if (String(id) === String(acct)) { menu.remove(); return; }
          Fastt.setAccount(id); // persists of_account_id + reloads → whole page re-scopes
        });
      });
      const close = () => { menu.remove(); document.removeEventListener('click', close); };
      setTimeout(() => document.addEventListener('click', close), 0);
    });
  }

  await refresh();
});
