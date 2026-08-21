// ==== live wiring: real feed post via POST /api/of/v2/posts (server.py _CreatePostBody) ====
// Grounded keys: text · media_files · price · previews (wire field `preview`) ·
// posted_at · tagged_users. The story lane is POST /api/of/v2/stories {media_url}
// (server _CreateStoryBody → of_client.post_story_from_url) — the same flow the
// auto_stories automation uses. Vault reads use `list_id`; `list` is ignored by
// the route, which is why the folder dropdown used to be a no-op.
Fastt.ready(async function () {
  var $ = Fastt.$, esc = Fastt.esc;
  var media = [];   // [{id, url, full, type}]
  var tagged = [];  // [{id, name}]

  // account label
  var row = Fastt.accountRow();
  $('#acct-name').textContent = row ? (row.nickname || row.id) : (Fastt.account() || '—');

  // The relay's create-post body has no expiry field — honest empty state, not a knob.
  Fastt.staticBadge($('#grp-expire').querySelector('h4'), 'NO BACKEND');

  Fastt.liveBadge($('.fx-card-h'));
  Fastt.liveBadge($('#recent-h'));
  Fastt.liveBadge($('#grp-tags').querySelector('h4'));
  Fastt.liveBadge($('#ck-story'));
  Fastt.liveBadge($('#grp-broadcast').querySelector('h4'));

  // ── broadcast to all models (Advanced) ─────────────────────
  // Real multi-account fan-out: one POST /api/of/v2/posts per included creator,
  // with a per-account X-Account-Id override. Text-only + free (vault ids and
  // priced posts don't cross accounts) — mirrors the app's all-models mode.
  var bcIncluded = {}, bcBuilt = false;
  function bcAccountIds() {
    return Fastt.accounts().map(function (a) { return String(a.id); })
      .filter(function (id) { return bcIncluded[id]; });
  }
  function buildBcChips() {
    var box = $('#bc-accounts'); box.innerHTML = '';
    var accts = Fastt.accounts();
    if (!accts.length) {
      box.innerHTML = '<span style="font-size:12.5px;color:var(--muted)">No creator roster loaded — sign in to broadcast.</span>';
      return;
    }
    accts.forEach(function (a) {
      var id = String(a.id);
      if (!(id in bcIncluded)) bcIncluded[id] = true;
      var c = document.createElement('span');
      c.className = 'fx-chip' + (bcIncluded[id] ? ' on' : '');
      c.dataset.aid = id;
      c.textContent = a.nickname || id;
      box.appendChild(c);
    });
  }
  $('#bc-accounts').addEventListener('click', function (e) {
    e.stopPropagation(); // own the .on class — keep fx-js out
    var c = e.target.closest('.fx-chip'); if (!c || !c.dataset.aid) return;
    bcIncluded[c.dataset.aid] = !bcIncluded[c.dataset.aid];
    c.classList.toggle('on', bcIncluded[c.dataset.aid]);
    updateBcLabel();
  });
  function broadcastOn() { return $('#sw-broadcast').classList.contains('on'); }
  function updateBcLabel() {
    var on = broadcastOn(), btn = $('#btn-post'), sch = $('#btn-schedule');
    if (on) {
      var n = bcAccountIds().length;
      btn.textContent = 'Post to ' + n + ' model' + (n === 1 ? '' : 's');
      sch.disabled = true; sch.style.opacity = '.4';
    } else {
      btn.textContent = 'Post';
      sch.disabled = false; sch.style.opacity = '';
    }
  }
  $('#sw-broadcast').addEventListener('click', function () {
    setTimeout(function () {
      if (broadcastOn() && !bcBuilt) { buildBcChips(); bcBuilt = true; }
      $('#bc-accounts').style.display = broadcastOn() ? 'flex' : 'none';
      updateBcLabel();
    }, 0);
  });
  async function fireBroadcast(text, ids) {
    var box = $('#bc-results'), results = [];
    for (var i = 0; i < ids.length; i++) {
      box.innerHTML = '<div style="font-size:12.5px;color:var(--muted);margin-top:10px">Posting to ' + i + ' / ' + ids.length + '…</div>';
      try {
        await Fastt.post('/api/of/v2/posts',
          { text: text, media_files: [], price: 0, previews: [] },
          { noAccount: true, headers: { 'X-Account-Id': ids[i] } });
        results.push({ id: ids[i], ok: true });
      } catch (e) {
        results.push({ id: ids[i], ok: false, err: (e.body && e.body.detail) || e.message });
      }
    }
    var ok = results.filter(function (r) { return r.ok; }).length;
    var rows = results.map(function (r) {
      var a = Fastt.accounts().find(function (x) { return String(x.id) === String(r.id); });
      var nm = a ? (a.nickname || a.id) : r.id;
      return '<div style="font-size:12px;color:' + (r.ok ? '#67d1ae' : '#e05b5b') + '">' +
        (r.ok ? '✓' : '✗') + ' ' + esc(nm) + (r.err ? ' — ' + esc(String(r.err).slice(0, 60)) : '') + '</div>';
    }).join('');
    box.innerHTML = '<div class="fx-note' + (ok === results.length ? '' : ' warn') +
      '" style="margin-top:12px;flex-direction:column;align-items:stretch;gap:6px">' +
      '<div style="font-weight:600">Broadcast complete — ' + ok + '/' + results.length + ' succeeded</div>' + rows + '</div>';
    Fastt.saved('Broadcast: ' + ok + '/' + results.length + ' posted');
    loadRecent(); loadChart();
  }

  // ── media tray ─────────────────────────────────────────────
  function previewVal() {
    var v = parseInt($('#in-preview').value, 10);
    if (!isFinite(v) || v < 0) v = 0;
    return Math.min(v, media.length);
  }
  function renderTray() {
    var tray = $('#media-tray'), add = $('#btn-vault');
    tray.querySelectorAll('.thumb:not(.addt)').forEach(function (t) { t.remove(); });
    var free = priceVal() > 0 ? previewVal() : 0;
    var paid = priceVal() > 0;
    media.forEach(function (m, i) {
      var d = document.createElement('div');
      d.className = 'thumb';
      d.draggable = media.length > 1;
      d.dataset.idx = i;
      if (media.length > 1) d.style.cursor = 'grab';
      // assign through CSSOM — an OF CDN url inside an HTML style="" attribute
      // gets cut in half by its own quotes.
      if (m.url) d.style.backgroundImage = 'url(' + JSON.stringify(m.url) + ')';
      d.innerHTML = (m.type === 'video' ? '<span class="vid"><svg width="8" height="8" viewBox="0 0 24 24" fill="currentColor"><path d="M7 5l12 7-12 7z"/></svg>video</span>' : '') +
        (paid ? (i < free ? '<span class="pv">FREE</span>' : '<span class="pv" style="background:rgba(236,75,155,.9);color:#fff">🔒</span>') : '') +
        '<span class="rm" data-id="' + m.id + '">×</span>';
      tray.insertBefore(d, add);
    });
    $('#media-count').textContent = media.length
      ? media.length + ' media attached' + (media.length > 1 ? ' · drag to reorder' : '')
      : 'no media attached';
    renderHint();
  }
  $('#media-tray').addEventListener('click', function (e) {
    var rm = e.target.closest('.rm');
    if (rm) { media = media.filter(function (m) { return String(m.id) !== rm.dataset.id; }); renderTray(); }
  });
  // drag-to-reorder — the leading tiles are the free previews, so order matters
  var dragFrom = null;
  $('#media-tray').addEventListener('dragstart', function (e) {
    var t = e.target.closest('.thumb:not(.addt)');
    if (!t) return;
    dragFrom = Number(t.dataset.idx);
    e.dataTransfer.effectAllowed = 'move';
    try { e.dataTransfer.setData('text/plain', String(dragFrom)); } catch (x) {}
  });
  $('#media-tray').addEventListener('dragover', function (e) {
    if (dragFrom === null) return;
    e.preventDefault(); e.dataTransfer.dropEffect = 'move';
  });
  $('#media-tray').addEventListener('drop', function (e) {
    if (dragFrom === null) return;
    e.preventDefault();
    var t = e.target.closest('.thumb:not(.addt)');
    var to = t ? Number(t.dataset.idx) : media.length - 1;
    if (to !== dragFrom && dragFrom >= 0 && dragFrom < media.length) {
      var moved = media.splice(dragFrom, 1)[0];
      media.splice(to, 0, moved);
      renderTray(); renderStoryHint();
    }
    dragFrom = null;
  });
  $('#media-tray').addEventListener('dragend', function () { dragFrom = null; });

  function priceVal() {
    var v = parseFloat($('#in-price').value);
    return isFinite(v) && v > 0 ? Math.round(v * 100) / 100 : 0;
  }
  function renderHint() {
    var p = priceVal(), free = p > 0 ? previewVal() : 0;
    $('#free-tag').innerHTML = p > 0
      ? '<i></i>Paid post — media locks until a fan pays $' + p
      : '<i></i>Free post — everyone sees the media';
    $('#prev-field').style.opacity = p > 0 ? '1' : '.45';
    $('#in-preview').disabled = p <= 0;
    $('#prev-hint').innerHTML = p > 0
      ? (free > 0
        ? '<span>The first <b>' + free + '</b> attachment' + (free === 1 ? '' : 's') +
          ' ride' + (free === 1 ? 's' : '') + ' unlocked as the teaser; the other ' +
          Math.max(0, media.length - free) + ' stay behind the $' + p + ' paywall. ' +
          '(sent as <code>previews</code> → OF <code>preview</code>)</span>'
        : '<span style="color:var(--muted2)">No teaser — every attachment is paywalled. Fans buy blind.</span>')
      : '<span style="color:var(--muted2)">Free preview only applies once the price is above $0.</span>';
    $('#foot-hint').textContent = (p > 0 ? '$' + p + ' post' : 'Free post') + ' · ' +
      (media.length ? media.length + ' media' : 'no media') +
      (p > 0 && free > 0 ? ' · ' + free + ' free' : '') +
      (tagged.length ? ' · ' + tagged.length + ' tagged' : '') +
      ($('#ck-story').classList.contains('on') ? ' · + story' : '') + ' · feed only';
  }
  $('#in-price').addEventListener('input', function () { renderTray(); });
  $('#in-preview').addEventListener('input', function () { renderTray(); });
  $('#ck-story').addEventListener('click', function () { setTimeout(renderStoryHint, 0); });
  function renderStoryHint() {
    var on = $('#ck-story').classList.contains('on');
    var first = media[0];
    $('#ck-story-sub').textContent = on
      ? (first && first.full
        ? 'the first attachment also goes up as a story, immediately, via POST /stories'
        : 'attach a vault photo first — the story lane needs the item’s full-size url')
      : 'first photo also goes up as a story, right away';
    renderHint();
  }

  // ── vault picker (folders → thumbnails → multi-select) ─────
  // Folder-first navigation, mirroring messages.html's openVault: show the
  // creator's folders from GET /api/of/v2/vault/lists first, drill into one to
  // GET /api/of/v2/vault/media?list_id=<id>, with an "All media" shortcut and a
  // back-to-folders breadcrumb. Selection persists across folder navigation.
  var pickerFolders = null;
  function openPicker() {
    var back = document.createElement('div');
    back.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,.6);z-index:9998;display:flex;align-items:center;justify-content:center';
    back.innerHTML =
      '<div style="background:#262626;border:1px solid #333;border-radius:12px;padding:18px;width:560px;max-height:80vh;display:flex;flex-direction:column;gap:12px;font:13px Inter,sans-serif;color:#fff">' +
        '<div style="display:flex;align-items:center;gap:10px;min-height:20px"><b style="font-size:15px">Add from vault</b>' +
          '<div id="pk-crumb" style="margin-left:auto"></div></div>' +
        '<div id="pk-body" style="flex:1;overflow-y:auto;min-height:140px"></div>' +
        '<div style="display:flex;gap:10px;justify-content:flex-end">' +
          '<button class="fx-btn ghost" id="pk-cancel">Cancel</button>' +
          '<button class="fx-btn" id="pk-add">Add selected</button></div></div>';
    document.body.appendChild(back);
    var picked = {};   // media id -> {id,url,full,type}, persists across folders
    function close() { back.remove(); }
    back.addEventListener('click', function (e) { if (e.target === back) close(); });
    $('#pk-cancel', back).addEventListener('click', close);
    $('#pk-add', back).addEventListener('click', function () {
      Object.values(picked).forEach(function (m) {
        if (!media.some(function (x) { return x.id === m.id; })) media.push(m);
      });
      close(); renderTray(); renderStoryHint();
    });
    function updAddBtn() {
      var n = Object.keys(picked).length;
      $('#pk-add', back).textContent = n ? 'Add selected (' + n + ')' : 'Add selected';
    }

    // MEDIA view — listId null ⇒ "All media" (flat), else that folder's media.
    async function showMedia(listId, label) {
      $('#pk-crumb', back).innerHTML =
        '<span id="pk-back" style="cursor:pointer;color:#8a8a8a;display:inline-flex;align-items:center;gap:4px">' +
        '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M15 18l-6-6 6-6"/></svg>Folders</span>' +
        '<span style="color:#555;margin:0 6px">/</span><span style="color:#fff">' + esc(label) + '</span>';
      $('#pk-back', back).addEventListener('click', showFolders);
      var body = $('#pk-body', back);
      body.innerHTML = '<div style="color:#8a8a8a;padding:6px 2px">Loading media…</div>';
      try {
        var params = { limit: 48, offset: 0 };
        // the route parameter is `list_id` — `list` is silently dropped, which
        // made every folder render the same all-recent page.
        if (listId) params.list_id = listId;
        var out = await Fastt.get('/api/of/v2/vault/media', params);
        var items = out.list || [];
        if (!items.length) {
          body.innerHTML = '<div style="color:#8a8a8a;padding:14px 2px">No media here — ' +
            (listId ? 'GET /api/of/v2/vault/media?list_id=' + esc(listId) + ' returned 0 items.'
                    : 'GET /api/of/v2/vault/media returned 0 items.') + '</div>';
          return;
        }
        var grid = document.createElement('div');
        grid.style.cssText = 'display:grid;grid-template-columns:repeat(6,1fr);gap:8px';
        items.forEach(function (it) {
          var f = it.files || {};
          var url = (f.squarePreview && f.squarePreview.url) || (f.thumb && f.thumb.url) || '';
          var full = (f.full && f.full.url) || '';
          var cell = document.createElement('div');
          cell.style.cssText = 'aspect-ratio:1;border-radius:8px;border:2px solid ' + (picked[it.id] ? '#4166f6' : 'transparent') + ';cursor:pointer;background:#1c1c1c;background-size:cover;background-position:center;position:relative';
          if (url) cell.style.backgroundImage = 'url(' + JSON.stringify(url) + ')';
          if (it.type === 'video') cell.innerHTML = '<span style="position:absolute;top:4px;left:4px;background:rgba(0,0,0,.55);border-radius:4px;padding:1px 5px;font-size:9px;font-weight:700">VID</span>';
          cell.addEventListener('click', function () {
            if (picked[it.id]) { delete picked[it.id]; cell.style.borderColor = 'transparent'; }
            else { picked[it.id] = { id: it.id, url: url, full: full, type: it.type }; cell.style.borderColor = '#4166f6'; }
            updAddBtn();
          });
          grid.appendChild(cell);
        });
        body.innerHTML = '';
        body.appendChild(grid);
      } catch (e) {
        body.innerHTML = '<div style="color:#e05b5b;padding:14px 2px">Vault media unavailable — GET /api/of/v2/vault/media returned an error.</div>';
        Fastt.oops(e);
      }
    }

    // FOLDER view — the creator's folders + an "All media" shortcut.
    async function showFolders() {
      $('#pk-crumb', back).innerHTML = '';
      var body = $('#pk-body', back);
      body.innerHTML = '<div style="color:#8a8a8a;padding:6px 2px">Loading folders…</div>';
      if (!pickerFolders) {
        try { var vl = await Fastt.get('/api/of/v2/vault/lists', { limit: 50 }); pickerFolders = vl.list || []; }
        catch (e) {
          body.innerHTML = '<div style="color:#e05b5b;padding:14px 2px">Vault folders unavailable — GET /api/of/v2/vault/lists returned an error.</div>';
          Fastt.oops(e); return;
        }
      }
      var chev = '<span style="color:#5a5a5a;margin-left:auto;display:inline-flex"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M9 6l6 6-6 6"/></svg></span>';
      var wrap = document.createElement('div');
      wrap.style.cssText = 'display:flex;flex-direction:column;gap:6px';
      function row(html) {
        var d = document.createElement('div');
        d.style.cssText = 'display:flex;align-items:center;gap:11px;padding:10px 12px;border-radius:9px;border:1px solid #333;background:#1f1f1f;cursor:pointer';
        d.onmouseenter = function () { d.style.background = '#2a2a2a'; };
        d.onmouseleave = function () { d.style.background = '#1f1f1f'; };
        d.innerHTML = html;
        return d;
      }
      // "All media" — the original flat view.
      var allRow = row(
        '<span style="width:34px;height:34px;border-radius:8px;background:#2b2b2b;display:inline-flex;align-items:center;justify-content:center;color:#8a8a8a">' +
        '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/></svg></span>' +
        '<span style="display:flex;flex-direction:column"><span style="font-weight:600">All recent media</span>' +
        '<span style="color:#8a8a8a;font-size:11.5px">Everything in the vault</span></span>' + chev);
      allRow.addEventListener('click', function () { showMedia(null, 'All recent media'); });
      wrap.appendChild(allRow);
      if (!pickerFolders.length) {
        var note = document.createElement('div');
        note.style.cssText = 'color:#8a8a8a;padding:12px 2px';
        note.textContent = 'No custom folders on this account — use “All recent media” above to pick from the full vault.';
        wrap.appendChild(note);
      }
      pickerFolders.forEach(function (fd) {
        var parts = [];
        if (fd.photosCount) parts.push(fd.photosCount + ' photo' + (fd.photosCount === 1 ? '' : 's'));
        if (fd.videosCount) parts.push(fd.videosCount + ' video' + (fd.videosCount === 1 ? '' : 's'));
        if (fd.gifsCount) parts.push(fd.gifsCount + ' gif' + (fd.gifsCount === 1 ? '' : 's'));
        if (fd.audiosCount) parts.push(fd.audiosCount + ' audio');
        var count = parts.length ? parts.join(' · ') : 'empty';
        var name = fd.name || 'Untitled folder';
        var r = row(
          '<span style="width:34px;height:34px;border-radius:8px;background:#2b2b2b;display:inline-flex;align-items:center;justify-content:center;color:#ec4b9b">' +
          '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/></svg></span>' +
          '<span style="display:flex;flex-direction:column;min-width:0"><span style="font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">' + esc(name) + '</span>' +
          '<span style="color:#8a8a8a;font-size:11.5px">' + esc(count) + '</span></span>' + chev);
        r.addEventListener('click', function () { showMedia(fd.id, name); });
        wrap.appendChild(r);
      });
      body.innerHTML = '';
      body.appendChild(wrap);
    }

    showFolders();
  }
  $('#btn-vault').addEventListener('click', openPicker);
  $('#ci-vault').addEventListener('click', openPicker);

  // ── caption helpers: insert text at the textarea caret ─────
  function insertAtCaret(el, str) {
    el.focus();
    var s = el.selectionStart, e = el.selectionEnd, v = el.value;
    el.value = v.slice(0, s) + str + v.slice(e);
    var pos = s + str.length;
    el.setSelectionRange(pos, pos);
    renderHint();
  }

  // ── emoji picker (client-side only — no backend, and none needed) ──
  var EMOJI = ['😍','🥰','😘','😏','😈','🥵','🔥','💦','🍑','🍆','💋','👅','😜','😉','🤤','🥺','💕','❤️','💗','💖','✨','🌶️','👀','🙈','🎁','💸','💰','🤑','🛒','⏰','🔓','📸','🎥','💬','👑','🦋','🌙','☀️','😅','😂','🤭','😳','🥴','😩','🫦','💅','🩷'];
  function openEmoji() {
    var anchor = $('#ci-emoji');
    var pop = document.createElement('div');
    pop.style.cssText = 'position:absolute;z-index:9997;background:#1b1b1d;border:1px solid #333;border-radius:12px;padding:10px;width:296px;box-shadow:0 12px 34px rgba(0,0,0,.55);display:grid;grid-template-columns:repeat(9,1fr);gap:2px';
    EMOJI.forEach(function (em) {
      var b = document.createElement('button');
      b.type = 'button'; b.textContent = em;
      b.style.cssText = 'border:0;background:transparent;font-size:19px;line-height:1;padding:6px 0;border-radius:7px;cursor:pointer';
      b.onmouseenter = function(){ b.style.background = '#2a2a2a'; };
      b.onmouseleave = function(){ b.style.background = 'transparent'; };
      b.addEventListener('click', function () { insertAtCaret($('#post-text'), em); });
      pop.appendChild(b);
    });
    var r = anchor.getBoundingClientRect();
    document.body.appendChild(pop);
    pop.style.left = Math.min(r.left, window.innerWidth - 306) + 'px';
    pop.style.top = (r.bottom + 6 + window.scrollY) + 'px';
    var close = function (e) { if (pop.contains(e.target) || anchor.contains(e.target)) return; pop.remove(); document.removeEventListener('click', close); };
    setTimeout(function () { document.addEventListener('click', close); }, 0);
  }
  $('#ci-emoji').addEventListener('click', function (e) { e.stopPropagation(); openEmoji(); });

  // ── saved templates (GET /api/of/v2/messages/templates) ────
  // Real backend — the same OF message-template store the app's TemplatePicker
  // reads. Picking one fills the caption (and price/previews if it carries them).
  Fastt.liveBadge($('#ci-template'));
  var stripHtml = function (h) {
    var d = document.createElement('div'); d.innerHTML = String(h || '');
    return (d.textContent || '').replace(/ /g, ' ').trim();
  };
  async function openTemplates() {
    var anchor = $('#ci-template');
    var pop = document.createElement('div');
    pop.style.cssText = 'position:absolute;z-index:9997;background:#1b1b1d;border:1px solid #333;border-radius:12px;padding:8px;width:340px;max-height:340px;overflow-y:auto;box-shadow:0 12px 34px rgba(0,0,0,.55);font:13px Inter,sans-serif;color:#fff';
    pop.innerHTML = '<div style="font-size:10.5px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;color:#6a6a6a;padding:6px 8px 8px">Saved templates</div><div id="tpl-list" style="color:#8a8a8a;padding:4px 8px">Loading…</div>';
    var r = anchor.getBoundingClientRect();
    document.body.appendChild(pop);
    pop.style.left = Math.min(r.left, window.innerWidth - 350) + 'px';
    pop.style.top = (r.bottom + 6 + window.scrollY) + 'px';
    var close = function (e) { if (pop.contains(e.target) || anchor.contains(e.target)) return; pop.remove(); document.removeEventListener('click', close); };
    setTimeout(function () { document.addEventListener('click', close); }, 0);
    try {
      var list = await Fastt.get('/api/of/v2/messages/templates');
      if (!Array.isArray(list)) list = list.list || [];
      // welcome/reply_on_subscribe is a settings template, not a feed caption
      list = list.filter(function (t) { return t.template !== 'reply_on_subscribe'; });
      var box = $('#tpl-list', pop);
      if (!list.length) { box.textContent = 'No saved templates on this account yet.'; box.style.color = '#8a8a8a'; return; }
      box.innerHTML = ''; box.style.color = '#fff';
      list.forEach(function (t) {
        var txt = stripHtml(t.displayText || t.text);
        var row = document.createElement('div');
        row.style.cssText = 'padding:9px 10px;border-radius:9px;cursor:pointer;border:1px solid transparent';
        row.onmouseenter = function(){ row.style.background = '#242424'; };
        row.onmouseleave = function(){ row.style.background = 'transparent'; };
        row.innerHTML = '<div style="font-size:13px;color:#e6e6e6;line-height:1.4;overflow:hidden;text-overflow:ellipsis;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical">' +
          (esc(txt) || '<span style="color:#8a8a8a">(no text)</span>') + '</div>' +
          '<div style="font-size:11px;color:#8a8a8a;margin-top:3px">' +
          (Number(t.price) > 0 ? '$' + t.price + ' · ' : '') +
          (t.mediaCount || 0) + ' media' + (t.mediaCount ? ' (add manually from vault)' : '') + '</div>';
        row.addEventListener('click', function () {
          $('#post-text').value = txt;
          if (Number(t.price) > 0) $('#in-price').value = t.price;
          if (Array.isArray(t.previews) && t.previews.length) $('#in-preview').value = t.previews.length;
          pop.remove(); document.removeEventListener('click', close);
          renderTray();
          Fastt.toast('Template loaded into caption' + (t.mediaCount ? ' — media not attached (pick from vault)' : ''), 'ok');
        });
        box.appendChild(row);
      });
    } catch (e) {
      $('#tpl-list', pop).innerHTML = '<span style="color:#e05b5b">Could not load templates.</span>';
    }
  }
  $('#ci-template').addEventListener('click', function (e) { e.stopPropagation(); openTemplates(); });

  // ── tag friends (GET /api/of/v2/posts/tagged-friend-users) ──
  function renderChips(sugg) {
    var box = $('#tag-chips');
    box.innerHTML = '';
    tagged.forEach(function (t) {
      var s = document.createElement('span');
      s.className = 'fx-chip on'; s.dataset.tid = t.id;
      s.innerHTML = esc(t.name) + ' <span class="x">×</span>';
      box.appendChild(s);
    });
    (sugg || []).forEach(function (u) {
      var id = (u.user && u.user.id) || u.id;
      if (tagged.some(function (t) { return t.id === id; })) return;
      var name = (u.user && (u.user.name || u.user.username)) || u.name || ('user' + id);
      var s = document.createElement('span');
      s.className = 'fx-chip'; s.dataset.tid = id; s.dataset.tname = name;
      s.textContent = '+ ' + name;
      box.appendChild(s);
    });
    if (!box.childNodes.length) {
      box.innerHTML = '<span style="font-size:12.5px;color:var(--muted)">No taggable creators found' +
        ($('#tag-search').value ? ' for this search.' : ' — this account follows no taggable friends.') + '</span>';
    }
    renderHint();
  }
  var lastSugg = [];
  async function searchTags() {
    try {
      var params = { limit: 20, offset: 0 };
      var q = $('#tag-search').value.trim();
      if (q) params.search = q;
      var out = await Fastt.get('/api/of/v2/posts/tagged-friend-users', params);
      lastSugg = out.items || out.list || [];
      renderChips(lastSugg);
    } catch (e) { $('#tag-chips').innerHTML = '<span style="font-size:12.5px;color:#e05b5b">Tag search failed.</span>'; }
  }
  $('#tag-search').addEventListener('input', Fastt.debounce(searchTags, 350));
  $('#tag-chips').addEventListener('click', function (e) {
    e.stopPropagation(); // own the .on class — keep fx-js out
    var chip = e.target.closest('.fx-chip');
    if (!chip || !chip.dataset.tid) return;
    var id = Number(chip.dataset.tid);
    if (chip.classList.contains('on')) tagged = tagged.filter(function (t) { return t.id !== id; });
    else tagged.push({ id: id, name: chip.dataset.tname || chip.textContent.replace(/^\+\s*/, '') });
    renderChips(lastSugg);
  });
  searchTags();

  // ── posts-per-day sparkline (GET /api/of/v2/posts/chart) ───
  async function loadChart() {
    var svg = $('.pchart-svg');
    var NS = 'http://www.w3.org/2000/svg';
    try {
      var out = await Fastt.get('/api/of/v2/posts/chart');
      var series = ((out.posts || {}).chart) || out.chart || [];
      if (!Array.isArray(series) || !series.length) {
        $('#post-chart').innerHTML = '<div style="font-size:12.5px;color:var(--muted)">' +
          'OnlyFans returned no posts-per-day series for this account — nothing to draw.</div>';
        Fastt.staticBadge($('#post-chart'), 'NO DATA');
        return;
      }
      var counts = series.map(function (d) { return Number(d.count) || 0; });
      var max = Math.max.apply(null, counts) || 1;
      var total = counts.reduce(function (a, b) { return a + b; }, 0);
      // geometry computed from the data: one column per returned day
      var W = 300, H = 60, PLOT = 54, n = series.length;
      var slot = W / n, bw = Math.max(1.5, slot * 0.72);
      svg.setAttribute('viewBox', '0 0 ' + W + ' ' + H);
      svg.innerHTML = '';
      var base = document.createElementNS(NS, 'line');
      base.setAttribute('x1', 0); base.setAttribute('x2', W);
      base.setAttribute('y1', PLOT + 0.5); base.setAttribute('y2', PLOT + 0.5);
      base.setAttribute('stroke', '#333'); base.setAttribute('stroke-width', '1');
      base.setAttribute('vector-effect', 'non-scaling-stroke');
      svg.appendChild(base);
      series.forEach(function (d, i) {
        var c = Number(d.count) || 0;
        var h = c > 0 ? Math.max(3, (c / max) * PLOT) : 2;
        var r = document.createElementNS(NS, 'rect');
        r.setAttribute('x', (i * slot + (slot - bw) / 2).toFixed(2));
        r.setAttribute('y', (PLOT - h).toFixed(2));
        r.setAttribute('width', bw.toFixed(2));
        r.setAttribute('height', h.toFixed(2));
        r.setAttribute('rx', '1');
        r.setAttribute('fill', c > 0 ? '#4166f6' : '#3a3a3a');
        var t = document.createElementNS(NS, 'title');
        t.textContent = String(d.date || '').slice(0, 10) + ' · ' + c + ' post' + (c === 1 ? '' : 's');
        r.appendChild(t);
        svg.appendChild(r);
      });
      var fmtD = function (s) {
        var dt = Fastt.parseUtc(s);
        return dt ? dt.toLocaleDateString('en-US', { month: 'short', day: 'numeric', timeZone: 'UTC' })
          : String(s).slice(0, 10);
      };
      $('#pchart-x0').textContent = fmtD(series[0].date);
      $('#pchart-y').textContent = 'max ' + max + '/day';
      $('#pchart-x1').textContent = fmtD(series[n - 1].date);
      $('#pchart-sub').textContent = total + ' post' + (total === 1 ? '' : 's') + ' over ' +
        n + ' days · ' + counts.filter(function (c) { return c > 0; }).length + ' active days';
    } catch (e) {
      $('#post-chart').innerHTML = '<div style="font-size:12.5px;color:var(--muted)">' +
        'Posts-per-day chart unavailable — OF /posts/chart did not answer for this account.</div>';
      Fastt.staticBadge($('#post-chart'), 'UNAVAILABLE');
    }
  }
  loadChart();

  // ── recent posts (GET /api/of/v2/posts) ────────────────────
  async function loadRecent() {
    var box = $('#recent-posts');
    try {
      var posts = await Fastt.get('/api/of/v2/posts', { limit: 8 });
      if (!Array.isArray(posts)) posts = posts.list || [];
      if (!posts.length) {
        box.innerHTML = '<div style="font-size:13px;color:var(--muted);padding:6px 0">No posts on the feed right now — Auto Posts deletes each drop after its keep-for window, so an empty feed here is normal.</div>';
        return;
      }
      box.innerHTML = '';
      posts.forEach(function (p) {
        var m = (p.media || [])[0] || {};
        var f = m.files || {};
        var url = (f.squarePreview && f.squarePreview.url) || (f.thumb && f.thumb.url) || (f.preview && f.preview.url) || '';
        var cap = (p.rawText || p.text || '').replace(/<[^>]*>/g, '').trim() || '(no caption)';
        var priceRaw = (typeof p.price === 'number') ? p.price : parseFloat(String(p.price || '').replace(/[^0-9.]/g, ''));
        var rowEl = document.createElement('div');
        rowEl.className = 'rrow';
        rowEl.innerHTML =
          '<span class="rth">' + (url ? '' : (m.type === 'video' ? 'VIDEO' : (p.mediaCount ? 'NO PIC' : 'TEXT'))) + '</span>' +
          '<span class="rcap">' + esc(cap) + '</span>' +
          (isFinite(priceRaw) && priceRaw > 0 ? '<span class="rprice">$' + priceRaw + '</span>' : '') +
          '<span class="rmeta">' + (p.mediaCount || 0) + ' media · ' + Fastt.fmtAgo(p.postedAt) + '</span>';
        box.appendChild(rowEl);
        // ← the whole point: paint through CSSOM, never an HTML style attribute
        if (url) rowEl.querySelector('.rth').style.backgroundImage = 'url(' + JSON.stringify(url) + ')';
      });
    } catch (e) {
      box.innerHTML = '<div style="font-size:13px;color:#e05b5b;padding:6px 0">Could not load posts.</div>';
      Fastt.oops(e);
    }
  }
  loadRecent();

  // ── the actual POST — only ever on an explicit click + confirm ──
  function buildBody(postedAt) {
    var ids = media.map(function (m) { return m.id; });
    var price = priceVal();
    var free = price > 0 ? previewVal() : 0;
    var body = {
      text: $('#post-text').value.trim(),
      media_files: ids,
      price: price,
      // ⊆ media_files — of_client sends it as OF's `preview` (singular)
      previews: ids.slice(0, free)
    };
    if (tagged.length) body.tagged_users = tagged.map(function (t) { return t.id; });
    if (postedAt) body.posted_at = postedAt;
    return body;
  }
  function validate(body) {
    if (!body.text && !body.media_files.length) { Fastt.toast('Write a caption or attach media first', 'err'); return false; }
    if (body.price > 0 && !body.media_files.length) { Fastt.toast('A paid post must include media — OF rejects priced text-only posts', 'err'); return false; }
    if (body.previews.length && body.previews.length >= body.media_files.length) {
      Fastt.toast('Leave at least one attachment locked — a fully-previewed paid post has nothing to buy', 'err'); return false;
    }
    return true;
  }
  async function alsoStory() {
    var first = media[0];
    if (!first || !first.full) {
      Fastt.toast('Story skipped — the attachment carries no full-size url', 'err');
      return;
    }
    try {
      await Fastt.post('/api/of/v2/stories', { media_url: first.full });
      Fastt.saved('Story posted ✓');
    } catch (e) { Fastt.oops(e); }
  }
  async function firePost(body, verb, withStory) {
    try {
      var out = await Fastt.post('/api/of/v2/posts', body);
      Fastt.saved(verb + ' ✓ (post id ' + ((out && out.id) || '?') + ')');
      if (withStory) await alsoStory();
      media = []; tagged = [];
      $('#post-text').value = ''; $('#in-price').value = '0'; $('#in-preview').value = '0';
      $('#ck-story').classList.remove('on');
      renderTray(); renderStoryHint(); renderChips(lastSugg); loadRecent(); loadChart();
    } catch (e) { Fastt.oops(e); }
  }
  function storyLine(on) {
    return on ? '\n\nAlso posts the first attachment as a STORY, immediately.' : '';
  }
  $('#btn-post').addEventListener('click', function () {
    if (broadcastOn()) {
      var text = $('#post-text').value.trim();
      if (!text) { Fastt.toast('Broadcast is text-only — write a caption first', 'err'); return; }
      var ids = bcAccountIds();
      if (!ids.length) { Fastt.toast('No models selected for broadcast', 'err'); return; }
      if (!confirm('Post this caption to the LIVE feed of ' + ids.length + ' model' + (ids.length === 1 ? '' : 's') + '?\n\n"' +
        text.slice(0, 120) + '"\n\n(text-only · free · media & tags dropped)')) return;
      fireBroadcast(text, ids);
      return;
    }
    var body = buildBody(null);
    if (!validate(body)) return;
    var story = $('#ck-story').classList.contains('on');
    if (!confirm('Post to the LIVE OnlyFans feed now?\n\n"' + (body.text || '(no caption)').slice(0, 120) + '"\n' +
        body.media_files.length + ' media · ' + (body.price > 0 ? '$' + body.price : 'free') +
        (body.previews.length ? ' · ' + body.previews.length + ' shown free' : '') + storyLine(story))) return;
    firePost(body, 'Posted', story);
  });
  $('#btn-schedule').addEventListener('click', function () {
    var body = buildBody(null);
    if (!validate(body)) return;
    var def = new Date(Date.now() + 3600e3);
    var raw = prompt('Schedule for (YYYY-MM-DD HH:MM, your local time):',
      def.getFullYear() + '-' + String(def.getMonth() + 1).padStart(2, '0') + '-' + String(def.getDate()).padStart(2, '0') +
      ' ' + String(def.getHours()).padStart(2, '0') + ':' + String(def.getMinutes()).padStart(2, '0'));
    if (!raw) return;
    var d = new Date(raw.replace(' ', 'T'));
    if (isNaN(d.getTime()) || d.getTime() < Date.now()) { Fastt.toast('Bad or past date — nothing scheduled', 'err'); return; }
    body.posted_at = d.toISOString();
    var story = $('#ck-story').classList.contains('on');
    if (!confirm('Schedule this REAL post for ' + d.toLocaleString() + '?' +
        (story ? '\n\nThe STORY is not schedulable — it goes up NOW.' : ''))) return;
    firePost(body, 'Scheduled', story);
  });
  renderTray();
  renderStoryHint();
});
