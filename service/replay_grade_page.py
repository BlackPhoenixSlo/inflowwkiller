"""Turn a replay_arms `--json-out` dump into an inspection page.

Shows, per case: the real conversation, and for EACH arm the prompt it actually sent
and the answer that came back. The prompt is rendered as its blocks, diffed against
arm A — kept / moved / dropped / added — because four 12KB walls of text side by side
is not something a person can read, and the difference is the whole point.

    python replay_arms.py --arms A,B,C,D --n 8 --json-out out.json
    python replay_grade_page.py out.json inspect.html
"""
from __future__ import annotations

import json
import sys

RATINGS = [("best", "Best"), ("good", "Good"), ("ok", "Ok"), ("bad", "Bad")]

ARM_DESC = {
    "A": "Current production prompt — the control",
    "B": "A + a CURRENT TASK footer naming the message he is waiting on",
    "C": "Same blocks regrouped: identity → hard rules → voice → situational → this turn → contract",
    "D": "Stripped to persona + facts. Every behavioural rule block dropped",
}

_CSS = """
*,*::before,*::after{box-sizing:border-box}
:root{
  --ground:#F4F7F7; --surface:#FFFFFF; --sunk:#EAEFEF; --raise:#FDFEFE;
  --ink:#0E1719; --muted:#5B7075; --line:#D6E0E0;
  --accent:#0E6E70; --accent-ink:#FFFFFF;
  --best:#1B6E48; --good:#2C5FA8; --ok:#6E7A7C; --bad:#9E2B36;
  --add:#1B6E48; --move:#8A6212; --drop:#9E2B36;
  --fan:#E8EEEF; --her:#0E6E70;
  --shadow:0 1px 2px rgba(14,23,25,.06),0 8px 24px -12px rgba(14,23,25,.16);
}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){
  --ground:#0B1112; --surface:#141D1F; --sunk:#101819; --raise:#1A2427;
  --ink:#DFEAEB; --muted:#8CA1A5; --line:#243133;
  --accent:#3AA9A6; --accent-ink:#07211F;
  --best:#57C48D; --good:#7BAAE8; --ok:#8B989A; --bad:#E2707A;
  --add:#57C48D; --move:#D6A745; --drop:#E2707A;
  --fan:#1E2A2C; --her:#3AA9A6;
  --shadow:0 1px 2px rgba(0,0,0,.4),0 10px 28px -14px rgba(0,0,0,.7);
}}
:root[data-theme="dark"]{
  --ground:#0B1112; --surface:#141D1F; --sunk:#101819; --raise:#1A2427;
  --ink:#DFEAEB; --muted:#8CA1A5; --line:#243133;
  --accent:#3AA9A6; --accent-ink:#07211F;
  --best:#57C48D; --good:#7BAAE8; --ok:#8B989A; --bad:#E2707A;
  --add:#57C48D; --move:#D6A745; --drop:#E2707A;
  --fan:#1E2A2C; --her:#3AA9A6;
  --shadow:0 1px 2px rgba(0,0,0,.4),0 10px 28px -14px rgba(0,0,0,.7);
}
html{-webkit-text-size-adjust:100%}
body{margin:0; background:var(--ground); color:var(--ink);
 font:400 15px/1.55 ui-sans-serif,-apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}
.mono{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
.eyebrow{font:500 11px/1 ui-monospace,SFMono-Regular,Menlo,monospace;
 letter-spacing:.14em; text-transform:uppercase; color:var(--muted)}
header{position:sticky; top:0; z-index:30; background:var(--surface);
 border-bottom:1px solid var(--line); padding:13px 20px;
 display:flex; align-items:center; gap:14px; flex-wrap:wrap}
h1{font-size:16px; font-weight:640; letter-spacing:-.01em; margin:0}
h1 span{color:var(--muted); font-weight:400}
.grow{flex:1}
button{font:inherit; color:inherit; background:var(--surface); cursor:pointer;
 border:1px solid var(--line); border-radius:8px; padding:7px 13px}
button:hover{border-color:var(--accent)}
button:focus-visible{outline:2px solid var(--accent); outline-offset:2px}
button.primary{background:var(--accent); color:var(--accent-ink); border-color:transparent; font-weight:560}
main{display:grid; grid-template-columns:minmax(0,34fr) minmax(0,66fr);
 gap:20px; padding:20px; max-width:1560px; margin:0 auto; align-items:start}
@media (max-width:1000px){main{grid-template-columns:1fr}}
.panel{background:var(--surface); border:1px solid var(--line); border-radius:13px; box-shadow:var(--shadow)}
.convo{position:sticky; top:74px; padding:15px 17px; max-height:calc(100vh-96px); overflow:auto}
@media (max-width:1000px){.convo{position:static; max-height:none}}
.chip{font:500 11px/1 ui-monospace,monospace; letter-spacing:.05em; padding:5px 9px;
 border-radius:99px; background:var(--sunk); color:var(--muted); white-space:nowrap}
.chip.q{background:color-mix(in srgb,var(--accent) 16%,transparent); color:var(--accent)}
.msgs{display:flex; flex-direction:column; gap:7px; margin-top:12px}
.msg{max-width:90%; padding:8px 12px; border-radius:13px; white-space:pre-wrap; word-break:break-word; font-size:14.5px}
.msg.in{background:var(--fan); border-bottom-left-radius:4px; align-self:flex-start}
.msg.out{background:var(--her); color:var(--accent-ink); border-bottom-right-radius:4px; align-self:flex-end}
.msg.last{outline:2px solid var(--accent); outline-offset:2px}
.arms{display:flex; flex-direction:column; gap:14px}
.arm{padding:0; overflow:hidden}
.arm>.top{display:flex; align-items:baseline; gap:12px; padding:13px 16px 0; flex-wrap:wrap}
.tag{font:700 15px/1 ui-monospace,monospace; letter-spacing:.04em;
 width:30px; height:30px; display:grid; place-items:center; border-radius:8px;
 background:var(--accent); color:var(--accent-ink); flex:none}
.desc{color:var(--muted); font-size:13px; flex:1; min-width:200px}
.answer{margin:11px 16px; padding:13px 15px; background:var(--sunk); border-radius:10px;
 white-space:pre-wrap; word-break:break-word; font-size:15.5px; min-height:1.5em}
.flags{display:flex; gap:6px; flex-wrap:wrap; padding:0 16px 10px}
.flag{font:500 10.5px/1 ui-monospace,monospace; letter-spacing:.05em; text-transform:uppercase;
 padding:4px 8px; border-radius:5px; background:color-mix(in srgb,var(--bad) 15%,transparent); color:var(--bad)}
.rates{display:flex; gap:7px; flex-wrap:wrap; padding:0 16px 12px}
.rates button{padding:5px 13px; font-size:13px; border-radius:99px}
.rates button[aria-pressed="true"]{color:var(--accent-ink); border-color:transparent; font-weight:600}
.rates button[data-r="best"][aria-pressed="true"]{background:var(--best)}
.rates button[data-r="good"][aria-pressed="true"]{background:var(--good)}
.rates button[data-r="ok"][aria-pressed="true"]{background:var(--ok)}
.rates button[data-r="bad"][aria-pressed="true"]{background:var(--bad)}
details{border-top:1px solid var(--line)}
summary{cursor:pointer; padding:11px 16px; font:500 12px/1.3 ui-monospace,monospace;
 letter-spacing:.06em; text-transform:uppercase; color:var(--muted); list-style:none}
summary::-webkit-details-marker{display:none}
summary::before{content:"▸ "; color:var(--accent)}
details[open]>summary::before{content:"▾ "}
summary:hover{color:var(--accent)}
.blocks{padding:0 16px 14px; display:flex; flex-direction:column; gap:5px}
.blk{border:1px solid var(--line); border-radius:8px; background:var(--raise); overflow:hidden}
.blk>summary{display:flex; gap:9px; align-items:center; padding:8px 11px; text-transform:none;
 letter-spacing:0; font-family:inherit; font-size:13.5px; color:var(--ink)}
.blk pre{margin:0; padding:10px 12px; background:var(--sunk); font:400 12.5px/1.5 ui-monospace,monospace;
 white-space:pre-wrap; word-break:break-word; overflow-x:auto}
.badge{font:600 10px/1 ui-monospace,monospace; letter-spacing:.07em; text-transform:uppercase;
 padding:3px 6px; border-radius:4px; flex:none}
.badge.moved{background:color-mix(in srgb,var(--move) 18%,transparent); color:var(--move)}
.badge.dropped{background:color-mix(in srgb,var(--drop) 16%,transparent); color:var(--drop)}
.badge.added{background:color-mix(in srgb,var(--add) 16%,transparent); color:var(--add)}
.blk.dropped>summary{opacity:.55; text-decoration:line-through}
.bnum{font:500 11px/1 ui-monospace,monospace; color:var(--muted); flex:none; width:2.2em}
.bhead{flex:1; min-width:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap}
.bsize{font:400 11px/1 ui-monospace,monospace; color:var(--muted); flex:none}
footer{display:flex; gap:11px; align-items:center; padding:14px 20px 40px;
 max-width:1560px; margin:0 auto; flex-wrap:wrap}
table{border-collapse:collapse; width:100%; font-variant-numeric:tabular-nums}
th,td{text-align:left; padding:9px 12px; border-bottom:1px solid var(--line)}
th{font:500 11px/1 ui-monospace,monospace; letter-spacing:.11em; text-transform:uppercase; color:var(--muted)}
.results{margin:0 20px 40px; padding:18px 20px; max-width:1560px}
.hint{color:var(--muted); font-size:13px}
@media (prefers-reduced-motion:reduce){*{transition:none!important}}
"""

_JS = r"""
const $=(s,r=document)=>r.querySelector(s), KEY='replay-inspect-v1';
let i=0, grades=JSON.parse(localStorage.getItem(KEY)||'{}');
const esc=s=>{const d=document.createElement('div'); d.textContent=s; return d.innerHTML;};

function blockRows(arm, c){
  const base = c.prompts.A.sys_blocks, mine = c.prompts[arm].sys_blocks;
  const baseIdx = new Map(base.map((b,n)=>[b,n]));
  const rows = mine.map((b,n)=>{
    const from = baseIdx.has(b) ? baseIdx.get(b) : null;
    const st = from===null ? 'added' : (from!==n ? 'moved' : '');
    return {b, n, from, st};
  });
  const kept = new Set(mine);
  base.forEach((b,n)=>{ if(!kept.has(b)) rows.push({b, n, from:n, st:'dropped'}); });
  return rows;
}

function render(){
  const c=CASES[i];
  $('#pos').textContent=`${i+1} / ${CASES.length}`;
  $('#fan').textContent='fan '+c.fan_id;
  $('#when').textContent=c.called_at.slice(0,16);
  $('#qchip').hidden=!c.quote;
  $('#msgs').innerHTML=c.convo.map((m,n)=>{
    const last=n===c.convo.length-1&&m.d==='in';
    return `<div class="msg ${m.d==='in'?'in':'out'}${last?' last':''}">${esc(m.t)}</div>`;}).join('');

  const g=grades[c.id]||{};
  $('#arms').innerHTML=ARMS.map(a=>{
    const rows=blockRows(a,c), sys=c.prompts[a].sys_blocks.join('\n\n');
    const nMoved=rows.filter(r=>r.st==='moved').length, nDrop=rows.filter(r=>r.st==='dropped').length;
    const userExtra=c.prompts[a].user.length-c.prompts.A.user.length;
    const summary=[`${c.prompts[a].sys_blocks.length} blocks`, `${sys.length.toLocaleString()} chars`,
      nMoved?`${nMoved} moved`:'', nDrop?`${nDrop} dropped`:'',
      userExtra>0?`+${userExtra} chars on the user message`:''].filter(Boolean).join(' · ');
    return `<article class="panel arm">
      <div class="top"><span class="tag">${a}</span>
        <span class="desc">${esc(ARM_DESC[a]||'')}</span></div>
      <div class="answer">${esc(c.texts[a])||'<em>(empty)</em>'}</div>
      ${(c.hard[a]||[]).length?`<div class="flags">${c.hard[a].map(f=>`<span class="flag">${esc(f)}</span>`).join('')}</div>`:''}
      <div class="rates">${RATINGS.map(([k,l])=>
        `<button data-arm="${a}" data-r="${k}" aria-pressed="${g[a]===k}">${l}</button>`).join('')}</div>
      <details><summary>Prompt — ${summary}</summary>
        <div class="blocks">
          ${userExtra>0?`<details class="blk"><summary><span class="bnum">+</span>
            <span class="badge added">added</span>
            <span class="bhead">appended to the user message</span></summary>
            <pre>${esc(c.prompts[a].user.slice(c.prompts.A.user.length))}</pre></details>`:''}
          ${rows.map(r=>`<details class="blk ${r.st==='dropped'?'dropped':''}">
            <summary><span class="bnum">${r.st==='dropped'?'—':r.n+1}</span>
              ${r.st?`<span class="badge ${r.st}">${r.st==='moved'?`moved ${r.from+1}→${r.n+1}`:r.st}</span>`:''}
              <span class="bhead">${esc(r.b.split('\n')[0])}</span>
              <span class="bsize">${r.b.length}c</span></summary>
            <pre>${esc(r.b)}</pre></details>`).join('')}
        </div>
        <details class="blk" style="margin:0 16px 14px"><summary>
          <span class="bhead">Full user message (facts, pins, her day, transcript)</span></summary>
          <pre>${esc(c.prompts[a].user)}</pre></details>
      </details></article>`;}).join('');
}

$('#arms').addEventListener('click',e=>{
  const b=e.target.closest('button[data-r]'); if(!b) return;
  const id=CASES[i].id, a=b.dataset.arm;
  grades[id]=grades[id]||{};
  if(grades[id][a]===b.dataset.r) delete grades[id][a]; else grades[id][a]=b.dataset.r;
  if(!Object.keys(grades[id]).length) delete grades[id];
  localStorage.setItem(KEY,JSON.stringify(grades)); render();
});
function go(d){i=Math.min(CASES.length-1,Math.max(0,i+d)); render(); window.scrollTo({top:0});}
$('#prev').onclick=()=>go(-1); $('#next').onclick=()=>go(1);
document.addEventListener('keydown',e=>{
  if(/INPUT|TEXTAREA/.test(e.target.tagName)) return;
  if(e.key==='ArrowLeft')go(-1); if(e.key==='ArrowRight')go(1);});
$('#tally').onclick=()=>{
  const sc={best:3,good:2,ok:1,bad:0}, t={};
  for(const [id,g] of Object.entries(grades))
    for(const [a,r] of Object.entries(g)){(t[a]=t[a]||{n:0,s:0,best:0,good:0,ok:0,bad:0});t[a].n++;t[a].s+=sc[r];t[a][r]++;}
  const arms=Object.keys(t).sort();
  $('#out').innerHTML=arms.length?`<table><thead><tr><th>Arm</th><th>Rated</th><th>Mean</th>
    <th>Best</th><th>Good</th><th>Ok</th><th>Bad</th></tr></thead><tbody>
    ${arms.map(a=>`<tr><td class="mono"><strong>${a}</strong></td><td>${t[a].n}</td>
      <td><strong>${(t[a].s/t[a].n).toFixed(2)}</strong></td><td>${t[a].best}</td>
      <td>${t[a].good}</td><td>${t[a].ok}</td><td>${t[a].bad}</td></tr>`).join('')}</tbody></table>
    <p class="hint">Best=3 · Good=2 · Ok=1 · Bad=0. ${Object.keys(grades).length} of ${CASES.length} cases rated.</p>`
    :'<p class="hint">Rate a few replies first.</p>';};
$('#reset').onclick=()=>{if(confirm('Clear every rating?')){grades={};localStorage.removeItem(KEY);$('#out').innerHTML='';render();}};
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


def build(rows: list[dict]) -> str:
    arms = sorted({a for r in rows for a in r["texts"]})
    cases = [{"id": r["call_id"], "fan_id": r["fan_id"], "called_at": r["called_at"],
              "convo": _convo(r["convo"]), "quote": "[replying to" in r["convo"],
              "texts": r["texts"], "hard": r["hard"], "prompts": r["prompts"]}
             for r in rows]
    return f"""<title>Prompt arms A/B/C/D — prompts and answers</title>
<style>{_CSS}</style>
<header>
  <h1>Prompt arms <span>· A / B / C / D</span></h1>
  <span class="chip mono" id="pos"></span>
  <span class="chip mono" id="fan"></span>
  <span class="chip mono" id="when"></span>
  <span class="chip q" id="qchip" hidden>quote-reply</span>
  <span class="grow"></span>
  <button id="prev">←</button><button id="next" class="primary">Next →</button>
</header>
<main>
  <section class="panel convo">
    <span class="eyebrow">The conversation</span>
    <div class="msgs" id="msgs"></div>
    <p class="hint" style="margin-top:14px">The outlined bubble is what he is waiting
    on. Each arm answered exactly this.</p>
  </section>
  <section class="arms" id="arms"></section>
</main>
<footer>
  <button id="prev2" onclick="document.getElementById('prev').click()">← Previous</button>
  <button onclick="document.getElementById('next').click()" class="primary">Next →</button>
  <span class="grow"></span>
  <button id="tally">Show my ratings</button><button id="reset">Clear</button>
</footer>
<section class="panel results" id="out"></section>
<script>
const RATINGS={json.dumps(RATINGS)}, ARM_DESC={json.dumps(ARM_DESC)},
      ARMS={json.dumps(arms)}, CASES={json.dumps(cases)};
{_JS}
</script>
"""


def main() -> None:
    rows = json.load(open(sys.argv[1]))
    with open(sys.argv[2], "w") as fh:
        fh.write(build(rows))
    print(f"{sys.argv[2]}  ({len(rows)} cases)", file=sys.stderr)


if __name__ == "__main__":
    main()
