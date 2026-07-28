"""
convo_review_render.py — the WRITE half of the convo reader.

Runs on the LAPTOP. Takes the JSON that `convo_review_fetch.py` produced on the
VPS, folds its new media into a permanent on-disk cache, and emits ONE
self-contained HTML file: every picture, every gif and every message inlined, no
network, nothing that expires.

The cache is the reason a re-run is cheap. Media is keyed by OF media id, and
those bytes never change, so the second run only pays for conversations (and
pictures) it has not seen before — `scripts/convo-review.sh` passes the cache's
key list up as `--have` and the fetcher skips them.

It is also the reason the cache must not be treated as disposable: OF's signed
urls die in ~2 days and OF itself eventually deletes media, so for anything older
than that this cache is the only copy left.
"""
from __future__ import annotations

import argparse
import base64
import html as _html
import json
import re
from pathlib import Path

# OF stores message bodies as HTML — 407k of prod's rows carry a <p>, 18k a <br>.
# The house strip (automation_executor._strip_html) drops every tag, which is
# right for a 120-char inbox preview and wrong for something you sit and READ:
# it welds paragraphs together. So the breaks are converted before the tags go.
_BR = re.compile(r"(?i)<br\s*/?>")
_P_END = re.compile(r"(?i)</p\s*>")
_TAG = re.compile(r"<[^>]+>")


def to_text(s: str | None) -> str:
    if not s:
        return ""
    if "<" not in s and "&" not in s:
        return s.strip()
    s = _P_END.sub("\n\n", _BR.sub("\n", s))
    s = _TAG.sub("", s)
    return re.sub(r"\n{3,}", "\n\n", _html.unescape(s)).strip()


def clean(payload: dict) -> None:
    """Bodies and names, in place — the viewer then escapes plain text only."""
    for t in payload.get("threads", []):
        t["fan_name"] = to_text(t.get("fan_name"))
        t["account_name"] = to_text(t.get("account_name"))
        for m in t.get("messages", []):
            if "body" in m:
                m["body"] = to_text(m["body"])

# Same budget question the fetcher answers for bandwidth, asked again for the
# file the user actually opens. A browser handles a 40 MB document fine; 300 MB
# is a different experience.
DEFAULT_MAX_MB = 60


def cache_path(cache: Path, mkey: str, is_gif: bool) -> Path:
    """`<acct>:<media_id>` → `<cache>/<acct>/<media_id>.<ext>`. Giphy keys arrive
    as `<acct>:g<giphy_id>`, whose ids are alphanumeric, so both are filename-safe
    after the one substitution."""
    acct, ident = mkey.split(":", 1)
    ident = re.sub(r"[^A-Za-z0-9_-]", "_", ident)
    return cache / re.sub(r"[^A-Za-z0-9_-]", "_", acct) / f"{ident}.{'gif' if is_gif else 'jpg'}"


def absorb(payload: dict, cache: Path) -> int:
    """Write the run's new media into the cache. Returns how many landed."""
    meta = payload.get("media_meta") or {}
    n = 0
    for mkey, b64 in (payload.get("media") or {}).items():
        is_gif = bool((meta.get(mkey) or {}).get("gif"))
        p = cache_path(cache, mkey, is_gif)
        p.parent.mkdir(parents=True, exist_ok=True)
        try:
            p.write_bytes(base64.b64decode(b64))
            n += 1
        except Exception as e:  # noqa: BLE001
            print(f"  ! could not cache {mkey}: {e}")
    return n


def have_keys(cache: Path) -> list[str]:
    """Every media key already on disk, in the fetcher's `--have` shape."""
    out: list[str] = []
    if not cache.is_dir():
        return out
    for acct_dir in cache.iterdir():
        if acct_dir.is_dir():
            for f in acct_dir.iterdir():
                if f.suffix in (".jpg", ".gif"):
                    out.append(f"{acct_dir.name}:{f.stem}")
    return out


def collect(payload: dict, cache: Path, max_bytes: int) -> tuple[dict[str, str], int]:
    """Referenced media → {key: data-url}, newest-thread-first until the budget
    runs out. Returns the map and how many were dropped for space."""
    wanted: list[str] = []
    seen = set()
    for t in payload.get("threads", []):
        for m in t.get("messages", []):
            for k in m.get("media", []) or []:
                if k not in seen:
                    seen.add(k)
                    wanted.append(k)
    meta = payload.get("media_meta") or {}
    out: dict[str, str] = {}
    used = 0
    dropped = 0
    for k in wanted:
        is_gif = bool((meta.get(k) or {}).get("gif"))
        p = cache_path(cache, k, is_gif)
        if not p.is_file():
            # A gif we cached before but whose meta this run didn't carry, or
            # vice versa — check the other extension before giving up.
            p = cache_path(cache, k, not is_gif)
            is_gif = not is_gif
            if not p.is_file():
                continue
        data = p.read_bytes()
        if used + len(data) > max_bytes:
            dropped += 1
            continue
        used += len(data)
        mime = "image/gif" if is_gif else "image/jpeg"
        out[k] = f"data:{mime};base64,{base64.b64encode(data).decode()}"
    return out, dropped


# ── the page ─────────────────────────────────────────────────────────────────

TEMPLATE = r"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Convo review — __GENERATED__</title>
<style>
  :root{
    --bg:#0e0f13; --panel:#15171d; --panel2:#1b1e26; --line:#262a35;
    --ink:#e8eaf0; --dim:#9aa1b1; --faint:#6b7386;
    --fan:#232733; --her:#2b2340; --her2:#3a2c58;
    --money:#39d98a; --warn:#ff6b6b; --gold:#ffc94d; --accent:#b98cff;
  }
  *{box-sizing:border-box}
  html,body{height:100%}
  body{margin:0;background:var(--bg);color:var(--ink);
       font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
       display:flex;flex-direction:column;overflow:hidden}
  header{display:flex;align-items:center;gap:14px;padding:10px 16px;
         background:var(--panel);border-bottom:1px solid var(--line);flex:0 0 auto}
  header h1{font-size:15px;margin:0;font-weight:600;letter-spacing:.2px}
  header .sub{color:var(--faint);font-size:12px}
  .tabs{display:flex;gap:6px;margin-left:auto}
  .tab{padding:5px 12px;border-radius:999px;background:var(--panel2);color:var(--dim);
       border:1px solid var(--line);cursor:pointer;font-size:13px;user-select:none}
  .tab:hover{color:var(--ink)}
  .tab.on{background:var(--accent);border-color:var(--accent);color:#14101f;font-weight:600}
  #q{background:var(--panel2);border:1px solid var(--line);color:var(--ink);
     border-radius:8px;padding:6px 10px;width:220px;outline:none}
  #q:focus{border-color:var(--accent)}
  main{flex:1;display:flex;min-height:0}
  #list{width:340px;flex:0 0 auto;border-right:1px solid var(--line);
        overflow-y:auto;background:var(--panel)}
  .card{padding:10px 12px;border-bottom:1px solid var(--line);cursor:pointer}
  .card:hover{background:var(--panel2)}
  .card.on{background:var(--panel2);box-shadow:inset 3px 0 0 var(--accent)}
  .card .who{display:flex;align-items:baseline;gap:6px}
  .card .nm{font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .card .acct{color:var(--faint);font-size:11px;margin-left:auto;white-space:nowrap}
  .card .why{color:var(--dim);font-size:12px;margin-top:3px}
  .card .why b{color:var(--money);font-weight:600}
  .chips{display:flex;gap:4px;margin-top:5px;flex-wrap:wrap}
  .chip{font-size:10px;padding:1px 7px;border-radius:999px;border:1px solid var(--line);
        color:var(--dim);background:#0f1116}
  .chip.sell{color:var(--money);border-color:#1e5c40}
  .chip.long{color:#7cc4ff;border-color:#1e3f5c}
  .chip.brk{color:var(--warn);border-color:#5c2020}
  #pane{flex:1;display:flex;flex-direction:column;min-width:0}
  #head{padding:10px 18px;border-bottom:1px solid var(--line);background:var(--panel);
        display:flex;align-items:center;gap:12px;flex:0 0 auto}
  #head .t{font-weight:600}
  #head .s{color:var(--faint);font-size:12px}
  #head .jump{margin-left:auto;display:flex;gap:6px}
  .btn{background:var(--panel2);border:1px solid var(--line);color:var(--dim);
       border-radius:7px;padding:4px 10px;cursor:pointer;font-size:12px}
  .btn:hover{color:var(--ink);border-color:var(--accent)}
  #thread{flex:1;overflow-y:auto;padding:18px 24px 60px;scroll-behavior:smooth}
  .day{text-align:center;color:var(--faint);font-size:11px;margin:18px 0 10px;
       text-transform:uppercase;letter-spacing:.6px}
  .gap{text-align:center;color:var(--faint);font-size:11px;margin:14px 0;
       border-top:1px dashed var(--line);padding-top:8px}
  .row{display:flex;margin:3px 0}
  .row.out{justify-content:flex-end}
  .bub{max-width:min(560px,72%);border-radius:16px;padding:8px 12px;
       background:var(--fan);position:relative}
  .row.out .bub{background:linear-gradient(160deg,var(--her),var(--her2))}
  .row.sys{justify-content:center}
  .row.sys .bub{background:transparent;color:var(--faint);font-size:12px;max-width:80%}
  .bub .txt{white-space:pre-wrap;overflow-wrap:anywhere}
  .bub .meta{font-size:10px;color:var(--faint);margin-bottom:2px;display:flex;gap:6px}
  .bub.hit{outline:2px solid var(--warn);outline-offset:2px}
  .bub.unsent{opacity:.45}
  .bub.unsent .txt{text-decoration:line-through}
  .kind{font-size:9px;padding:0 5px;border-radius:4px;background:#0f1116;color:var(--faint)}
  .tag{display:inline-block;margin-top:6px;font-size:11px;padding:2px 8px;border-radius:6px;
       border:1px solid var(--line);color:var(--dim)}
  .tag.paid{color:var(--money);border-color:#1e5c40;background:#0f1c16}
  .tag.locked{color:var(--gold);border-color:#5c4a1e;background:#1c1810}
  .tag.tip{color:var(--gold);border-color:#5c4a1e;background:#1c1810}
  .med{display:flex;flex-wrap:wrap;gap:6px;margin-top:6px}
  .med img{max-width:220px;max-height:300px;border-radius:10px;display:block;
           background:#0b0c10;cursor:zoom-in}
  .med .miss{width:120px;height:150px;border-radius:10px;border:1px dashed var(--line);
             display:flex;align-items:center;justify-content:center;color:var(--faint);
             font-size:11px;text-align:center;padding:6px}
  .desc{font-size:11px;color:var(--faint);font-style:italic;margin-top:4px}
  #zoom{position:fixed;inset:0;background:rgba(0,0,0,.9);display:none;
        align-items:center;justify-content:center;z-index:50;cursor:zoom-out}
  #zoom img{max-width:94vw;max-height:94vh;border-radius:8px}
  .empty{color:var(--faint);text-align:center;margin-top:80px}
  ::-webkit-scrollbar{width:10px} ::-webkit-scrollbar-thumb{background:#2a2e3a;border-radius:6px}
</style></head>
<body>
<header>
  <h1>Convo review</h1>
  <span class="sub" id="stamp"></span>
  <input id="q" placeholder="search names + messages…" autocomplete="off">
  <div class="tabs">
    <div class="tab on" data-f="all">All</div>
    <div class="tab" data-f="selling">💰 Selling</div>
    <div class="tab" data-f="long">💬 Long</div>
    <div class="tab" data-f="breaks">⚠️ Breaks</div>
  </div>
</header>
<main>
  <div id="list"></div>
  <div id="pane">
    <div id="head"><span class="t">—</span></div>
    <div id="thread"><div class="empty">Pick a conversation on the left.<br>
      <span style="font-size:12px">j / k move · / searches</span></div></div>
  </div>
</main>
<div id="zoom"><img alt=""></div>

<script id="payload" type="application/json">__PAYLOAD__</script>
<script id="mediamap" type="application/json">__MEDIA__</script>
<script>
const DATA  = JSON.parse(document.getElementById('payload').textContent);
const MEDIA = JSON.parse(document.getElementById('mediamap').textContent);
const $ = s => document.querySelector(s);
let filter = 'all', query = '', cur = -1, shown = [];

document.getElementById('stamp').textContent =
  `${DATA.threads.length} conversations · pulled ${DATA.generated_at} UTC`;

const money = c => '$' + (c/100).toLocaleString(undefined,{maximumFractionDigits:0});
const when  = ts => ts ? new Date(ts.replace(' ','T') + 'Z') : null;
const esc   = s => (s||'').replace(/[&<>]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));

function matches(t){
  if (filter !== 'all' && !(t.buckets||[]).includes(filter)) return false;
  if (!query) return true;
  const q = query.toLowerCase();
  if ((t.fan_name+' '+t.account_name+' '+(t.fan_username||'')).toLowerCase().includes(q)) return true;
  return (t.messages||[]).some(m => (m.body||'').toLowerCase().includes(q));
}

// Each tab ranks by its OWN metric. Leaving one order for all three puts the
// richest thread at the top of "breaks", which is not what that tab is for.
const RANK = {selling: t => -t.revenue_cents,
              long:    t => -t.inbound,
              breaks:  t => -(t.break_score||0)};

function drawList(){
  shown = DATA.threads.filter(matches);
  if (RANK[filter]) shown.sort((a,b) => RANK[filter](a) - RANK[filter](b));
  const el = $('#list');
  el.innerHTML = shown.map((t,i) => {
    const chips = (t.buckets||[]).map(b =>
      `<span class="chip ${b==='selling'?'sell':b==='long'?'long':'brk'}">${
        b==='selling'?'💰 selling':b==='long'?'💬 long':'⚠️ breaks'}</span>`).join('');
    const why = (t.why||[]).map(w => /^\$/.test(w) ? `<b>${esc(w)}</b>` : esc(w)).join(' · ');
    return `<div class="card ${DATA.threads.indexOf(t)===cur?'on':''}" data-i="${i}">
      <div class="who"><span class="nm">${esc(t.fan_name)}</span>
        <span class="acct">${esc(t.account_name)}</span></div>
      <div class="why">${why}</div><div class="chips">${chips}</div></div>`;
  }).join('') || '<div class="empty" style="margin-top:40px">nothing matches</div>';
  el.querySelectorAll('.card').forEach(c =>
    c.onclick = () => openThread(DATA.threads.indexOf(shown[+c.dataset.i])));
}

function bubble(m){
  if (m.gap !== undefined)
    return `<div class="gap">… ${m.gap.toLocaleString()} messages skipped …</div>`;
  const cls = m.dir === 'in' ? 'in' : m.dir === 'out' ? 'out' : 'sys';
  const d = when(m.ts);
  const time = d ? d.toLocaleTimeString([], {hour:'2-digit', minute:'2-digit'}) : '';
  const kind = m.kind ? `<span class="kind">${esc(m.kind)}</span>` : '';
  let tag = '';
  if (m.tip)             tag = `<span class="tag tip">💸 tip ${money(m.price)}</span>`;
  else if (m.price > 0)  tag = m.paid === 1
      ? `<span class="tag paid">🔓 unlocked ${money(m.price)}</span>`
      : `<span class="tag locked">🔒 ${money(m.price)} never opened</span>`;
  const pics = (m.media||[]).map(k => MEDIA[k]
      ? `<img loading="lazy" data-k="${k}" alt="">`
      : `<div class="miss">not in this build<br>(size budget)</div>`).join('');
  // Media the DB knows about but nothing could re-sign — OF's url expired and
  // the item is no longer in the history pages we can reach.
  const missing = !m.media && m.n_media
      ? `<div class="miss">${m.n_media} 📷<br>OF wouldn't re-sign</div>` : '';
  return `<div class="row ${cls}"><div class="bub ${m.hit?'hit':''} ${m.unsent?'unsent':''}"
      data-id="${m.id}">
    <div class="meta">${time}${m.unsent?' · unsent':''}${kind}</div>
    ${m.body ? `<div class="txt">${esc(m.body)}</div>` : ''}
    ${pics || missing ? `<div class="med">${pics || missing}</div>` : ''}
    ${m.desc ? `<div class="desc">he sent: ${esc(m.desc)}</div>` : ''}
    ${tag}</div></div>`;
}

function openThread(idx){
  cur = idx;
  const t = DATA.threads[idx];
  drawList();
  $('#head').innerHTML = `<span class="t">${esc(t.fan_name)}</span>
    <span class="s">${esc(t.account_name)} · ${t.total.toLocaleString()} messages
    · ${t.inbound.toLocaleString()} from him
    ${t.revenue_cents ? '· <b style="color:var(--money)">'+money(t.revenue_cents)+'</b>' : ''}
    ${t.truncated ? '· showing '+t.shown.toLocaleString() : ''}
    ${t.media_note ? '· <span style="color:var(--gold)">'+esc(t.media_note)+'</span>' : ''}</span>
    <span class="jump"><button class="btn" data-j="money">💰 next sale</button>
    <button class="btn" data-j="hit">⚠️ next break</button></span>`;
  let html = '', day = '';
  for (const m of t.messages || []) {
    const d = when(m.ts);
    const dk = d ? d.toDateString() : '';
    if (dk && dk !== day) { day = dk; html += `<div class="day">${dk}</div>`; }
    html += bubble(m);
  }
  const th = $('#thread');
  th.innerHTML = html;
  th.scrollTop = th.scrollHeight;
  lazy();
  $('#head').querySelectorAll('[data-j]').forEach(b => b.onclick = () => jump(b.dataset.j));
}

// Images are inlined as data-urls, so "lazy" here is about DECODE cost, not
// network: swapping src only as a bubble nears the viewport keeps a thread with
// forty pictures from hitching on open.
let io;
function lazy(){
  if (io) io.disconnect();
  io = new IntersectionObserver(es => es.forEach(e => {
    if (!e.isIntersecting) return;
    const img = e.target;
    img.src = MEDIA[img.dataset.k];
    img.onclick = () => { $('#zoom img').src = img.src; $('#zoom').style.display='flex'; };
    io.unobserve(img);
  }), {root: $('#thread'), rootMargin: '600px'});
  $('#thread').querySelectorAll('img[data-k]').forEach(i => io.observe(i));
}
$('#zoom').onclick = () => $('#zoom').style.display = 'none';

let jumpAt = {money:-1, hit:-1};
function jump(kind){
  const t = DATA.threads[cur]; if (!t) return;
  const want = (t.messages||[]).filter(m => kind==='money'
      ? (m.tip || m.paid === 1) : m.hit);
  if (!want.length) return;
  jumpAt[kind] = (jumpAt[kind] + 1) % want.length;
  const el = $('#thread').querySelector(`[data-id="${want[jumpAt[kind]].id}"]`);
  if (el) { el.scrollIntoView({block:'center'}); el.style.transition='box-shadow .3s';
            el.style.boxShadow = '0 0 0 3px var(--gold)';
            setTimeout(() => el.style.boxShadow = '', 1200); }
}

document.querySelectorAll('.tab').forEach(tb => tb.onclick = () => {
  document.querySelectorAll('.tab').forEach(x => x.classList.remove('on'));
  tb.classList.add('on'); filter = tb.dataset.f; drawList();
});
$('#q').oninput = e => { query = e.target.value.trim(); drawList(); };
document.onkeydown = e => {
  if (e.target.tagName === 'INPUT') { if (e.key === 'Escape') e.target.blur(); return; }
  if (e.key === '/') { e.preventDefault(); $('#q').focus(); return; }
  if (e.key === 'j' || e.key === 'k') {
    const pos = shown.findIndex(t => DATA.threads.indexOf(t) === cur);
    const nxt = Math.max(0, Math.min(shown.length-1, pos + (e.key==='j'?1:-1)));
    if (shown[nxt]) openThread(DATA.threads.indexOf(shown[nxt]));
  }
};
drawList();
if (DATA.threads.length) openThread(0);
</script></body></html>
"""


def render(payload: dict, media: dict[str, str], out: Path) -> None:
    def embed(obj) -> str:
        # `</script>` anywhere in a fan's message would end the block early.
        return (json.dumps(obj, separators=(",", ":"))
                .replace("<", "\\u003c").replace(">", "\\u003e")
                .replace("&", "\\u0026"))

    slim = {"generated_at": payload.get("generated_at", ""),
            "threads": payload.get("threads", [])}
    html = (TEMPLATE
            .replace("__GENERATED__", payload.get("generated_at", ""))
            .replace("__PAYLOAD__", embed(slim))
            .replace("__MEDIA__", embed(media)))
    out.write_text(html, encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("payload", help="JSON from convo_review_fetch.py")
    ap.add_argument("--cache", required=True, help="permanent media cache dir")
    ap.add_argument("--out", required=True, help="HTML to write")
    ap.add_argument("--max-mb", type=float, default=DEFAULT_MAX_MB)
    ap.add_argument("--have", default="", help="write the cache's key list here and exit")
    args = ap.parse_args()

    cache = Path(args.cache).expanduser()
    if args.have:
        cache.mkdir(parents=True, exist_ok=True)
        Path(args.have).write_text(json.dumps(have_keys(cache)))
        return 0

    payload = json.loads(Path(args.payload).read_text())
    clean(payload)
    cache.mkdir(parents=True, exist_ok=True)
    added = absorb(payload, cache)
    media, dropped = collect(payload, cache, int(args.max_mb * 1024 * 1024))
    out = Path(args.out).expanduser()
    render(payload, media, out)

    size = out.stat().st_size / 1e6
    print(f"  cached {added} new media (cache now {len(have_keys(cache))} items)")
    print(f"  embedded {len(media)} pictures"
          + (f", dropped {dropped} over the {args.max_mb:g} MB budget" if dropped else ""))
    print(f"  wrote {out} ({size:.1f} MB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
