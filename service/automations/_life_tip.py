"""service/automations/_life_tip.py — the life-expense tip ask, pure parts.

"Lots of fans just chat." Nobody asks them for anything, so nothing comes in.
This is the loop that does: every so many of HIS messages, one reply carries a
short line about something real she is paying for today (nails, groceries, a
night out) and asks him to spoil her with it — a tip, right here in chat.

Everything here is PURE (no DB, no I/O) so it unit-tests without a harness:
the per-fan state shape, the cadence, the prompt block, and the "did the model
actually make the ask" check. ai_chatter owns the gate (which turn may carry
it), the wire-side price-strip exemption, and the stamp on confirmed send.

⚠️ NOT the offer-lane tip ask removed on 2026-08-19 (commit 010705e — "PPV is
the only offer lane"). That was a tip that UNLOCKED a catalog piece. This ask
never has content behind it: the block itself bans promising anything for it,
and the armed turn is a `_TURNS_NOT_SELLING` beat so no price tag can ride
along. The house rule since 2026-07-23 (a tip-ask beside a priced offer let a
fan pay twice for one promise) is enforced by that membership, not by prose.

Leaf module: imports nothing from ai_chatter or _common, same rule as
fan_state.py, so a second engine can pick it up without the 8k-line import.
"""
from __future__ import annotations

import math
import random
import re
from dataclasses import dataclass
from datetime import datetime, timedelta

#: fans.custom_fields slot — {"at": iso, "landed": bool}
KEY = "_life_tip"
#: The prompt block's first line. `_prompt_shape` seats it with the this-turn blocks.
HEADER = "ASK HIM FOR A LITTLE HELP THIS MESSAGE"

# Cadence, in HIS messages since the anchor (an ask or a tip, whichever is newer).
# Operator numbers (2026-09-03): ask more often while he has not tipped since the
# last ask, less often once he has. A miss (the model ignored the block) retries
# soon instead of burning a whole cycle.
BAND = (15, 25)
BAND_TIPPED = (20, 30)
BAND_RETRY = (3, 5)
# The TIME floor, ANDed with the count: a man who sends fifty a day must not be
# asked daily. Days since the anchor, drawn log-uniform in 3-20 so the median
# is ~7.7 ("about a week") and the ends are rare. Not applied on the cold start
# (no anchor) nor on the retry band (the ask never landed).
DAYS_BAND = (3, 20)

# What she is paying for today — the block shows a handful per turn and she
# picks ONE that fits the chat. Split by lane: a fighter does not get his nails
# done and a girl is not renewing fight camp.
REASONS = {
    "her": (
        "nails", "hair", "lashes", "brows", "a spa day", "a massage", "skincare",
        "groceries", "a food order", "a coffee run", "brunch with the girls",
        "a night out", "an uber home", "gas", "the phone bill", "the electric bill",
        "the gym membership", "a new outfit", "shoes", "lingerie", "a new bikini",
        "flowers for the apartment", "a plant", "wine for tonight", "concert tickets",
        "the vet bill", "a treat for the dog", "a birthday gift for mum", "a book",
    ),
    "him": (
        "the gym renewal", "protein and supplements", "training gear", "new gloves",
        "wraps", "fight camp fees", "a barber cut", "a big meal", "meal prep for the week",
        "gas", "a car wash", "new tyres", "the phone bill", "a hoodie", "boots",
        "fight-night tickets", "a sauna session", "a coaching cert", "a road trip tank",
        "a treat for the dog",
    ),
}
REASONS_SHOWN = 6
# The band she draws from when no amount is configured ("name ONE modest real number").
RANGE_DOLLARS = (10, 40)


# ── The BIG ask ────────────────────────────────────────────────────────
# The little ask above is a $10-40 line for anyone who chats. This is the other
# one, and its rules are different in every way that matters: about once a
# month, ONLY for a fan who has tipped at least once, only inside a thread that
# is alive right now, for something that HIT her today — locked out, a smashed
# window, the car towed — at $100-300.
#
# It rides the SAME turn kind (`_TURN_LIFE_TIP`), so every safety built for the
# little ask covers it unchanged: nothing priced rides along, the goal block
# replaces the get-to-know question, the price strip lets a bare figure through
# and no opener is drawn. What differs is only the cadence, the pool and the copy.

#: fans.custom_fields slot for the big ask — same shape as KEY.
KEY_BIG = "_life_tip_big"
#: Its prompt block's first line. Listed in `_prompt_shape._CATS` beside HEADER.
HEADER_BIG = "ASK HIM FOR REAL HELP THIS MESSAGE"

# Days since the anchor, log-uniform like DAYS_BAND → median ~31.6, "once a month".
BIG_DAYS = (25, 40)
# "A live thread", and the whole reason the big ask counts differently from the
# little one. His FIRST tip can be years old, so "messages since the anchor" is
# his entire history and a $200 ask would land on the first "hey" of a man who
# came back after a year. The little ask accepts that at $10-40; this one counts
# only messages inside a short window and needs a handful of them. Constants,
# not knobs — an operator lowering these to 1 would be turning the rule off.
BIG_LIVE_DAYS = 3
BIG_LIVE_MSGS = 5
#: Never within this many days AFTER a little ask — two asks in 48h reads needy.
BIG_GAP_DAYS = 3
BIG_RANGE_DOLLARS = (100, 300)
BIG_REASONS_SHOWN = 4
# What hit her TODAY. Not a standing expense (that is the little ask) — a thing
# that happened, with a real bill attached, big enough to need help with.
BIG_REASONS = {
    "her": (
        "locked out, the locksmith and a new key", "a window got broken", "the car got towed",
        "the car broke down and needs a repair", "my phone screen shattered", "the laptop died",
        "an emergency vet visit", "a dentist bill", "a flight home for a family thing",
        "the fridge died", "the heating broke", "a parking fine", "new tyres",
        "rent came up short this month", "a deposit for the new place",
    ),
    "him": (
        "locked out, locksmith and a new key", "the truck window got smashed",
        "the car got towed", "the car broke down and needs a repair", "phone screen shattered",
        "the laptop died", "a dentist bill", "fight camp fees are due", "a flight home for family",
        "the AC died", "a fine to pay", "new tyres for the truck", "a deposit for the new place",
        "gym rent for the month",
    ),
}


# ── Per-account knobs ──────────────────────────────────────────────────
# Every number and list above is the SHIPPED value; an account may override any
# of them in ai_chatter_config_json (the "life_tip_ask_*" keys — one card in the
# AI Chatter tab). `settings(cfg)` is the only reader: it tolerates a missing,
# None or malformed value by falling back to the constant, so a half-saved
# config can never turn the ask off by accident or draw from an empty pool.

@dataclass(frozen=True)
class Settings:
    every: tuple[int, int] = BAND
    every_tipped: tuple[int, int] = BAND_TIPPED
    retry: tuple[int, int] = BAND_RETRY
    days: tuple[int, int] = DAYS_BAND
    range_dollars: tuple[int, int] = RANGE_DOLLARS
    reasons_her: tuple[str, ...] = REASONS["her"]
    reasons_him: tuple[str, ...] = REASONS["him"]
    shown: int = REASONS_SHOWN
    #: An operator sentence appended to the block ("mention the dog by name").
    extra: str = ""

    def reasons(self, lane: str) -> tuple[str, ...]:
        return self.reasons_him if lane == "him" else self.reasons_her


DEFAULTS = Settings()

CFG_PREFIX = "life_tip_ask_"
#: config key (without the prefix) → Settings field. The two non-Settings keys
#: (`enabled`, `amount_dollars`) are read by ai_chatter directly.
CFG_FIELDS = {
    "every": "every", "every_tipped": "every_tipped", "retry": "retry",
    "days": "days", "range_dollars": "range_dollars",
    "reasons_her": "reasons_her", "reasons_him": "reasons_him",
    "reasons_shown": "shown", "extra": "extra",
}


@dataclass(frozen=True)
class BigSettings:
    """The big ask's knobs — the `life_tip_big_*` keys, same card, same tolerance.

    Deliberately NOT a superset of `Settings`: the two asks share only the turn
    they ride. There is no `amount_dollars` twin either — a locksmith is not a
    configured number, so she names one inside the range and the reason picks it.
    """
    days: tuple[int, int] = BIG_DAYS
    range_dollars: tuple[int, int] = BIG_RANGE_DOLLARS
    reasons_her: tuple[str, ...] = BIG_REASONS["her"]
    reasons_him: tuple[str, ...] = BIG_REASONS["him"]
    #: An operator sentence appended to the block, like Settings.extra.
    extra: str = ""

    def reasons(self, lane: str) -> tuple[str, ...]:
        return self.reasons_him if lane == "him" else self.reasons_her


BIG_DEFAULTS = BigSettings()

CFG_PREFIX_BIG = "life_tip_big_"
#: config key (without the prefix) → BigSettings field. `enabled` is read by
#: ai_chatter directly, like the little ask's.
CFG_FIELDS_BIG = {
    "days": "days", "range_dollars": "range_dollars",
    "reasons_her": "reasons_her", "reasons_him": "reasons_him", "extra": "extra",
}


def _pair(raw, default: tuple[int, int]) -> tuple[int, int]:
    """[lo, hi] of ints with 1 ≤ lo ≤ hi; anything else → `default`."""
    try:
        lo, hi = int(raw[0]), int(raw[1])
    except (TypeError, ValueError, IndexError, KeyError):
        return default
    if lo < 1 or hi < lo:
        return default
    return (lo, hi)


def _strs(raw, default: tuple[str, ...]) -> tuple[str, ...]:
    """A non-empty list of non-blank strings; anything else → `default`."""
    if isinstance(raw, str):
        raw = [x for x in re.split(r"[\n,]", raw)]
    if not isinstance(raw, (list, tuple)):
        return default
    out = tuple(str(x).strip() for x in raw if str(x or "").strip())
    return out or default


def settings(cfg: dict | None) -> Settings:
    """The account's knobs out of its merged ai_chatter config. Absent, None or
    malformed → the shipped constant, field by field."""
    c = cfg or {}
    g = lambda k: c.get(CFG_PREFIX + k)  # noqa: E731
    shown_raw = g("reasons_shown")
    try:
        shown = max(1, min(12, int(shown_raw))) if shown_raw is not None else REASONS_SHOWN
    except (TypeError, ValueError):
        shown = REASONS_SHOWN
    extra = g("extra")
    return Settings(
        every=_pair(g("every"), BAND),
        every_tipped=_pair(g("every_tipped"), BAND_TIPPED),
        retry=_pair(g("retry"), BAND_RETRY),
        days=_pair(g("days"), DAYS_BAND),
        range_dollars=_pair(g("range_dollars"), RANGE_DOLLARS),
        reasons_her=_strs(g("reasons_her"), REASONS["her"]),
        reasons_him=_strs(g("reasons_him"), REASONS["him"]),
        shown=shown,
        extra=str(extra).strip() if isinstance(extra, str) else "",
    )

def big_settings(cfg: dict | None) -> BigSettings:
    """The big ask's knobs out of the same merged config. Absent, None or
    malformed → the shipped constant, field by field — `settings`'s contract."""
    c = cfg or {}
    g = lambda k: c.get(CFG_PREFIX_BIG + k)  # noqa: E731
    extra = g("extra")
    return BigSettings(
        days=_pair(g("days"), BIG_DAYS),
        range_dollars=_pair(g("range_dollars"), BIG_RANGE_DOLLARS),
        reasons_her=_strs(g("reasons_her"), BIG_REASONS["her"]),
        reasons_him=_strs(g("reasons_him"), BIG_REASONS["him"]),
        extra=str(extra).strip() if isinstance(extra, str) else "",
    )


#: On the armed turn a bubble may name a price — UNLESS it also names content.
#: "$35 for nails" passes; "$50 unlocks everything" is still unbacked talk.
CONTENT_WORDS_RE = re.compile(
    r"\b(unlock\w*|ppv|pics?|photos?|vids?|videos?|clips?|sets?|bundles?|customs?)\b"
    r"|\bsend\s+(?:u|you|ya)\b",
    re.IGNORECASE)
#: Did the sent text actually carry the ask? A figure, or the word itself.
#: Any of $ £ € — a Manchester persona writes "£25, cover it" and the fan tips
#: in whatever OnlyFans charges him; the CADENCE must not care. Live 2026-09-03:
#: 7 of 8 male-lane "misses" were £ or a bare figure, and each one fired the
#: 3-5 message retry on a man who had just been asked.
_LANDED_RE = re.compile(r"[$£€]\s*\d|\d\s*€|\btip\b", re.IGNORECASE)
#: Every money figure in the text (symbol before, or € after), for `big_landed`.
_FIGURE_RE = re.compile(r"[$£€]\s*(\d[\d,]*)|(\d[\d,]*)\s*€")


def figures(text: str) -> list[int]:
    """Every priced figure in the text, as ints, whatever the currency symbol."""
    return [int((a or b).replace(",", "")) for a, b in _FIGURE_RE.findall(text or "")]


def asked_at(state: dict) -> datetime | None:
    """The last ask's instant out of the fan's slot; None when absent or corrupt."""
    raw = (state or {}).get("at")
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw))
    except (TypeError, ValueError):
        return None


def anchor(state: dict, tipped_at: datetime | None) -> datetime | None:
    """Where counting starts: the newer of the last ask and the last tip. None
    when neither exists — his whole history counts, which is the cold start:
    a fan who has chatted 25+ messages with no ask ever is due now."""
    a = asked_at(state)
    if a is None:
        return tipped_at
    if tipped_at is None:
        return a
    return max(a, tipped_at)


def band(state: dict, tipped_at: datetime | None, s: Settings = DEFAULTS) -> tuple[int, int]:
    """Which cadence applies. Product decision (2026-09-03, flagged to the
    operator): "tipped" means a tip NEWER than the last ask — or any tip when
    he has never been asked. A man who tipped once in May and ignored five asks
    since is, for this loop, a man who hasn't. A missed ask (the model wrote no
    ask) uses the short retry band."""
    a = asked_at(state)
    if a is not None and (state or {}).get("landed") is False:
        return s.retry
    tipped = tipped_at is not None and (a is None or tipped_at > a)
    return s.every_tipped if tipped else s.every


def target(fan_id: int, anchor_at: datetime | None, lo_hi: tuple[int, int]) -> int:
    """The drawn interval for this (fan, anchor) — deterministic, so it is the
    same on every tick and re-rolls exactly when the anchor moves. Seeded at
    second precision: the stamp we write and the Message.created_at we read
    back are formatted by the same process, but a microsecond drift between
    them must not re-roll the draw every tick (which would collapse the band
    to its minimum)."""
    lo, hi = lo_hi
    key = (anchor_at.replace(microsecond=0).isoformat() if anchor_at is not None
           else "none")
    return random.Random(f"{int(fan_id)}:{key}").randint(int(lo), int(hi))


def days_target(fan_id: int, anchor_at: datetime, lo_hi: tuple[int, int]) -> float:
    """Days the ask must wait after `anchor_at`, log-uniform in `lo_hi` and
    fixed per (fan, anchor) exactly like `target`. Takes the PAIR, not a
    Settings: the big ask has its own days band on its own dataclass, and one
    function drawing from a pair serves both without either knowing the other."""
    lo, hi = lo_hi
    rng = random.Random(f"{int(fan_id)}:days:{anchor_at.replace(microsecond=0).isoformat()}")
    return math.exp(rng.uniform(math.log(lo), math.log(hi)))


def due(fan_id: int, state: dict, tipped_at: datetime | None, *, n_since: int,
        now: datetime | None = None, s: Settings = DEFAULTS) -> bool:
    """Is the ask due? BOTH: `n_since` (HIS messages since the anchor — the caller
    counts, this module never touches the DB) ≥ the count draw, AND, when there is
    an anchor and the ask is not a retry, `now` - anchor ≥ the days draw."""
    a = anchor(state, tipped_at)
    b = band(state, tipped_at, s)
    if int(n_since) < target(fan_id, a, b):
        return False
    if a is None or b == s.retry or now is None:
        return True
    return (now - a).total_seconds() >= days_target(fan_id, a, s.days) * 86400


def big_anchor(big_state: dict, first_tip: datetime | None) -> datetime | None:
    """Where the big ask's month is measured from: the last big ask if there has
    been one, else his FIRST tip. None ⇔ he has never tipped ⇔ never eligible.

    His first tip, not his newest, and that distinction is the whole design. A
    later tip does NOT move this clock: anchoring on the newest tip would restart
    the month with every tip, so a man who tips every two weeks — exactly the fan
    most likely to pay a $200 ask — would never reach one."""
    if first_tip is None:
        return None
    return asked_at(big_state) or first_tip


def big_live_since(anchor_at: datetime | None, now: datetime) -> datetime | None:
    """Where the live count starts: the NEWER of the anchor and `now` minus
    BIG_LIVE_DAYS. None only when there is no anchor (he has never tipped, so
    nothing is due anyway and the caller need not count).

    For an old anchor that is just the window — "is he talking right now". For a
    fresh MISS it is the miss itself, so the messages he sent before the ask that
    failed to land can never satisfy the retry draw a second time."""
    if anchor_at is None:
        return None
    edge = now - timedelta(days=BIG_LIVE_DAYS)
    return max(anchor_at, edge)


def big_due(fan_id: int, big_state: dict, first_tip: datetime | None, *,
            n_live: int, now: datetime, little_at: datetime | None,
            s: BigSettings = BIG_DEFAULTS,
            retry: tuple[int, int] = BAND_RETRY) -> bool:
    """Is the BIG ask due? `n_live` is HIS messages since `big_live_since` — the
    caller counts, this module never touches the DB.

    ALL of: he has tipped at least once (the operator's eligibility rule); the
    thread is alive right now (`n_live` ≥ BIG_LIVE_MSGS, or the retry draw when
    the last big ask never landed); a month has passed since the anchor (skipped
    on a retry, like the little ask); and no little ask inside BIG_GAP_DAYS."""
    if first_tip is None:
        return False
    a = big_anchor(big_state, first_tip)
    big_at = asked_at(big_state)
    # No second ask on top of a fresh one — two in 48 hours reads needy.
    #
    # ⚠️ ONLY a little ask NEWER than the last big one counts. A big ask stamps
    # BOTH slots (the little clock restarts from it, see ai_chatter's send path),
    # so reading `little_at` raw would have every big ask block its own retry for
    # three days — and since the retry band is 3-5 of his messages, that is the
    # retry never firing at all. A miss means nothing was asked; re-asking is not
    # a second ask.
    #
    # The `>` is STRICT and load-bearing, and it rests on a fact ai_chatter owns:
    # both slots are stamped from ONE run-level `now`, so a big ask leaves
    # `little_at == big_at` and this comparison is False. Stamp the two from two
    # `datetime.utcnow()` calls instead and the little one lands microseconds
    # later, `little_at > big_at` becomes True on every big ask, and the retry is
    # dead again — silently, with every test but a microsecond-level one passing.
    if (little_at is not None and (big_at is None or little_at > big_at)
            and (now - little_at).total_seconds() < BIG_GAP_DAYS * 86400):
        return False
    if big_at is not None and (big_state or {}).get("landed") is False:
        # The ask went out and carried no number. Re-ask inside the live window
        # on the short band; the month does not restart on a miss.
        return int(n_live) >= target(fan_id, a, retry)
    if int(n_live) < BIG_LIVE_MSGS:
        return False
    return (now - a).total_seconds() >= days_target(fan_id, a, s.days) * 86400


def landed(text: str) -> bool:
    """Did the reply actually carry the ask?"""
    return bool(_LANDED_RE.search(text or ""))


def big_landed(text: str, s: BigSettings = BIG_DEFAULTS) -> bool:
    """Did the reply carry the BIG ask? A `$` figure of at least HALF the range
    floor — `landed`'s "a figure, or the word tip" is far too loose here, because
    a "$5 coffee" in the ordinary half of the same reply would mark a dropped
    $200 ask as landed and skip the retry entirely.

    Half the floor, not the floor: a model that names $90 against a 100-300 range
    still made the ask, and re-asking that man in three messages is the worse of
    the two errors."""
    floor = s.range_dollars[0] / 2
    return any(n >= floor for n in figures(text))


def stamp(now: datetime, *, landed: bool) -> dict:
    """The slot value written on CONFIRMED send."""
    return {"at": now.isoformat(), "landed": bool(landed)}


# The rule "this ask IS the one question this message" is NOT restated in these
# blocks, on purpose. The persona says "at most ONE question"; handed a bio gap
# beside the ask the model kept the gap (live 2026-09-03: 8 of 32), so ai_chatter
# drops the gap in code on the armed turn (`ask = False`) and its goal block says
# "No get-to-know question this turn". A third copy here was words, not a rule.
_HER = (
    f"\n\n{HEADER}: answer him like normal, then ONE short playful line about "
    "something real you're paying for today — pick ONE that fits the chat and "
    "your day ({reasons}) — and ask him to spoil you with it: {ask}. Sweet and "
    "cheeky, never needy, never a demand, no guilt if he passes. Say it like "
    "\"ugh nail appointment today n its like $35 😩 spoil me a lil?\" — never the "
    "bare word \"tip\" as a demand, no price list, and NEVER promise content, a "
    "secret or any reward for it."
)
_HIM = (
    f"\n\n{HEADER}: answer him like normal, then ONE short line about something "
    "real you're paying for today — pick ONE that fits the chat and your day "
    "({reasons}) — and tell him to cover it: {ask}. Flat and matter-of-fact, not "
    "begging, no guilt if he passes. Say it like \"gym renewal today, $35. cover "
    "it.\" — never the bare word \"tip\" as a demand, no price list, and NEVER "
    "promise content, a secret or any reward for it."
)


_HER_BIG = (
    f"\n\n{HEADER_BIG}: answer him like normal, then ONE honest line about something "
    "that hit you TODAY — pick ONE that fits the chat ({reasons}) — with a real "
    "number, bigger than usual (name ONE, ${lo}-${hi}) — and ask if he could help "
    "you with it: tip you that amount right here in chat. A bit shaken but composed, "
    "no sob story, never a demand, no guilt if he can't — and if he passes, drop it. "
    "Say it like \"ok so i locked myself out n the locksmith wants $180 for a new key "
    "😭 any chance u could help me out?\" — never the bare word \"tip\" as a demand, "
    "no price list, and NEVER promise content, a secret or any reward for it."
)
_HIM_BIG = (
    f"\n\n{HEADER_BIG}: answer him like normal, then ONE flat line about something that "
    "hit you TODAY — pick ONE that fits the chat ({reasons}) — with a real number, "
    "bigger than usual (name ONE, ${lo}-${hi}) — and tell him to handle it: tip you "
    "that amount right here in chat. No story, no explaining, not begging, no guilt "
    "if he passes — and if he does, drop it. Say it like \"truck window got smashed "
    "last night. $250 to fix. handle it.\" — never the bare word \"tip\" as a demand, "
    "no price list, and NEVER promise content, a secret or any reward for it."
)


def _shown(pool: tuple[str, ...], n: int, seed: str) -> list[str]:
    """Which `n` of the pool she is shown this turn, fixed per seed (the caller
    passes fan + day), so the same fan is not handed the same handful twice
    running. One implementation for both asks."""
    return random.Random(f"reasons:{seed}").sample(pool, min(int(n), len(pool)))


def prompt_block(amount_dollars: int | None = None, voice: str = "her",
                 seed: str = "", s: Settings = DEFAULTS) -> str:
    """The block appended to the system prompt on the armed turn. With an amount
    she names it; without one she picks a modest real number herself (inside
    `s.range_dollars`). `seed` (the caller passes fan + day) picks which `s.shown`
    of the lane's pool she sees, so the same fan is not shown the same handful
    twice running. `s.extra`, when set, is the operator's own sentence, last."""
    amt = int(amount_dollars) if amount_dollars else 0
    lo, hi = s.range_dollars
    ask = (f"tip you ${amt} right here in chat" if amt > 0 else
           "tip you a little something right here in chat (name ONE modest real "
           f"number, ${lo}-${hi})")
    lane = "him" if str(voice or "").strip().lower() == "him" else "her"
    pool = s.reasons(lane)
    shown = _shown(pool, s.shown, seed)
    tmpl = _HIM if lane == "him" else _HER
    out = tmpl.replace("{ask}", ask).replace("{reasons}", ", ".join(shown))
    if s.extra:
        out += " " + s.extra
    return out


def big_prompt_block(voice: str = "her", seed: str = "",
                     s: BigSettings = BIG_DEFAULTS) -> str:
    """The BIG ask's block, appended to the system prompt on the armed turn.

    No amount parameter, unlike `prompt_block`: the reason dictates the number
    (a locksmith is not a configured $150), so she names one inside the range."""
    lo, hi = s.range_dollars
    lane = "him" if str(voice or "").strip().lower() == "him" else "her"
    shown = _shown(s.reasons(lane), BIG_REASONS_SHOWN, seed)
    tmpl = _HIM_BIG if lane == "him" else _HER_BIG
    out = (tmpl.replace("{reasons}", ", ".join(shown))
               .replace("{lo}", str(int(lo))).replace("{hi}", str(int(hi))))
    if s.extra:
        out += " " + s.extra
    return out
