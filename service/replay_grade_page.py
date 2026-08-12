"""Turn a replay_arms `--json-out` dump into a BLIND human-grading page.

The judge model called arm B a wash; a human reading the actual replies is the better
instrument, and this is the tool for it. Every case shows the real conversation and the
four candidate replies **shuffled and unlabelled**. A grader who can see which one is
production is not a blind grader, so the slot→arm mapping is base64'd into the page and
only decoded when you press Reveal.

    python replay_arms.py --arms A,B,C,D --n 200 --json-out out.json
    python replay_grade_page.py out.json grade.html
"""
from __future__ import annotations

import base64
import html
import json
import random
import sys

RATINGS = [("best", "Best"), ("good", "Good"), ("ok", "Ok"), ("bad", "Bad")]

_CSS = """
*,*::before,*::after{box-sizing:border-box}
:root{
  --ground:#F4F7F7; --surface:#FFFFFF; --sunk:#EAEFEF;
  --ink:#0E1719; --muted:#5B7075; --line:#D6E0E0;
  --accent:#0E6E70; --accent-ink:#FFFFFF;
  --best:#1B6E48; --good:#2C5FA8; --ok:#6E7A7C; --bad:#9E2B36;
  --fan:#E8EEEF; --her:#0E6E70;
  --shadow:0 1px 2px rgba(14,23,25,.06),0 8px 24px -12px rgba(14,23,25,.18);
}
@media (prefers-color-scheme:dark){
  :root:not([data-theme="light"]){
    --ground:#0B1112; --surface:#141D1F; --sunk:#101819;
    --ink:#DFEAEB; --muted:#8CA1A5; --line:#243133;
    --accent:#3AA9A6; --accent-ink:#07211F;
    --best:#57C48D; --good:#7BAAE8; --ok:#8B989A; --bad:#E2707A;
    --fan:#1E2A2C; --her:#3AA9A6;
    --shadow:0 1px 2px rgba(0,0,0,.4),0 10px 28px -14px rgba(0,0,0,.7);
  }
}
:root[data-theme="dark"]{
  --ground:#0B1112; --surface:#141D1F; --sunk:#101819;
  --ink:#DFEAEB; --muted:#8CA1A5; --line:#243133;
  --accent:#3AA9A6; --accent-ink:#07211F;
  --best:#57C48D; --good:#7BAAE8; --ok:#8B989A; --bad:#E2707A;
  --fan:#1E2A2C; --her:#3AA9A6;
  --shadow:0 1px 2px rgba(0,0,0,.4),0 10px 28px -14px rgba(0,0,0,.7);
}
html{-webkit-text-size-adjust:100%}
body{
  margin:0; background:var(--ground); color:var(--ink);
  font:400 15px/1.55 ui-sans-serif,-apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
}
.mono{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
.eyebrow{
  font:500 11px/1 ui-monospace,SFMono-Regular,Menlo,monospace;
  letter-spacing:.14em; text-transform:uppercase; color:var(--muted);
}
header{
  position:sticky; top:0; z-index:20; background:var(--surface);
  border-bottom:1px solid var(--line); padding:14px 22px;
  display:flex; align-items:center; gap:18px; flex-wrap:wrap;
}
h1{font-size:16px; font-weight:640; letter-spacing:-.01em; margin:0}
h1 span{color:var(--muted); font-weight:400}
.grow{flex:1}
.bar{height:5px; background:var(--sunk); border-radius:99px; overflow:hidden; width:190px}
.bar>i{display:block; height:100%; background:var(--accent); width:0; transition:width .25s}
button{
  font:inherit; color:inherit; background:var(--surface); cursor:pointer;
  border:1px solid var(--line); border-radius:8px; padding:7px 13px;
}
button:hover{border-color:var(--accent)}
button:focus-visible{outline:2px solid var(--accent); outline-offset:2px}
button.primary{background:var(--accent); color:var(--accent-ink); border-color:transparent; font-weight:560}
main{
  display:grid; grid-template-columns:minmax(0,4fr) minmax(0,6fr);
  gap:22px; padding:22px; max-width:1420px; margin:0 auto; align-items:start;
}
@media (max-width:940px){main{grid-template-columns:1fr}}
.panel{background:var(--surface); border:1px solid var(--line); border-radius:14px; box-shadow:var(--shadow)}
.convo{position:sticky; top:78px; padding:16px 18px; max-height:calc(100vh - 110px); overflow:auto}
@media (max-width:940px){.convo{position:static; max-height:none}}
.meta{display:flex; gap:14px; flex-wrap:wrap; align-items:center; margin:0 0 14px}
.chip{
  font:500 11px/1 ui-monospace,monospace; letter-spacing:.06em; padding:5px 9px;
  border-radius:99px; background:var(--sunk); color:var(--muted); white-space:nowrap;
}
.chip.q{background:color-mix(in srgb,var(--accent) 16%,transparent); color:var(--accent)}
.msgs{display:flex; flex-direction:column; gap:8px}
.msg{max-width:88%; padding:9px 13px; border-radius:14px; white-space:pre-wrap; word-break:break-word}
.msg.in{background:var(--fan); border-bottom-left-radius:5px; align-self:flex-start}
.msg.out{background:var(--her); color:var(--accent-ink); border-bottom-right-radius:5px; align-self:flex-end}
.msg.last{outline:2px solid var(--accent); outline-offset:2px}
.cards{display:flex; flex-direction:column; gap:14px}
.card{padding:15px 17px; display:flex; flex-direction:column; gap:12px}
.card[data-rated="best"]{border-color:var(--best)}
.card[data-rated="good"]{border-color:var(--good)}
.card[data-rated="ok"]{border-color:var(--ok)}
.card[data-rated="bad"]{border-color:var(--bad)}
.reply{white-space:pre-wrap; word-break:break-word; font-size:15.5px; min-height:1.5em}
.rates{display:flex; gap:7px; flex-wrap:wrap}
.rates button{padding:6px 14px; font-size:13px; border-radius:99px}
.rates button[aria-pressed="true"]{color:var(--accent-ink); border-color:transparent; font-weight:600}
.rates button[data-r="best"][aria-pressed="true"]{background:var(--best)}
.rates button[data-r="good"][aria-pressed="true"]{background:var(--good)}
.rates button[data-r="ok"][aria-pressed="true"]{background:var(--ok)}
.rates button[data-r="bad"][aria-pressed="true"]{background:var(--bad)}
.flags{display:flex; gap:6px; flex-wrap:wrap}
.flag{font:500 10.5px/1 ui-monospace,monospace; letter-spacing:.06em; text-transform:uppercase;
      padding:4px 8px; border-radius:5px; background:color-mix(in srgb,var(--bad) 15%,transparent); color:var(--bad)}
footer{display:flex; gap:12px; align-items:center; padding:16px 22px 44px; max-width:1420px; margin:0 auto; flex-wrap:wrap}
table{border-collapse:collapse; width:100%; font-variant-numeric:tabular-nums}
th,td{text-align:left; padding:9px 12px; border-bottom:1px solid var(--line)}
th{font:500 11px/1 ui-monospace,monospace; letter-spacing:.12em; text-transform:uppercase; color:var(--muted)}
.results{margin:0 22px 40px; padding:20px 22px; max-width:1420px}
@media (min-width:1464px){.results{margin-inline:auto}}
.hint{color:var(--muted); font-size:13px}
@media (prefers-reduced-motion:reduce){*{transition:none!important}}
"""

_JS = r"""
const $ = (s,r=document)=>r.querySelector(s);
const KEY = 'replay-grades-v1';
let i = 0;
let grades = JSON.parse(localStorage.getItem(KEY) || '{}');

function esc(s){const d=document.createElement('div'); d.textContent=s; return d.innerHTML;}

function render(){
  const c = CASES[i];
  $('#pos').textContent = `${i+1} / ${CASES.length}`;
  $('#fan').textContent = 'fan ' + c.fan_id;
  $('#when').textContent = c.called_at.slice(0,16);
  $('#qchip').hidden = !c.quote;
  const done = Object.keys(grades).length;
  $('#done').textContent = `${done} graded`;
  $('#bar>i').style.width = (100*done/CASES.length) + '%';

  $('#msgs').innerHTML = c.convo.map((m,idx)=>{
    const last = idx===c.convo.length-1 && m.d==='in';
    return `<div class="msg ${m.d==='in'?'in':'out'}${last?' last':''}">${esc(m.t)}</div>`;
  }).join('');

  const g = grades[c.id] || {};
  $('#cards').innerHTML = c.slots.map((s,n)=>`
    <article class="panel card" data-slot="${n}" ${g[n]?`data-rated="${g[n]}"`:''}>
      <div class="eyebrow">Reply ${n+1}</div>
      <div class="reply">${esc(s.t)||'<em>(empty)</em>'}</div>
      ${s.f.length?`<div class="flags">${s.f.map(x=>`<span class="flag">${esc(x)}</span>`).join('')}</div>`:''}
      <div class="rates">${RATINGS.map(([k,l])=>
        `<button data-r="${k}" aria-pressed="${g[n]===k}">${l}</button>`).join('')}</div>
    </article>`).join('');
}

$('#cards').addEventListener('click', e=>{
  const b = e.target.closest('button[data-r]'); if(!b) return;
  const card = b.closest('.card'), n = card.dataset.slot, id = CASES[i].id;
  grades[id] = grades[id] || {};
  if(grades[id][n] === b.dataset.r) delete grades[id][n]; else grades[id][n] = b.dataset.r;
  if(!Object.keys(grades[id]).length) delete grades[id];
  localStorage.setItem(KEY, JSON.stringify(grades));
  render();
});

function go(d){ i = Math.min(CASES.length-1, Math.max(0, i+d)); render(); window.scrollTo({top:0}); }
$('#prev').onclick = ()=>go(-1);
$('#next').onclick = ()=>go(1);
document.addEventListener('keydown', e=>{
  if(e.target.tagName==='BUTTON' && e.key===' ') return;
  if(e.key==='ArrowLeft') go(-1);
  if(e.key==='ArrowRight') go(1);
});

$('#reveal').onclick = ()=>{
  const score = {best:3, good:2, ok:1, bad:0};
  const tally = {};
  for(const [id, g] of Object.entries(grades)){
    const c = CASES.find(x=>String(x.id)===String(id)); if(!c) continue;
    for(const [n, r] of Object.entries(g)){
      const arm = c.slots[n].a;
      (tally[arm] = tally[arm] || {n:0, sum:0, best:0, good:0, ok:0, bad:0});
      tally[arm].n++; tally[arm].sum += score[r]; tally[arm][r]++;
    }
  }
  const arms = Object.keys(tally).sort();
  if(!arms.length){ $('#out').innerHTML = '<p class="hint">Grade a few replies first.</p>'; return; }
  $('#out').innerHTML = `
    <table><thead><tr><th>Arm</th><th>What it is</th><th>Rated</th><th>Mean</th>
    <th>Best</th><th>Good</th><th>Ok</th><th>Bad</th></tr></thead><tbody>
    ${arms.map(a=>{const t=tally[a]; return `<tr><td class="mono"><strong>${a}</strong></td>
      <td>${ARM_DESC[a]||''}</td><td>${t.n}</td>
      <td><strong>${(t.sum/t.n).toFixed(2)}</strong></td>
      <td>${t.best}</td><td>${t.good}</td><td>${t.ok}</td><td>${t.bad}</td></tr>`;}).join('')}
    </tbody></table>
    <p class="hint">Mean is Best=3 · Good=2 · Ok=1 · Bad=0. Slots were shuffled per case,
    so the arm labels were hidden until now.</p>`;
};

$('#copy').onclick = async ()=>{
  const rows = [];
  for(const [id, g] of Object.entries(grades)){
    const c = CASES.find(x=>String(x.id)===String(id)); if(!c) continue;
    for(const [n, r] of Object.entries(g)) rows.push({call_id:c.id, arm:c.slots[n].a, rating:r});
  }
  const payload = JSON.stringify({graded_cases:Object.keys(grades).length, ratings:rows});
  try{ await navigator.clipboard.writeText(payload); $('#copy').textContent='Copied'; }
  catch{ $('#out').innerHTML = `<pre class="mono" style="white-space:pre-wrap">${payload}</pre>`; }
  setTimeout(()=>$('#copy').textContent='Copy results', 1600);
};

$('#reset').onclick = ()=>{
  if(!confirm('Clear every rating you have given?')) return;
  grades = {}; localStorage.removeItem(KEY); $('#out').innerHTML=''; render();
};
render();
"""


def _convo(block: str) -> list[dict]:
    out = []
    for line in block.splitlines():
        if line.startswith("FAN: "):
            out.append({"d": "in", "t": line[5:]})
        elif line.startswith("YOU: "):
            out.append({"d": "out", "t": line[5:]})
    return out


def build(rows: list[dict], arms_desc: dict[str, str]) -> str:
    cases = []
    for r in rows:
        arms = sorted(r["texts"])
        slots = [{"a": a, "t": r["texts"][a], "f": r["hard"].get(a, [])} for a in arms]
        random.Random(f"slot:{r['call_id']}").shuffle(slots)
        cases.append({
            "id": r["call_id"], "fan_id": r["fan_id"],
            "called_at": r["called_at"], "convo": _convo(r["convo"]),
            "quote": "[replying to" in r["convo"], "slots": slots,
        })
    # base64 so a curious glance at the DOM/source does not un-blind the grader.
    blob = base64.b64encode(json.dumps(cases).encode()).decode()
    return f"""<title>Prompt arms — blind grading</title>
<style>{_CSS}</style>
<header>
  <h1>Prompt arms <span>· blind grading</span></h1>
  <span class="chip mono" id="pos"></span>
  <span class="chip mono" id="fan"></span>
  <span class="chip mono" id="when"></span>
  <span class="chip q" id="qchip" hidden>quote-reply</span>
  <span class="grow"></span>
  <span class="eyebrow" id="done"></span>
  <div class="bar" id="bar"><i></i></div>
</header>
<main>
  <section class="panel convo">
    <div class="meta"><span class="eyebrow">The conversation</span></div>
    <div class="msgs" id="msgs"></div>
    <p class="hint" style="margin-top:16px">The outlined bubble is what the fan is
    waiting on. Rate each candidate reply on the right.</p>
  </section>
  <section class="cards" id="cards"></section>
</main>
<footer>
  <button id="prev">← Previous</button>
  <button id="next" class="primary">Next →</button>
  <span class="grow"></span>
  <button id="reveal">Reveal which arm is which</button>
  <button id="copy">Copy results</button>
  <button id="reset">Clear</button>
</footer>
<section class="panel results" id="out"></section>
<script>
const RATINGS = {json.dumps(RATINGS)};
const ARM_DESC = {json.dumps(arms_desc)};
const CASES = JSON.parse(atob("{blob}"));
{_JS}
</script>
"""


def main() -> None:
    src, dst = sys.argv[1], sys.argv[2]
    rows = json.load(open(src))
    desc = {
        "A": "Current production prompt",
        "B": "Current + a CURRENT TASK footer naming the target message",
        "C": "Same rules, regrouped and reordered — zero policy removed",
        "D": "Stripped: persona + facts only, every behavioural rule dropped",
    }
    with open(dst, "w") as fh:
        fh.write(build(rows, desc))
    print(f"{dst}  ({len(rows)} cases)", file=sys.stderr)


if __name__ == "__main__":
    main()
