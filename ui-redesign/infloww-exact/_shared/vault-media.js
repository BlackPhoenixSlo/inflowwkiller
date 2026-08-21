/* vault-media.js — the OnlyFans vault MEDIA layer.
 *
 * Loads after _shared/fastt.js and before the page's own script (see the
 * /infloww StaticFiles mount in service/server.py). Exposes `window.FasttVault`.
 *
 * WHAT LIVES HERE, and why only this:
 *
 * Four pages render vault rows — messages (chat picker), group (group-chat
 * picker), growth-vault-pro and vault-ai (managers). They are NOT one component:
 * the pickers ATTACH to a draft, the managers WRITE to OnlyFans, and each owns
 * its own grid markup, empty-state copy and DOM ids. Trying to serve all four
 * from one configurable picker would trade duplication for option flags, which
 * is a worse trade.
 *
 * What they genuinely share is the OF vault-row PAYLOAD CONTRACT — "given this
 * row, what can I show and can it play" — plus the hover-scrub session the two
 * pickers run over it. That is domain knowledge about OnlyFans' wire shape, not
 * page UI, and it is where drift is expensive: a wrong videoSources ladder shows
 * a poster JPEG where a video should play, and a missed DRM manifest offers a
 * play button that can never work.
 *
 * Every function below was moved here verbatim — each was verified to have an
 * IDENTICAL token stream across the pages it came from before being lifted, so
 * this file introduces no new behaviour. Anything that differed between pages
 * stayed in the pages.
 */
(function () {
  "use strict";

  // ── the OF vault-row payload contract ────────────────────────
  // These read OnlyFans' /vault/media row shape and nothing else: no DOM, no
  // page state, no relay calls. Identical in messages.js, group.js and
  // growth-vault-pro.js before the move.

  /** Cheapest playable video source first: videoSources['240'] || ['720'] ||
   *  files.full.url. Deliberately NOT files.preview — that is a poster JPEG,
   *  and handing it to a <video> renders a still that never plays. */
  function progressiveVideoSrc(m) {
    if (!m || m.type !== 'video') return null;
    var vs = m.videoSources || {};
    return vs['240'] || vs['720'] || (m.files && m.files.full && m.files.full.url) || null;
  }

  /** FairPlay-protected video: files.drm.manifest carries an hls or dash entry.
   *  These cannot play in-page at all — the caller must fall back to poster
   *  frames rather than offering a play button that does nothing. */
  function isDrmVideo(m) {
    if (!m || m.type !== 'video') return false;
    var man = m.files && m.files.drm && m.files.drm.manifest;
    return !!(man && (man.hls || man.dash));
  }

  /** The OF-supplied poster-frame list (files.preview.options[].url) — the only
   *  visual a DRM video can show. Null-guarded: growth-vault-pro's copy guarded
   *  `m` and the pickers' did not, so the guarded form is the one that moved.
   *  Strictly safer; no caller passed null. */
  function videoPosterFrames(m) {
    return (((m && m.files && m.files.preview && m.files.preview.options) || []))
      .map(function (o) { return o && o.url; }).filter(Boolean);
  }

  /** One frame of the relay's storyboard for a video. `sid` scopes the build so
   *  a hover that moves on can cancel its own work and not someone else's. */
  function scrubUrl(u, i, dur, sid) {
    return '/img/scrub?u=' + encodeURIComponent(u) + '&i=' + i +
           (dur ? '&dur=' + dur : '') + '&sid=' + encodeURIComponent(sid);
  }

  // Frames the relay builds per storyboard. The cycling below is the only
  // consumer; it is not a knob.
  var SCRUB_FRAMES = 12;

  // ── hover-scrub preview session ──────────────────────────────

  /** Wire hover-to-preview onto a tile grid, and own the whole session.
   *
   *   var hov = FasttVault.hoverScrub(bodyEl, function (mid) { return meta[mid]; });
   *   hov.stop();   // before re-rendering the grid, or when the sheet closes
   *
   * `bodyEl` is the grid container (delegated `.mt` listeners are attached
   * here); `getMeta(mid)` returns the OF row for a tile's data-mid. Exactly one
   * preview runs at a time, per session.
   *
   * The flow: dwell 400ms → show poster frame 0 immediately → probe the relay's
   * storyboard while a countdown ticks → on success prefetch the remaining
   * frames and cycle them; on failure tear down. A video with no progressive
   * source but several posters cycles those instead. Every timer is tracked so
   * `stop()` (and a detached tile) can never leak a ticker or an orphan build.
   *
   * Lifted from messages.js and group.js, whose copies had identical token
   * streams (stopHover 196 tokens, startHover 683, both exact). ONE deliberate
   * unification: `stop()` also cancels a pending dwell timer. messages.js
   * already did that; group.js did not, so its close/confirm buttons could let a
   * queued dwell fire ~400ms later against a detached tile and start a scrub
   * build for a sheet that was already gone.
   */
  function hoverScrub(bodyEl, getMeta) {
    var HOVER = null, hoverDwell = null;   // one active hover-scrub preview at a time
    var imgProxy = window.Fastt.imgProxy;

    function stopHover() {
      if (!HOVER) return;
      var h = HOVER; HOVER = null;
      h.timers.forEach(function (t) { clearInterval(t); clearTimeout(t); });
      if (h.imgEl && h.origSrc != null && h.imgEl.isConnected) h.imgEl.src = h.origSrc;   // restore the static thumb
      if (h.tile && h.tile.isConnected) { h.tile.classList.remove('vhovering'); var cd = h.tile.querySelector('.vhover-cd'); if (cd) cd.remove(); }
      if (h.url) {   // abort the in-flight storyboard build for this hover session
        try { fetch('/img/scrub/cancel?u=' + encodeURIComponent(h.url) + '&sid=' + encodeURIComponent(h.sid), { method: 'POST', keepalive: true }); } catch (e) {}
      }
    }
    function slideTick(sid) {
      // stop the moment the picker closed / re-rendered (tile detached) or the
      // hover moved on — self-cleaning so no orphan scrub fetches leak.
      if (!HOVER || HOVER.sid !== sid || !HOVER.tile.isConnected) { stopHover(); return; }
    }
    function startHover(tile) {
      var m = getMeta(tile.getAttribute('data-mid'));
      if (!m || m.type !== 'video') return;
      var url = progressiveVideoSrc(m), posters = videoPosterFrames(m);
      if (!url && !posters.length) return;   // nothing to preview
      stopHover();
      var img = tile.querySelector('img'); if (!img) return;
      var sid = Date.now().toString(36) + Math.random().toString(36).slice(2, 8);
      HOVER = { sid: sid, url: url, dur: m.duration || 0, tile: tile, imgEl: img, origSrc: img.getAttribute('src'), frame: 0, timers: [] };
      tile.classList.add('vhovering');
      if (posters.length) img.src = imgProxy(posters[0]);   // instant fallback while the storyboard builds
      if (url) {
        // rough cold-build estimate; the poster preview covers the wait
        var est = Math.max(3, Math.round((m.duration || 30) / 8));
        var cd = document.createElement('span'); cd.className = 'vhover-cd'; cd.textContent = est + 's'; tile.appendChild(cd);
        var started = Date.now();
        var cdTimer = setInterval(function () {
          slideTick(sid); if (!HOVER || HOVER.sid !== sid) return;   // self-clean if the sheet closed mid-build
          var left = Math.max(0, est - (Date.now() - started) / 1000);
          cd.textContent = left > 0 ? (left.toFixed(1) + 's') : '…';
        }, 100);
        HOVER.timers.push(cdTimer);
        var probe = new Image();
        probe.onload = function () {
          if (!HOVER || HOVER.sid !== sid) return;
          clearInterval(cdTimer);   // storyboard is up — stop the countdown ticker
          var el = HOVER.tile.querySelector('.vhover-cd'); if (el) el.remove();
          // prefetch the rest, then cycle
          for (var i = 1; i < SCRUB_FRAMES; i++) { var pi = new Image(); pi.src = scrubUrl(url, i, m.duration, sid); }
          HOVER.timers.push(setInterval(function () {
            slideTick(sid); if (!HOVER || HOVER.sid !== sid) return;
            HOVER.frame = (HOVER.frame + 1) % SCRUB_FRAMES;
            HOVER.imgEl.src = scrubUrl(url, HOVER.frame, m.duration, sid);
          }, 600));
        };
        probe.onerror = function () { if (HOVER && HOVER.sid === sid) stopHover(); };   // build failed → tear down, no orphan ticker
        probe.src = scrubUrl(url, 0, m.duration, sid);
      } else if (posters.length > 1) {
        var pi2 = 0;
        HOVER.timers.push(setInterval(function () {
          slideTick(sid); if (!HOVER || HOVER.sid !== sid) return;
          pi2 = (pi2 + 1) % posters.length; HOVER.imgEl.src = imgProxy(posters[pi2]);
        }, 700));
      }
    }

    bodyEl.addEventListener('mouseover', function (e) {
      var tile = e.target.closest('.mt'); if (!tile) return;
      if (HOVER && HOVER.tile === tile) return;   // already previewing this tile
      var m = getMeta(tile.getAttribute('data-mid'));
      if (!m || m.type !== 'video') return;
      if (hoverDwell) clearTimeout(hoverDwell);
      hoverDwell = setTimeout(function () { startHover(tile); }, 400);
    });
    bodyEl.addEventListener('mouseout', function (e) {
      var tile = e.target.closest('.mt'); if (!tile) return;
      if (e.relatedTarget && tile.contains(e.relatedTarget)) return;   // moving within the same tile
      if (hoverDwell) { clearTimeout(hoverDwell); hoverDwell = null; }
      if (HOVER && HOVER.tile === tile) stopHover();
    });

    return {
      /** Tear down any running preview AND any queued dwell. Call before a grid
       *  re-render (the tiles are about to be replaced) and on close/confirm. */
      stop: function () {
        if (hoverDwell) { clearTimeout(hoverDwell); hoverDwell = null; }
        stopHover();
      },
    };
  }

  window.FasttVault = {
    progressiveVideoSrc, isDrmVideo, videoPosterFrames, scrubUrl, SCRUB_FRAMES,
    hoverScrub,
  };
})();
