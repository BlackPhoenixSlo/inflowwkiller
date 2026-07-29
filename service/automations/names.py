"""service/automations/names.py — what we are allowed to CALL a fan.

One concern, one file: the predicate (`is_greetable_name`), the parsers
(`name_token`, `resolve_fan_name`) and the label we push back to OF
(`build_structured_nickname`). Lifted out of _common.py, which had grown to
2.5k lines; behaviour is unchanged and `_common` re-exports every name below,
so existing `from ._common import name_token` call sites keep working.

Why it is a cluster and not four loose helpers: every one of these answers the
same question about the same string, and when they each answered it their own
way she greeted 132 fans "hey Canada", 551 "hey u1234567", and pushed a name to
the OF chat header that the chat itself refused to say.
"""
from __future__ import annotations

import html
import re
from collections.abc import Iterable

from db.models import Fan


# The nickname's Bonus slot — (label, cents floor), richest first. ONE source:
# gen_info._spend_tier picks a label from here and _PLACE_WORDS below rejects the
# same labels, so a new tier can never quietly become a fan's name. They landed in
# the Name slot on 50 prod fans when every other slot was empty.
SPEND_TIERS = (("Whale", 50000), ("Spender", 5000), ("Buyer", 1), ("Free", 0))


# ── Words that are a PLACE (or a derived label), never a fan's first name ─────
# gen_info builds `Name/City,Country/Age/Job/Bonus` and, when it can't find a
# name, the model puts the COUNTRY in the Name slot ('Canada/Canada/office job')
# or _clean_nickname slides the derived spend tier up into it ('Spender/…').
# Both then read as the fan's name — 132 prod fans resolved to "Canada" and 50
# to "Spender", so she greeted them "hey Canada". Countries + demonyms + US
# states + provinces only: CITIES are checked per-fan against that fan's own
# home_city in resolve_fan_name, because plenty of cities are real first names
# (Austin, Phoenix, Dallas, Milan, Paris). Places that are ALSO common first
# names (Chad, Jordan, Israel, Georgia, India, Kenya, Virginia, Carolina,
# Dakota, Montana, Indiana, Alberta, Victoria) are deliberately ABSENT here —
# the per-fan home_country check catches those without costing a real Chad his
# name. Multi-word entries are matched against the whole segment; their tail
# word is listed too because `last=True` picks it ('New Zealand' → 'Zealand').
# The bare CODES (usa/uk/gb/uae/nz) are here rather than only in name_token's
# all-caps rule: is_greetable_name is what the senders and the nickname sweep
# ask, and it happily accepted 'USA' as somebody's first name.
_PLACE_WORDS = frozenset("""
afghanistan albania algeria america american andorra angola arab arabia
argentina argentine argentinian armenia aruba asia asian australia australian
austria azerbaijan bahamas bahrain bangladesh barbados belarus belgian belgium
belize benin bermuda bhutan bolivia bosnia bosnian botswana brasil brazil
brazilian britain british brunei bulgaria bulgarian cambodia cameroon canada
canadian chile chilean china chinese colombia colombian comoros congo croatia
croatian cuba cuban curacao cyprus czech czechia danish denmark djibouti
dominica dominican dutch ecuador egypt egyptian emirates england english
eritrea estonia eswatini ethiopia europe european faso fiji filipino finland
finnish france french gabon gambia germany german ghana gibraltar greece greek
greenland grenada guadeloupe guatemala guinea guyana haiti haitian hispanic
holland honduras hungarian hungary iceland indonesia indonesian iran iraq
ireland irish israeli italia italian italy jamaica jamaican japan japanese
kazakhstan korea korean kosovo kuwait kyrgyzstan lanka laos latino latvia
lebanon leone lesotho liberia libya liechtenstein lithuania luxembourg
macedonia madagascar malawi malaysia maldives mali malta mauritania mauritius
mexican mexico méxico moldova monaco mongolia montenegro morocco moroccan
mozambique myanmar namibia nepal netherlands nicaragua niger nigeria nigerian
norway norwegian oman pakistan pakistani palestine panama paraguay peru
peruvian philippines poland polish portugal portuguese qatar rico romania
romanian russia russian rwanda samoa scotia scotland scottish senegal serbia
serbian seychelles singapore slovak slovakia slovenia slovenian somalia spain
spaniard spanish sudan suriname swedish sweden swiss switzerland syria taiwan
tajikistan tanzania thai thailand togo tonga trinidad tunisia turkey turkish
türkiye turkmenistan uganda ukraine ukrainian uruguay uzbekistan vanuatu
vatican venezuela venezuelan vietnam vietnamese wales welsh yemen zambia
zealand zimbabwe réunion reunion españa
alabama alaska arizona arkansas california colorado connecticut delaware
florida hawaii idaho illinois iowa kansas kentucky louisiana maine maryland
massachusetts michigan minnesota mississippi missouri nebraska nevada ohio
oklahoma oregon pennsylvania tennessee texas utah vermont washington wisconsin
wyoming manitoba nunavut ontario quebec saskatchewan yukon newfoundland
unknown null blank none
usa us uk gb uae nz
""".split()) | frozenset({
    "new zealand", "north macedonia", "south africa", "south korea",
    "north korea", "puerto rico", "saudi arabia", "sri lanka", "costa rica",
    "el salvador", "cape verde", "sierra leone", "ivory coast", "hong kong",
    "united states", "united kingdom", "great britain", "new hampshire",
    "new jersey", "new mexico", "new york", "rhode island", "west virginia",
    "nova scotia", "british columbia", "burkina faso", "puerto rican",
}) | frozenset(label.lower() for label, _ in SPEND_TIERS)


def is_place_word(s: str | None) -> bool:
    """True when `s` names a country/region/state (or is a derived label like a
    spend tier) — i.e. it is never the fan's first name. See _PLACE_WORDS."""
    t = " ".join(str(s or "").lower().split()).strip(".,")
    return bool(t) and t in _PLACE_WORDS


def is_greetable_name(s: str | None, *, here: Iterable[str] = ()) -> bool:
    """THE test for 'can she say this out loud as his name'. One predicate, because
    every place that answered it separately answered it differently: the greeting
    said 'babe' while the OF nickname push still wrote 'Fanlover577' into the Name
    slot, and gen_info's own guard disagreed with both.

    Not a name: a handle (any digit or '_' — 'u1234567', 'Sparky10', 'joe_the_man'),
    something with no letters at all (',', '.'), a place or derived label
    (`is_place_word`), or the fan's OWN location — pass his home_city/home_country
    as `here` so a city no static list can hold ('Millbrook') is caught too."""
    t = " ".join(str(s or "").lower().split())
    if not t or not any(ch.isalpha() for ch in t):
        return False
    if any(ch.isdigit() or ch == "_" for ch in t):
        return False
    if is_place_word(t):
        return False
    return t not in {" ".join(str(p or "").lower().split()) for p in here if p}


def name_token(s: str | None, *, last: bool = False) -> str:
    """Pull a real-NAME token (Capitalized, letters only, len≥2) out of a string,
    or '' if it doesn't look like a name (handles like 'xx_gamer_99' / 'u123' are
    rejected — they aren't Capitalized real names). Slash-structured nicknames
    ('John/Orange City,USA/Horny-Fan') keep the first slot ('John'); pass last=True
    for AI/curated nicknames so 'Sexy Sofie' → 'Sofie' (the name, not the adjective).

    This is the canonical parser (lifted from send_welcome._name_token so every
    sender derives names identically — single source of truth)."""
    if not s:
        return ""
    # OF stores these fields HTML-ESCAPED ('Alex&amp;Sam', 'Sunny Shine &lt;33'),
    # and stripping the entity to letters invented names nobody has ('Chrisamp').
    # First NON-EMPTY slot. A leading '/' is sloppy human input ('/Christopher/'),
    # not a claim that the name is unknown — the empty name slot has an explicit
    # marker now (_NO_NAME), so a bare gap no longer has to carry that meaning.
    # Reading it literally cost Christopher his name.
    seg = next((p.strip() for p in html.unescape(str(s)).split("/") if p.strip()), "")
    if "," in seg:                            # 'Whistler,Canada/Whale' has no name slot
        return ""                             # (a comma marks a City,Country location)
    # '&', brackets and semicolons separate words just like a space does — a couple
    # shares one account ('Alex&Sam' → Alex) and the team types notes into the
    # curated nickname ('Kevin(on the other account)', 'Ed; New Jersey').
    words = re.split(r"[\s();&]+", seg) if seg else []
    words = [w for w in words if w]
    if not words:
        return ""
    raw = words[-1] if last else words[0]
    if not raw[:1].isupper():                 # real names are Capitalized; handles aren't
        return ""
    # A SHOUTED token is a country/region code, never a first name. gen_info emits
    # the name slot title-cased ('Mani'), so anything all-caps reaching here came
    # from the location slot after _guard_name blanked an unusable name — without
    # this, 'USA/Free' resolves to the greeting "u around USA?".
    if raw.isupper() and len(raw) <= 4:
        return ""
    # Keep letters only, but UNICODE-correct: str.isalpha() preserves accented names
    # (José, Ángel, Muñoz, Nicolás) that the old [^A-Za-z] strip mangled to Jos/ngel/Muoz.
    w = "".join(ch for ch in raw if ch.isalpha())
    if len(w) < 2:
        return ""
    # A COUNTRY is not a name. The model fills an unknown Name slot with the
    # country ('Canada/Canada/office job') and _clean_nickname slides the spend
    # tier up when everything else is empty ('Spender') — both survive every
    # check above (Capitalized, letters, not a short shout), so she greeted 182
    # prod fans as "Canada"/"Spender". Reject the whole segment too, so a
    # multi-word place ('New Zealand', 'Puerto Rico') dies on either word.
    if is_place_word(w) or is_place_word(seg):
        return ""
    return w


def resolve_fan_name(f) -> str:
    """Single source of truth for 'what real first name do we greet this fan by'.

    Why this exists: OF *message* payloads carry only `fromUser:{id}` — no name — so
    `of_username`/`of_display_name` are empty for ~80% of fans, and the WS pump used
    to clobber them to NULL on every message. The team-curated `custom_nickname`
    ('John/City/Tag') is then the most reliable name signal we hold, but the senders
    never consulted it and emitted a literal 'Babe'.

    Precedence: real_name → custom_nickname → generated_nickname → of_display_name.
    CURATED BEATS RAW: `custom_nickname` is the CRM label the whole UI shows, so it
    outranks OF's own display name — on 154 prod fans the chat greeted "hey
    surferdude" / "hey Gym Guy" while the team had typed 'Marcus' / 'Dave', and
    send_welcome had always ordered it this way. Note the column is NOT purely
    hand-typed: `push_nick_and_notes` mirrors `build_structured_nickname` back into
    it, so ~1.6k of 2k prod values are our own structured push. That is why it sits
    ABOVE generated_nickname rather than below — measured, the two orders disagree on
    exactly ONE fan in 8,550, so the mirror never robs the AI label of a better name.
    `of_username` is deliberately excluded — handles like 'u123' / 'alexnielsen'
    aren't greetable names. Returns '' when we truly have nothing; callers keep their
    own soft 'babe' fallback.

    BOTH nickname columns hold a STRUCTURED LABEL, not a name: gen_info builds
    `Name/City,Country/Age/Job/Tag` and, when it can't find a usable name, blanks the
    first slot — `_clean_nickname` then drops the empty slot so the LOCATION slides
    into first position ('Vancouver,Canada/39', 'USA/Free'). generated_nickname used
    to be returned verbatim, so those labels reached the chat engines as the fan's
    name and she greeted people with "hey Kentucky,USA u still there?". Both now go
    through `name_token`, which keeps a real name slot and rejects a location one.

    `f` may be a Fan ORM row or a dict of the same fields."""
    def _g(attr: str) -> str:
        if f is None:
            return ""
        raw = f.get(attr) if isinstance(f, dict) else getattr(f, attr, "")
        # OF hands these fields back HTML-escaped, and of_display_name is greeted
        # VERBATIM — without this she says "hey Alex&amp;Sam".
        return html.unescape(str(raw or "")).strip()

    # HIS OWN LOCATION IS NOT HIS NAME, and neither is his handle — both are
    # `is_greetable_name`'s job, checked against THIS fan's home_city/home_country
    # because a city list can't be static (Austin/Phoenix/Dallas/Milan/Paris are
    # real first names). The first candidate that IS a name wins; the rest of the
    # chain still runs, so a handle in of_display_name falls through to the curated
    # nickname instead of stopping there. of_display_name is OF's own field (a real
    # display name like 'garrett baydala'), so it stays verbatim; only the two label
    # columns are parsed.
    # last=True on the AI label only ('Sexy Sofie' → 'Sofie' — the model writes an
    # adjective in front). The CURATED column is typed by a human as 'First Last'
    # (+ notes), so its FIRST word is the name: 'John Miller' → John, not Miller.
    here = (_g("home_country"), _g("home_city"))
    candidates = (_g("real_name"),
                  name_token(_g("custom_nickname")),
                  name_token(_g("generated_nickname"), last=True),
                  _g("of_display_name"))
    return next((n for n in candidates if is_greetable_name(n, here=here)), "")


# ── Live nickname + note push (V1 build_structured_nickname / apply_nickname) ──
_NICK_MAX = 70
# Placeholder for an EMPTY Name slot. Dropping the slot silently slid the location
# into first position ('Canada/office job'), which reads like the fan is called
# Canada — to the team in the OF chat header and to anything that parses slot 0.
# '_' keeps the slot visible and is not a name by construction: name_token wants a
# Capitalized letter, so the marker can never be read back out as a name.
_NO_NAME = "_"


def build_structured_nickname(f: Fan) -> str:
    """`Name/City,Country/Age/Job` from a fan's facts (port of V1
    build_structured_nickname; Job replaces V1's hobbies to match gen_info's
    nickname format). Empty segments are dropped, so a thin profile yields a
    short nickname. '' when nothing is known."""
    # `custom_nickname` is the OUTPUT this function mirrors back (via
    # of_ai_chat._maybe_push_nickname, every tick). Feeding the whole structured
    # string back in as the Name made each tick re-append loc/age/job, growing
    # 'Donovon/chef/25' → 'Donovon/chef/25/chef/25/chef/nanaimo,canada/25/…' until
    # the 70-char cap. Pull only the NAME slot from it so the loop can't compound.
    age = (f.his_age or "").strip()
    country = (f.home_country or "").strip()
    city = (f.home_city or "").strip()
    # Same predicate the senders greet by (is_greetable_name), so the OF chat header
    # can't claim a name the chat itself won't use: this function mirrors its own
    # output back into custom_nickname every tick, so one 'Canada' — or one
    # 'Fanlover577' — in the Name slot re-pushed itself forever.
    name = next((n for n in ((getattr(f, "real_name", None) or "").strip(),
                             name_token(getattr(f, "custom_nickname", None)),
                             (getattr(f, "of_display_name", None) or "").strip())
                 if is_greetable_name(n, here=(country, city))), "")
    job = (f.occupation or "").strip()
    loc = ",".join(p for p in (city, country) if p)
    parts = [p for p in (name, loc, age, job) if p]
    if not parts:
        return ""
    if not name:                     # '_/Canada/office job', never 'Canada/office job'
        parts.insert(0, _NO_NAME)
    return "/".join(parts)[:_NICK_MAX]
