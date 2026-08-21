// ==== live wiring: auto_stories automation rule (service/automations/auto_stories.py,
// consumer shape mirrors app/components/settings/AutoStoriesTab.tsx) ====
Fastt.ready(async function () {
  var $ = Fastt.$, esc = Fastt.esc;
  var X_SVG = '<span class="x"><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2"><path d="M6 6l12 12M18 6L6 18" stroke-linecap="round"/></svg></span>';
  var CLK_SVG = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9"><circle cx="12" cy="12" r="9"/><path d="M12 8v4l2.5 1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>';

  var rule = await Fastt.rule('auto_stories');
  var folders = [], folderNames = {};
  try {
    var vl = await Fastt.get('/api/of/v2/vault/lists', { limit: 50 });
    folders = vl.list || [];
    folders.forEach(function (f) { folderNames[f.id] = f.name; });
  } catch (e) { Fastt.oops(e); }

  // form state derived from the rule (mirrors AutoStoriesTab.ruleToForm)
  var form = { mode: 'interval', times: ['09:00'], everyHours: 6, perRun: 1, ttl: 6, maxRuns: null, folderId: null, watermark: '', mediaIds: [], removeVaultDupe: true };
  function ruleToForm() {
    var t = (rule && rule.trigger) || {}, p = (rule && rule.payload) || {};
    var clock = Array.isArray(t.daily_at) && t.daily_at.length > 0;
    form.mode = clock ? 'clock' : 'interval';
    form.times = clock ? t.daily_at.slice() : ['09:00'];
    form.everyHours = t.every_seconds ? Math.max(1, Math.round(t.every_seconds / 3600)) : 6;
    form.maxRuns = (typeof t.max_runs === 'number') ? t.max_runs : null;
    var fid = (p.folder_id != null) ? p.folder_id : p.media_folder_id;
    form.folderId = (fid != null) ? Number(fid) : null;
    form.perRun = (typeof p.per_run === 'number') ? p.per_run : ((typeof p.media_count === 'number') ? p.media_count : 1);
    form.ttl = (typeof p.hours_to_live === 'number') ? p.hours_to_live : 0;
    form.watermark = p.watermark_text || '';
    form.mediaIds = Array.isArray(p.media_files) ? p.media_files.map(Number) : [];
    // Default ON — only an explicit `false` opts a rule out (matches backend default).
    form.removeVaultDupe = p.remove_vault_dupe !== false;
  }
  if (rule) ruleToForm();

  function buildTrigger() {
    var mr = (form.maxRuns && form.maxRuns > 0) ? form.maxRuns : undefined;
    if (form.mode === 'clock') {
      return { daily_at: form.times, tz_offset_minutes: -new Date().getTimezoneOffset(), max_runs: mr };
    }
    return { every_seconds: Math.max(1, Math.round(form.everyHours)) * 3600, max_runs: mr };
  }
  function buildPayload() {
    var p = Object.assign({}, (rule && rule.payload) || {});
    delete p.media_folder_id;                    // one source of truth: folder_id
    p.folder_id = form.folderId;
    p.per_run = Math.max(1, Math.round(form.perRun) || 1);
    p.hours_to_live = Math.max(0, Math.round(form.ttl) || 0);
    p.remove_vault_dupe = !!form.removeVaultDupe;
    if (form.mediaIds.length) p.media_files = form.mediaIds.slice();
    else delete p.media_files;
    if (form.watermark.trim()) p.watermark_text = form.watermark.trim();
    else delete p.watermark_text;
    return p;
  }
  async function saveRule(body) {
    try {
      if (rule) {
        rule = await Fastt.patch('/admin/automation-rules/' + rule.id, body);
      } else {
        if (!form.folderId && !form.mediaIds.length) { Fastt.toast('Pick a vault folder first', 'err'); renderAll(); return; }
        rule = await Fastt.post('/admin/automation-rules', Object.assign({
          account_id: Fastt.account(), kind: 'auto_stories', name: 'Auto stories',
          is_enabled: false, trigger: buildTrigger(), payload: buildPayload()
        }, body));
      }
      Fastt.saved();
    } catch (e) { Fastt.oops(e); }
    if (rule) ruleToForm();
    renderAll();
  }
  var saveTrigger = function () { return saveRule({ trigger: buildTrigger() }); };
  var savePayload = function () { return saveRule({ payload: buildPayload() }); };

  function folderRow() {
    for (var i = 0; i < folders.length; i++) if (Number(folders[i].id) === form.folderId) return folders[i];
    return null;
  }
  function folderName() { var f = folderRow(); return f ? f.name : (form.folderId ? 'folder ' + form.folderId : 'no folder'); }
  function cadenceText() {
    return form.mode === 'clock' ? ('daily at ' + form.times.join(', ')) : ('every ' + form.everyHours + ' h');
  }
  function inHM(d) {
    var s = Math.max(0, (d.getTime() - Date.now()) / 1000);
    return 'in ' + Math.floor(s / 3600) + 'h ' + Math.floor((s % 3600) / 60) + 'm';
  }
  function dayLbl(d) {
    var now = new Date(), td = new Date(now.getFullYear(), now.getMonth(), now.getDate());
    var dd = new Date(d.getFullYear(), d.getMonth(), d.getDate());
    var diff = Math.round((dd - td) / 864e5);
    if (diff === 0) return 'Today';
    if (diff === 1) return 'Tomorrow';
    return d.toLocaleDateString([], { weekday: 'short', day: 'numeric' });
  }
  var hm = function (d) { return String(d.getHours()).padStart(2, '0') + ':' + String(d.getMinutes()).padStart(2, '0'); };

  function nextFires(n) {
    var out = [];
    if (!rule || !rule.is_enabled) return out;
    var now = new Date();
    if (form.mode === 'clock') {
      var times = form.times.slice().sort();
      for (var day = 0; out.length < n && day < 8; day++) {
        for (var k = 0; k < times.length && out.length < n; k++) {
          var p = times[k].split(':');
          var d = new Date(now.getFullYear(), now.getMonth(), now.getDate() + day, +p[0], +p[1]);
          if (d > now) out.push(d);
        }
      }
    } else {
      var everyMs = Math.max(1, form.everyHours) * 3600e3;
      var base = null;
      if (rule.next_due_at) {
        var s = rule.next_due_at;
        base = new Date(s.endsWith('Z') ? s : s + 'Z');
      }
      if (!base || base.getTime() < now.getTime()) base = new Date(now.getTime() + everyMs);
      for (var i = 0; i < n; i++) out.push(new Date(base.getTime() + i * everyMs));
    }
    return out;
  }

  // ── renders ────────────────────────────────────────────────
  function renderStatus() {
    var on = !!(rule && rule.is_enabled);
    var st = $('#st-run');
    st.classList.toggle('ok', on);
    st.innerHTML = '<i></i>' + (on ? 'Running' : (rule ? 'Off' : 'Not set up'));
    $('#st-next').innerHTML = '<i></i>' + (rule
      ? cadenceText() + (on && nextFires(1)[0] ? ' · next ' + hm(nextFires(1)[0]) : (on ? '' : ' · paused'))
      : 'no auto_stories rule yet');
    $('#st-ttl').innerHTML = '<i></i>' + (form.ttl > 0 ? 'Auto-delete after ' + form.ttl + ' h' : 'Stories kept (no auto-delete)');
    var f = folderRow();
    $('#st-pool').innerHTML = '<i></i>Pool: ' + esc(folderName()) +
      (f ? ' · ' + (f.photosCount || 0) + ' photos' : '') +
      (form.mediaIds.length ? ' + ' + form.mediaIds.length + ' picked' : '');
    $('#lbl-state').textContent = on ? 'Running' : 'Off';
    $('#lbl-state').style.color = on ? 'var(--green)' : 'var(--muted)';
    $('#sw-en').classList.toggle('on', on);
    $('#card-sub').textContent = rule
      ? (form.perRun + ' random photo' + (form.perRun === 1 ? '' : 's') + ' ' + cadenceText() + ' from “' + folderName() + '”, hands-free. ' +
         (form.ttl > 0 ? 'Each story removes itself after ' + form.ttl + ' hours.' : 'Stories stay until they expire naturally.'))
      : 'No auto_stories rule for this account yet — pick a folder and a cadence below to create one (it starts disabled).';
    $('#hint-mode').textContent = form.mode === 'clock' ? 'clock mode — this field is ignored' : 'interval mode';
    $('#in-every').value = form.everyHours;
    $('#in-perrun').value = form.perRun;
    $('#in-ttl').value = form.ttl;
    $('#in-maxruns').value = form.maxRuns || '';
    $('#in-watermark').value = form.watermark;
    $('#runnow-hint').textContent = 'Fires the saved rule once — ' + form.perRun + ' random photo' + (form.perRun === 1 ? '' : 's') + ', same auto-delete.';
  }

  function renderFolderSelect() {
    var sel = $('#sel-folder');
    sel.innerHTML = '';
    var withPhotos = folders.filter(function (f) { return (f.photosCount || 0) > 0; });
    if (!withPhotos.length) {
      sel.innerHTML = '<option value="">No vault folders with photos found</option>';
      return;
    }
    var opt0 = document.createElement('option');
    opt0.value = ''; opt0.textContent = '— pick a folder —';
    sel.appendChild(opt0);
    var seen = false;
    withPhotos.forEach(function (f) {
      var o = document.createElement('option');
      o.value = f.id; o.textContent = f.name + ' — ' + f.photosCount + ' photos';
      if (Number(f.id) === form.folderId) { o.selected = true; seen = true; }
      sel.appendChild(o);
    });
    if (form.folderId && !seen) {
      var o2 = document.createElement('option');
      o2.value = form.folderId; o2.textContent = folderName() + ' (current)'; o2.selected = true;
      sel.appendChild(o2);
    }
  }

  async function renderThumbs() {
    var box = $('#pool-thumbs');
    if (!form.folderId) { box.innerHTML = '<span class="pool-meta" style="color:var(--muted)">No folder picked yet.</span>'; return; }
    try {
      // the route parameter is `list_id` — passing `list` is silently ignored and
      // returns all-recent vault media, i.e. photos that are NOT in the pool.
      var out = await Fastt.get('/api/of/v2/vault/media', { list_id: form.folderId, limit: 6 });
      var items = (out.list || []).filter(function (m) { return m.files; });
      box.innerHTML = '';
      items.forEach(function (m) {
        var f = m.files || {};
        var url = (f.squarePreview && f.squarePreview.url) || (f.thumb && f.thumb.url) || '';
        var s = document.createElement('span');
        s.className = 'thumb';
        s.style.background = url ? ('url(' + JSON.stringify(url) + ') center/cover') : '#1c1c1c';
        box.appendChild(s);
      });
      var f0 = folderRow();
      var rest = f0 ? Math.max(0, (f0.photosCount || 0) - items.length) : 0;
      if (rest > 0) {
        var more = document.createElement('span');
        more.className = 'thumb more'; more.textContent = '+' + rest;
        box.appendChild(more);
      }
      if (!items.length) box.innerHTML = '<span class="pool-meta" style="color:var(--muted)">Folder is empty.</span>';
    } catch (e) { box.innerHTML = '<span class="pool-meta" style="color:#e05b5b">Could not load folder preview.</span>'; }
  }

  function renderTrigger() {
    // segmented buttons + blocks
    var seg = $('#trigseg');
    seg.querySelectorAll('button').forEach(function (b) {
      b.classList.toggle('on', (b.dataset.t === 'clock') === (form.mode === 'clock'));
    });
    $('#trig-clock').style.display = form.mode === 'clock' ? '' : 'none';
    $('#trig-int').style.display = form.mode === 'clock' ? 'none' : '';
    $('#trig-int').textContent = 'Using the ' + form.everyHours + '-hour cadence set above — one run per tick, ' +
      form.perRun + ' photo' + (form.perRun === 1 ? '' : 's') + ' per run.';
    // time chips
    var box = $('#timechips');
    box.innerHTML = '';
    form.times.forEach(function (t, i) {
      var chip = document.createElement('span');
      chip.className = 'tch';
      chip.innerHTML = CLK_SVG + esc(t) + X_SVG;
      chip.querySelector('.x').addEventListener('click', function (e) {
        e.stopPropagation();
        form.times.splice(i, 1);
        if (!form.times.length) form.times = ['09:00'];
        if (form.mode === 'clock') saveTrigger(); else renderTrigger();
      });
      box.appendChild(chip);
    });
    var add = document.createElement('span');
    add.className = 'tch add'; add.textContent = '+ Add time';
    add.addEventListener('click', function () {
      var raw = prompt('New time (HH:MM, 24h, creator-local):', '21:00');
      if (!raw) return;
      var m = /^(\d{1,2}):(\d{2})$/.exec(raw.trim());
      if (!m || +m[1] > 23 || +m[2] > 59) { Fastt.toast('Bad time — want HH:MM', 'err'); return; }
      form.times.push(String(+m[1]).padStart(2, '0') + ':' + m[2]);
      if (form.mode === 'clock') saveTrigger(); else renderTrigger();
    });
    box.appendChild(add);
  }

  function renderUpcoming() {
    var box = $('#upcoming');
    var fires = nextFires(4);
    if (!fires.length) {
      box.innerHTML = '<div class="uprow last"><div class="up-t"><b style="font-size:11px;letter-spacing:.04em">OFF</b>' +
        '<span>no runs</span></div>' +
        '<div class="up-m"><div class="a">' + (rule ? 'Automation is off' : 'No rule yet') + '</div>' +
        '<div class="b">' + (rule
          ? 'nothing is scheduled — flip the switch to start the ' + esc(cadenceText()) + ' cadence'
          : 'save a folder + cadence to create it') + '</div></div>' +
        '<span class="up-pill q">paused</span></div>';
      return;
    }
    box.innerHTML = fires.map(function (d, i) {
      return '<div class="uprow' + (i === fires.length - 1 ? ' last' : '') + '">' +
        '<div class="up-t"><b>' + hm(d) + '</b><span>' + dayLbl(d) + '</span></div>' +
        '<div class="up-m"><div class="a">' + esc(folderName()) + ' · ' + form.perRun + ' photo' + (form.perRun === 1 ? '' : 's') + '</div>' +
        '<div class="b">random pick · ' + (form.ttl > 0 ? 'auto-delete ' + form.ttl + ' h' : 'kept') + '</div></div>' +
        '<span class="up-pill ' + (i === 0 ? 'next' : 'q') + '">' + (i === 0 ? inHM(d) : 'Scheduled') + '</span></div>';
    }).join('');
    $('#up-sub').textContent = 'What the ' + cadenceText() + ' cadence does over the next runs.';
  }

  /** Thumb for a story row. Painted through CSSOM: an OF CDN url dropped into an
   *  HTML style="" attribute is cut in half by the url's own quoting. */
  function storyThumb(s) {
    var m = (s.media || [])[0] || {}, f = m.files || {};
    return (f.thumb && f.thumb.url) || (f.squarePreview && f.squarePreview.url) || (f.preview && f.preview.url) || '';
  }
  function paint(el, url) {
    if (!el) return;
    if (url) el.style.backgroundImage = 'url(' + JSON.stringify(url) + ')';
  }

  async function renderRecentAndCount() {
    var box = $('#recent-stories');
    try {
      var out = await Fastt.get('/api/of/v2/stories/archive', { limit: 30 });
      var list = out.list || [];
      // /stories/archive is OF-proxied — createdAt is already tz-aware, so it
      // must NOT go through the naive-UTC repair.
      var mid = new Date(); mid.setHours(0, 0, 0, 0);
      var today = list.filter(function (s) { return new Date(s.createdAt).getTime() >= mid.getTime(); }).length;
      $('#st-posted').innerHTML = '<i></i>' + today + (today === 1 ? ' story' : ' stories') + ' posted today';
      if (!list.length) {
        box.innerHTML = '<div class="rc"><span class="mth"></span>' +
          '<div class="rm"><div class="n" style="color:var(--muted)">No stories in the archive yet.</div><div class="s"></div></div></div>';
        return;
      }
      box.innerHTML = '';
      list.slice(0, 4).forEach(function (s) {
        var el = document.createElement('div');
        el.className = 'rc';
        el.innerHTML = '<span class="mth"></span>' +
          '<div class="rm"><div class="n">Posted ' + Fastt.fmtAgo(s.createdAt) + '</div>' +
          '<div class="s">' + new Date(s.createdAt).toLocaleString() + '</div></div>';
        box.appendChild(el);
        paint(el.querySelector('.mth'), storyThumb(s));
      });
      if (list.length > 4) {
        var more = document.createElement('div');
        more.className = 'pool-meta';
        more.textContent = out.hasMore
          ? (list.length + '+ archived stories on this account')
          : (list.length + ' archived stories on this account');
        box.appendChild(more);
      }
    } catch (e) {
      $('#st-posted').innerHTML = '<i></i>archive unavailable';
      box.innerHTML = '<div class="rc"><span class="mth"></span>' +
        '<div class="rm"><div class="n" style="color:#e05b5b">Could not load the story archive.</div><div class="s"></div></div></div>';
    }
  }

  // ── stories that are LIVE on the profile right now ─────────
  // GET /api/of/v2/users/me/stories — the only feedback loop that answers
  // "are these stories doing anything": viewers / likes / tips per story.
  var EYE = '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9"><path d="M2 12s3.6-6 10-6 10 6 10 6-3.6 6-10 6-10-6-10-6z"/><circle cx="12" cy="12" r="2.6"/></svg>';
  var HEART = '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9"><path d="M12 20s-7-4.4-7-9a4 4 0 0 1 7-2.6A4 4 0 0 1 19 11c0 4.6-7 9-7 9z" stroke-linejoin="round"/></svg>';
  async function renderLiveStories() {
    var box = $('#live-stories');
    try {
      var list = await Fastt.get('/api/of/v2/users/me/stories');
      if (!Array.isArray(list)) list = list.list || [];
      if (!list.length) {
        box.innerHTML = '<div class="lsrow"><div class="lsm"><div class="n" style="color:var(--muted)">' +
          'Nothing live on the profile right now — every story has expired or been auto-deleted.</div>' +
          '<div class="s">The automation posts on its cadence; each drop comes down after its keep-for window.</div></div></div>';
        return;
      }
      box.innerHTML = '';
      var views = 0, likes = 0, tips = 0;
      list.slice().sort(function (a, b) { return new Date(b.createdAt) - new Date(a.createdAt); }).forEach(function (s) {
        views += Number(s.viewersCount) || 0;
        likes += Number(s.likesCount) || 0;
        tips += Number(s.tipsAmountRaw) || 0;
        var el = document.createElement('div');
        el.className = 'lsrow';
        var tipTxt = (Number(s.tipsAmountRaw) || 0) > 0
          ? '<span class="tip">' + esc(s.tipsAmount || Fastt.fmtMoney(s.tipsAmountRaw)) + '</span>' : '';
        el.innerHTML = '<span class="lsth"></span>' +
          '<div class="lsm"><div class="n">Posted ' + Fastt.fmtAgo(s.createdAt) + (s.isWatched ? ' · seen' : '') + '</div>' +
          '<div class="s">story #' + esc(s.id) + (s.commentsCount ? ' · ' + s.commentsCount + ' comments' : '') + '</div></div>' +
          '<div class="lsstat">' + EYE + '<b>' + Fastt.fmtInt(s.viewersCount || 0) + '</b>' +
          HEART + '<b>' + Fastt.fmtInt(s.likesCount || 0) + '</b>' + tipTxt + '</div>';
        box.appendChild(el);
        paint(el.querySelector('.lsth'), storyThumb(s));
      });
      var sum = document.createElement('div');
      sum.className = 'pool-meta';
      sum.innerHTML = list.length + ' live · ' + Fastt.fmtInt(views) + ' views · ' + Fastt.fmtInt(likes) + ' likes' +
        (tips > 0 ? ' · ' + Fastt.fmtMoney(tips) + ' tipped' : ' · no tips yet');
      box.appendChild(sum);
    } catch (e) {
      box.innerHTML = '<div class="lsrow"><div class="lsm"><div class="n" style="color:#e05b5b">' +
        'Could not read the live stories from OnlyFans.</div><div class="s">GET /users/me/stories did not answer.</div></div></div>';
    }
  }

  // ── last run + previous runs ───────────────────────────────
  var parseStats = Fastt.ruleStats;
  function statusCls(s) {
    s = String(s || '').toLowerCase();
    if (s === 'ok' || s === 'done' || s === 'success') return 'ok';
    if (s === 'skipped') return 'skip';
    if (s === 'error' || s === 'failed') return 'err';
    return '';
  }
  function runBits(r) {
    var st = parseStats(r), bits = [];
    if (st) {
      if (st.dry_run) bits.push('dry run');
      if (typeof st.posted === 'number') bits.push('posted ' + st.posted);
      if (typeof st.failed === 'number' && st.failed > 0) bits.push(st.failed + ' failed');
      if (Array.isArray(st.stories) && st.stories.length) {
        bits.push('story #' + st.stories.map(function (x) { return x.story_id; }).join(', #'));
        var da = Fastt.parseUtc(st.stories[0].delete_at), sa = Fastt.parseUtc(r.started_at);
        if (da && sa && da > sa) bits.push('auto-deletes after ' + (Math.round((da - sa) / 360e3) / 10) + ' h');
      } else if (typeof st.hours_to_live === 'number' && st.hours_to_live > 0) {
        bits.push('keeps ' + st.hours_to_live + ' h');
      }
      if (st.status === 'skipped' && st.reason) bits.push('skipped (' + st.reason + ')');
      if (typeof st.folder_id === 'number' && folderNames[st.folder_id]) bits.push('from ' + folderNames[st.folder_id]);
    }
    if (r.error_text) bits.push(String(r.error_text).slice(0, 120));
    return bits.join(' · ');
  }
  function renderLastRun() {
    var box = $('#lastrun');
    var lr = rule && rule.last_run;
    if (!rule) {
      box.innerHTML = '<span style="color:var(--muted)">No auto_stories rule saved for this creator yet — nothing has run.</span>';
      return;
    }
    if (!lr || !lr.started_at) {
      box.innerHTML = '<span style="color:var(--muted)">This rule has never run.</span>';
      return;
    }
    var bits = runBits({ started_at: lr.started_at, stats_json: lr.stats, stats: lr.stats, error_text: lr.error_text });
    box.innerHTML = '<span class="rstat ' + statusCls(lr.status) + '">' + esc(lr.status || '?') + '</span>' +
      '<span><b style="color:#fff;font-weight:600">Last run</b> ' + Fastt.fmtAgo(lr.started_at) +
      (bits ? ' · ' + esc(bits) : '') +
      (rule.has_pending_job ? ' · a job is queued right now' : '') + '</span>';
  }
  async function renderRuns() {
    var box = $('#runs-list');
    try {
      var out = await Fastt.get('/admin/stats/automation-runs', { kind: 'auto_stories', limit: 8 });
      var all = out.runs || [];
      // Run rows carry no rule_id — prefer this account's rows, fall back to the
      // global (account_id null) ones only when this account has none.
      var mine = all.filter(function (r) { return String(r.account_id) === String(Fastt.account()); });
      if (!mine.length) mine = all.filter(function (r) { return r.account_id == null; });
      if (!mine.length) {
        box.innerHTML = '<div class="runrow"><span style="color:var(--muted)">No runs recorded for this creator yet.</span></div>';
        return;
      }
      box.innerHTML = mine.slice(0, 8).map(function (r) {
        var bits = runBits(r);
        return '<div class="runrow"><span class="rstat ' + statusCls(r.status) + '">' + esc(r.status || '?') + '</span>' +
          '<span class="rwhen" title="' + esc(r.started_at || '') + '">' + Fastt.fmtAgo(r.started_at) + '</span>' +
          '<span class="rbits">' + (bits ? '· ' + esc(bits) : '') + '</span></div>';
      }).join('');
    } catch (e) {
      box.innerHTML = '<div class="runrow"><span class="rstat err">error</span><span style="color:var(--muted)">Could not load run history.</span></div>';
    }
  }

  function renderPool() {
    var n = form.mediaIds.length;
    $('#pick-lbl').textContent = n ? (n + ' photo' + (n === 1 ? '' : 's') + ' picked') : 'Add specific photos';
    $('#pick-clear').style.display = n ? '' : 'none';
    var ck = $('#ck-dupe'); if (ck) ck.classList.toggle('on', !!form.removeVaultDupe);
    var dz = $('#adv-danger'); if (dz) dz.style.display = rule ? '' : 'none';
  }

  // ── explicit image-pool picker (mirrors AutoStoriesTab's VaultPicker) ──
  // Folder ∪ picked ids form one pool the executor samples per run. This lets
  // an owner pin exactly which vault photos rotate, with or without a folder.
  var pickerBusy = false;
  async function openImgPicker() {
    if (pickerBusy) return; pickerBusy = true;
    var chosen = {}; form.mediaIds.forEach(function (id) { chosen[Number(id)] = true; });
    var back = document.createElement('div');
    back.className = 'imgpick-back';
    back.innerHTML =
      '<div class="imgpick">' +
        '<div class="imgpick-h"><span class="t">Pick photos for the pool</span>' +
          '<span class="c" id="ip-count"></span></div>' +
        '<div class="imgpick-body" id="ip-body"><div class="imgpick-msg">Loading vault photos…</div></div>' +
        '<div class="imgpick-f">' +
          '<span class="imgpick-msg" id="ip-foot">Tap photos to add or remove them.</span>' +
          '<button class="fx-btn ghost" id="ip-clear" style="margin-left:auto">Clear all</button>' +
          '<button class="fx-btn ghost" id="ip-cancel">Cancel</button>' +
          '<button class="fx-btn" id="ip-save">Save pool</button>' +
        '</div>' +
      '</div>';
    document.body.appendChild(back);
    var close = function () { back.remove(); pickerBusy = false; };
    back.addEventListener('click', function (e) { if (e.target === back) close(); });
    $('#ip-cancel', back).addEventListener('click', close);
    var updCount = function () {
      var c = Object.keys(chosen).filter(function (k) { return chosen[k]; }).length;
      $('#ip-count', back).textContent = c + ' selected';
    };
    updCount();
    $('#ip-clear', back).addEventListener('click', function () {
      chosen = {};
      back.querySelectorAll('.imgpick-cell.sel').forEach(function (c) { c.classList.remove('sel'); });
      updCount();
    });
    $('#ip-save', back).addEventListener('click', function () {
      form.mediaIds = Object.keys(chosen).filter(function (k) { return chosen[k]; }).map(Number);
      close();
      if (!form.folderId && !form.mediaIds.length) { Fastt.toast('Pool cleared — pick a folder or photos to keep it live', 'err'); renderAll(); return; }
      savePayload();
    });
    var body = $('#ip-body', back);
    try {
      var out = await Fastt.get('/api/of/v2/vault/media', { limit: 60 });
      var items = (out.list || []).filter(function (m) { return m.type === 'photo' && m.files; });
      if (!items.length) { body.innerHTML = '<div class="imgpick-msg">No vault photos found for this creator.</div>'; return; }
      var grid = document.createElement('div');
      grid.className = 'imgpick-grid';
      items.forEach(function (m) {
        var f = m.files || {};
        var url = (f.squarePreview && f.squarePreview.url) || (f.thumb && f.thumb.url) || (f.preview && f.preview.url) || '';
        var cell = document.createElement('div');
        cell.className = 'imgpick-cell' + (chosen[Number(m.id)] ? ' sel' : '');
        if (url) cell.style.backgroundImage = 'url(' + JSON.stringify(url) + ')';
        cell.addEventListener('click', function () {
          if (chosen[Number(m.id)]) { delete chosen[Number(m.id)]; cell.classList.remove('sel'); }
          else { chosen[Number(m.id)] = true; cell.classList.add('sel'); }
          updCount();
        });
        grid.appendChild(cell);
      });
      body.innerHTML = '';
      body.appendChild(grid);
      if (out.hasMore) {
        var more = document.createElement('div');
        more.className = 'imgpick-msg'; more.style.marginTop = '12px';
        more.textContent = 'Showing the 60 most recent — already-picked photos outside this window stay in the pool.';
        body.appendChild(more);
      }
    } catch (e) {
      body.innerHTML = '<div class="imgpick-msg" style="color:#e05b5b">Could not load the vault. ' + esc((e && e.message) || '') + '</div>';
    }
  }

  function renderAll() {
    renderStatus(); renderFolderSelect(); renderTrigger(); renderUpcoming();
    renderThumbs(); renderLastRun(); renderPool();
  }
  renderAll();
  renderRecentAndCount();
  renderLiveStories();
  renderRuns();
  Fastt.liveBadge($('.colL .fx-card-h'));
  Fastt.liveBadge($('.colR .fx-card-h'));
  Fastt.liveBadge($('#live-h'));
  Fastt.liveBadge($('#recent-h'));
  Fastt.liveBadge($('#runs-h'));

  // ── events ─────────────────────────────────────────────────
  $('#sw-en').addEventListener('click', function (e) {
    var next = !(rule && rule.is_enabled);
    if (!rule) { e.stopPropagation(); Fastt.toast('Save a folder + cadence first — that creates the rule (disabled)', 'err'); return; }
    if (next && !confirm('Enable Auto Stories? On its cadence this WILL post real stories to the live OnlyFans account.')) {
      e.stopPropagation();
      return;
    }
    saveRule({ is_enabled: next });
  });
  $('#sel-folder').addEventListener('change', function () {
    form.folderId = $('#sel-folder').value ? Number($('#sel-folder').value) : null;
    if (!form.folderId && !form.mediaIds.length) { Fastt.toast('auto_stories needs a media source — folder kept', 'err'); renderAll(); return; }
    savePayload();
  });
  $('#in-every').addEventListener('change', function () {
    var v = parseInt($('#in-every').value, 10);
    form.everyHours = (isFinite(v) && v >= 1) ? v : form.everyHours;
    if (form.mode === 'interval') saveTrigger(); else renderAll();
  });
  $('#in-perrun').addEventListener('change', function () {
    var v = parseInt($('#in-perrun').value, 10);
    form.perRun = (isFinite(v) && v >= 1) ? v : form.perRun;
    savePayload();
  });
  $('#in-ttl').addEventListener('change', function () {
    var v = parseInt($('#in-ttl').value, 10);
    form.ttl = (isFinite(v) && v >= 0) ? v : form.ttl;
    savePayload();
  });
  $('#in-maxruns').addEventListener('change', function () {
    var v = parseInt($('#in-maxruns').value, 10);
    form.maxRuns = (isFinite(v) && v >= 1) ? v : null;
    saveTrigger();
  });
  $('#in-watermark').addEventListener('change', function () {
    form.watermark = $('#in-watermark').value;
    savePayload();
  });
  $('#btn-pickimgs').addEventListener('click', function (e) { e.preventDefault(); openImgPicker(); });
  $('#pick-clear').addEventListener('click', function () {
    if (!form.mediaIds.length) return;
    form.mediaIds = [];
    if (!form.folderId) { Fastt.toast('Pool cleared — pick a folder to keep it live', 'err'); renderAll(); return; }
    savePayload();
  });
  // remove_vault_dupe toggle — own the click so the global fx-check handler
  // doesn't double-toggle; then persist.
  $('#ck-dupe').addEventListener('click', function (e) {
    e.stopPropagation();
    form.removeVaultDupe = !form.removeVaultDupe;
    $('#ck-dupe').classList.toggle('on', form.removeVaultDupe);
    savePayload();
  });
  $('#btn-delete').addEventListener('click', function () {
    if (!rule) return;
    if (!confirm('Delete the Auto Stories automation for this creator? Stories already posted stay up; the schedule stops.')) return;
    Fastt.del('/admin/automation-rules/' + rule.id)
      .then(function () {
        rule = null;
        form = { mode: 'interval', times: ['09:00'], everyHours: 6, perRun: 1, ttl: 6, maxRuns: null, folderId: null, watermark: '', mediaIds: [], removeVaultDupe: true };
        Fastt.saved('Automation deleted');
        renderAll();
      })
      .catch(Fastt.oops);
  });
  $('#trigseg').addEventListener('click', function (e) {
    var b = e.target.closest('button');
    if (!b) return;
    var mode = b.dataset.t === 'clock' ? 'clock' : 'interval';
    if (mode === form.mode) return;
    form.mode = mode;
    saveTrigger();
  });
  $('#btn-runnow').addEventListener('click', function () {
    if (!rule) { Fastt.toast('No rule saved yet', 'err'); return; }
    if (!confirm('Post ' + form.perRun + ' random stor' + (form.perRun === 1 ? 'y' : 'ies') +
        ' from “' + folderName() + '” to the LIVE account right now?')) return;
    Fastt.post('/admin/automation-rules/' + rule.id + '/run-now', {})
      .then(function () {
        Fastt.saved('Run queued — stories appear within ~30s');
        setTimeout(function () { renderRuns(); renderLiveStories(); renderRecentAndCount(); }, 20000);
      })
      .catch(Fastt.oops);
  });
});
