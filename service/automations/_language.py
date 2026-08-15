"""service/automations/_language.py — the per-account language layer (R3).

The pivotal lesson from the adversarial passes: you CANNOT make the English guards
bilingual by unioning Spanish tokens into them — Spanish/English homographs (once, come,
solo, sale, red, mas, …) mean a Spanish token fires on English at scale (proven: 6,390
real outbound English messages mangled). So language is a GATE, not a union.

This module gives every sender:
  • resolve_language(account_lang, fan_lang)  → the effective ISO 639-1 code
  • output_language_directive(lang)           → the OUTPUT-LANGUAGE block (appended at
                                                 the END of a system prompt; "" for en)
  • language-aware detector dispatchers        → English delegates to the EXISTING tuned
                                                 _common/upsell functions unchanged; only
                                                 a non-en account runs the Spanish layer.

The Spanish regexes are separately authored and WORD-BOUNDARY anchored. Because they
only run on non-en accounts they never touch English traffic — and the test suite still
proves zero false positives against the real English corpus (defense in depth for
code-switching fans).
"""
from __future__ import annotations

import re

from sqlalchemy import select

from . import _common
from db.engine import get_session
from db.models import AccountAiConfig

# Languages the product can route. NULL/unknown ⇒ "en".
KNOWN_LANGS = ("en", "es", "sl", "pt", "fr", "de", "it")
LANG_DISPLAY = {
    "en": "English", "es": "Español", "sl": "Slovenščina",
    "pt": "Português", "fr": "Français", "de": "Deutsch", "it": "Italiano",
}
# Which languages have a hand-built guard + output layer TODAY. Others fall back to the
# English guards but still get the output-language directive (best effort).
GUARD_LANGS = frozenset({"es"})


def norm_lang(raw) -> str:
    """A value → a known ISO 639-1 code, else '' (caller treats '' as 'en')."""
    s = (raw or "")
    s = s.strip().lower() if isinstance(s, str) else ""
    if not s:
        return ""
    code = s.split("-")[0].split("_")[0]
    return code if code in KNOWN_LANGS else ""


async def load_account_language(account_id: str) -> str:
    """The account's configured language ('en' when unset). One tiny read; senders call
    it once per run and thread the result into resolve_language + the guards."""
    async with get_session() as s:
        raw = (await s.execute(
            select(AccountAiConfig.language).where(AccountAiConfig.account_id == str(account_id))
        )).scalar_one_or_none()
    return norm_lang(raw) or "en"


def resolve_language(account_lang=None, fan_lang=None) -> str:
    """The effective language for a turn. A per-fan value (manual override or, later,
    detection) wins; then the account default; then 'en'. For v1 the senders pass only
    the account default (per-fan routing is deferred) — passing fan_lang is the one-line
    flip that turns per-fan routing on."""
    return norm_lang(fan_lang) or norm_lang(account_lang) or "en"


def localized(registry: dict, lang, key):
    """Pick registry[lang][key] with an English fallback. `registry` is a
    {lang: {key: value}} map whose 'en' entry is the authoritative/complete set; a
    missing language OR a missing/empty key falls back to English. This is the ONE
    seam every localized content pack shares (script packs, PPV caption pools, unlock
    reactions) — adding sl/pt/fr/de/it later is just dropping another
    {'<code>': {...}} block into the registry, with zero call-site changes."""
    en = registry.get("en", {})
    loc = registry.get(norm_lang(lang) or "en", en)
    return loc.get(key) or en.get(key)


# ── The OUTPUT-LANGUAGE directive (appended at the END of a system prompt) ──
def output_language_directive(lang: str) -> str:
    """The block that forces the model's OUTPUT language. Appended at the very END of a
    system prompt so the stable prefix (and its provider cache) is untouched. '' for en
    (English prompts already produce English). Names the language explicitly and pins
    the protocol token so a Spanish reply never leaks '>>OFERTA'."""
    code = norm_lang(lang)
    if not code or code == "en":
        return ""
    name = LANG_DISPLAY.get(code, code)
    return (
        f"\n\nOUTPUT LANGUAGE: {name} ({code}) only, casual native texting — but "
        f"protocol markers like '>>OFFER' stay in English, verbatim."
    )


def qtease_directive(lang: str) -> str:
    """The instruction appended to gen_info's prompt so the fan-facing openers it
    generates (Q1-3, Tease1-3) come out in the fan's language — deep_convo / reengage
    send those VERBATIM, so they'd otherwise ship English on a Spanish account. The
    extracted FACTS stay as the fan stated them; only the openers are localized. '' for en."""
    code = norm_lang(lang)
    if not code or code == "en":
        return ""
    name = LANG_DISPLAY.get(code, code)
    return (
        f"\n\nLANGUAGE: Q1-Q3 and Tease1-Tease3 in {name} ({code}), native texting "
        f"(sent to him verbatim). extracted facts exactly as he stated them; "
        f"short_bio and bullet_points in English."
    )


# ── Spanish guard vocabulary (anchored; runs ONLY on non-en accounts) ──
# Buying signal — he asks to SEE content.
_ES_CONTENT_ASK = re.compile(
    r"(?<!\w)("
    r"m[aá]ndame|env[ií]ame|mu[eé]strame|ens[eé][nñ]ame|"
    r"quiero ver|d[eé]jame ver|me ense[nñ]as|puedo ver|"
    r"cu[aá]nto (cuesta|vale|por)|"
    r"tienes (fotos?|v[ií]deos?|algo)|"
    r"(manda|mandame|quiero|dame|tienes) (fotos?|nudes?|desnuda|contenido|v[ií]deos?)|"
    r"m[aá]s (fotos?|contenido|v[ií]deos?)|"
    r"desnud[ao]s?|nudes?"
    r")(?!\w)", re.IGNORECASE)

# Escalation — he describes a horny state or an act he wants.
_ES_ESCALATION = re.compile(
    r"(?<!\w)("
    r"estoy (muy )?(caliente|cachond[ao]|duro)|me pones (caliente|cachond[ao])|"
    r"quiero (cogerte|follarte|chuparte|comerte|lamerte|penetrarte|tocarte)|"
    r"me quiero (correr|venir)|c[oó]rrete|hazme (acabar|correr)|"
    r"est[aá]s buen[ao]|qu[eé] rica|te deseo|te necesito dentro"
    r")(?!\w)", re.IGNORECASE)

# Poverty brake — he says he's broke / can't pay / it's too expensive.
_ES_SPEND_REGRET = re.compile(
    r"(?<!\w)("
    r"no tengo (dinero|plata|para)|sin dinero|estoy (quebrado|pelado|sin (un )?peso)|"
    r"no puedo (pagar|permitirme|gastar)|no me alcanza|ando corto|"
    r"(muy|demasiado) (caro|cara)|est[aá] caro|no tengo tanto"
    r")(?!\w)", re.IGNORECASE)

# Hard decline — stop / not interested / no thanks. NB: bare "para" is EXCLUDED — it is
# the ubiquitous preposition ("para cojerte" = "to fuck you"), not the imperative "stop".
# Only the unambiguous imperative forms ("para ya", "párate", "basta ya") are decline.
_ES_DECLINE = re.compile(
    r"(?<!\w)("
    r"no gracias|no me interesa|d[eé]jame en paz|ya no quiero|"
    r"para ya|p[aá]rate|basta ya|ya basta|"
    r"no quiero (nada|comprar|pagar|m[aá]s)|"
    r"me voy a (dar de baja|desuscribir)|cancelar la suscripci[oó]n"
    r")(?!\w)", re.IGNORECASE)

# Stated price ceiling — "I'll pay you 25". Note the inner group is NON-capturing so the
# number is always group(2).
_ES_STATED_CAP = re.compile(
    r"(?<!\w)("
    r"te pago|te doy|puedo pagar|pago hasta|mi (?:l[ií]mite|presupuesto) es"
    r")\s*\$?\s*(\d{1,4})", re.IGNORECASE)

# Bot accusation — "are you a bot / are you real / am I talking to an AI".
_ES_BOT = re.compile(
    r"(?<!\w)("
    r"eres (un[ao]? )?(bot|robot|m[aá]quina|ia|inteligencia artificial)|"
    r"eres real|de verdad eres|hablo con (una?|un) (bot|robot|m[aá]quina|ia|persona real)|"
    r"esto es (un[ao]? )?(bot|ia|autom[aá]tico)"
    r")(?!\w)", re.IGNORECASE)

# Haggle — a price objection / asking for a cheaper number. This is the discount-
# resend trigger, NOT the poverty brake: "why so expensive?" earns a real discount,
# "I have no money" earns a pause. A live fan haggled twice in Spanish ("porque
# subió tanto el precio? cada vez se sube más y más") and the English-only regex
# never fired — the AI answered with chat while the ladder kept climbing.
_ES_HAGGLE = re.compile(
    r"(?<!\w)("
    r"por ?qu[eé] tan car[oa]|(por ?qu[eé]|porque) tanto|"
    r"sub(i[oó]|e) (tanto )?(el )?precio|el precio sub(i[oó]|e)|se sube m[aá]s|"
    r"(muy|demasiado|bien) car[oa]|est[aá] (muy |bien )?car[oa]|"
    r"m[aá]s barat[oa]|descuent(o|ito)|rebaj(a|ita)|b[aá]jale|"
    r"mejor precio|(un )?precio m[aá]s bajo|m[aá]s econ[oó]mico"
    r")(?!\w)", re.IGNORECASE)

# The English haggle detector (moved here from ai_chatter so both packs live together).
_EN_HAGGLE = re.compile(
    r"\b(too\s+(?:much|expensive|pricey|steep)|expensive|pricey|cheaper|cheap|"
    r"discount|deal|lower(?:\s+price)?|less|"
    r"how\s+about\s+\$?\d|\$?\d+\s*(?:instead|max|tops)|any\s+cheaper)\b",
    re.IGNORECASE)

# OF ToS / restricted vocabulary in Spanish (the English list can't cover it). Doubled
# like the English list to soften OF's own filter.
_ES_RESTRICTED = (
    "menor", "menores", "violaci[oó]n", "violar", "sangre", "orina", "esclav[ao]",
    "secuestr[oa]", "incesto", "embarazada", "borracha", "dormida", "inconsciente",
    "prostitut[ao]", "drogad[ao]", "menor de edad",
)
_ES_RESTRICTED_RE = re.compile(
    r"(?<!\w)(" + "|".join(_ES_RESTRICTED) + r")(?!\w)", re.IGNORECASE)


# ── Slovenian buy-signals (sl) ───────────────────────────────────────
# Detectors run on the FAN's inbound (male → female creator). Only content-ask +
# escalation are authored — the two signals that gate the closer's intent gate and
# sell-pivot; the other guards fall back to English for sl (see the maps below).
_SL_CONTENT_ASK = re.compile(
    r"(?<!\w)("
    # imperatives — Slovenian + English code-switch (fans mix English freely)
    r"pošlji|pokaži|send( me)?|show me|"
    # price ask
    r"(koliko|kolk[oa]) (stane|stanejo|za)|how much|"
    # verb (SL + English) + object (SL + English nouns)
    r"(hočem|želim|rad bi|bi rad|daj|daj mi|imaš|maš|want|i want|wanna see|gimme|give me) "
    r"(te |to )?(videt|videl|vidim|see|slik[eio]|fotk[eio]|foto|photos?|pics?|"
    r"video|posnetek|golo|nud(es?|i|ce)|kaj lepega)|"
    # strong bare content nouns (SL + English)
    r"(gole slike|gola slika|nud(es?|i|ce)|pics?|več slik|več fotk|več golega)"
    r")(?!\w)", re.IGNORECASE)

# NOTE the gender split: the FAN is male, so his self-description is MASCULINE
# ("vroč sem", "trd sem"), while describing HER is FEMININE ("vroča si", "mokra si").
# Slovenians code-switch English a lot, so horny/hard/hot/wet are matched too.
_SL_ESCALATION = re.compile(
    r"(?<!\w)("
    # HIM about himself (masc): hot / horny / hard — Slovenian + English mix
    r"(zelo |tako |čist |cist )?(vroč|napaljen|nabrit|horni|horny|hard) sem|"
    r"vroče mi je|trd sem|imam (ga )?trd(ega)?|so (horny|hard)|"
    # explicit acts he wants (both word orders, SL)
    r"(hočem te|te hočem|hočem|želim te) (pofukat|fukat|polizat|lizat|čutit|začutit)|"
    r"(pofukal|polizal|lizal|nategnil) bi te|bi te (rad )?(pofukal|polizal|nategnil)|"
    r"hočem (biti |bit )?v tebi|drkam|drka mi|"
    # HER, as he describes her (fem): hot / wet / sexy — Slovenian + English
    r"si (tako |čist )?(seksi|sexy|vroča|mokra|hot|wet)|"
    r"tako si (seksi|sexy|vroča|mokra|hot|wet)|so (hot|sexy|wet)|"
    # general wanting
    r"hočem te|želim te|rabim te"
    r")(?!\w)", re.IGNORECASE)

# Per-language detector maps — a language present here uses its OWN regex; absent →
# the English _common detector. 'es' reproduces the exact prior behavior. To make a
# signal bilingual for a new language, add its regex here.
_CONTENT_ASK_BY_LANG = {"es": _ES_CONTENT_ASK, "sl": _SL_CONTENT_ASK}
_ESCALATION_BY_LANG = {"es": _ES_ESCALATION, "sl": _SL_ESCALATION}


# ── Language-aware dispatchers ───────────────────────────────────────
# English (and any language without a guard layer) delegates to the EXISTING tuned
# functions — unchanged behaviour. Only a GUARD_LANGS account runs the Spanish layer.

def _es(lang: str) -> bool:
    return norm_lang(lang) in GUARD_LANGS


def is_content_ask(text, lang="en") -> bool:
    rx = _CONTENT_ASK_BY_LANG.get(norm_lang(lang))
    if rx is not None:
        return bool(text) and bool(rx.search(text or ""))
    return _common.is_content_ask(text)


def is_escalation(text, lang="en") -> bool:
    rx = _ESCALATION_BY_LANG.get(norm_lang(lang))
    if rx is not None:
        return bool(text) and bool(rx.search(text or ""))
    return _common.is_escalation(text)


def thread_heat(messages, lang="en") -> bool:
    """Bilingual thread_heat: the 24.3× purchase signal. For es, ANY recent fan message
    that is a content-ask or an escalation. For en, the existing helper unchanged."""
    ca = _CONTENT_ASK_BY_LANG.get(norm_lang(lang))
    esc = _ESCALATION_BY_LANG.get(norm_lang(lang))
    if ca is None or esc is None:
        return _common.thread_heat(messages)
    for m in messages or []:
        body = m[1] if isinstance(m, (tuple, list)) and len(m) > 1 else getattr(m, "body", "")
        direction = m[0] if isinstance(m, (tuple, list)) else getattr(m, "direction", "in")
        if direction == "in" and (ca.search(body or "") or esc.search(body or "")):
            return True
    return False


def detect_spend_regret(text, lang="en") -> bool:
    if _es(lang):
        return bool(text) and bool(_ES_SPEND_REGRET.search(text or ""))
    from . import upsell
    return bool(upsell.detect_spend_regret(text))


def is_hard_decline(text, lang="en") -> bool:
    if _es(lang):
        return bool(text) and bool(_ES_DECLINE.search(text or ""))
    from . import upsell
    return upsell.classify_decline(text) is not None


def detect_stated_cap(text, lang="en"):
    """Returns the stated cap in CENTS, or None. Spanish or English."""
    if _es(lang):
        m = _ES_STATED_CAP.search(text or "") if text else None
        return int(m.group(2)) * 100 if m else None
    from . import upsell
    return upsell.detect_stated_cap(text)


def is_fan_pull(text, lang="en") -> bool:
    """Is HE explicitly PULLING to buy — a content-ask, or a price he named himself?

    THE CANONICAL COPY. This predicate is the single thing that lifts a spend-regret
    offers-pause (`upsell.qualify`'s `fan_pull`), and it had grown four
    implementations: `ai_chatter._fan_pull`, `upsell.qualify`'s own inline version,
    and two in `sell_lane`. Three of those were English-only while the engines armed
    on `is_content_ask(text, lang)` — so on an `es` account the lane fired and then
    the pause refused to lift, killing the one measured exception in the ladder
    (37/37 broke-then-buyers bought a FRESH offer).

    Deliberately STRICTER than heat: pure arousal is not consent to be re-priced. A
    broke man mid-sext trips the escalation detector every turn; only a real buy
    signal — or a price HE names — proves the "broke" wasn't final.
    """
    return is_content_ask(text, lang) or detect_stated_cap(text, lang) is not None


def detect_bot_accusation(text, lang="en") -> bool:
    if _es(lang):
        return bool(text) and bool(_ES_BOT.search(text or ""))
    return bool(_common.detect_bot_accusation(text))


def is_haggle(text, lang="en") -> bool:
    """Price objection / cheaper-please — routes to the discount-resend paths.
    Unlike the replace-style detectors above, a guarded lang runs BOTH packs:
    Spanish-speaking fans code-switch price words ("discount", "$20 instead")
    freely, and a missed haggle answers a price complaint with a HIGHER rung."""
    if not text:
        return False
    if _es(lang) and _ES_HAGGLE.search(text):
        return True
    return bool(_EN_HAGGLE.search(text))


def apply_word_restriction(text, lang="en") -> str:
    """OF banned-word softening. English delegates to the existing filter (unchanged).
    Spanish runs the SPANISH banned list (the English list both misses Spanish ToS terms
    AND mangles Spanish homographs, so it must NOT run on es)."""
    if not text:
        return text
    # Register floor for BOTH branches — the en branch would inherit the ¿/¡
    # strip via _common.apply_word_restriction, but the es branch (the one that
    # actually sees the chars) would not, so strip up front.
    text = _common.strip_inverted_punct(text)
    if _es(lang):
        return _ES_RESTRICTED_RE.sub(lambda m: _common._double_first_vowel(m), text)
    return _common.apply_word_restriction(text)
