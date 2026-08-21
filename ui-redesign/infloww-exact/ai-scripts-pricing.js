Fastt.ready(async () => {
  // Section-header persona chip: inert chrome that shipped a hardcoded "Aria".
  // Name the creator this page actually reads/writes, and badge it so it is not
  // mistaken for a working creator switcher (the sidebar .creator block is that).
  (function fixPersonaChip() {
    const lbl = document.getElementById('hdr-persona');
    const drop = document.getElementById('hdr-persona-drop');
    if (!lbl) return;
    const row = Fastt.accountRow();
    lbl.textContent = row ? (row.nickname || String(row.id)) : (Fastt.account() || '—');
    if (drop) Fastt.staticBadge(drop, 'DISPLAY ONLY');
  })();

  if (!Fastt.account()) return;

  const $ = Fastt.$, $$ = Fastt.$$;
  const [cfgResp, catResp, ppvResp, acctResp] = await Promise.all([
    Fastt.get('/admin/ai-chatter-config'),
    Fastt.get('/admin/scripts'),
    Fastt.get('/admin/ppv-library-config').catch(() => null),
    Fastt.get('/admin/account-config').catch(() => null),
  ]);
  let stored = cfgResp.config || {};       // sparse blob — PUT replaces it whole
  const defs = cfgResp.defaults || {};
  const shipped = cfgResp.script_pack || {};
  const eff = () => ({ ...defs, ...stored });

  async function saveCfg(patch) {
    try {
      const resp = await Fastt.put('/admin/ai-chatter-config',
        { account_id: Fastt.account(), config: { ...stored, ...patch } });
      stored = resp.config || {};
      Fastt.saved();
    } catch (e) { Fastt.oops(e); }
    render();
  }

  const setTxt = (id, t) => { const el = document.getElementById(id); if (el) el.textContent = t; };
  const setVal = (id, v) => { const el = document.getElementById(id);
    if (el && document.activeElement !== el) el.value = v; };

  // ---- catalog ladder: LIVE and EDITABLE -----------------------------------
  // Only SINGLES are editable here. PUT /admin/catalog/singles → _replace_items()
  // DELETEs then re-inserts every row (scripts_api.py:838), so a save must post the
  // WHOLE list in one go — and the item ids regenerate, which detaches the per-rung
  // offer stats. That is exactly what the Next app does; the confirm() spells it out.
  // Rows belonging to a multi-item SCRIPT are shown read-only: they live under
  // PUT /admin/scripts/{id}/items and saving them from here would be a different call.
  const singles = (catResp.singles || []).slice();
  const scriptItems = (catResp.scripts || []).flatMap(sc =>
    (sc.items || []).map(it => Object.assign({}, it, { _script: sc.name || sc.id })));
  const byPrice = (a, b) => (a.price_cents || 0) - (b.price_cents || 0);
  // draft = the on-screen singles; `items` stays the union used by the status strip.
  let draft = singles.slice().sort(byPrice).map(it => ({
    id: it.id, kind: it.kind || 'video', label: it.label || '',
    description_for_ai: it.description_for_ai || '',
    media_ids: (it.media_ids || []).slice(),
    preview_media_ids: (it.preview_media_ids || []).slice(),
    duration_sec: it.duration_sec, price_cents: it.price_cents || 0,
    tip_unlock_cents: it.tip_unlock_cents || 0,
    is_free_teaser: !!it.is_free_teaser, tags: (it.tags || []).slice(),
    enabled: it.enabled !== false, stats: it.stats || { offers: 0, delivered: 0 },
  }));
  const clean = JSON.stringify(draft);
  const items = draft.concat(scriptItems).slice().sort(byPrice);
  const thumbUrl = (id) => '/admin/vault-ai/thumb?account_id='
    + encodeURIComponent(Fastt.account()) + '&media_id=' + encodeURIComponent(id);

  function thumbStrip(it) {
    // Preview ids first (that's the frame the fan actually sees), then the rest.
    const prev = (it.preview_media_ids || []).map(Number);
    const rest = (it.media_ids || []).map(Number).filter(m => prev.indexOf(m) < 0);
    const ids = prev.concat(rest).slice(0, 5);
    if (!ids.length) return '<span class="pchip warn">no media</span>';
    return '<span class="rung-thumbs">' + ids.map(m =>
      '<span class="rung-thumb" title="media #' + m + '" style="background-image:url('
      + thumbUrl(m) + ')"></span>').join('')
      + ((it.media_ids || []).length > ids.length
          ? '<span class="rung-thumb" style="display:flex;align-items:center;justify-content:center;'
            + 'font-size:9px;color:#8a8a8a">+' + ((it.media_ids || []).length - ids.length) + '</span>'
          : '') + '</span>';
  }
  function statChip(st) {
    const o = Number((st && st.offers) || 0), d = Number((st && st.delivered) || 0);
    if (!o) return '<span class="rung-stat">never offered</span>';
    const dead = d === 0 && o >= 5;
    return '<span class="rung-stat' + (dead ? ' dead' : '') + '" title="content_offer rows for this rung">'
      + '<b>' + o + '</b> offered · <b>' + d + '</b> sold'
      + (dead ? ' — never converts' : '') + '</span>';
  }
  const KINDS = ['video', 'image', 'image_set'];
  function rungRow(n, it, idx) {
    if (!it) return '<div class="fx-rung empty"><span class="n">' + n + '</span>'
      + '<input class="fx-input lbl" value="" placeholder="— empty rung —" disabled>'
      + '<div class="fx-unit" data-unit="$"><input class="fx-input" value="" disabled></div></div>';
    if (it._script) {                       // belongs to a script, not the singles list
      return '<div class="fx-rung"><span class="n">' + n + '</span>'
        + '<input class="fx-input lbl" value="' + Fastt.esc(it.label || ('item #' + it.id))
        + '" disabled title="Part of the script &quot;' + Fastt.esc(String(it._script))
        + '&quot; — edit it there"><div class="fx-unit" data-unit="$">'
        + '<input class="fx-input" value="' + ((it.price_cents || 0) / 100) + '" disabled></div>'
        + '<span class="pchip warn">in a script</span>'
        + '<div class="rung-2">' + thumbStrip(it) + statChip(it.stats) + '</div></div>';
    }
    const nMedia = (it.media_ids || []).length;
    return '<div class="fx-rung' + (nMedia ? '' : ' empty') + '" data-idx="' + idx + '">'
      + '<span class="n">' + n + '</span>'
      + '<input class="fx-input lbl" data-f="label" value="' + Fastt.esc(it.label)
      + '" placeholder="rung name">'
      + '<div class="fx-unit" data-unit="$"><input class="fx-input" data-f="price" value="'
      + ((it.price_cents || 0) / 100) + '"></div>'
      + '<span class="fx-check rung-on' + (it.enabled ? ' on' : '') + '" data-f="enabled"'
      + ' title="' + (it.enabled ? 'Offered' : 'Never offered') + '"><span class="bx"></span></span>'
      + '<div class="rung-2">'
      + '<select class="rung-kind" data-f="kind">' + KINDS.map(k =>
          '<option value="' + k + '"' + (it.kind === k ? ' selected' : '') + '>' + k + '</option>').join('')
      + '</select>' + thumbStrip(it) + statChip(it.stats) + '</div></div>';
  }
  function renderLadder() {
    const holder = $('#lad2'); if (!holder) return;
    const rows = [];
    const list = draft.slice().sort(byPrice).concat(scriptItems.slice().sort(byPrice));
    const total = Math.max(list.length, 12);
    for (let i = 0; i < total; i++) {
      const it = list[i];
      rows.push(rungRow(i + 1, it, it && !it._script ? draft.indexOf(it) : -1));
    }
    const half = Math.ceil(rows.length / 2);
    holder.innerHTML = '<div class="fx-ladder">' + rows.slice(0, half).join('') + '</div>'
      + '<div class="fx-ladder">' + rows.slice(half).join('') + '</div>';
    markDirty();
  }
  function markDirty() {
    const dirty = JSON.stringify(draft) !== clean;
    $('#btn-save-lad').disabled = !dirty;
    $('#btn-revert-lad').disabled = !dirty;
    const msg = $('#lad-msg');
    msg.classList.toggle('dirty', dirty);
    msg.textContent = dirty
      ? 'Unsaved edits — Save rewrites all ' + draft.length + ' catalog rows in one call.'
      : 'Edit a name, price or kind and the Save button lights up.';
  }

  function render() {
    const c = eff();
    const withMedia = items.filter(it => it.enabled && (it.media_ids || []).length).length;
    const noMedia = items.filter(it => !(it.media_ids || []).length).length;
    // summary lead card
    setTxt('sum-selling', withMedia + ' of ' + items.length);
    setTxt('sum-empty', String(noMedia));
    const et = $('#sum-empty-tile');
    if (et) { et.className = 'sum-tile' + (noMedia ? ' warn' : ''); }
    setTxt('sum-plain', 'She opens at your lowest rung and climbs one step per buy. Right now she can sell from '
      + withMedia + ' of ' + items.length + ' rung' + (items.length === 1 ? '' : 's')
      + (noMedia ? ' — ' + noMedia + ' still need vault content before she can offer them.'
                 : ' — every rung is stocked.'));
    const pc = (ppvResp && ppvResp.config) || {};
    const lo = pc.price_min_cents != null ? pc.price_min_cents : 300;
    const hi = pc.price_max_cents != null ? pc.price_max_cents : 20000;
    setTxt('sum-band', Fastt.fmtCents(lo) + ' – ' + Fastt.fmtCents(hi));
    setTxt('band-floor', Fastt.fmtCents(lo));
    setTxt('band-ceil', Fastt.fmtCents(hi));
    setTxt('sum-floor', 'Proven-spend floor ' + (c.proven_spend_floor_enabled ? 'on' : 'off'));
    const model = acctResp && acctResp.config && acctResp.config.model;
    setTxt('sum-model', model || 'house model');
    setTxt('chip-filled', withMedia + ' filled');
    setTxt('chip-empty', noMedia + ' empty');
    const psf = $('#psf-sw'); if (psf) psf.classList.toggle('on', !!c.proven_spend_floor_enabled);
    setTxt('psf-pct', Math.round((c.proven_spend_floor_mult || 0) * 100) + '% of his biggest buy');
    setVal('esc-mult', c.escalation_mult);
    setVal('esc-hist', c.max_ask_history_mult);
    // per-PPV caps: READ-ONLY here — saving ppv-library-config re-syncs (and can
    // enable) the ppv_send rules, which this page must never do.
    const caps = pc.ppv_caps || { per_day: 2, per_week: 14, per_month: 60 };  // house default
    setVal('caps-day', caps.per_day); setVal('caps-week', caps.per_week);
    setVal('caps-month', caps.per_month);
    ['caps-day', 'caps-week', 'caps-month'].forEach(id => {
      const el = document.getElementById(id); if (el) el.disabled = true;
    });
    // script pack: override line if stored, else the first shipped line.
    // A stored override is a LIST. This editor only surfaces line 1, so remember
    // lines 2..n per slot and carry them through the save (see the change handler)
    // instead of silently collapsing a multi-line override to a single line.
    const ov = c.script_pack_overrides || {};
    $$('input[data-slot]').forEach(inp => {
      const slot = inp.dataset.slot;
      const arr = Array.isArray(ov[slot]) ? ov[slot] : [];
      const line = arr[0] || (shipped[slot] && shipped[slot][0]) || '';
      if (document.activeElement !== inp) inp.value = line;
      inp.dataset.shippedFirst = (shipped[slot] && shipped[slot][0]) || '';
      const extra = arr.slice(1);
      inp.dataset.extraLines = JSON.stringify(extra);
      const lbl = inp.parentElement && inp.parentElement.querySelector('.slot-lbl');
      let tag = lbl && lbl.querySelector('.slot-extra');
      if (extra.length) {
        inp.title = 'This slot stores ' + arr.length + ' lines; only line 1 is editable here. '
                  + 'Lines 2–' + arr.length + ' are preserved on save.';
        if (lbl && !tag) {
          tag = document.createElement('span');
          tag.className = 'pchip warn slot-extra';
          lbl.appendChild(tag);
        }
        if (tag) tag.textContent = '+' + extra.length + ' more line' + (extra.length === 1 ? '' : 's') + ' kept';
      } else {
        inp.removeAttribute('title');
        if (tag) tag.remove();
      }
    });
  }
  renderLadder();
  render();

  Fastt.liveBadge($('#lad-h'));
  Fastt.staticBadge($('#esc-cold-lbl'), 'STATIC');
  Fastt.staticBadge($('#caps-h'), 'READ-ONLY');
  const coldEl = $('#esc-cold'); if (coldEl) coldEl.disabled = true;
  const adv = $('.fx-adv-head'); if (adv) Fastt.liveBadge(adv);

  const num = (el) => {
    const v = parseFloat(String(el.value).replace(/[^0-9.eE+-]/g, ''));
    return isFinite(v) ? v : null;
  };

  // ---- ladder edits + save -------------------------------------------------
  const rowIdx = (el) => {
    const r = el.closest('.fx-rung');
    const i = r ? Number(r.dataset.idx) : -1;
    return (isFinite(i) && i >= 0 && draft[i]) ? i : -1;
  };
  document.addEventListener('input', (e) => {
    const f = e.target.dataset && e.target.dataset.f;
    if (!f || (f !== 'label' && f !== 'price')) return;
    const i = rowIdx(e.target); if (i < 0) return;
    if (f === 'label') draft[i].label = e.target.value.slice(0, 80);
    else {
      const v = num(e.target);
      if (v === null) return;
      // The validator clamps to [0, $100,000]; the engine additionally clamps every
      // ask into the account's PPV price band (floor $3 / ceiling $200 on the wire).
      draft[i].price_cents = Math.max(0, Math.round(v * 100));
      draft[i].tip_unlock_cents = draft[i].price_cents;
    }
    markDirty();
  });
  document.addEventListener('change', (e) => {
    if (e.target.dataset && e.target.dataset.f === 'kind') {
      const i = rowIdx(e.target); if (i < 0) return;
      draft[i].kind = e.target.value; markDirty();
    }
  });
  document.addEventListener('click', (e) => {
    const on = e.target.closest('.rung-on');                 // fx kit already flipped it
    if (!on) return;
    const i = rowIdx(on); if (i < 0) return;
    draft[i].enabled = on.classList.contains('on');
    on.title = draft[i].enabled ? 'Offered' : 'Never offered';
    markDirty();
  });
  $('#btn-revert-lad').addEventListener('click', () => {
    draft = JSON.parse(clean);
    renderLadder(); render();
    Fastt.toast('Reverted to the saved catalog');
  });
  $('#btn-save-lad').addEventListener('click', async () => {
    if (!confirm('Save all ' + draft.length + ' catalog rows?\n\n'
      + 'The relay REPLACES the whole singles list (delete + re-insert), so every row '
      + 'gets a new id and the per-rung "offered / sold" counters above reset to 0. '
      + 'Nothing is sent to any fan.')) return;
    const btn = $('#btn-save-lad'); btn.disabled = true;
    try {
      await Fastt.put('/admin/catalog/singles', {
        account_id: String(Fastt.account()),
        items: draft.slice().sort(byPrice).map(it => ({
          kind: it.kind, label: it.label, description_for_ai: it.description_for_ai,
          media_ids: it.media_ids, preview_media_ids: it.preview_media_ids,
          duration_sec: it.duration_sec, price_cents: it.price_cents,
          tip_unlock_cents: it.tip_unlock_cents, is_free_teaser: it.is_free_teaser,
          tags: it.tags, enabled: it.enabled,
        })),
      });
      Fastt.saved('Catalog saved — reloading');
      setTimeout(() => location.reload(), 700);
    } catch (err) { Fastt.oops(err); btn.disabled = false; }
  });

  // ---- ✨ Fill content / 🪄 Write lines from vault --------------------------
  // Both endpoints propose ONLY — they write nothing to the catalog and send nothing
  // to a fan. `suggest` is free (a local match against the stored describe fields);
  // `generate-lines` costs one cheap capped DeepSeek call per media group, so it asks first.
  function sugShow(title, html) {
    $('#sug-box').style.display = '';
    $('#sug-h').textContent = title;
    $('#sug-body').innerHTML = html;
  }
  const money = (c) => Fastt.fmtCents(c);
  $('#btn-fill').addEventListener('click', async () => {
    const btn = $('#btn-fill'); btn.disabled = true; btn.textContent = '✨ Matching…';
    try {
      const r = await Fastt.post('/admin/catalog/singles/suggest', {
        account_id: String(Fastt.account()), only_empty: true, allow_reuse: false, drafts: [],
      });
      const props = r.proposals || [], s = r.summary || {};
      sugShow('✨ Fill content — ' + (s.targets || 0) + ' rung'
        + ((s.targets === 1) ? '' : 's') + ' with text but no media',
        props.length
          ? props.map(p => '<div class="sug-row"><b>' + Fastt.esc(p.label || ('#' + p.id)) + '</b>'
              + '<span>' + Fastt.esc(String(p.kind || '')) + ' · ' + money(p.price_cents) + '</span>'
              + '<span class="sug-why">' + (p.available
                  ? Fastt.esc(String(p.available)) + ' vault item(s) match — '
                    + Fastt.esc(String(p.candidates)) + ' candidate(s)'
                  : Fastt.esc(String(p.empty_reason || 'no match'))) + '</span></div>').join('')
            + '<div class="sug-empty" style="margin-top:10px">Matching is read-only: '
            + (s.filled ? s.filled + ' row(s) could be filled. ' : 'nothing could be bound. ')
            + 'Binding media to a rung happens in <a class="plink" href="vault-ai.html">Vault AI</a>.</div>'
          : '<div class="sug-empty">Every rung that has text already has media — nothing to fill.</div>');
    } catch (e) { Fastt.oops(e); }
    finally { btn.disabled = false; btn.textContent = '✨ Fill content'; }
  });
  $('#btn-lines').addEventListener('click', async () => {
    if (!confirm('Write new catalog lines from the vault?\n\nThis makes one cheap, capped '
      + 'DeepSeek call per unsold media group (it counts against this creator\'s daily AI '
      + 'budget). It writes nothing and sends nothing — you still choose what to keep.')) return;
    const btn = $('#btn-lines'); btn.disabled = true; btn.textContent = '🪄 Writing…';
    try {
      const r = await Fastt.post('/admin/catalog/singles/generate-lines',
        { account_id: String(Fastt.account()), limit: 8 });
      const props = r.proposals || [];
      if (props.length) {
        sugShow('🪄 ' + props.length + ' line(s) written from unsold vault media',
          props.map(p => '<div class="sug-row"><b>' + Fastt.esc(p.label || '—') + '</b>'
            + '<span>' + Fastt.esc(String(p.kind || '')) + (p.price_cents ? ' · ' + money(p.price_cents) : '')
            + '</span><span class="sug-why">' + Fastt.esc(String(p.description_for_ai || '')) + '</span></div>').join('')
          + '<div class="sug-empty" style="margin-top:10px">Nothing is saved. Add the ones you '
          + 'want as catalog rows in <a class="plink" href="vault-ai.html">Vault AI</a>.</div>');
      } else {
        // Ask the free sibling for the shot-list so a blocked run still says something useful.
        let extra = '';
        try {
          const t = await Fastt.get('/admin/catalog/singles/suggest-texts');
          const shoot = t.shoot || [];
          if (shoot.length) extra = '<div class="sug-h" style="margin-top:12px">What she would '
            + 'need to shoot</div>' + shoot.map(x => '<div class="sug-row"><b>'
            + Fastt.esc(x.label) + '</b><span>' + money(x.price_cents) + '</span>'
            + '<span class="sug-why">' + Fastt.esc(String(x.why || '')) + '</span></div>').join('');
        } catch (_) { /* the shot-list is a bonus, not the answer */ }
        sugShow('🪄 Nothing to write',
          '<div class="sug-empty">' + Fastt.esc(String(r.blocked
            || 'The vault has no unsold described media to write a line about.'))
          + '</div>' + extra);
      }
    } catch (e) { Fastt.oops(e); }
    finally { btn.disabled = false; btn.textContent = '🪄 Write lines from vault'; }
  });

  document.addEventListener('click', (e) => {
    const sw = e.target.closest('#psf-sw');
    if (sw) saveCfg({ proven_spend_floor_enabled: sw.classList.contains('on') });
  });
  document.addEventListener('change', (e) => {
    const t = e.target;
    if (t.id === 'esc-mult') {
      const v = num(t); if (v === null) render(); else saveCfg({ escalation_mult: v });
      return;
    }
    if (t.id === 'esc-hist') {
      const v = num(t); if (v === null) render(); else saveCfg({ max_ask_history_mult: v });
      return;
    }
    if (t.dataset && t.dataset.slot) {
      const slot = t.dataset.slot;
      const line = t.value.trim();
      const ov = { ...(eff().script_pack_overrides || {}) };
      let extra = [];
      try { extra = JSON.parse(t.dataset.extraLines || '[]'); } catch (_) { extra = []; }
      if (!Array.isArray(extra)) extra = [];
      // Lines 2..n of a stored override are not editable here — carry them through
      // verbatim so editing line 1 never truncates the slot's set.
      if (!line) ov[slot] = extra;
      else if (line === t.dataset.shippedFirst && !extra.length) delete ov[slot];
      else ov[slot] = [line].concat(extra);
      if (Array.isArray(ov[slot]) && !ov[slot].length) delete ov[slot];
      saveCfg({ script_pack_overrides: ov });
    }
  });
});
