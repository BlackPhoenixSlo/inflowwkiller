"""
service/automations/script_packs.py — the default script pack.

Every line below is a REAL line from this agency's own 1:1 chat archive (bespoke
single-fan outbound, non-mass, non-automation), lightly normalised: names swapped
for {name}, prices for {price}, HTML stripped. Nothing here is invented, because a
model-invented "sexy line" reads like a model-invented sexy line.

The operator attaches media and does nothing else — that is the whole point. Lines
are overridable per account (`script_pack_overrides_json`) from the UI, but the
shipped defaults are meant to work untouched.

ONE pack, not three. A "Gentle / Balanced / Aggressive" preset was designed and
then cut: nothing in the product selects a pack, persona voice is already governed
by the account's AI persona config, and offering a non-technical operator a choice
between three untested voices is a decision with no evidence behind it.
"""
from __future__ import annotations

import re
from random import Random

# `_voice` is a leaf: text + normalisation, no DB and no sibling imports, so a
# top-level import here cannot cycle. (`_language` below stays lazy because it
# is not one.)
from . import _voice

# Slots the engine actually fires. A slot that nothing references is dead weight —
# `bump_no_reply` (the "you're ignoring me 🥺" guilt line) was deliberately CUT from
# every shipping path: it is aimed at the majority who did NOT buy, it is the
# highest-churn message anyone proposed, and there is zero measurement behind it.
PACK: dict[str, list[str]] = {
    # Opens a scene. Free, never priced.
    "question_hook": [
        "cant stop thinking about our chat 😏 u around {name}?",
        "hmmmmm, can we have some fun, dirty fun, the kind that leaves us breathless and aching?🥵",
        "what kind do you play when youre all alone?",
        "hey kitten, do you like 69?",
        "got me in such a mood tonight... what are you up to right now?",
        "are you free now, {name}?",
    ],
    # The cold open — the FIRST priced thing he ever sees. Sits LOW in the band.
    "rung_open": [
        "I need your honest opinion about this babe😘",
        "you will like this vid, check it out",
        "here you go, babe😘",
        "you can have a taste of this when you get home🥵🥵",
        "Are you going to let me dance for you😋🥰",
    ],
    # After a PAID rung, inside the hot window. This is where the money is.
    "rung_escalate": [
        "{name}, watch me strip here in my bedroom. Do you think you can help me out using your mouth only? 🥵",
        "mmm, {name}. Do you wanna bury your face here between my legs and make my pussy wet?🥵",
        "do you really want me right here babyyyy😋",
        "i know my worth honey, and i know you gonna enjoy seeing me getting fucked 😈",
        "dont nut all over the place baby🥵💦",
    ],
    # Immediately after an unlock — FREE. Keeps the scene alive so the next rung
    # lands on a conversation, not on silence.
    "post_buy_bridge": [
        "mmm yeah? did you like that 😏",
        "awww baby, how i wish i saw the mess you just caused there",
        "youll have to let me know all about it",
    ],
    "edge_hold": [
        "u love that right?",
        "mhmmm yeah?",
        "hmm, is that a yes?",
    ],
    # Sells the illusion she is filming it RIGHT NOW. Corpus-universal.
    "pre_ppv_stall": [
        "ok give me two secs im filming it rn 🎥",
        "hold on... im getting it ready for you 😈",
        "one sec baby, setting my phone up",
    ],
    # Answers "too expensive" by citing the CONTENT, never his wallet.
    "objection_price": [
        "well, my usual price for sexting is higher than that babe 🤤 this one's a full video though",
        "its a long one baby, not a quick clip — i promise its worth it",
    ],
    # Concede ONCE, then stop. Never haggle twice — that teaches him to haggle.
    "haggle_counter": [
        "mmm ok... {price} and its yours, but thats me being nice 😏",
    ],
    # ONLY on the unsend-first path: the original must be pulled before the cheaper
    # copy goes out, or he can buy both and we've sold the same clip twice.
    "discount_resend": [
        "hey stranger... where u been? made this hopin itd bring u back — {price} just for you",
        "i took it down and put it back cheaper for you baby, {price} 😘",
    ],
    # SOFT decline ("i'm broke"): keep TALKING, stop SELLING. A poverty plea is the
    # highest-value moment to be a person and the worst moment to be a salesman.
    "soft_broke_ack": [
        # He said he's short on money → she eases off, NO pressure. But these lines must
        # NOT declare "i just wanna talk" — because if HE later pulls (asks for content),
        # the pause lifts and she sells, and a line that foreclosed selling would make her
        # contradict herself. So: warm, no-rush, no promise to never sell.
        "aww no rush at all baby 🥺 im not going anywhere",
        "no worries love, whenever you're ready — im just happy you're here",
        "totally okay babe, lets just enjoy this 😘",
    ],
    # Session end by SILENCE after a PAID rung (§7.1) → ONE warm free closer, then she
    # goes quiet (she does not keep poking him). Reseeded from real corpus closers —
    # warm, NO gift/revenue copy: the "free gift" is §7.2 and code-owned, an aftercare
    # line must never imply one.
    "aftercare": [
        "thanks baby 🥰",
        "dream of me!! talk to you tomorrow!",
        "no pressure okay? 😘",
        "night night, message me when you wake up",
    ],
    # Fan says he just wants to TALK (companion intent, §6.3): seller OFF, conversation
    # ON. Acknowledge it warmly and MEAN it — never a pitch, never a "but if you change
    # your mind" hook.
    "companion_ack": [
        "aww i love just talking to you too 🥰",
        "honestly this is nice, no pressure at all",
        "i'm happy just like this babe",
    ],
}

# ── Spanish pack (es) — native casual sexting register, not translated-formal.
#    Any slot omitted here falls back to the English line (per-slot, in render()),
#    so a partial pack is safe. Add pt/fr/de/it/sl the same way: a new PACK_<CODE>
#    dict + an entry in PACKS. ──────────────────────────────────────────────────
PACK_ES: dict[str, list[str]] = {
    "question_hook": [
        "no dejo de pensar en nuestra charla 😏 andas por ahí {name}?",
        "hmmmmm, jugamos un rato? algo sucio, del que te deja sin aliento y con ganas 🥵",
        "a qué juegas cuando estás solito?",
        "hey gatito, te gusta el 69?",
        "me dejaste de un humor esta noche... qué haces ahorita?",
        "estás libre ahora {name}?",
    ],
    "rung_open": [
        "necesito tu opinión sincera sobre esto bebé 😘",
        "este video te va a encantar, échale un ojo",
        "aquí tienes, bebé 😘",
        "puedes probar un poquito de esto cuando llegues a casa 🥵🥵",
        "vas a dejar que baile para ti? 😋🥰",
    ],
    "rung_escalate": [
        "{name}, mírame desnudarme aquí en mi cama. crees que puedes ayudarme usando solo tu boca? 🥵",
        "mmm {name}, quieres enterrar tu cara aquí entre mis piernas y ponerme bien mojada? 🥵",
        "de verdad me deseas aquí mismo bebé? 😋",
        "sé cuánto valgo cariño, y sé que vas a disfrutar verme bien cogida 😈",
        "no te vengas por todos lados bebé 🥵💦",
    ],
    "post_buy_bridge": [
        "mmm sí? te gustó eso 😏",
        "ayy bebé, cómo quisiera haber visto el desastre que acabas de hacer",
        "me vas a tener que contar todo",
    ],
    "edge_hold": [
        "te encanta eso verdad?",
        "mhmmm sí?",
        "hmm, eso es un sí?",
    ],
    "pre_ppv_stall": [
        "ok dame dos segundos que lo estoy grabando ahorita 🎥",
        "espera... te lo estoy preparando 😈",
        "un segundo bebé, estoy acomodando el teléfono",
    ],
    "objection_price": [
        "bueno, mi precio normal por sexting es más alto que eso bebé 🤤 pero este es un video completo",
        "es uno largo bebé, no un clip rapidito — te prometo que vale la pena",
    ],
    "haggle_counter": [
        "mmm ok... {price} y es tuyo, pero es porque estoy siendo buena 😏",
    ],
    "discount_resend": [
        "hey extraño... dónde te metiste? hice esto esperando que te trajera de vuelta — {price} solo para ti",
        "lo bajé y lo volví a subir más barato para ti bebé, {price} 😘",
    ],
    "soft_broke_ack": [
        "ayy sin prisa bebé 🥺 no me voy a ningún lado",
        "tranquilo amor, cuando estés listo — me alegra que estés aquí",
        "todo bien bebé, disfrutemos esto nomás 😘",
    ],
    "aftercare": [
        "gracias bebé 🥰",
        "sueña conmigo!! hablamos mañana!",
        "sin presión ok? 😘",
        "buenas noches, escríbeme cuando despiertes",
    ],
    "companion_ack": [
        "ayy a mí también me encanta solo hablar contigo 🥰",
        "la verdad esto está rico, sin ninguna presión",
        "soy feliz así nomás bebé",
    ],
}

# ── Slovenian pack (sl). GENDER: female speaker → male fan, so HER verbs are
#    feminine (naredila, posnela, pripravljena) and HIS are masculine (si prišel,
#    boš videl, sramežljiv). Endearment addresses him (srček). ──────────────────
PACK_SL: dict[str, list[str]] = {
    "question_hook": [
        "ne morem nehat mislit na najin klepet 😏 si tu {name}?",
        "hmmmmm, se lahko malo poigrava? umazano, tako da nama zmanjka sape 🥵",
        "kaj počneš ko si čisto sam?",
        "hej srček, ti je všeč 69?",
        "v takem razpoloženju si me pustil nocoj... kaj počneš zdaj?",
        "si zdaj prost {name}?",
    ],
    "rung_open": [
        "rabim tvoje iskreno mnenje o tem srček 😘",
        "ta video ti bo všeč, poglej",
        "izvoli, srček 😘",
        "tega lahko poskusiš ko prideš domov 🥵🥵",
        "mi boš pustil da plešem zate? 😋🥰",
    ],
    "rung_escalate": [
        "{name}, glej me kako se slačim tu v svoji spalnici. misliš da mi lahko pomagaš samo z usti? 🥵",
        "mmm {name}, bi rad zakopal obraz tukaj med moje noge in me naredil čisto mokro? 🥵",
        "me res želiš prav tukaj srček? 😋",
        "vem koliko sem vredna srček, in vem da boš užival ko me boš gledal kako me fukajo 😈",
        "ne pridi povsod naokrog srček 🥵💦",
    ],
    "post_buy_bridge": [
        "mmm ja? ti je bilo všeč 😏",
        "ojoj srček, kako želim da bi videla nered ki si ga pravkar naredil",
        "mi boš moral vse povedat",
    ],
    "edge_hold": [
        "to ti je všeč ne?",
        "mhmmm ja?",
        "hmm, je to ja?",
    ],
    "pre_ppv_stall": [
        "ok daj mi dve sekundi ravno snemam 🎥",
        "počakaj... ti pripravljam 😈",
        "sekundo srček, nastavljam telefon",
    ],
    "objection_price": [
        "no, moja običajna cena za sexting je višja od tega srček 🤤 tale je pa cel video",
        "dolg je srček, ne hiter posnetek — obljubim da je vreden",
    ],
    "haggle_counter": [
        "mmm ok... {price} in je tvoj, ampak to sem samo prijazna 😏",
    ],
    "discount_resend": [
        "hej tujec... kje si bil? tole sem naredila v upanju da te pripelje nazaj — {price} samo zate",
        "snela sem ga in dala nazaj ceneje zate srček, {price} 😘",
    ],
    "soft_broke_ack": [
        "ojoj brez skrbi srček 🥺 nikamor ne grem",
        "brez skrbi ljubi, ko boš pripravljen — samo vesela sem da si tu",
        "čisto v redu srček, samo uživajva 😘",
    ],
    "aftercare": [
        "hvala srček 🥰",
        "sanjaj o meni!! se slišiva jutri!",
        "brez pritiska ok? 😘",
        "lahko noč, piši mi ko se zbudiš",
    ],
    "companion_ack": [
        "ojoj tudi meni je všeč samo klepetat s tabo 🥰",
        "iskreno tole je lepo, brez pritiska",
        "srečna sem kar tako srček",
    ],
}

# Language registry. 'en' is authoritative; render() falls back to English per-slot
# for any language/slot not present. Seam for adding more languages later.
PACKS: dict[str, dict[str, list[str]]] = {"en": PACK, "es": PACK_ES, "sl": PACK_SL}

# ── The MALE lane (creator is a man) ─────────────────────────────────
# Same slots, same job, opposite power axis. This exists because the pack is a
# DETERMINISTIC send path: `_pack_line` fires on nudge / post-buy / aftercare
# turns with no model in the loop, and those turns are reached BEFORE the engine
# loads any per-account flag. So on a male account the shipped pack was not a
# tone problem — it sent "make my pussy wet" and "watch me strip here in my
# bedroom" verbatim to a fan, from a man, with nothing able to intercept it.
#
# Written to the same rules as the female pack with one addition:
#
#   • NO female anatomy, and no line that only works if the creator is desired
#     rather than doing the desiring. In the male lane the fan pursues.
#   • NO word from `_common._RESTRICTED_WORDS`. That list is OnlyFans' soft-block
#     set and `apply_word_restriction` doubles a vowel in each to slip the filter
#     ("slave" → "slaave"). The operator has accepted that trade for MODEL output,
#     where it is rare and contextual — but a canned line is sent verbatim every
#     time it is picked, so a restricted word here would be a permanent visible
#     typo in a fixed string. The dom register has plenty of vocabulary that
#     isn't on the list; this pack uses that.
#   • Terse. Brevity reads as authority, and it is the one stylistic property that
#     survives the lane flip unchanged.
#
# English only for now: `_language.localized` falls back to the English line for
# any language a pack omits, so an es/sl male account degrades to English here
# rather than to a grammatically-female Spanish or Slovenian line. That is the
# correct failure direction and it is why no PACK_HIM_ES exists yet.
PACK_HIM: dict[str, list[str]] = {
    "question_hook": [
        "you been slacking or working? {name}",
        "whats your excuse today",
        "still awake. figured youd be up too",
        "tell me what youve actually done this week",
        "hows the training going, honestly",
        "you around {name}",
    ],
    "rung_open": [
        "made this. tell me what you think",
        "have a look at this one",
        "this ones worth your time",
        "put this together earlier. see what you make of it",
        "you'll want to see this",
    ],
    "rung_escalate": [
        "{name}. you want this or not",
        "stop overthinking it. you already know you want it",
        "this is the one you've been waiting for. dont waste it",
        "youve earned a look at this one",
        "last one like this for a while. your call",
    ],
    "post_buy_bridge": [
        "good. what did you think",
        "knew youd like that one",
        "tell me it was worth it",
    ],
    "edge_hold": [
        "you know im right",
        "go on then",
        "thats a yes",
    ],
    "pre_ppv_stall": [
        "give me two mins, recording it now",
        "hold on. getting it ready",
        "one sec, setting up",
    ],
    "objection_price": [
        "thats what it costs. its a full one, not a clip",
        "its worth more than that and you know it",
    ],
    "haggle_counter": [
        "{price}. thats the last time i move on it",
    ],
    "discount_resend": [
        "you went quiet. made this anyway — {price}, and only because its you",
        "put it back up cheaper. {price}. dont make me do that twice",
    ],
    "soft_broke_ack": [
        "no rush. it'll still be here",
        "fair enough. sort yourself out first",
        "all good. we're just talking",
    ],
    "aftercare": [
        "good man. talk tomorrow",
        "get some sleep",
        "no pressure. youre alright",
        "night. message me when youre up",
    ],
    "companion_ack": [
        "this is fine too. no pressure",
        "im happy just talking",
        "yeah, this works",
    ],
}

# lang → pack, per voice. The male registry is English-only on purpose (see above).
# Keyed on `_voice`'s own constants, not the bare strings: this dict and the lane
# must be unable to disagree about what a voice is called.
PACKS_HIM: dict[str, dict[str, list[str]]] = {"en": PACK_HIM}
PACKS_BY_VOICE: dict[str, dict[str, dict[str, list[str]]]] = {
    _voice.VOICE_HER: PACKS,      # every account that has ever run here
    _voice.VOICE_HIM: PACKS_HIM,
}


_PLACEHOLDER_RE = re.compile(r"\{(name|price)\}")


def render(slot: str, *, rng: Random, name: str = "babe", price_cents: int | None = None,
           overrides: dict[str, list[str]] | None = None, lang: str = "en",
           voice: str = _voice.VOICE_HER) -> str | None:
    """Pick one line for `slot` and fill its placeholders.

    `overrides` is the account's edited pack (UI). An override REPLACES the shipped
    pool for that slot; an empty list falls back to the default rather than sending
    an empty message. `lang` selects the language pack; a language/slot the pack
    omits falls back to the English line.

    `voice` selects the REGISTRY (see PACKS_BY_VOICE). It matters more here than
    anywhere else the lane reaches: these lines are sent VERBATIM with no model in
    the loop, on turns that fire before the engine loads any flag, so a female line
    on a male account is not a bad-sounding reply — it is anatomically impossible
    text from the creator, unintercepted. Anything that is not "him" resolves to the
    female registry, so the default is byte-identical."""
    pool = None
    if overrides:
        candidate = overrides.get(slot)
        if isinstance(candidate, list) and any(str(x).strip() for x in candidate):
            pool = [str(x) for x in candidate if str(x).strip()]
    if pool is None:
        from . import _language
        # `_voice.norm_voice`, not a hand-rolled strip/lower — this is the one send
        # path with no model in the loop, and a second copy of the normalisation
        # would drift the day the first one changes.
        registry = PACKS_BY_VOICE[_voice.norm_voice(voice)]
        pool = _language.localized(registry, lang, slot) or []
    if not pool:
        return None

    line = rng.choice(pool)
    price = ""
    if price_cents:
        price = (f"${price_cents // 100}" if price_cents % 100 == 0
                 else f"${price_cents / 100:.2f}")
    return _PLACEHOLDER_RE.sub(
        lambda m: (name if m.group(1) == "name" else price), line
    ).strip()


def slots() -> list[str]:
    """Slot names, for the UI's editor."""
    return list(PACK)
