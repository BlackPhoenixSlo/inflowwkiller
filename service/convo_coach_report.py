"""
convo_coach_report.py — the WRITE half of the 4-hourly convo coach.

Runs on the laptop. Takes the JSON that `convo_coach_fetch.py` produced inside
fastt-relay and turns it into the three files a batch folder holds:

    convos.md   the sampled threads as readable transcripts
    prompt.md   exactly what the reviewer was asked, transcripts included
    index.md    (repo-level, updated in place) one row per batch, newest first

The shell then pipes `prompt.md` into `claude -p` and saves the answer as
`review.md`. Keeping the prompt on disk is not decoration: six months from now
the only way to judge whether a finding was fair is to see what the reviewer
could actually see when it made it.

── Why the transcript is rebuilt here and not read off the HTML ───────────────
The viewer page is for a human with a mouse. A reviewer reading 60 bubbles
needs the things a chat bubble hides: which automation produced each outbound
line, what the picture was, what it cost, whether he opened it, and — above all
— where the four-hour window starts, so it writes findings about tonight rather
than about a line she sent last Tuesday.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# OF stores message bodies as HTML — `<p><span>hey</span></p>`, with `<br />`
# for the line breaks. Unstripped, the reviewer spends its attention on markup
# and quotes tags back at you. The viewer already solved this; borrow its
# solution rather than writing a second, subtly different one. Same directory,
# so sys.path[0] already covers it when run by path.
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
from convo_review_render import to_text  # noqa: E402

# Enough prior review to give the loop a memory, not so much that the rubric
# drowns. Three batches is half a day of context.
PRIOR_BATCHES = 3
PRIOR_CHARS = 6000

STALL_HINT = {
    "dead_offer": "the last thing in the thread is a PPV he never opened",
    "we_went_quiet": "he kept talking and nothing came back",
    "he_went_quiet": "she spoke last and he never returned",
    "no_ask": "he talked and was never offered anything",
    "faded": "no clear break — it just stopped",
}


def money(cents: int) -> str:
    return f"${cents / 100:,.2f}"


def fmt_ts(ts: str | None) -> str:
    if not ts:
        return "??:??"
    return (ts[5:16] or "").replace("T", " ")


def bubble(m: dict) -> str:
    """One message as one markdown line.

    Shape: `| ◂ 09-11 14:03 | she [ai_chatter] | body | $12 unopened |`
    Deliberately a table row: a reviewer quoting evidence can cite the line, and
    a 60-row table is far easier to scan for a turn than 60 paragraphs.
    """
    if "gap" in m:
        return f"| … | | _{m['gap']} earlier messages not shown_ | |"

    who = "HIM" if m["dir"] == "in" else "her"
    kind = f" [{m['kind']}]" if m.get("kind") else ""
    mark = "▸" if m.get("in_window") else " "

    body = to_text(m.get("body")).replace("\n", " ").replace("|", "\\|").strip()
    if not body:
        body = "_(no text)_"

    tags: list[str] = []
    if m.get("n_media"):
        desc = to_text(m.get("desc")).replace("\n", " ").replace("|", "\\|").strip()
        tags.append(f"{m['n_media']} media" + (f" — {desc}" if desc else ""))
    if m.get("price"):
        tags.append(money(m["price"]) + (" OPENED" if m.get("paid") else " **unopened**"))
    if m.get("tip"):
        tags.append("tip")
    if m.get("hit"):
        tags.append("⚠️")

    return (f"| {mark} {fmt_ts(m.get('ts'))} | {who}{kind} | {body} | "
            f"{' · '.join(tags)} |")


def thread_md(t: dict, n: int) -> str:
    """One conversation, header + transcript."""
    head = [
        f"### {n}. {to_text(t.get('fan_name')) or 'fan'} — "
        f"{to_text(t.get('account_name')) or t['account_id']}",
        "",
        f"- **outcome:** `{t['outcome']}`"
        + (f" — {STALL_HINT.get(t.get('stall',''), t.get('stall',''))}"
           if t["outcome"] == "nowhere" else ""),
        f"- **in the window:** {t.get('in_window', 0)} from him, "
        f"{t.get('out_window', 0)} from her",
    ]
    if t["outcome"] == "solved":
        head.append(f"- **money:** {money(t.get('window_cents', 0))}"
                    + (f", {t['window_tips']} tip(s)" if t.get("window_tips") else "")
                    + (f", **{t['window_chargebacks']} charged back**"
                       if t.get("window_chargebacks") else ""))
    if t.get("signals"):
        head.append("- **flags:** " + ", ".join(f"`{k}`×{v}"
                                                for k, v in sorted(t["signals"].items())))
    if t.get("lifetime_cents"):
        head.append(f"- **he has spent:** {money(t['lifetime_cents'])} lifetime")
    if t.get("truncated"):
        head.append(f"- _(showing the last {t.get('shown', 0)} messages of a longer thread)_")

    head += [
        "",
        "`▸` marks the messages inside the review window; everything above it is "
        "history for context.",
        "",
        "| | who | message | |",
        "|---|---|---|---|",
    ]
    return "\n".join(head + [bubble(m) for m in t.get("messages", [])]) + "\n"


def convos_md(payload: dict) -> str:
    threads = payload.get("threads", [])
    n_solved = sum(1 for t in threads if t["outcome"] == "solved")
    out = [
        f"# Sampled conversations — {payload.get('generated_at')} UTC",
        "",
        f"Window: last **{payload.get('window_hours')}h** (since "
        f"{payload.get('window_since')}Z). "
        f"{payload.get('counts', {}).get('judged', 0)} threads had a fan reply and "
        f"were finished enough to judge; **{len(threads)}** sampled — "
        f"{n_solved} closed, {len(threads) - n_solved} went nowhere.",
        "",
    ]
    for i, t in enumerate(threads, 1):
        out.append(thread_md(t, i))
        out.append("")
    return "\n".join(out)


# ── the rubric ───────────────────────────────────────────────────────────────
# This is the part worth arguing about. Everything else is plumbing.
RUBRIC = """\
You are reviewing real OnlyFans conversations from the last few hours, sampled
automatically. Roughly half went nowhere, the rest closed. You are not writing
a report card — you are looking for **changes worth making to the chatter AI**.

Each outbound line is tagged with the automation that produced it (`ai_chatter`,
`send_welcome`, `upsell`, `send_followup`, `mass_*`, and so on), or untagged if
a human sent it. That tag is how a finding becomes actionable: name the producer.

## What to write, per conversation

**1. What he wanted** — one line, grounded in his own words.
**2. What she did** — one line.
**3. The turn** — the single bubble where it changed, quoted, with its timestamp.
   For a thread that closed, this is the line that *earned* it.
**4. Verdict** — one of:
   - `FIX` — there is a specific, repeatable change worth making.
   - `FINE` — she handled it; nothing to change. Say so and stop. A batch where
     everything is FINE is a real and useful answer.
   - `UNCLEAR` — the transcript does not contain enough to judge. Say what is
     missing.
**5. If FIX** — the change, in the form: *when <situation>, she should <do X>
   instead of <what she did>* — plus which automation owns it.

## Ground rules

- **Quote or drop it.** Every claim points at a bubble in the transcript. If you
  cannot quote it, you cannot claim it.
- **Judge the window, not the history.** Lines marked `▸` are what this batch is
  about. The rest is context so you know who he is.
- **`unopened` is not automatically a failure** and a purchase is not
  automatically a success. Ask what the previous three bubbles earned.
- **No generic coaching.** "Be more engaging", "build rapport", "personalise it"
  are not findings. A finding names a trigger and a behaviour.
- **Do not invent context.** You cannot see her prompt, her vault, her pricing
  rules or her schedule. If a line looks wrong but a rule you cannot see might
  explain it, say that — it belongs in UNCLEAR, not FIX.
- **Repeats are signal.** If something you flagged in an earlier batch happened
  again here, say so explicitly and raise it in the summary. If a thread proves
  an earlier finding WRONG, say that too — that is the most valuable line you
  can write.

## Output

Markdown. Start with a single `# ` line: one sentence, the headline of this
batch — it becomes the row in the rolling index, so make it specific
("three threads died on the same price jump", not "some issues found").

Then:

## The one thing
The single change most worth making, or "nothing this batch". Be concrete
enough that someone could open the right file and act on it.

## Conversations
One `### ` section per conversation, in the order given, using the five points
above. Short. A FINE verdict is two lines.

## Watching
Anything that is not yet a finding but would be if it shows up again. Carry
forward unresolved items from earlier batches and drop resolved ones.

Under 800 words. No preamble, no restating these instructions.
"""


def prior_context(root: Path, current: Path) -> str:
    """The last few reviews, so the loop compounds instead of repeating itself.

    Without this every batch re-discovers the same three habits and the folder
    becomes six identical opinions a day.
    """
    batches = sorted((p for p in root.iterdir()
                      if p.is_dir() and p.name != "state" and p != current
                      and (p / "review.md").is_file()),
                     key=lambda p: p.name, reverse=True)[:PRIOR_BATCHES]
    if not batches:
        return ""
    chunks = ["\n---\n\n# What you already said\n",
              "Earlier batches, newest first. Do not repeat a finding that is "
              "already here — either confirm it recurred (with the new evidence), "
              "retract it, or move on to something new.\n"]
    budget = PRIOR_CHARS
    for b in batches:
        text = (b / "review.md").read_text(errors="replace").strip()
        if len(text) > budget:
            text = text[:budget] + "\n…(truncated)"
        budget -= len(text)
        chunks.append(f"\n## Batch {b.name}\n\n{text}\n")
        if budget <= 0:
            break
    return "\n".join(chunks)


def build_prompt(payload: dict, transcripts: str, root: Path, current: Path) -> str:
    return "\n".join([RUBRIC, "\n---\n", transcripts, prior_context(root, current)])


# ── the rolling index ────────────────────────────────────────────────────────

def headline(review: Path) -> str:
    """The reviewer's `# ` line. Falls back to something honest rather than
    inventing a summary when the review is missing or malformed."""
    if not review.is_file():
        return "_(review missing — see the folder)_"
    for line in review.read_text(errors="replace").splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return "_(no headline line)_"


INDEX_HEAD = """\
# Convo coach — rolling index

Every four hours a launchd job samples 3-6 conversations from production where
a fan actually replied, splits them into the ones that closed and the ones that
died, and has Claude review them against the rubric in each batch's `prompt.md`.
Newest first. Each row links to the batch folder: `review.md` is the findings,
`convos.html` is the readable transcripts, `convos.md` is what the reviewer saw.

Run one by hand: `scripts/convo-coach.sh`

| when (UTC) | window | sampled | headline |
|---|---|---|---|
"""


def update_index(root: Path, batch: Path, payload: dict) -> None:
    """Rebuilt from the folders on disk, not appended to.

    Appending means a re-run of an existing batch silently duplicates its row,
    and a deleted folder leaves a dead link forever. The folders are the truth;
    this file is a view of them.
    """
    rows = []
    for b in sorted((p for p in root.iterdir()
                     if p.is_dir() and p.name != "state" and not p.is_symlink()),
                    key=lambda p: p.name, reverse=True):
        meta: dict[str, Any] = {}
        pack = b / "pack.json"
        if pack.is_file():
            try:
                meta = json.loads(pack.read_text())
            except Exception:  # noqa: BLE001
                meta = {}
        threads = meta.get("threads", [])
        n_solved = sum(1 for t in threads if t.get("outcome") == "solved")
        sampled = (f"{len(threads)} ({n_solved}💰/{len(threads) - n_solved}🧊)"
                   if threads else "—")
        rows.append(f"| [{b.name}]({b.name}/review.md) | "
                    f"{meta.get('window_hours', '?')}h | {sampled} | "
                    f"{headline(b / 'review.md')} |")
    (root / "index.md").write_text(INDEX_HEAD + "\n".join(rows) + "\n")


def update_seen(state: Path, payload: dict) -> None:
    """Remember which threads were reviewed, so the next batch finds new ones."""
    state.parent.mkdir(parents=True, exist_ok=True)
    seen: dict[str, str] = {}
    if state.is_file():
        try:
            loaded = json.loads(state.read_text())
            if isinstance(loaded, dict):
                seen = loaded
        except Exception:  # noqa: BLE001
            seen = {}
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    for t in payload.get("threads", []):
        if t.get("key"):
            seen[t["key"]] = stamp
    state.write_text(json.dumps(seen, indent=1, sort_keys=True))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("pack", help="JSON from convo_coach_fetch.py")
    ap.add_argument("--batch", required=True, help="the batch folder to write into")
    ap.add_argument("--root", required=True, help="reports/convo-coach")
    ap.add_argument("--stage", choices=("prompt", "finish"), default="prompt",
                    help="'prompt' writes convos.md + prompt.md; 'finish' runs "
                         "after the reviewer answered and updates index + seen")
    args = ap.parse_args()

    payload = json.loads(Path(args.pack).read_text())
    root = Path(args.root)
    batch = Path(args.batch)
    batch.mkdir(parents=True, exist_ok=True)

    if args.stage == "prompt":
        transcripts = convos_md(payload)
        (batch / "convos.md").write_text(transcripts)
        (batch / "prompt.md").write_text(build_prompt(payload, transcripts, root, batch))
        print(f"  convos.md  {len(transcripts):,} chars, "
              f"{len(payload.get('threads', []))} threads")
        return 0

    update_seen(root / "state" / "seen.json", payload)
    update_index(root, batch, payload)
    print(f"  index.md updated · {headline(batch / 'review.md')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
