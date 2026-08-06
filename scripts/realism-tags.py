#!/usr/bin/env python3
"""scripts/realism-tags.py — every switch and every prompt block that makes a
reply read like a person instead of a bot.

There was no single file holding this. The SWITCHES live in
`service/style_config_api.py` (which owns the allowlist — a key absent from it is
dropped silently on a 200), the DEFAULTS live in `service/automations/_common.py`
(`_STYLE_DEFAULT_ON`, plus one bespoke `load_*` per flag), and the PROMPT TEXT
lives in `service/automations/_voice.py` + `_common.py` + `cat_stickers.py`.

So this script does not COPY any of it — it imports the real constants and
renders them. A hand-written cheatsheet would be wrong within a week; this one is
wrong only if the code is.

    ./venv/bin/python scripts/realism-tags.py                 # the tag catalog
    ./venv/bin/python scripts/realism-tags.py --prompt        # the blocks, verbatim
    ./venv/bin/python scripts/realism-tags.py --prompt --voice him --customs
    ./venv/bin/python scripts/realism-tags.py --json          # a full-ON config body
    ./venv/bin/python scripts/realism-tags.py --curl 123456789  # ready-to-run PUT

Read-only. `--curl` PRINTS a command; it never sends one.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "service"))

from automations import _common as c          # noqa: E402
from automations import _voice                # noqa: E402
from automations import cat_stickers          # noqa: E402
from automations._pins import PINS_ENABLED_KEY, PINS_WRITE_KEY  # noqa: E402

AUTOMATIONS = c.STYLE_AUTOMATIONS   # of_ai_chat, autoreply, deep_convo, ai_chatter


# ── The catalog ──────────────────────────────────────────────────────
# (key, default, one-line effect, where the text/logic lives)
# `default` is what the LOADER resolves for an ABSENT key — not a flat False.
# Per-automation rows carry the tri-state: ai_chatter ON, every other sender OFF.

def _tri(automation: str) -> str:
    return "ON" if c._style_default(automation) else "off"


PER_AUTOMATION = [
    ("{a}", "humanizer + multi-bubble: lowercase, no em-dash, no echoing his "
            f"words, varied length, <=1 question; bubble cap {c.STYLE_MAX_BUBBLES}",
     "_voice._humanizer / _common.STYLE_3LINE + STYLE_BRIEF"),
    ("typos_{a}", "thumb-typo injector — ~1 slip per 5 sentences, capped 1/reply, "
                  "with an occasional '*fix' correction (throttled 1h / 50 replies)",
     "_common.humanize_typos"),
    ("nonnative_{a}", "non-native English: NONNATIVE_REGISTER in the prompt + a "
                      "deterministic misspelling dict applied post-generation",
     "_common.NONNATIVE_REGISTER / apply_nonnative_style"),
    ("nonnative_spacing_{a}", "the space-before-'?' habit — 'you like it ?' — "
                              "rolled at 26% per question-mark run; only applied "
                              "when nonnative_{a} is also on",
     "_common.apply_nonnative_spacing"),
]

# Same key shape, but keyed off CONSISTENCY_AUTOMATIONS and NOT tri-state: it
# costs a second LLM call per reply it fires on, so it needs an explicit True.
CONSISTENCY = [
    ("consistency_{a}", "pre-send self-consistency check (PHASE 2) — catches the "
                        "reply that contradicts something she already said. Costs "
                        "a 2nd LLM call. Explicit opt-in, no tri-state.",
     "_persona.verify_self_consistency"),
]

ACCOUNT_WIDE = [
    (c.PAINFUL_TEXTING_KEY, True,
     "THE FEEL OF TEXTING — the brevity governor, injected above everything else. "
     "Fewest words that land the feeling. This is the block doing most of the work.",
     "_voice._painful_texting"),
    (c.FACTGROUND_KEY, True,
     "Auto Convo grounds replies in the fan's rich profile instead of generic flirt.",
     "_common.load_factground_flag"),
    (c.CAT_STICKERS_KEY, True,
     "reaction-gif pack — she can end a reply with a sticker, or send just the "
     "sticker. Female lane = cats; male lane = dogs & wolves.",
     "automations/cat_stickers.py"),
    (c.SELL_CUSTOMS_KEY, False,
     "may this creator agree to a paid CUSTOM (voice note, recorded later, "
     "delivered on OF)? Governs what the bot may PROMISE — explicit opt-in.",
     "_voice.CUSTOMS_CONDITIONS"),
    ("strip_emojis", False,
     "strip EVERY emoji at the send chokepoint. Off by default — the humanizer "
     "already budgets 0-1.",
     "_common.load_strip_emojis"),
]

NUMERIC = [
    (c.CAT_STICKER_SKIP_PCT_KEY, 0.0, 100.0, "% of replies that hide the pack from the prompt"),
    (c.CAT_STICKER_SOLO_PCT_KEY, 5.0, 100.0, "% nudged toward a gif-ONLY reply"),
    (c.CAT_STICKER_GAP_MIN_KEY, 0.0, 7 * 24 * 60.0, "per-fan minutes between stickers"),
]

# In the same JSON blob and the same allowlist, but NOT realism — `pins_write`
# starts mutating OnlyFans. Never part of --json's all-on body.
NOT_REALISM = [
    (PINS_ENABLED_KEY, False, "pins: compute + log only (shadow)"),
    (PINS_WRITE_KEY, False, "pins: ALSO write to OnlyFans"),
]

# Realism knobs that do NOT live in style_config_json.
ELSEWHERE = [
    ("webhook_config_json.typing_wpm", "38", "send pacing — how long a reply takes "
                                             "to 'type'. 0 disables the delay."),
    ("webhook_config_json.typing_indicator", "ON", "the live typing bubble on OF."),
    ("accounts.voice", "her", "'her' or 'him' — flips the humanizer's pushback "
                              "register, the emoji vocabulary, the FaceTime refusal, "
                              "the off-platform deflections and the persona labels."),
    ("env STYLE_FORCE_OFF", "unset", "PANIC SWITCH — truthy forces every realism "
                                     "flag False fleet-wide, ignoring stored config."),
]


def cmd_tags() -> None:
    print("REALISM TAGS — account_ai_config.style_config_json\n")
    print("Tri-state: an EXPLICIT true/false always wins; an ABSENT key falls back")
    print("to the per-automation default below (ai_chatter ON, every other sender off).\n")

    print("PER-AUTOMATION  (one key per automation — substitute {a})")
    print(f"  {{a}} in: {', '.join(AUTOMATIONS)}\n")
    for tmpl, effect, where in PER_AUTOMATION:
        print(f"  {tmpl:<28} default: " +
              ", ".join(f"{a}={_tri(a)}" for a in AUTOMATIONS))
        print(f"  {'':<28} {effect}")
        print(f"  {'':<28} → {where}\n")
    for tmpl, effect, where in CONSISTENCY:
        print(f"  {tmpl:<28} default: off (explicit opt-in)")
        print(f"  {'':<28} only for: {', '.join(c.CONSISTENCY_AUTOMATIONS)}")
        print(f"  {'':<28} {effect}")
        print(f"  {'':<28} → {where}\n")

    print("ACCOUNT-WIDE")
    for key, default, effect, where in ACCOUNT_WIDE:
        print(f"  {key:<28} default: {'ON' if default else 'off'}")
        print(f"  {'':<28} {effect}")
        print(f"  {'':<28} → {where}\n")

    print("NUMERIC (never bools — style_config_api clamps them)")
    for key, default, hi, effect in NUMERIC:
        print(f"  {key:<28} default: {default:g}   max: {hi:g}   {effect}")

    print("\nSAME BLOB, NOT REALISM — left out of --json deliberately")
    for key, default, effect in NOT_REALISM:
        print(f"  {key:<28} default: {'ON' if default else 'off'}   {effect}")

    print("\nREALISM THAT LIVES SOMEWHERE ELSE")
    for key, default, effect in ELSEWHERE:
        print(f"  {key:<38} default: {default:<6} {effect}")

    print("\nALWAYS ON — no switch, in every conversational prompt")
    print("  ONPLATFORM_GUARDRAIL       never trade contact info / arrange a meetup")
    print("  LIVE_PROOF_GUARDRAIL       flat refusal to FaceTime / prove-you're-real")
    print("  NO_NARRATION_RULE          no *asterisk actions*, no stage directions")
    print("  BIO_CONSISTENCY_GUARDRAIL  never contradict the profile or an earlier claim")
    print("\n  See --prompt for all of them verbatim.")


def cmd_prompt(voice: str, customs: bool) -> None:
    v = _voice.blocks(voice, customs)
    lane = "MALE (him)" if v.is_male else "FEMALE (her, the shipped default)"

    def block(title: str, gate: str, text: str) -> None:
        print("=" * 72)
        print(f"{title}\n  gate: {gate}")
        print("=" * 72)
        print(text.rstrip() + "\n")

    print(f"# realism prompt stack — voice lane: {lane}, customs: {'on' if customs else 'off'}")
    print(f"# texter noun: '{v.texter_noun}'   fan address: '{v.fan_address}'\n")

    block("1. THE FEEL OF TEXTING (goes FIRST — governs everything below)",
          f"{c.PAINFUL_TEXTING_KEY} (default ON)", v.painful_texting)
    block("2. TEXT LIKE A REAL PERSON, NOT AN AI",
          f"<automation> — e.g. ai_chatter (default {_tri('ai_chatter')})", v.humanizer)
    block("3a. MULTI-BUBBLE STYLE DIE",
          f"same flag; raises the bubble cap to {c.STYLE_MAX_BUBBLES}", c.STYLE_3LINE)
    block("3b. BREVITY STYLE DIE (the counterweight — rolled against 3a)",
          "same flag", c.STYLE_BRIEF)
    block("4. NON-NATIVE REGISTER",
          "nonnative_<automation>", c.NONNATIVE_REGISTER)
    block("5. STICKER PACK",
          f"{c.CAT_STICKERS_KEY} (default ON)",
          cat_stickers.prompt_block("allow", v.voice))
    block("6. NO NARRATION (always on)", "none", c.NO_NARRATION_RULE)
    block("7. WHO YOU ARE (always on)", "none", c.BIO_CONSISTENCY_GUARDRAIL)
    block("8. STAY ON ONLYFANS (always on)", "none", c.ONPLATFORM_GUARDRAIL)
    block("9. LIVE PROOF / FACETIME (always on; carve-out is gated)",
          f"{c.SELL_CUSTOMS_KEY} appends the customs carve-out", v.live_proof)

    print("=" * 72)
    print("POST-GENERATION — applied to the TEXT, not the prompt")
    print("=" * 72)
    print("  typos_<automation>            ~1 slip / 5 sentences, max 1 per reply")
    print(f"  nonnative_<automation>        {len(c.NONNATIVE_MISSPELLINGS)} word swaps + "
          f"{len(c.NONNATIVE_PHRASES)} phrase swap(s), deterministic")
    sample = list(c.NONNATIVE_MISSPELLINGS.items())[:8]
    print("    e.g. " + ", ".join(f"{k}→{val}" for k, val in sample))
    print(f"  nonnative_spacing_<automation>  space before '?' at "
          f"{c._NONNATIVE_SPACE_Q_RATE:.0%} per '?' run")
    print("  strip_emojis                  every emoji removed at the send chokepoint")
    print("\n  Canned off-platform deflections (sent VERBATIM when the guard trips —")
    print("  no model in the loop, so these are pure voice):")
    for d in v.off_deflections:
        print(f"    {d!r}")


def full_on() -> dict:
    """Every realism tag explicitly ON, for every automation. Deliberately omits
    the pins keys (same blob, not realism, and pins_write mutates OnlyFans)."""
    cfg: dict = {}
    for a in AUTOMATIONS:
        cfg[a] = True
        cfg[c.typo_flag_key(a)] = True
        cfg[c.nonnative_flag_key(a)] = True
        cfg[c.spacing_flag_key(a)] = True
    for a in c.CONSISTENCY_AUTOMATIONS:
        cfg[c.consistency_flag_key(a)] = True      # ⚠ a 2nd LLM call per reply
    cfg[c.PAINFUL_TEXTING_KEY] = True
    cfg[c.FACTGROUND_KEY] = True
    cfg[c.CAT_STICKERS_KEY] = True
    cfg[c.SELL_CUSTOMS_KEY] = False                # promises a product — opt in by hand
    cfg["strip_emojis"] = False                    # humanizer already budgets 0-1
    for key, default, _hi, _e in NUMERIC:
        cfg[key] = default
    return cfg


def cmd_json() -> None:
    print(json.dumps(full_on(), indent=2))


def cmd_curl(account_id: str, base: str) -> None:
    body = {"account_id": account_id, "config": full_on()}
    print("# PUT merges onto what is already stored — absent keys keep resolving")
    print("# to their tri-state default, so this only ever ADDS explicit trues.")
    print(f"curl -sS -X PUT {base}/admin/style-config \\")
    print("  -H 'Content-Type: application/json' \\")
    print(f"  -d '{json.dumps(body)}'")
    print("\n# read it back (the RESOLVED view, not the sparse stored dict):")
    print(f"curl -sS '{base}/admin/style-config?account_id={account_id}'")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--prompt", action="store_true",
                    help="render every realism prompt block verbatim")
    ap.add_argument("--json", action="store_true",
                    help="a style_config body with every realism tag explicitly ON")
    ap.add_argument("--curl", metavar="ACCOUNT_ID",
                    help="print (do not send) the PUT that applies --json")
    ap.add_argument("--voice", choices=["her", "him"], default="her",
                    help="which lane to render (--prompt only)")
    ap.add_argument("--customs", action="store_true",
                    help="render the customs carve-out (--prompt only)")
    ap.add_argument("--base", default="http://127.0.0.1:8787",
                    help="relay base url for --curl")
    args = ap.parse_args()

    if args.prompt:
        cmd_prompt(args.voice, args.customs)
    elif args.json:
        cmd_json()
    elif args.curl:
        cmd_curl(args.curl, args.base.rstrip("/"))
    else:
        cmd_tags()


if __name__ == "__main__":
    main()
