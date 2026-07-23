"""service/automations/cat_stickers.py — the cat-sticker reaction pack.

A reply from the AI chatter can end with a cat reaction gif — occasionally the
gif IS the whole reply (he says "good morning" → just a waking-up kitten), the
way a real girl texts. The protocol mirrors `>>OFFER`: the model ends its reply
with a line that is exactly `STICKER: <tag>`; the line is ALWAYS stripped
before anything reaches the fan, and the tag maps here to a giphy id sent via
`of_client.send_message(fan_id, "", giphy_id=...)` (top-level `giphyId`,
GIF-only empty-text sends verified live 2026-05-23).

Rate control is CODE-SIDE, not trust: measured on real convos (07-23),
DeepSeek attaches a sticker to ~48% of replies whenever the block is visible —
double what the prompt asks for. `roll_mode` can hide the block ("skip") to
thin that out, and a per-fan minute floor can space sends. All three knobs
(skip %, solo %, gap minutes) are per-account in the Styles card; the house
defaults below run wide open (skip 0, gap 0).
"solo" injects a sticker-ONLY nudge (measured ~46% pure-sticker obedience).

Catalog: 89 real-cat giphy gifs across 22 emotion tags, hand-picked in
`cat_stickers/picker.html` (repo root) — `picks.json` there is the source of
truth; regenerate this literal via `cat_stickers/finalize_catalog.py`.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta

# tag -> (when-to-use line for the prompt, giphy ids to rotate among)
_CATALOG: dict[str, tuple[str, tuple[str, ...]]] = {
    'laugh': ('he said something genuinely funny',
              ('2g6sCTsSoVuSfSxK4W', 'M8lbkkn8BFFLO', '96suQaNpBRyE')),
    'love': ('warm affectionate moment, he said something sweet',
             ('a9VAlh8bgb0SA', 'LVXJQat47MwQU', 'ohiWzVWGkAOCA', '5QXd9CLYmU944', 'zGJegUgNbPOtW')),
    'kiss': ('goodnight/goodbye kiss or after a compliment',
             ('W1hd3uXRIbddu', 'xI1pK446iNJeg')),
    'flirty': ('playful teasing, being cheeky',
               ('KI5JqBqOKCPjG', 'HB9nUzmw6L74HHWpqN')),
    'shy': ('he complimented you and you play coy',
            ('xFOc3rYIGE3aE', 'Iwt30C4BH94ty', '33JJnbUuorikA5zWgB', 'dxqOkrl29R8ac')),
    'excited': ('genuinely hyped about something he said',
                ('Yt09iFvD9u5AQ', 'PUBxelwT57jsQ')),
    'dance': ('celebrating, happy vibes, party mood',
              ('TjSPQgowhhJdHgvnwA', '6v1v8O3go6r0uLI1xT', 'fx8HHVnj3Zpo1lbCkS', 'WXB88TeARFVvi')),
    'celebrate': ('he bought/tipped or shared good news',
                  ('TbRkubcqlgBksEqMv4', 'A0Zt7yuDULiy4ofmVD', 'NfzERYyiWcXU4', 'Ga0gHvT84m3yTE8lIp')),
    'miss_you': ('he was away, you missed him',
                 ('vxsoC27UotxBK', 'vFjAksOeerSdW', 'MDJ9IbxxvDUQM', '1FkCqpyObTuo0', 'd9eL06htb5Vks', 'SgZtvjwcfq0ww')),
    'sad': ('he said something sad or is leaving',
            ('LdMWg0xQTfmcpFF5Yt', 'aWMJvA76tNnBR9gkpT', 'TZBED1pP5m8N2', 'UrhRmF81nrHG3cBIqO', '7AzEXdIb1wyCTWJntb')),
    'pout': ('playfully sulking, he ignored you or said no',
             ('Ev477g37MJORyOWfdG', 'OHRF8LZis06OiPDJby', '7H9Cl4X9d68RIk3CNv', 'ZJieMJ372LLjXsInfV')),
    'eyeroll': ('sassy reaction to something silly',
                ('1wqK6DFqm5FW8ZCdJ9', 'NxwRDSUNzXshW', 'nazt106Gb1Ga4')),
    'shocked': ('surprising/wild thing he said',
                ('Cdkk6wFFqisTe', '135QXxqZ9d0QGk', 'BBNYBoYa5VwtO', '113YkW9oWdtFlu')),
    'confused': ('his message made no sense',
                 ('qZgHBlenHa1zKqy6Zn', 'blPpTGDhn6hEI', 'EqTWMRQnJniN1ymau8', 'GRk3GLfzduq1NtfGt5', 'lvrIdlYCod54kOrguY')),
    'sleepy': ('goodnight, being cozy in bed',
               ('12cYyFxlbIgXeg', 'GxQABXQhII7cY', 'Tfr91anUahoME', 'lFpvlU0or3SHC', 'DRNsbfCHNznxe')),
    'good_morning': ('morning greeting',
                     ('8B0TmFIOZ9iJJgJu8T', 'OWQzUnzgX7bS8', '10zHDq77BLwcy4', 'cwxKYaFLOBd1S', 'AC8n0wdJvnA6A', 'papAALBn286ty')),
    'wave_hi': ('greeting after a while, saying hi',
                ('ToMjGpRZ4gF6YuAT4li', 'X7Bckr1JaJS1opWTzO', 'H55IwUvuvmd8YiyGZX', 'xun2qNfnK1cV5r07GM', 'HYpZKsyLOn1ks')),
    'thumbs_up': ('agreeing, confirming plans',
                  ('KtKi9n1k5h5bW', 'kchkvMhb25mwPADv4z', '5gXYzsVBmjIsw')),
    'grumpy': ('playful fake-mad at him',
               ('xinkUUmE1ww5mbOd8E', 'haCYYKWRVeilcEL65X', '7rBemb9RiAEtW')),
    'waiting': ('he went quiet mid-convo',
                ('ZT0YXuyEN2ZdNLmAq8', 'cbLcSvHw50bJ8UuImn', 'm22Lj3VfcwDNvqc2Rd', 'tTImgMAq1DDuVnPrbX')),
    'beg': ('playfully begging him for something',
            ('5Ac3lAE4Ydz6E', 'aXlUz1p4dpNks', '4u8WdgQEMVaj6', 'MFYVUQWYW3ZPC2QNJh', 'ZyNQFqZLFUhr2', 'ciZYL6PAuysz6')),
    'money': ('money talk, after he spoils you',
              ('XQKBuQmfjt1xm', '10RTemEe5yjo0U', 'cLLgfNJiKppgA', 'ND6xkVPaj8tHO')),
}

TAGS = frozenset(_CATALOG)

# Per-reply roll knobs — house defaults, overridable PER ACCOUNT via
# style_config_json (cat_sticker_skip_pct / cat_sticker_solo_pct /
# cat_sticker_gap_min — see _common.load_cat_sticker_tuning); the resolved
# values arrive here as call args. "skip" hides the protocol entirely,
# "allow" shows it and lets the model judge, "solo" nudges a sticker-ONLY
# reply (the gif replaces the text). Wide open since 07-23: skip=0 and no
# per-fan gap — volume is tuned live from the Styles card.
DEFAULT_SKIP = 0.0     # chance a reply never sees the sticker block
DEFAULT_SOLO = 0.05    # chance of the gif-replaces-the-text nudge
DEFAULT_GAP_MIN = 0.0  # per-fan minutes between stickers (0 = no floor)

# Per-fan floor state — in-memory (restart = clean slate).
_last_sent: dict[tuple[str, int], datetime] = {}

# A well-formed marker (capture the tag) vs ANY marker-ish line (strip-all —
# the fan must never see the protocol, malformed included). Same split as the
# >>OFFER regex pair.
_MARKER_RE = re.compile(r"^[ \t]*STICKER:[ \t]*([a-z_]+)[ \t]*$", re.I | re.M)
_LINE_RE = re.compile(r"^[ \t]*STICKER:.*$", re.I | re.M)


def parse_marker(raw: str) -> tuple[str, str | None]:
    """Extract the FIRST well-formed STICKER tag (None when absent or not in
    the catalog), then strip EVERY marker-ish line from the reply."""
    m = _MARKER_RE.search(raw or "")
    tag = m.group(1).lower() if m else None
    if tag is not None and tag not in _CATALOG:
        tag = None
    clean = _LINE_RE.sub("", raw or "").strip()
    return clean, tag


def roll_mode(rng, on_cooldown: bool,
              skip_w: float = DEFAULT_SKIP,
              solo_w: float = DEFAULT_SOLO) -> str:
    """Per-reply dice: 'skip' | 'allow' | 'solo'. A cooldown forces 'skip' so
    the prompt never even offers a sticker the gate would drop. Weights are
    0..1; solo is carved out first, allow fills the rest up to 1-skip."""
    if on_cooldown:
        return "skip"
    skip_w = min(max(float(skip_w), 0.0), 1.0)
    solo_w = min(max(float(solo_w), 0.0), 1.0 - skip_w)
    r = rng.random()
    if r < solo_w:
        return "solo"
    if r < 1.0 - skip_w:
        return "allow"
    return "skip"


def cooldown_active(account_id: str, fan_id: int,
                    now: datetime | None = None,
                    gap_min: float = DEFAULT_GAP_MIN) -> bool:
    if gap_min <= 0:
        return False
    last = _last_sent.get((str(account_id), int(fan_id)))
    return (last is not None
            and (now or datetime.utcnow()) - last < timedelta(minutes=gap_min))


def mark_sent(account_id: str, fan_id: int,
              now: datetime | None = None) -> None:
    _last_sent[(str(account_id), int(fan_id))] = now or datetime.utcnow()


def pick_gif(tag: str, rng) -> str | None:
    """The giphy id to send for `tag` — random among the tag's hand-picked
    gifs so the same reaction doesn't always land the same clip."""
    gifs = _CATALOG.get(tag, ("", ()))[1]
    return rng.choice(gifs) if gifs else None


def prompt_block(mode: str) -> str:
    """The prompt section for 'allow'/'solo' rolls ('' otherwise). Wording is
    the one validated on real convos (0 invalid tags across 90 samples)."""
    if mode not in ("allow", "solo"):
        return ""
    tag_lines = "\n".join(f"- {t}: {when}" for t, (when, _) in _CATALOG.items())
    block = (
        "CAT STICKERS — you have a pack of cat reaction gifs, the kind real "
        "girls spam in texts. Tags you can send:\n" + tag_lines + "\n"
        "Sticker rules:\n"
        "- MOST replies need NO sticker — use one only when the emotion is "
        "strong. Never force it.\n"
        "- To attach one, end your reply with a line that is exactly: "
        "STICKER: <tag>\n"
        "- A sticker can BE the whole reply — when a reaction says it all, "
        "output ONLY the STICKER line, no text.\n"
        "- Max ONE sticker per reply. The STICKER line is protocol — it's "
        "stripped before sending, the fan only sees the gif."
    )
    if mode == "solo":
        block += (
            "\n- THIS message: a reaction sticker says it all — if ANY sticker "
            "fits this moment, reply with ONLY the STICKER line, no text. Only "
            "add text if truly no sticker fits.")
    return block
