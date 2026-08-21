/* Script Library — the sellable-content CATALOG for the AI Chatter, wired live.
 *
 *  GET  /admin/scripts?account_id=          -> {scripts:[ordered ladders], singles:[flat pool]}
 *  PUT  /admin/catalog/singles              -> replaces the WHOLE singles list.
 *       FOOTGUN (verified live): this deletes+recreates every single, reassigning
 *       ids and DROPPING the offer/delivery attribution joined on the old ids.
 *       So Save is confirm-gated with that warning, and per-item "sold" counts are
 *       recomputed CLIENT-SIDE from the live offers feed (by label), which survives.
 *  POST /admin/scripts {account_id,name,...} -> create/rename a ladder.
 *  DELETE /admin/scripts/{id}                -> delete a ladder.
 *  POST /admin/scripts/simulate {fan_says}   -> dry run (nothing sent).
 *  GET  /admin/ai-chatter/sessions           -> {progress, offers} (live monitor).
 *  POST /admin/ai-chatter/offers/{id}/cancel -> hand an open offer to a human.
 *  GET  /admin/ai-chatter-config             -> {config, script_pack} (canned lines).
 *  POST /admin/catalog/singles/suggest-texts | generate-lines | suggest  -> content helpers.
 *  All keys grounded in service/scripts_api.py + app/hooks/useCatalog.ts.
 */
Fastt.ready(async function () {
  var esc = Fastt.esc, $ = Fastt.$, $$ = Fastt.$$;
  var dollars = function (c) { return Fastt.fmtMoney((c || 0) / 100); };
  var centsFromDollars = function (v) { return Math.round((parseFloat(v) || 0) * 100); };

  var body = $('#viewbody'), toolbar = $('#toolbar');

  /* ── account label + live badge ───────────────────────────────────── */
  var row = Fastt.accountRow();
  var nm = $('#acct-name'); if (nm) nm.textContent = row ? (row.nickname || row.id) : (Fastt.account() || '—');
  Fastt.liveBadge($('.gtitle'));

  /* ── state ────────────────────────────────────────────────────────── */
  var S = {
    view: 'singles',
    scripts: [], singles: [], singlesOrig: '[]',
    offers: [], progress: [],
    cfg: null, pack: {}, packErr: null,
    q: '', sort: 'default', filter: 'all',
    ofFilter: 'all',
    suggested: [], picked: {}, shootList: [], fillNote: '', allowReuse: false, aiOpen: false,
  };
  var offersByLabel = {};     // label -> {offers, delivered}
  var DELIVERED = { delivered: 1, purchased: 1, paid: 1 };

  function recomputeStats() {
    offersByLabel = {};
    S.offers.forEach(function (o) {
      var k = o.item_label || '(none)';
      var e = offersByLabel[k] || (offersByLabel[k] = { offers: 0, delivered: 0 });
      e.offers++;
      if (DELIVERED[o.status]) e.delivered++;
    });
  }
  function statOf(item) {
    var e = offersByLabel[item.label] || (item.stats || null);
    if (!e) return { offers: 0, delivered: 0 };
    return { offers: e.offers || 0, delivered: e.delivered || 0 };
  }

  /* ── load ─────────────────────────────────────────────────────────── */
  async function loadCatalog() {
    var d = await Fastt.get('/admin/scripts');
    S.scripts = d.scripts || [];
    S.singles = (d.singles || []).map(function (x) { return Object.assign({}, x); });
    S.singlesOrig = JSON.stringify(S.singles);
  }
  async function loadSessions() {
    try {
      var d = await Fastt.get('/admin/ai-chatter/sessions');
      S.offers = d.offers || []; S.progress = d.progress || [];
    } catch (e) { S.offers = []; S.progress = []; }
    recomputeStats();
  }
  async function loadPack() {
    try { S.cfg = await Fastt.get('/admin/ai-chatter-config'); S.pack = (S.cfg && S.cfg.script_pack) || {}; }
    catch (e) { S.packErr = e; }
  }

  try {
    await Promise.all([loadCatalog(), loadSessions(), loadPack()]);
  } catch (e) {
    body.innerHTML = '<div class="sl-empty"><h4>Could not load the content library</h4>'
      + '<p>The relay did not return this creator’s catalog, so nothing is shown and no save is possible '
      + '(an editor filled with placeholders would overwrite her real items on the first save).</p></div>';
    Fastt.oops(e);
    return;
  }

  /* ── KPIs + tab counts ────────────────────────────────────────────── */
  function singlesDirty() { return JSON.stringify(S.singles) !== S.singlesOrig; }
  function packDirtyCount() {
    if (!S.pack) return 0;
    var ov = (S.cfg.config && S.cfg.config.script_pack_overrides) || {};
    return Object.keys(S.pack).filter(function (s) {
      var cur = (packText[s] != null ? linesOf(packText[s]) : (ov[s] || S.pack[s] || []));
      return !sameArr(cur, ov[s] || S.pack[s] || []);
    }).length;
  }

  function renderKpis() {
    var sg = S.singles;
    var enabled = sg.filter(function (i) { return i.enabled; }).length;
    var prices = sg.map(function (i) { return i.price_cents; }).filter(function (c) { return c > 0; });
    var lo = prices.length ? Math.min.apply(null, prices) : 0;
    var hi = prices.length ? Math.max.apply(null, prices) : 0;
    var openOffers = S.offers.filter(function (o) { return o.status === 'open'; }).length;
    var noMedia = sg.filter(function (i) { return !(i.media_ids || []).length; }).length;
    var k = $('#kpis');
    k.innerHTML =
      tile('Sellable singles', enabled + '<small>/' + sg.length + '</small>',
           '#67d1ae', noMedia ? (noMedia + ' need media') : 'all have media', 'singles') +
      tile('Script ladders', String(S.scripts.length),
           '#7b93fb', S.scripts.length ? 'ordered sexting arcs' : 'none built yet', 'scripts') +
      tile('Price range', prices.length ? (dollars(lo) + '<small> – ' + dollars(hi) + '</small>') : '—',
           '#ec7bb5', prices.length ? (prices.length + ' priced items') : 'no prices set', null) +
      tile('Live offers', String(openOffers),
           '#e8b24e', S.offers.length ? (S.offers.length + ' in history') : 'none right now', 'offers');
    $$('.kpi.tap', k).forEach(function (el) {
      el.addEventListener('click', function () { switchView(el.dataset.go); });
    });
    $('#c-singles').textContent = sg.length;
    $('#c-scripts').textContent = S.scripts.length;
    $('#c-offers').textContent = S.offers.filter(function (o) { return o.status === 'open'; }).length || S.offers.length || 0;
    var pk = S.pack ? Object.keys(S.pack).length : 0;
    $('#c-lines').textContent = pk || '';
  }
  function tile(label, val, dot, sub, go) {
    return '<div class="kpi' + (go ? ' tap' : '') + '"' + (go ? ' data-go="' + go + '"' : '') + '>'
      + '<div class="kl"><span class="kdot" style="background:' + dot + '"></span>' + esc(label) + '</div>'
      + '<div class="kv">' + val + '</div>'
      + '<div class="kl" style="color:#6a6a6a">' + esc(sub) + '</div></div>';
  }

  /* ── view switching ───────────────────────────────────────────────── */
  var VIEWS = { singles: renderSingles, scripts: renderScripts, simulate: renderSimulate, offers: renderOffers, lines: renderLines };
  function switchView(v) {
    if (!VIEWS[v]) return;
    S.view = v;
    $$('#viewtabs .sbtab').forEach(function (t) { t.classList.toggle('on', t.dataset.view === v); });
    var pl = $('#primary-lbl'), pb = $('#btn-primary');
    if (v === 'scripts') { pl.textContent = 'New script ladder'; pb.style.display = ''; }
    else if (v === 'singles') { pl.textContent = 'Add single'; pb.style.display = ''; }
    else { pb.style.display = 'none'; }
    VIEWS[v]();
  }
  $$('#viewtabs .sbtab').forEach(function (t) {
    t.addEventListener('click', function () { switchView(t.dataset.view); });
  });
  $('#btn-primary').addEventListener('click', function () {
    if (S.view === 'singles') addSingle();
    else if (S.view === 'scripts') newScript();
  });

  /* ══════════════════════════ SINGLES ══════════════════════════════ */
  function visibleSingles() {
    var q = S.q.toLowerCase();
    var arr = S.singles.map(function (it, i) { return { it: it, i: i }; });
    if (q) arr = arr.filter(function (r) {
      return (r.it.label || '').toLowerCase().indexOf(q) >= 0
          || (r.it.description_for_ai || '').toLowerCase().indexOf(q) >= 0;
    });
    if (S.filter === 'enabled') arr = arr.filter(function (r) { return r.it.enabled; });
    else if (S.filter === 'disabled') arr = arr.filter(function (r) { return !r.it.enabled; });
    else if (S.filter === 'teaser') arr = arr.filter(function (r) { return r.it.is_free_teaser; });
    else if (S.filter === 'nomedia') arr = arr.filter(function (r) { return !(r.it.media_ids || []).length; });
    if (S.sort === 'price') arr.sort(function (a, b) { return b.it.price_cents - a.it.price_cents; });
    else if (S.sort === 'label') arr.sort(function (a, b) { return (a.it.label || '').localeCompare(b.it.label || ''); });
    else if (S.sort === 'offers') arr.sort(function (a, b) { return statOf(b.it).offers - statOf(a.it).offers; });
    return arr;
  }

  function renderSingles() {
    var dirty = singlesDirty();
    toolbar.innerHTML =
      '<div class="sl-search"><svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="#8a8a8a" stroke-width="1.8"><circle cx="11" cy="11" r="7"/><path d="M20 20l-3.2-3.2" stroke-linecap="round"/></svg>'
        + '<input id="s-q" placeholder="Search label or what the fan sees" value="' + esc(S.q) + '"></div>'
      + '<select class="sl-sel" id="s-filter">'
        + opt('all', 'All items', S.filter) + opt('enabled', 'Sellable only', S.filter)
        + opt('disabled', 'Disabled', S.filter) + opt('teaser', 'Free teasers', S.filter)
        + opt('nomedia', 'Missing media', S.filter) + '</select>'
      + '<select class="sl-sel" id="s-sort">'
        + opt('default', 'Catalog order', S.sort) + opt('price', 'Price (high→low)', S.sort)
        + opt('offers', 'Most offered', S.sort) + opt('label', 'A–Z', S.sort) + '</select>'
      + '<div class="sl-spacer"></div>'
      + '<button class="sl-pill" id="s-ai">✨ AI content tools</button>'
      + '<button class="sl-pill warn" id="s-save"' + (dirty ? '' : ' disabled') + '>'
        + (dirty ? '<span class="dirty-dot"></span>' : '') + 'Save catalog</button>';

    $('#s-q').addEventListener('input', Fastt.debounce(function (e) { S.q = e.target.value.trim(); paintSinglesBody(); }, 150));
    $('#s-filter').addEventListener('change', function (e) { S.filter = e.target.value; paintSinglesBody(); });
    $('#s-sort').addEventListener('change', function (e) { S.sort = e.target.value; paintSinglesBody(); });
    $('#s-ai').addEventListener('click', function () { S.aiOpen = !S.aiOpen; paintSinglesBody(); });
    $('#s-save').addEventListener('click', saveSingles);

    paintSinglesBody();
  }

  function paintSinglesBody() {
    var rows = visibleSingles();
    var html = aiPanelHtml();
    if (!S.singles.length) {
      html += '<div class="sl-empty"><h4>No singles yet</h4>'
        + '<p>Singles are standalone pieces she offers as a flat pool — she picks whichever fits what he asks for. '
        + 'Add one manually, or let the AI tools propose captions your vault can actually fill.</p></div>';
      body.innerHTML = html; bindAi(); return;
    }
    html += '<table class="ctable"><thead><tr>'
      + '<th style="width:44px">On</th><th style="width:150px">Label</th><th>What the fan sees</th>'
      + '<th style="width:142px">Kind</th><th style="width:120px">Media</th>'
      + '<th style="width:96px">Tip $</th><th style="width:96px">PPV $</th>'
      + '<th style="width:56px" title="Sent out unlocked as a teaser">Free</th>'
      + '<th style="width:92px" title="delivered / offered — from the live offers feed">Sold</th><th style="width:40px"></th>'
      + '</tr></thead><tbody>';
    if (!rows.length) html += '<tr><td colspan="10" style="color:#8a8a8a;padding:26px;text-align:center">No item matches this filter.</td></tr>';
    rows.forEach(function (r) {
      var it = r.it, st = statOf(it), media = (it.media_ids || []).length, prev = (it.preview_media_ids || []).length;
      html += '<tr class="' + (it.enabled ? '' : 'disabled') + '" data-i="' + r.i + '">'
        + '<td><span class="tgl blue' + (it.enabled ? ' on' : '') + '" data-tog="enabled"></span></td>'
        + '<td><input class="ci-in" data-f="label" value="' + esc(it.label || '') + '"></td>'
        + '<td><textarea class="ci-in ci-desc" data-f="description_for_ai" rows="1" placeholder="present-tense pitch the AI may claim">' + esc(it.description_for_ai || '') + '</textarea></td>'
        + '<td><select class="ci-in" data-f="kind">' + kindOpt('video', it.kind) + kindOpt('image', it.kind) + kindOpt('image_set', it.kind) + '</select></td>'
        + '<td><div class="mediacell' + (media ? '' : ' none') + '">'
          + (media ? ('<span class="mchip">' + media + '</span> item' + (media > 1 ? 's' : '') + (prev ? ' · ' + prev + ' free' : ''))
                   : '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 8v5M12 16h.01" stroke-linecap="round"/><circle cx="12" cy="12" r="9"/></svg> none') + '</div></td>'
        + '<td><input class="ci-in ci-price" data-f="tip_unlock_cents" value="' + (it.tip_unlock_cents / 100) + '"></td>'
        + '<td><input class="ci-in ci-price" data-f="price_cents" value="' + (it.price_cents / 100) + '"></td>'
        + '<td style="text-align:center"><input type="checkbox" class="teaser-chk" data-f="is_free_teaser"' + (it.is_free_teaser ? ' checked' : '') + '></td>'
        + '<td class="soldcell"><b>' + st.delivered + '</b> / ' + st.offers + '</td>'
        + '<td><button class="xbtn" data-del="' + r.i + '" title="Remove this single">✕</button></td>'
        + '</tr>';
    });
    html += '</tbody></table>'
      + '<div class="sl-hint">Media is attached in <b>Vault Pro</b> or by “Fill content” below — existing media is preserved on save. '
      + '“Sold” is delivered / offered, recomputed live from the offers feed (it survives a catalog rewrite; the API’s own per-item stats do not).</div>';
    body.innerHTML = html;
    bindSinglesRows(); bindAi();
  }

  function autosize(ta) { ta.style.height = 'auto'; ta.style.height = Math.max(34, ta.scrollHeight) + 'px'; }
  function bindSinglesRows() {
    $$('.ctable textarea.ci-desc', body).forEach(function (ta) {
      autosize(ta);
      ta.addEventListener('input', function () { autosize(ta); });
    });
    $$('.ctable [data-f]', body).forEach(function (el) {
      var i = +el.closest('tr').dataset.i, f = el.dataset.f;
      var ev = (el.type === 'checkbox') ? 'change' : (el.tagName === 'SELECT' ? 'change' : 'input');
      el.addEventListener(ev, Fastt.debounce(function () {
        var it = S.singles[i];
        if (f === 'is_free_teaser') it[f] = el.checked;
        else if (f === 'price_cents' || f === 'tip_unlock_cents') it[f] = centsFromDollars(el.value);
        else it[f] = el.value;
        refreshSaveBtn();
      }, 180));
    });
    $$('.ctable [data-tog]', body).forEach(function (tg) {
      tg.addEventListener('click', function () {
        var i = +tg.closest('tr').dataset.i;
        S.singles[i].enabled = !S.singles[i].enabled;
        tg.classList.toggle('on', S.singles[i].enabled);
        tg.closest('tr').classList.toggle('disabled', !S.singles[i].enabled);
        refreshSaveBtn();
      });
    });
    $$('.ctable [data-del]', body).forEach(function (b) {
      b.addEventListener('click', function () {
        S.singles.splice(+b.dataset.del, 1);
        renderKpis(); paintSinglesBody();
      });
    });
  }
  function refreshSaveBtn() {
    var d = singlesDirty(), b = $('#s-save');
    if (b) { b.disabled = !d; b.innerHTML = (d ? '<span class="dirty-dot"></span>' : '') + 'Save catalog'; }
  }

  function addSingle() {
    if (S.view !== 'singles') switchView('singles');
    S.singles.push({ kind: 'image_set', label: '', description_for_ai: '', media_ids: [], preview_media_ids: [],
      duration_sec: null, price_cents: 0, tip_unlock_cents: 0, is_free_teaser: false, tags: [], enabled: true });
    renderKpis(); paintSinglesBody();
    var last = body.querySelector('.ctable tbody tr:last-child .ci-in');
    if (last) last.focus();
  }

  async function saveSingles() {
    var n = S.singles.length;
    if (!confirm('Save the singles catalog?\n\nThis REPLACES all ' + n + ' singles at once. The relay reassigns '
      + 'item ids on save, which resets the API’s per-item offer/delivery history (the live offers feed itself is kept). '
      + 'Media already attached is preserved.\n\nProceed?')) return;
    try {
      var payload = S.singles.map(function (it) {
        return {
          id: it.id, kind: it.kind, label: it.label, description_for_ai: it.description_for_ai,
          media_ids: it.media_ids || [], preview_media_ids: it.preview_media_ids || [],
          duration_sec: it.duration_sec != null ? it.duration_sec : null,
          price_cents: it.price_cents || 0, tip_unlock_cents: it.tip_unlock_cents || 0,
          is_free_teaser: !!it.is_free_teaser, tags: it.tags || [], enabled: it.enabled !== false,
        };
      });
      await Fastt.put('/admin/catalog/singles', { account_id: Fastt.account(), items: payload });
      await loadCatalog();
      Fastt.saved('Catalog saved ✓');
      renderKpis(); renderSingles();
    } catch (e) { Fastt.oops(e); }
  }

  /* ── AI content tools (progressive-disclosure drawer) ─────────────── */
  function aiPanelHtml() {
    if (!S.aiOpen) return '';
    var h = '<div class="lad-card" style="margin-bottom:16px">'
      + '<div class="lad-top" style="margin-bottom:6px"><b style="color:#fff;font-size:15px">Fill this catalog from the vault</b>'
        + '<span class="sl-hint" style="margin:0">nothing is saved until you press Save catalog</span></div>'
      + '<div style="display:flex;gap:10px;flex-wrap:wrap;align-items:center">'
        + '<button class="sl-pill" id="ai-suggest">📝 Suggest text sets</button>'
        + '<button class="sl-pill" id="ai-generate">🪄 Write lines from vault</button>'
        + '<button class="sl-pill" id="ai-fill">✨ Fill content into empty rows</button>'
        + '<label class="sl-hint" style="display:flex;align-items:center;gap:6px;cursor:pointer;margin:0">'
          + '<input type="checkbox" id="ai-reuse"' + (S.allowReuse ? ' checked' : '') + '> ♻️ allow media reuse</label>'
      + '</div>';
    if (S.fillNote) h += '<div class="sl-hint" style="margin-top:10px;color:#a9a9a9">' + esc(S.fillNote) + '</div>';
    if (S.shootList.length) h += '<div class="sl-hint" style="margin-top:8px">🎥 Worth shooting (nothing in the vault fits): <b>' + esc(S.shootList.map(function (p) { return p.label; }).join(', ')) + '</b></div>';
    if (S.suggested.length) {
      h += '<div style="margin-top:12px;border-top:1px solid #232323;padding-top:12px">'
        + '<div style="display:flex;align-items:center;gap:10px;margin-bottom:8px"><b style="color:#fff;font-size:14px">Suggested captions</b>'
        + '<span class="sl-hint" style="margin:0">tick the ones to add (empty rows — then Fill content)</span>'
        + '<div class="sl-spacer"></div><button class="sl-pill" id="ai-add" style="height:34px">Add selected</button></div>';
      S.suggested.forEach(function (p, idx) {
        h += '<label style="display:flex;gap:9px;align-items:flex-start;padding:6px 4px;cursor:pointer">'
          + '<input type="checkbox" class="ai-pick" data-idx="' + idx + '"' + (S.picked[p.label] ? ' checked' : '') + ' style="margin-top:3px">'
          + '<span><b>' + esc(p.label) + '</b> <span style="color:#8a8a8a">· ' + esc(p.kind) + '</span> '
          + '<span style="color:#67d1ae">' + dollars(p.price_cents) + '</span> '
          + '<span class="mchip">' + p.fillable + ' to fill</span>'
          + '<span style="display:block;color:#7a7a7a;font-size:13px;font-style:italic">“' + esc(p.description_for_ai || '') + '”</span></span></label>';
      });
      h += '</div>';
    }
    h += '</div>';
    return h;
  }
  function bindAi() {
    if (!S.aiOpen) return;
    var s = $('#ai-suggest'); if (s) s.addEventListener('click', aiSuggestTexts);
    var g = $('#ai-generate'); if (g) g.addEventListener('click', aiGenerateLines);
    var f = $('#ai-fill'); if (f) f.addEventListener('click', aiFill);
    var r = $('#ai-reuse'); if (r) r.addEventListener('change', function () { S.allowReuse = r.checked; });
    var a = $('#ai-add'); if (a) a.addEventListener('click', aiAddPicked);
    $$('.ai-pick', body).forEach(function (c) {
      c.addEventListener('change', function () { S.picked[S.suggested[+c.dataset.idx].label] = c.checked; });
    });
  }
  async function aiSuggestTexts() {
    S.fillNote = 'Thinking…'; paintSinglesBody();
    try {
      var res = await Fastt.get('/admin/catalog/singles/suggest-texts');
      S.shootList = res.shoot || [];
      if (res.blocked) { S.suggested = []; S.fillNote = res.blocked; }
      else { S.suggested = res.proposals || []; S.picked = {}; S.suggested.forEach(function (p) { S.picked[p.label] = true; });
        S.fillNote = S.suggested.length ? '' : ((res.summary && res.summary.already_present) ? 'Nothing new — all default captions are already in your catalog.' : 'Nothing to suggest from this vault.'); }
    } catch (e) { S.fillNote = 'Couldn’t suggest: ' + (e.message || e); }
    paintSinglesBody();
  }
  async function aiGenerateLines() {
    S.fillNote = 'Writing…'; paintSinglesBody();
    try {
      var res = await Fastt.post('/admin/catalog/singles/generate-lines', { account_id: Fastt.account(), limit: 8 });
      S.shootList = [];
      if (res.blocked) { S.suggested = []; S.fillNote = res.blocked; }
      else { S.suggested = res.proposals || []; S.picked = {}; S.suggested.forEach(function (p) { S.picked[p.label] = true; });
        S.fillNote = 'Wrote ' + ((res.summary && res.summary.generated) || S.suggested.length) + ' line(s). Tick the ones you want, then Add selected.'; }
    } catch (e) { S.fillNote = 'Couldn’t write lines: ' + (e.message || e); }
    paintSinglesBody();
  }
  function aiAddPicked() {
    var rows = S.suggested.filter(function (p) { return S.picked[p.label]; });
    rows.forEach(function (p) {
      S.singles.push({ kind: p.kind, label: p.label, description_for_ai: p.description_for_ai, media_ids: [], preview_media_ids: [],
        duration_sec: null, price_cents: p.price_cents, tip_unlock_cents: p.price_cents, is_free_teaser: false, tags: [], enabled: true });
    });
    S.suggested = []; S.picked = {};
    S.fillNote = 'Added ' + rows.length + ' caption row(s) with no content. Press ✨ Fill content, then Save catalog.';
    renderKpis(); paintSinglesBody();
  }
  async function aiFill() {
    S.fillNote = 'Finding vault media…'; paintSinglesBody();
    var drafts = S.singles.filter(function (it) { return it.id == null; });
    var draftKey = new Map(); drafts.forEach(function (it, i) { draftKey.set(it, 'draft:' + i); });
    try {
      var res = await Fastt.post('/admin/catalog/singles/suggest',
        { account_id: Fastt.account(), only_empty: true, allow_reuse: S.allowReuse,
          drafts: drafts.map(function (it) { return { label: it.label || '', description_for_ai: it.description_for_ai || '', kind: it.kind, price_cents: it.price_cents }; }) });
      var byKey = {}; (res.proposals || []).forEach(function (p) { byKey[p.key || ('db:' + p.id)] = p; });
      S.singles.forEach(function (it) {
        var key = it.id != null ? ('db:' + it.id) : draftKey.get(it);
        var p = key ? byKey[key] : null;
        if (!p || !(p.media_ids || []).length || (it.media_ids || []).length) return;
        it.media_ids = p.media_ids; it.preview_media_ids = p.preview_media_ids || [];
      });
      var filled = (res.proposals || []).filter(function (p) { return (p.media_ids || []).length; }).length;
      var stuck = (res.proposals || []).filter(function (p) { return !(p.media_ids || []).length; });
      S.fillNote = 'Filled ' + filled + ' of ' + (res.proposals || []).length + '. '
        + (stuck.length ? 'Left empty (no honest match): ' + stuck.map(function (p) { return (p.label || '(unnamed)') + ' — ' + (p.empty_reason || 'no match'); }).join('; ')
                        : 'Review the counts, then Save catalog.');
    } catch (e) { S.fillNote = 'Couldn’t fill: ' + (e.message || e); }
    renderKpis(); paintSinglesBody();
  }

  /* ══════════════════════════ SCRIPTS (ladders) ════════════════════ */
  function renderScripts() {
    toolbar.innerHTML = '<div class="sl-hint" style="margin:0">Scripts are <b>ordered</b> sexting ladders — she walks them in sequence, ignoring what he asks for. Each rung is a priced item.</div>';
    if (!S.scripts.length) {
      body.innerHTML = '<div class="sl-empty"><svg width="70" height="70" viewBox="0 0 24 24" fill="none" stroke="#3a3a3a" stroke-width="1.4"><rect x="3" y="4" width="7" height="5" rx="1.5"/><rect x="14" y="15" width="7" height="5" rx="1.5"/><path d="M6.5 9v3.5a2 2 0 0 0 2 2H14"/></svg>'
        + '<h4>No script ladders yet</h4>'
        + '<p>This creator sells from the flat <b>Singles</b> pool only. Build a ladder when you want a fixed escalation arc '
        + '(tease → first rung → escalate → finish) that she walks in order. Press <b>New script ladder</b> above to start one.</p>'
        + '<button class="btn-blue" id="empty-new"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2"><path d="M12 5v14M5 12h14" stroke-linecap="round"/></svg>New script ladder</button></div>';
      var b = $('#empty-new'); if (b) b.addEventListener('click', newScript);
      return;
    }
    body.innerHTML = S.scripts.map(scriptCardHtml).join('');
    S.scripts.forEach(bindScriptCard);
  }
  function scriptCardHtml(sc) {
    var items = sc.items || [];
    return '<div class="lad-card" data-sid="' + sc.id + '">'
      + '<div class="lad-top">'
        + '<input class="lad-name" data-f="name" value="' + esc(sc.name || '') + '">'
        + '<select class="sl-sel" data-f="status">' + statusOpt('draft', sc.status) + statusOpt('enabled', sc.status) + statusOpt('disabled', sc.status) + '</select>'
        + '<button class="sl-pill" data-save style="height:38px">Save</button>'
        + '<div class="sl-spacer"></div>'
        + '<span class="lad-status ' + esc(sc.status) + '">' + esc(sc.status) + '</span>'
        + '<button class="xbtn" data-delscript title="Delete ladder">🗑</button>'
      + '</div>'
      + '<div class="sl-hint" style="margin:0 0 8px">' + items.length + ' rung' + (items.length === 1 ? '' : 's')
        + (sc.theme ? ' · theme: ' + esc(sc.theme) : '') + '</div>'
      + (items.length
          ? '<table class="ctable"><thead><tr><th style="width:36px">#</th><th>Rung</th><th style="width:112px">Kind</th><th style="width:110px">Media</th><th style="width:90px">PPV $</th><th style="width:90px">Sold</th></tr></thead><tbody>'
            + items.map(function (it, n) { var st = statOf(it), media = (it.media_ids || []).length;
                return '<tr><td>' + (n + 1) + '</td><td><b>' + esc(it.label || '(unnamed)') + '</b>'
                  + (it.description_for_ai ? '<span style="display:block;color:#7a7a7a;font-size:13px">' + esc(it.description_for_ai) + '</span>' : '') + '</td>'
                  + '<td><span class="kind-badge kind-' + esc(it.kind) + '">' + esc(it.kind) + '</span></td>'
                  + '<td class="mediacell' + (media ? '' : ' none') + '">' + (media ? media + ' item' + (media > 1 ? 's' : '') : 'none') + '</td>'
                  + '<td style="color:#67d1ae;font-weight:600">' + dollars(it.price_cents) + '</td>'
                  + '<td class="soldcell"><b>' + st.delivered + '</b> / ' + st.offers + '</td></tr>'; }).join('')
            + '</tbody></table>'
          : '<div class="sl-hint">No rungs yet — add sellable items to this ladder in <b>Vault Pro</b> (rung media is picked there).</div>')
      + '</div>';
  }
  function bindScriptCard(sc) {
    var card = body.querySelector('.lad-card[data-sid="' + sc.id + '"]');
    if (!card) return;
    var meta = { name: sc.name, theme: sc.theme || '', status: sc.status };
    card.querySelector('[data-f="name"]').addEventListener('input', function (e) { meta.name = e.target.value; });
    card.querySelector('[data-f="status"]').addEventListener('change', function (e) { meta.status = e.target.value; });
    card.querySelector('[data-save]').addEventListener('click', async function () {
      try { await Fastt.post('/admin/scripts', { account_id: Fastt.account(), id: sc.id, name: meta.name, theme: meta.theme, status: meta.status });
        await loadCatalog(); Fastt.saved('Script saved ✓'); renderKpis(); renderScripts(); }
      catch (e) { Fastt.oops(e); }
    });
    card.querySelector('[data-delscript]').addEventListener('click', async function () {
      if (!confirm('Delete script “' + (sc.name || '') + '” and all its rungs?')) return;
      try { await Fastt.del('/admin/scripts/' + sc.id); await loadCatalog(); Fastt.saved('Deleted'); renderKpis(); renderScripts(); }
      catch (e) { Fastt.oops(e); }
    });
  }
  async function newScript() {
    if (S.view !== 'scripts') switchView('scripts');
    var name = prompt('Name this script ladder:', 'script_' + (S.scripts.length + 1));
    if (name == null) return;
    try {
      await Fastt.post('/admin/scripts', { account_id: Fastt.account(), name: name || ('script_' + (S.scripts.length + 1)), status: 'draft' });
      await loadCatalog(); Fastt.saved('Ladder created — add rungs in Vault Pro'); renderKpis(); renderScripts();
    } catch (e) { Fastt.oops(e); }
  }

  /* ══════════════════════════ SIMULATE ═════════════════════════════ */
  function renderSimulate() {
    toolbar.innerHTML = '<div class="sl-hint" style="margin:0"><b>Dry run</b> — type what a fan says and see what she’d reply and offer from this catalog. Nothing is sent to anyone. Costs one small AI call.</div>';
    body.innerHTML = '<div class="sim-wrap">'
      + '<textarea class="sim-in" id="sim-in" placeholder="what the fan says…">ngl im kinda in the mood.. u got anything for me? 🥵</textarea>'
      + '<div style="margin-top:12px"><button class="btn-blue" id="sim-run"><svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M8 5v14l11-7z" stroke-linejoin="round"/></svg>Run dry run</button></div>'
      + '<div id="sim-out" style="margin-top:18px"></div></div>';
    $('#sim-run').addEventListener('click', runSimulate);
  }
  async function runSimulate() {
    var btn = $('#sim-run'), out = $('#sim-out');
    var txt = $('#sim-in').value.trim();
    if (!txt) return;
    btn.disabled = true; out.innerHTML = '<div class="sl-hint">Running…</div>';
    try {
      var res = await Fastt.post('/admin/scripts/simulate', { account_id: Fastt.account(), fan_says: txt });
      var h = '';
      if (!res.manifest_present) h += '<div class="sl-hint" style="color:#e8b24e;margin-bottom:10px">⚠ No sellable items reached the prompt (are items enabled with media + prices?).</div>';
      else h += '<div class="sl-hint" style="margin-bottom:10px">' + (res.offerable_count || 0) + ' item(s) were offerable to the prompt.</div>';
      h += '<div>' + (res.bubbles || []).map(function (b) { return '<span class="bubble">' + esc(b) + '</span>'; }).join('') + '</div>';
      if (res.offer) {
        var o = res.offer;
        h += '<div style="margin-top:12px"><span class="offer-badge">would offer: <b>' + esc(o.label || ('item ' + o.item_id)) + '</b> '
          + (o.is_free_teaser ? '(free teaser)' : '— tip ' + dollars(o.tip_unlock_cents) + ' / ppv ' + dollars(o.price_cents)) + '</span></div>';
      } else if (res.bubbles && res.bubbles.length) {
        h += '<div class="sl-hint" style="margin-top:10px">She replied but made no offer — which is most of the time by design.</div>';
      }
      out.innerHTML = h;
    } catch (e) { out.innerHTML = ''; Fastt.oops(e); }
    btn.disabled = false;
  }

  /* ══════════════════════════ LIVE OFFERS ══════════════════════════ */
  function renderOffers() {
    var statuses = {}; S.offers.forEach(function (o) { statuses[o.status] = (statuses[o.status] || 0) + 1; });
    toolbar.innerHTML = '<select class="sl-sel" id="of-filter">'
      + opt('all', 'All offers (' + S.offers.length + ')', S.ofFilter)
      + Object.keys(statuses).sort().map(function (s) { return '<option value="' + s + '"' + (S.ofFilter === s ? ' selected' : '') + '>' + esc(s) + ' (' + statuses[s] + ')</option>'; }).join('')
      + '</select>'
      + '<button class="sl-pill" id="of-refresh"><svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9"><path d="M4 12a8 8 0 0 1 14-5l2 2M20 12a8 8 0 0 1-14 5l-2-2" stroke-linecap="round" stroke-linejoin="round"/></svg>Refresh</button>'
      + '<div class="sl-spacer"></div>'
      + (S.progress.length ? '<span class="sl-hint" style="margin:0">' + S.progress.length + ' active script pin(s)</span>' : '');
    $('#of-filter').addEventListener('change', function (e) { S.ofFilter = e.target.value; paintOffers(); });
    $('#of-refresh').addEventListener('click', async function () { await loadSessions(); renderKpis(); renderOffers(); });
    paintOffers();
  }
  function paintOffers() {
    var list = S.ofFilter === 'all' ? S.offers : S.offers.filter(function (o) { return o.status === S.ofFilter; });
    if (!list.length) {
      body.innerHTML = '<div class="sl-empty"><h4>No live offers</h4>'
        + '<p>Once the AI Chatter puts a priced item in front of a fan, every open, delivered and expired offer shows here in real time — with the fan, the item, the price and how long ago. '
        + (S.offers.length ? 'Nothing matches the “' + esc(S.ofFilter) + '” filter right now.' : 'The automation is currently paused in this workspace, so there are none.') + '</p></div>';
      return;
    }
    body.innerHTML = list.map(function (o) {
      var ago = o.offered_at ? Fastt.fmtAgo(Fastt.parseUtc(o.offered_at)) : '';
      var st = (o.status || '').toLowerCase();
      return '<div class="ofrow">'
        + '<span class="ofan">' + esc(o.fan_name || ('fan ' + o.fan_id)) + '</span>'
        + '<span class="oitem">' + esc(o.item_label || '(item)') + ' <span style="color:#7a7a7a">· ' + esc(o.mode || '') + '</span></span>'
        + '<span class="oprice">' + dollars(o.price_cents) + (o.tips_accum_cents ? ' <span style="color:#7a7a7a;font-weight:400">(+' + dollars(o.tips_accum_cents) + ' tipped)</span>' : '') + '</span>'
        + '<span class="ostat ' + esc(st) + '">' + esc(o.status) + (o.resolved_by ? ' · ' + esc(o.resolved_by) : '') + '</span>'
        + '<span class="otime">' + esc(ago) + '</span>'
        + (st === 'open' ? '<button class="ocancel" data-cancel="' + o.id + '" title="Hand this offer to a human (does not message the fan)">✕ cancel</button>' : '<span style="width:66px"></span>')
        + '</div>';
    }).join('');
    $$('[data-cancel]', body).forEach(function (b) {
      b.addEventListener('click', async function () {
        if (!confirm('Cancel this open offer and hand the fan to a human? (No message is sent to the fan.)')) return;
        try { await Fastt.post('/admin/ai-chatter/offers/' + b.dataset.cancel + '/cancel', { account_id: Fastt.account() });
          await loadSessions(); Fastt.saved('Offer cancelled'); renderKpis(); renderOffers(); }
        catch (e) { Fastt.oops(e); }
      });
    });
  }

  /* ══════════════════════════ CANNED LINES (pack overrides) ════════ */
  var SLOT_HELP = {
    question_hook: 'she opens a scene — free, never has a price on it',
    rung_open: 'the first thing he ever sees with a price on it',
    rung_escalate: 'after he buys, she asks for more',
    post_buy_bridge: 'right after he unlocks — free, keeps the conversation alive',
    edge_hold: 'a short beat to keep him talking mid-scene',
    pre_ppv_stall: 'while she’s “filming it right now”, just before the paid message',
    objection_price: 'he says it’s too expensive — she answers about the content, not his wallet',
    haggle_counter: 'she drops the price once, and only once',
    discount_resend: 'he didn’t buy — she takes it down and offers it cheaper',
    soft_broke_ack: 'he says he’s broke — she stops selling and keeps talking',
    aftercare: 'the end of the ladder, then she leaves him alone',
  };
  var LIVE_SLOTS = {
    discount_resend: 'ai_chatter.py:1714', post_buy_bridge: 'ai_chatter.py:2434',
    rung_escalate: 'ai_chatter.py:2531', aftercare: 'ai_chatter.py:2628',
    soft_broke_ack: 'ai_chatter.py:4212', companion_ack: 'ai_chatter.py:4227',
  };
  var packText = {};      // slot -> textarea buffer
  var linesOf = function (t) { return String(t || '').split('\n').map(function (l) { return l.trim(); }).filter(Boolean); };
  var sameArr = function (a, b) { return a.length === b.length && a.every(function (l, i) { return l === b[i]; }); };

  function renderLines() {
    toolbar.innerHTML = '';
    if (S.packErr || !S.cfg) {
      body.innerHTML = '<div class="sl-empty"><h4>Couldn’t load her canned lines</h4>'
        + '<p>The AI-chatter config did not load, so the line editor is disabled — an editor filled with placeholders '
        + 'would overwrite her real lines on the first save.</p></div>';
      return;
    }
    var pack = S.pack, baseCfg = S.cfg.config || {}, overrides = baseCfg.script_pack_overrides || {};
    var order = Object.keys(pack);
    order.forEach(function (s) { if (packText[s] == null) packText[s] = (overrides[s] || pack[s] || []).join('\n'); });
    var dirty = order.filter(function (s) { return !sameArr(linesOf(packText[s]), overrides[s] || pack[s] || []); }).length;
    var nDead = order.filter(function (s) { return !LIVE_SLOTS[s]; }).length;

    toolbar.innerHTML = '<div class="sl-hint" style="margin:0">Her ready-made lines per conversational beat — she picks one at random. '
      + 'Empty a box and she falls back to the lines that ship with the app. '
      + '<span style="color:#8fa6ff">{name}</span> = fan’s name, <span style="color:#8fa6ff">{price}</span> = price.'
      + (nDead ? ' <span style="color:#e2a94e">' + nDead + ' slot(s) are stored but never rendered by the engine.</span>' : '') + '</div>'
      + '<div class="sl-spacer"></div>'
      + '<button class="sl-pill" id="pk-save"' + (dirty ? '' : ' disabled') + '>' + (dirty ? '<span class="dirty-dot"></span>' : '') + 'Save lines</button>';
    $('#pk-save').addEventListener('click', savePack);

    body.innerHTML = order.map(function (slot) {
      var live = LIVE_SLOTS[slot], shipped = pack[slot] || [], cur = linesOf(packText[slot]);
      var slotDirty = !sameArr(cur, overrides[slot] || shipped);
      return '<div class="lad-card" style="' + (live ? '' : 'border-color:#33301f;') + '">'
        + '<div class="lad-top" style="margin-bottom:8px"><span style="font:600 14px ui-monospace,Menlo,monospace;color:#fff">' + esc(slot) + '</span>'
          + '<span class="sl-hint" style="margin:0">' + (SLOT_HELP[slot] ? esc(SLOT_HELP[slot]) : '<span style="color:#7a7a7a">no description ships for this slot</span>') + '</span>'
          + (live ? '<span class="lad-status enabled">SENT</span>' : '<span class="lad-status draft" title="ai_chatter.py never renders this slot">NEVER SENT</span>')
          + (overrides[slot] ? '<span class="lad-status disabled">saved override</span>' : '')
          + (slotDirty ? '<span class="dirty-dot"></span>' : '')
          + '<div class="sl-spacer"></div>'
          + (sameArr(cur, shipped) ? '' : '<a href="#" class="sp-reset" data-reset="' + esc(slot) + '">Reset to default</a>')
        + '</div>'
        + '<textarea class="sim-in" data-ta="' + esc(slot) + '" spellcheck="false" style="min-height:' + Math.min(200, Math.max(70, cur.length * 26 + 24)) + 'px">' + esc(packText[slot]) + '</textarea>'
        + '<div class="sl-hint">' + (cur.length ? (live ? cur.length + ' line(s) sent' : 'Stored but never rendered by the engine.') : 'Empty — she’ll use the ' + shipped.length + ' shipped line(s).') + '</div>'
        + '</div>';
    }).join('');

    $$('[data-ta]', body).forEach(function (ta) {
      ta.addEventListener('input', Fastt.debounce(function () {
        packText[ta.dataset.ta] = ta.value;
        var d = order.filter(function (s) { return !sameArr(linesOf(packText[s]), overrides[s] || pack[s] || []); }).length;
        var b = $('#pk-save'); if (b) { b.disabled = !d; b.innerHTML = (d ? '<span class="dirty-dot"></span>' : '') + 'Save lines'; }
        renderKpis();
      }, 200));
    });
    $$('[data-reset]', body).forEach(function (a) {
      a.addEventListener('click', function (e) { e.preventDefault(); packText[a.dataset.reset] = (pack[a.dataset.reset] || []).join('\n'); renderLines(); });
    });
  }
  async function savePack() {
    var pack = S.pack, baseCfg = S.cfg.config || {};
    var next = {};
    Object.keys(pack).forEach(function (s) {
      var cur = linesOf(packText[s]);
      if (cur.length && !sameArr(cur, pack[s] || [])) next[s] = cur;
    });
    try {
      var out = await Fastt.put('/admin/ai-chatter-config',
        { account_id: Fastt.account(), config: Object.assign({}, baseCfg, { script_pack_overrides: next }) });
      S.cfg.config = (out && out.config) || Object.assign({}, baseCfg, { script_pack_overrides: next });
      packText = {};
      Fastt.saved('Lines saved ✓'); renderLines();
    } catch (e) { Fastt.oops(e); }
  }

  /* ── header helpers ───────────────────────────────────────────────── */
  $('#btn-share').addEventListener('click', function () {
    var doc = { account_id: Fastt.account(), singles: S.singles, scripts: S.scripts };
    var blob = new Blob([JSON.stringify(doc, null, 2)], { type: 'application/json' });
    var a = document.createElement('a'); a.href = URL.createObjectURL(blob);
    a.download = 'content-library-' + Fastt.account() + '.json'; a.click();
    setTimeout(function () { URL.revokeObjectURL(a.href); }, 4000);
    Fastt.saved('Exported content-library-' + Fastt.account() + '.json');
  });

  /* ── small html helpers ───────────────────────────────────────────── */
  function opt(v, l, cur) { return '<option value="' + v + '"' + (cur === v ? ' selected' : '') + '>' + esc(l) + '</option>'; }
  function kindOpt(v, cur) { return '<option value="' + v + '"' + (cur === v ? ' selected' : '') + '>' + v + '</option>'; }
  function statusOpt(v, cur) { return '<option value="' + v + '"' + (cur === v ? ' selected' : '') + '>' + v + '</option>'; }

  renderKpis();
  switchView('singles');
});
