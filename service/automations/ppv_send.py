"""service/automations/ppv_send.py — the PPV Library sender.

One premade PPV → fanned out to spend×recency fan SEGMENTS, the SAME media at a
DIFFERENT price per segment, with a rotated preview teaser. Self-registers via
`@register("ppv_send")`.

Where it fits:
  • Operator builds ~20 PPVs in the "PPV Library" tab (ppv_library_config_api),
    each = vault media ids + a caption-pool key + a base price + preview options
    + sends_per_week + resend_monthly.
  • On save the config API upserts ONE `ppv_send` AutomationRule per enabled PPV,
    cadence `every_seconds = 604800 / sends_per_week`. The existing rule
    materializer is the scheduler — there is no rotator here.
  • When a rule fires, the job payload names one PPV (`{account_id, ppv_id}`).
    THIS module reads that PPV from the config blob and does the send.

Per run it:
  1. reads the PPV from `account_ai_config.ppv_library_config_json`,
  2. buckets every fan into exactly ONE spend×recency cell (so nobody is double
     billed in a run) via the indexed `fans.lifetime_spend_cents` /
     `fans.last_message_received_at` columns,
  3. for each non-empty cell: price = base × spend_mult × recency_mult (rounded to
     .99, floored at $3.99 — OF rejects priced messages under $3.00), a day-ROTATED
     preview from the pool (every resend looks fresh), a random caption with the
     {now}/{was}/{off} discount tokens filled, then delegates to `send_mass_message.run`,
  4. if `resend_monthly`, enqueues a one-shot `ppv_send` at now+30d (a later day →
     a different rotated preview). Buyers drop out (exclude_buyers); non-buyers keep
     getting the same locked content re-pitched with a new teaser until they unlock.

Idempotency / duplicate-fire protection:
  • duplicate-fire gate — skip when THIS ppv already sent (cells_sent>0) within
    `min_send_gap_minutes` (account cfg, default 60; 0 = off). Kills the
    executor's whole-job-retry double-blast seen live 2026-07-01/03: a proxy
    error mid-batch failed the job AFTER some cells had broadcast, and the
    retry re-sent them ~90s later.
  • per-cell containment — a cell/broadcast send failure is RECORDED in the
    stats (status 'error' per cell), never raised, so the executor NEVER
    retries a partially-sent batch wholesale. A failed cell's fans catch the
    next cadence fire instead.
  • ppv_caps even-spread spacing is ON BY DEFAULT (2/day, 14/week, 60/month —
    a 12h minimum gap between ANY two PPV sends on the account); saving explicit
    zeros in the PPV Library tab turns it off. The contact guard (`pause_hours`
    > 0) still applies on top, but its default 0 keeps it OFF.

Payload shape::

    {"account_id": "123456789", "ppv_id": "ppv_a3f9",
     "is_resend": false,            # true = the +30d monthly repeat (fresh preview)
     "dry_run": false,              # plan only — no send, no enqueue
     "force_ids": [123],            # test-scope: restrict the audience, BYPASSES the caps
     "only_fan_ids": [123]}         # scope the audience with EVERY GATE STILL ARMED

`force_ids` vs `only_fan_ids` (the ai_chatter convention, mirrored here):
  • force_ids   — a human live-test scope. Bypasses the per-account cap and the
    duplicate-fire gate on purpose. It is NOT a whitelist: it says WHO, never
    "and therefore anything goes".
  • only_fan_ids — a MACHINE scope (the Human Rhythm scheduler resuming a deferred
    send for one fan). Every gate stays armed: caps, duplicate-fire, the ownership
    guard. A resume that bypassed those would re-sell owned media / double-blast,
    which is precisely what the deferral was trying to avoid.
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
from datetime import datetime, timedelta

from sqlalchemy import func, select

import ownership
import vault_ai_to_chatter
from automation_registry import register
from db.engine import get_session
from db.models import (
    AccountAiConfig, AutomationRun, Blacklist, Fan,
    LadderState, Post, ScheduledJob, Transaction,
)
from ._common import load_hard_skip_ids

log = logging.getLogger("of-relay.automation.ppv_send")

# ── Price matrix (confirmed defaults: whale 2×, $100 whale cutoff, medium win-back)
# spend band: (name, min_cents_inclusive, max_cents_exclusive, multiplier)
SPEND_BANDS: list[tuple[str, int, float, float]] = [
    ("whale", 10_000, float("inf"), 2.0),   # $100+ lifetime
    ("mid",    2_500, 10_000,       1.0),    # $25–100
    ("low",        1,  2_500,       0.7),    # under $25
    ("free",       0,      1,       0.5),    # never paid
]
# recency band by days since last inbound message: (name, max_days_exclusive, mult)
RECENCY_BANDS: list[tuple[str, float, float]] = [
    ("hot",     3,            1.15),   # active in the last 3 days
    ("warm",   14,            1.0),    # this week-ish
    ("cool",   90,            0.8),    # cooling off
    ("quiet",  float("inf"),  0.55),   # gone quiet (also: never messaged)
]
# OF rejects any priced message below $3.00 ("Minimum message price is $3.00",
# verified live 2026-06-20; exactly $3.00 assumed inclusive — untested live).
# These are the HARD wire limits AND the defaults for the operator's per-account
# price_min_cents/price_max_cents; a clamped price sits exactly on the bound.
_PRICE_FLOOR_CENTS = 300
_PRICE_CEIL_CENTS = 20_000     # OF PPV max ($200)


def price_bounds(cfg: dict | None) -> tuple[int, int]:
    """Operator price limits from the top-level library config — (min_cents,
    max_cents), defaults $3/$200. Applied wherever a send/post price is COMPUTED
    (per-cell, broadcast, feed) — never rewrites the authored base_price_cents.
    min is held to OF's hard $3.00 floor; max is held into [min, $200]."""
    cfg = cfg if isinstance(cfg, dict) else {}
    try:
        lo = int(cfg.get("price_min_cents") or _PRICE_FLOOR_CENTS)
    except (TypeError, ValueError):
        lo = _PRICE_FLOOR_CENTS
    try:
        hi = int(cfg.get("price_max_cents") or _PRICE_CEIL_CENTS)
    except (TypeError, ValueError):
        hi = _PRICE_CEIL_CENTS
    lo = max(_PRICE_FLOOR_CENTS, min(lo, _PRICE_CEIL_CENTS))
    hi = max(lo, min(hi, _PRICE_CEIL_CENTS))
    return lo, hi

# House-default spacing between PPV blasts: 2/day, 14/week, 60/month — all three
# even-spread to the SAME 12h minimum gap. Rides into runtime via the _DEFAULTS
# merge in _load_ppv, so it covers every account whose blob has no ppv_caps key
# (nothing ever wrote one before 2026-07). An explicitly SAVED caps dict — even
# all-zero = spacing off — always wins over this. Born after three Library PPVs
# hit the same fan at 06:43/06:48/06:51: the dup-fire gate is per-ppv and the
# sibling rules share a creation moment, so nothing else spaces them.
_DEFAULT_PPV_CAPS = {"per_day": 2, "per_week": 14, "per_month": 60}

_DEFAULTS = {"enabled": False, "ppvs": [], "ppv_caps": _DEFAULT_PPV_CAPS}

# ── Caption pools (mirror of library/PPV_CAPTIONS.md) ────────────────────────
# The library JSON stores a pool KEY per PPV (captions are global, not per-account);
# the runner random-picks one line at send time and fills {was}/{now}.
PPV_CAPTION_POOLS: dict[str, list[str]] = {
    "intro_new": [
        "ok i dont usually do this but i made somethin just for u 🙈 wanna see?",
        "u been so sweet to me so im lettin u in first... its only a lil to unlock, promise its worth it",
        "i was thinkin bout u when i shot this lol. open it, dont leave me hangin",
        "first one basically on me, barely costs anything. just unlock n tell me what u think",
        "im a lil nervous to send this ngl... but u get to see it before anyone. go on 👀",
        "new here so i wanna spoil u a bit. peek at this n lemme know",
    ],
    "standard_active": [
        "been thinkin about sendin u this all day... finally did it 🤭 unlock it for me",
        "u always know what i like so i made this with u in mind. go see",
        "okay this one might be my fav ive done. dont sleep on it babe",
        "i shouldnt be this naughty on a tuesday lol. its waitin for u",
        "filmed somethin earlier n immediately thought of u. its urs if u want it",
        "stop bein shy n open it already 😏 u know u wanna",
    ],
    "vip_whale": [
        "only sendin this to like 3 of my fav people n ur one of them. its special, dont share ok",
        "this is the one i dont post anywhere. made it for the ones who actually take care of me 🖤",
        "i went all out on this... its way more than i usually give. for u only",
        "u spoil me so im spoilin u back, this my best work hands down. go see what i did",
        "savin the real stuff for u. this aint the lil teasers i send everyone, unlock n youll get it",
    ],
    "winback_dormant": [
        "hey stranger... where u been? made this hopin itd bring u back. {off} off just for u, was {was} now {now}",
        "okay i miss u fr 🙈 heres a lil somethin to make up for it, basically nothing. come back to me",
        "u forgot about me?? rude lol. unlock this n ill forgive u, takin {off} off so its just {now}",
        "been a min since i heard from u. this used to be {was}, givin it {off} off for {now}, dont waste it",
        "i kept this one for when u came back... here. its only {now} ({off} off babe), just open it n say hi",
    ],
    "teaser_free": [
        "this is just the lil preview... the rest is so much better trust me 👀",
        "im only showin u this much for free lol. wait til u see what happens after",
        "lil taste. u want the full thing? say the word",
        "couldnt help myself today. this the soft version, the real one is comin",
        "consider this a teaser. ull be thinkin bout the rest all day promise",
    ],
    "photoset_striptease": [
        "starts cute n innocent... it does NOT stay that way lol. unlock to see where it goes",
        "i may have started dressed in these 🙈 may have not ended that way. ur call to find out",
        "it gets better with every single pic. dont stop til the last one",
        "watch me lose the outfit one pic at a time. the last few r my fav",
        "this set is a whole lil story... u gotta see how it ends",
    ],
    "video_ppv": [
        "couldnt keep my hands still in this one 🤭 its a video, unlock n watch",
        "made u a lil clip. first few seconds r tame, give it a min n youll see why i sent it",
        "this video is longer than i usually do... worth every second i promise. go play it",
        "filmed this in one take n didnt even edit much, its all me",
        "u said u wanted to actually see me move... here. press play",
    ],
    "followup_nonunlocker": [
        "u left me on read with that one 🥺 it dont expire yet but dont make me wait",
        "still sittin there waitin for u to open it lol. u good?",
        "ok ill sweeten it since u been busy. same one, lil cheaper now",
        "hellooo did u forget what i sent u 👀 its still there",
        "not gonna lie i kept checkin if u opened it. u gonna leave me hangin?",
    ],
    "bundle_anchor": [
        "okay im doin somethin crazy... a whole bundle of my stuff for like 80 off. its a LOT, unlock it",
        "cleanin out my vault n givin u everything for one price. this wont be up long babe",
        "ive never dropped this much at once. all of it, one unlock, way less than its worth. go fast",
        "huge drop just for my real ones. tons of content one price, dont overthink it just open it",
    ],
    # ── long-form, multi-paragraph sales copy (the "screenshot" look). \n\n =
    #    a blank line between paragraphs; {off}/{was}/{now} auto-fill the discount.
    "bundle_long": [
        "🌟 just for u babe 🌟\n\nim doin {off} off my full bundle today only ❤️‍🔥 unlock this n u get everything... all the pics, all the vids, the stuff i dont post anywhere else\n\nwas {was}, urs for {now} right now 🙈 i wont leave it up long so dont sleep on it 😘",
        "okay im finally droppin the big one 🔥\n\nthis is everything ive been holdin back from u. the whole set, nothin cut, n u keep it for life the second u open it\n\nnormally {was}... takin it down to {now} just for the ones who actually show up for me. thats u 🖤 go before i change my mind",
        "im doin somethin a lil crazy tonight 😳\n\nu always take such good care of me so heres my whole vault in one drop. every angle, every tease, the full thing\n\n{off} off so its only {now} (was {was}) babe. trust me ur gonna want this one forever 😈",
        "real talk i almost didnt post this 🙈\n\nbut i put together my biggest bundle yet n i want u to have it. tons of content, one unlock, way more than its worth\n\nwas {was}, urs for {now} today only. dont leave me waitin ok 💋",
    ],
    # ── short, punchy discount blasts with urgency
    "flash_discount": [
        "🔥 {off} OFF today only babe 🔥 ive literally never sold it this cheap... was {was} now just {now} 🙈 unlock before i put it back up",
        "okay sale time 😈 {off} off for the next few hours only. {now} instead of {was}. go go go before i change my mind 🤭",
        "{now}?? thats nothin for what u get 🙈 was {was}, droppin it just for today. unlock n thank me later 😘",
        "flash deal just for u 🔥 {off} off, only {now} (was {was}). ends tonight, dont leave it sittin there 👀",
        "spoilin u today babe 💋 {off} off my newest set. was {was}, urs for {now} rn. dont make me regret bein this generous 😏",
    ],
    # ── "you're on my special list" exclusivity + a bulleted what's-inside
    "exclusive_list": [
        "⚠️ this aint a mass dm babe ur on my special lil list 🙈\n\nso ur gettin my brand new bundle before i drop it to everyone. heres whats inside (n u keep it all for life):\n\n✨ the full set\n✨ all my fav angles\n✨ the views u always ask for\n✨ n more 😻\n\n{off} off so its just {now} (was {was}). dont tell the others 🤫",
        "psst... not sendin this to everyone ok 🙈 just my real ones\n\ni made a lil exclusive drop n ur on the list for it. whats included:\n\n✨ my newest pics\n✨ a video i never posted\n✨ the closeups u love\n\nonly {now} for u babe (was {was}, thats {off} off). keep it between us 🤍",
        "hey u 🙈 ur literally one of like a handful gettin this\n\nbrand new bundle, way too much to show all my fans on here haha. so its just for the special list. heres the rundown:\n\n✨ full set\n✨ behind the scenes\n✨ my absolute fav angles\n\nyours for {now} (was {was}) 😻 dont share ok",
    ],
    # ── vulnerable / personal "im finally ready to show u this side of me"
    "intimate_reveal": [
        "it took me a while to get this comfy with u but im finally ready to show u this side of me 🙈 please dont judge me ok... its a lot for me. unlock it n be sweet",
        "this is one of the most personal things ive ever filmed 😳 kinda nervous sendin it ngl. but i trust u. go see, just dont share it 🤍",
        "i guess we all got our secret lil kinks right 🙈 well this is mine. i never show this part of me but... here. dont disappoint me ok",
        "ive never been this open on here. unlockin this honestly feels like a big step for me. be gentle with it babe, its just for u 🥺",
        "okay i almost didnt send this one... its real intimate. but somethin bout u makes me wanna show u everything. its urs 😳",
    ],
}
_FALLBACK_CAPTION = "made somethin for u 🙈 unlock it"

# ── Spanish message pools (es) — same keys as PPV_CAPTION_POOLS; _pick_caption
#    falls back to the English pool PER KEY for anything omitted, so a partial set
#    is safe. Add pt/fr/de/it/sl the same way: a PPV_CAPTION_POOLS_<CODE> dict + a
#    PPV_CAPTION_POOLS_BY_LANG entry. Discount tokens {now}/{was}/{off} preserved. ─
PPV_CAPTION_POOLS_ES: dict[str, list[str]] = {
    "intro_new": [
        "ok normalmente no hago esto pero hice algo solo para ti 🙈 quieres verlo?",
        "has sido tan lindo conmigo que te dejo entrar primero... solo cuesta un poquito abrirlo, prometo que vale la pena",
        "pensaba en ti cuando grabé esto jaja. ábrelo, no me dejes esperando",
        "el primero casi va por mi cuenta, apenas cuesta nada. solo ábrelo y dime qué piensas",
        "estoy un poco nerviosa de mandarte esto la verdad... pero lo ves antes que nadie. dale 👀",
        "soy nueva aquí así que quiero consentirte un poco. échale un ojo a esto y dime",
    ],
    "standard_active": [
        "llevo todo el día pensando en mandarte esto... por fin lo hice 🤭 ábrelo por mí",
        "siempre sabes lo que me gusta así que hice esto pensando en ti. míralo",
        "ok este quizás es mi favorito que he hecho. no lo dejes pasar bebé",
        "no debería ser tan traviesa un martes jaja. te está esperando",
        "grabé algo hace rato y pensé en ti al instante. es tuyo si lo quieres",
        "deja de ser tímido y ábrelo ya 😏 sabes que quieres",
    ],
    "vip_whale": [
        "solo le mando esto a como 3 de mis personas favoritas y tú eres una. es especial, no lo compartas ok",
        "este es el que no publico en ningún lado. lo hice para los que de verdad me cuidan 🖤",
        "me esforcé muchísimo en este... es mucho más de lo que suelo dar. solo para ti",
        "tú me consientes así que yo te consiento a ti, este es mi mejor trabajo sin duda. mira lo que hice",
        "guardo lo bueno de verdad para ti. esto no son los teasers que le mando a todos, ábrelo y lo verás",
    ],
    "winback_dormant": [
        "hey extraño... dónde te metiste? hice esto esperando que te trajera de vuelta. {off} de descuento solo para ti, era {was} ahora {now}",
        "ok te extraño en serio 🙈 aquí un detallito para compensar, casi nada. vuelve conmigo",
        "te olvidaste de mí?? qué grosero jaja. abre esto y te perdono, te doy {off} de descuento así queda en {now}",
        "hace rato que no sé de ti. esto costaba {was}, te doy {off} de descuento por {now}, no lo desperdicies",
        "guardé este para cuando volvieras... toma. son solo {now} ({off} menos bebé), ábrelo y saluda",
    ],
    "teaser_free": [
        "esto es solo el adelantito... lo demás está muchísimo mejor créeme 👀",
        "solo te muestro esto gratis jaja. espera a ver lo que pasa después",
        "una probadita. quieres la cosa completa? dilo",
        "no me pude aguantar hoy. esta es la versión suave, la de verdad viene en camino",
        "considera esto un teaser. vas a estar pensando en lo demás todo el día prometido",
    ],
    "photoset_striptease": [
        "empieza tierno e inocente... pero NO se queda así jaja. ábrelo para ver hasta dónde llega",
        "puede que haya empezado vestida con esto 🙈 puede que no haya terminado así. tú decides si averiguas",
        "se pone mejor con cada foto. no pares hasta la última",
        "mírame quitarme la ropa una foto a la vez. las últimas son mis favoritas",
        "este set es toda una historia... tienes que ver cómo termina",
    ],
    "video_ppv": [
        "no pude quedarme quieta en este 🤭 es un video, ábrelo y míralo",
        "te hice un clip. los primeros segundos son tranquilos, dale un momento y verás por qué te lo mandé",
        "este video es más largo de lo que suelo hacer... vale cada segundo prometido. dale play",
        "grabé esto de una sola toma y casi no lo edité, soy toda yo",
        "dijiste que querías verme moverme de verdad... aquí está. dale play",
    ],
    "followup_nonunlocker": [
        "me dejaste en visto con ese 🥺 todavía no expira pero no me hagas esperar",
        "ahí sigue esperando a que lo abras jaja. estás bien?",
        "ok te lo endulzo ya que has estado ocupado. el mismo, un poquito más barato ahora",
        "holaaa se te olvidó lo que te mandé 👀 sigue ahí",
        "no te voy a mentir seguía revisando si lo abriste. me vas a dejar esperando?",
    ],
    "bundle_anchor": [
        "ok voy a hacer una locura... todo un bundle de mis cosas por como {off} menos. es un montón, ábrelo",
        "estoy limpiando mi vault y te doy todo por un solo precio. esto no va a estar mucho tiempo bebé",
        "nunca había soltado tanto de una vez. todo, un solo pago, muchísimo menos de lo que vale. corre",
        "drop enorme solo para mis de verdad. montón de contenido un solo precio, no lo pienses tanto solo ábrelo",
    ],
    "bundle_long": [
        "🌟 solo para ti bebé 🌟\n\nhoy hago {off} de descuento en mi bundle completo ❤️‍🔥 abre esto y tienes todo... todas las fotos, todos los videos, lo que no publico en ningún lado\n\nera {was}, tuyo por {now} ahora mismo 🙈 no lo dejo mucho tiempo así que no te duermas 😘",
        "ok por fin voy a soltar el grande 🔥\n\nesto es todo lo que te he estado guardando. el set completo, nada cortado, y es tuyo de por vida en cuanto lo abras\n\nnormalmente {was}... te lo dejo en {now} solo para los que de verdad aparecen por mí. ese eres tú 🖤 corre antes de que cambie de opinión",
        "voy a hacer algo un poco loco esta noche 😳\n\nsiempre me cuidas tan bien así que aquí tienes todo mi vault en un solo drop. cada ángulo, cada tease, la cosa completa\n\n{off} de descuento así queda en solo {now} (era {was}) bebé. créeme que vas a querer este para siempre 😈",
        "en serio casi no publico esto 🙈\n\npero armé mi bundle más grande hasta ahora y quiero que lo tengas. montón de contenido, un solo pago, mucho más de lo que vale\n\nera {was}, tuyo por {now} solo por hoy. no me dejes esperando ok 💋",
    ],
    "flash_discount": [
        "🔥 {off} DE DESCUENTO solo por hoy bebé 🔥 literal nunca lo he vendido tan barato... era {was} ahora solo {now} 🙈 ábrelo antes de que lo vuelva a subir",
        "ok hora de sale 😈 {off} de descuento por las próximas horas nada más. {now} en vez de {was}. corre corre antes de que cambie de opinión 🤭",
        "{now}?? eso no es nada por lo que recibes 🙈 era {was}, lo bajo solo por hoy. ábrelo y me agradeces después 😘",
        "oferta flash solo para ti 🔥 {off} de descuento, solo {now} (era {was}). termina esta noche, no lo dejes ahí 👀",
        "consintiéndote hoy bebé 💋 {off} de descuento en mi set más nuevo. era {was}, tuyo por {now} ahorita. no me hagas arrepentirme de ser tan generosa 😏",
    ],
    "exclusive_list": [
        "⚠️ esto no es un dm masivo bebé estás en mi listita especial 🙈\n\nasí que recibes mi bundle nuevecito antes de que se lo suelte a todos. esto es lo que trae (y te lo quedas todo de por vida):\n\n✨ el set completo\n✨ todos mis ángulos favoritos\n✨ las vistas que siempre pides\n✨ y más 😻\n\n{off} de descuento así queda en solo {now} (era {was}). no le digas a los demás 🤫",
        "psst... no le mando esto a todos ok 🙈 solo a mis de verdad\n\narmé un drop exclusivo y estás en la lista. lo que incluye:\n\n✨ mis fotos más nuevas\n✨ un video que nunca publiqué\n✨ los closeups que te encantan\n\nsolo {now} para ti bebé (era {was}, eso es {off} menos). que quede entre nosotros 🤍",
        "hey tú 🙈 eres literal de como un puñado que recibe esto\n\nbundle nuevecito, demasiado para mostrárselo a todos mis fans aquí jaja. así que es solo para la lista especial. el resumen:\n\n✨ set completo\n✨ detrás de cámaras\n✨ mis ángulos favoritos absolutos\n\ntuyo por {now} (era {was}) 😻 no lo compartas ok",
    ],
    "intimate_reveal": [
        "me tomó un rato agarrar esta confianza contigo pero por fin estoy lista para mostrarte este lado mío 🙈 por favor no me juzgues ok... es mucho para mí. ábrelo y sé lindo",
        "esto es una de las cosas más personales que he grabado 😳 la verdad un poco nerviosa de mandarlo. pero confío en ti. míralo, solo no lo compartas 🤍",
        "supongo que todos tenemos nuestros kinks secretos verdad 🙈 pues este es el mío. nunca muestro esta parte de mí pero... aquí está. no me decepciones ok",
        "nunca he sido tan abierta aquí. abrir esto honestamente se siente como un gran paso para mí. sé gentil con él bebé, es solo para ti 🥺",
        "ok casi no mando este... es bien íntimo. pero algo de ti me hace querer mostrarte todo. es tuyo 😳",
    ],
}
_FALLBACK_CAPTION_ES = "hice algo para ti 🙈 ábrelo"

# ── Slovenian message pools (sl). GENDER: female speaker → male fan (HER verbs
#    feminine: naredila/posnela/pripravljena; HIS masculine: si bil/boš videl). ──
PPV_CAPTION_POOLS_SL: dict[str, list[str]] = {
    "intro_new": [
        "ok ponavadi tega ne počnem ampak naredila sem nekaj samo zate 🙈 hočeš videt?",
        "bil si tako prijazen z mano zato te spustim prvega... samo malo stane da odkleneš, obljubim da je vredno",
        "mislila sem nate ko sem tole posnela lol. odkleni, ne pusti me viset",
        "prvi je skoraj zastonj, komaj kaj stane. samo odkleni in povej kaj misliš",
        "malo sem živčna da ti tole pošljem ngl... ampak ti to vidiš pred vsemi. daj 👀",
        "nova sem tukaj zato te hočem malo razvajat. poglej tole in mi povej",
    ],
    "standard_active": [
        "cel dan razmišljam da bi ti to poslala... končno sem 🤭 odkleni zame",
        "vedno veš kaj mi je všeč zato sem tole naredila s tabo v mislih. poglej",
        "ok tale je mogoče moj najljubši kar sem jih naredila. ne spreglej ga srček",
        "ne bi smela bit tako poredna v torek lol. čaka te",
        "nekaj sem posnela prej in takoj pomislila nate. tvoj je če hočeš",
        "nehaj bit sramežljiv in ga že odkleni 😏 saj veš da hočeš",
    ],
    "vip_whale": [
        "tole pošiljam samo kakim 3 najljubšim ljudem in ti si eden od njih. posebno je, ne deli ok",
        "tale je tisti ki ga ne objavim nikjer. naredila sem ga za tiste ki zares skrbijo zame 🖤",
        "res sem se potrudila pri tem... veliko več kot ponavadi dam. samo zate",
        "ti me razvajaš zato te jaz razvajam nazaj, tole je moje najboljše delo brez dvoma. poglej kaj sem naredila",
        "pravo stvar hranim zate. to niso tisti mali teaserji ki jih pošiljam vsem, odkleni in boš dobil 😈",
    ],
    "winback_dormant": [
        "hej tujec... kje si bil? tole sem naredila v upanju da te pripelje nazaj. {off} popusta samo zate, bilo {was} zdaj {now}",
        "ok res te pogrešam 🙈 tukaj nekaj malega da se odkupim, skoraj nič. vrni se k meni",
        "si me pozabil?? nesramno lol. odkleni tole in ti odpustim, dam ti {off} popusta da je samo {now}",
        "že dolgo se nisi oglasil. tole je bilo {was}, dajem ti {off} popusta za {now}, ne zapravi ga",
        "tega sem hranila za ko se vrneš... izvoli. samo {now} je ({off} manj srček), samo odkleni in pozdravi",
    ],
    "teaser_free": [
        "tole je samo mali predogled... ostalo je veliko boljše verjemi 👀",
        "samo tolikane ti pokažem zastonj lol. počakaj da vidiš kaj pride potem",
        "mala pokušina. hočeš celo stvar? samo reci",
        "nisem se mogla zadržat danes. tole je nežna verzija, prava pride",
        "vzemi to kot teaser. cel dan boš razmišljal o ostalem obljubim",
    ],
    "photoset_striptease": [
        "začne se srčkano in nedolžno... ampak NE ostane tako lol. odkleni da vidiš kam gre",
        "mogoče sem bila oblečena v tem 🙈 mogoče nisem tako končala. tvoja izbira da izveš",
        "boljše je z vsako sliko. ne ustavi se do zadnje",
        "glej me kako se slačim sliko za sliko. zadnje so moje najljubše",
        "tale set je cela zgodbica... moraš videt kako se konča",
    ],
    "video_ppv": [
        "nisem mogla držat rok pri miru v tem 🤭 video je, odkleni in glej",
        "naredila sem ti posnetek. prvih par sekund je mirnih, daj mu minuto pa boš videl zakaj sem ti ga poslala",
        "tale video je daljši kot ponavadi delam... vreden vsake sekunde obljubim. daj predvajaj",
        "posnela sem to v enem posnetku in skoraj nič urejala, vse sem jaz",
        "rekel si da me hočeš res videt kako se premikam... izvoli. pritisni play",
    ],
    "followup_nonunlocker": [
        "pustil si me na prebrano s tem 🥺 še ne poteče ampak ne pusti me čakat",
        "še vedno tam čaka da ga odkleneš lol. si ok?",
        "ok osladim ti ga ker si bil zaposlen. isti, malo ceneje zdaj",
        "haloo si pozabil kaj sem ti poslala 👀 še vedno je tam",
        "ne bom lagala ves čas sem preverjala če si odklenil. me boš pustil viset?",
    ],
    "bundle_anchor": [
        "ok delam noro stvar... cel bundle mojih stvari za kakih {off} manj. veliko je, odkleni",
        "čistim svoj vault in ti dam vse za eno ceno. tole ne bo dolgo gor srček",
        "nikoli nisem spustila toliko naenkrat. vse, eno plačilo, veliko manj kot je vredno. pohiti",
        "ogromen drop samo za moje prave. kup vsebine ena cena, ne razmišljaj preveč samo odkleni",
    ],
    "bundle_long": [
        "🌟 samo zate srček 🌟\n\ndanes dajem {off} popusta na cel bundle ❤️‍🔥 odkleni tole in imaš vse... vse slike, vse videe, stvari ki jih ne objavim nikjer\n\nbilo {was}, tvoje za {now} prav zdaj 🙈 ne pustim ga dolgo gor zato ne zaspi 😘",
        "ok končno spuščam velikega 🔥\n\ntole je vse kar sem ti prihranila. cel set, nič odrezano, in obdržiš ga za vedno v trenutku ko odkleneš\n\nponavadi {was}... dajem ti ga za {now} samo za tiste ki se zares pojavijo zame. to si ti 🖤 pohiti preden si premislim",
        "delam malo noro stvar nocoj 😳\n\nvedno tako lepo skrbiš zame zato ti dam cel svoj vault v enem dropu. vsak kot, vsak tease, cela stvar\n\n{off} popusta da je samo {now} (bilo {was}) srček. verjemi da boš tole hotel za vedno 😈",
        "res skoraj nisem objavila tega 🙈\n\nampak sestavila sem svoj največji bundle do zdaj in hočem da ga imaš. kup vsebine, eno plačilo, veliko več kot je vredno\n\nbilo {was}, tvoje za {now} samo danes. ne pusti me čakat ok 💋",
    ],
    "flash_discount": [
        "🔥 {off} POPUSTA samo danes srček 🔥 dobesedno še nikoli nisem prodala tako poceni... bilo {was} zdaj samo {now} 🙈 odkleni preden ga dam nazaj gor",
        "ok čas za razprodajo 😈 {off} popusta za naslednjih par ur. {now} namesto {was}. pohiti pohiti preden si premislim 🤭",
        "{now}?? to ni nič za to kar dobiš 🙈 bilo {was}, spuščam samo za danes. odkleni in se mi zahvali kasneje 😘",
        "flash deal samo zate 🔥 {off} popusta, samo {now} (bilo {was}). konča se nocoj, ne pusti ga tam 👀",
        "razvajam te danes srček 💋 {off} popusta na moj najnovejši set. bilo {was}, tvoj za {now} zdaj. ne pusti da obžalujem da sem tako radodarna 😏",
    ],
    "exclusive_list": [
        "⚠️ tole ni množičen dm srček na mojem posebnem seznamčku si 🙈\n\nzato dobiš moj čisto nov bundle preden ga spustim vsem. tukaj je kaj je notri (in vse obdržiš za vedno):\n\n✨ cel set\n✨ vsi moji najljubši koti\n✨ pogledi ki jih vedno prosiš\n✨ in več 😻\n\n{off} popusta da je samo {now} (bilo {was}). ne povej drugim 🤫",
        "psst... tega ne pošiljam vsem ok 🙈 samo mojim pravim\n\nnaredila sem ekskluziven drop in si na seznamu zanj. kaj vključuje:\n\n✨ moje najnovejše slike\n✨ video ki ga nisem nikoli objavila\n✨ close upi ki jih obožuješ\n\nsamo {now} zate srček (bilo {was}, to je {off} manj). naj ostane med nama 🤍",
        "hej ti 🙈 dobesedno eden od peščice si ki to dobi\n\nčisto nov bundle, preveč da bi ga pokazala vsem svojim fanom tukaj haha. zato je samo za posebni seznam. povzetek:\n\n✨ cel set\n✨ zakulisje\n✨ moji absolutno najljubši koti\n\ntvoj za {now} (bilo {was}) 😻 ne deli ok",
    ],
    "intimate_reveal": [
        "trajalo je nekaj časa da sem se ti tako odprla ampak končno sem pripravljena da ti pokažem to plat sebe 🙈 prosim ne obsojaj me ok... veliko je zame. odkleni in bodi prijazen",
        "tole je ena najbolj osebnih stvari kar sem jih posnela 😳 res malo živčna da pošiljam ngl. ampak zaupam ti. poglej, samo ne deli 🤍",
        "verjetno imamo vsi svoje skrivne kinke ne 🙈 no tale je moj. nikoli ne pokažem te plati sebe ampak... izvoli. ne razočaraj me ok",
        "nikoli nisem bila tako odprta tukaj. odkleniti tole se iskreno zdi kot velik korak zame. bodi nežen z njim srček, samo zate je 🥺",
        "ok skoraj nisem poslala tega... res je intimno. ampak nekaj pri tebi me naredi da ti hočem pokazat vse. tvoj je 😳",
    ],
}
_FALLBACK_CAPTION_SL = "nekaj sem naredila zate 🙈 odkleni"

# Language registries — 'en' authoritative; _pick_caption falls back per-key.
PPV_CAPTION_POOLS_BY_LANG: dict[str, dict[str, list[str]]] = {
    "en": PPV_CAPTION_POOLS, "es": PPV_CAPTION_POOLS_ES, "sl": PPV_CAPTION_POOLS_SL}
_FALLBACK_CAPTION_BY_LANG: dict[str, str] = {
    "en": _FALLBACK_CAPTION, "es": _FALLBACK_CAPTION_ES, "sl": _FALLBACK_CAPTION_SL}

# ── Message-pool → feed-pool mapping ─────────────────────────────────────────
# When a PPV has no explicit feed_captions/feed_caption_pool_key, the feed post
# must still use PUBLIC-feed voice — never the 1:1 message caption. This maps each
# message caption_pool_key to the closest-matching feed pool key.
_MSG_TO_FEED_POOL: dict[str, str] = {
    "photoset_striptease": "feed_photoset",
    "video_ppv": "feed_video_drop",
    "bundle_anchor": "feed_bundle_drop",
    "bundle_long": "feed_bundle_drop",
    "flash_discount": "feed_flash_sale",
    "exclusive_list": "feed_teaser",
    "intimate_reveal": "feed_teaser",
    "followup_nonunlocker": "feed_new_drop",
}

# ── Feed-post caption pools ──────────────────────────────────────────────────
# Public-FEED voice (a post is broadcast to everyone, NOT a 1:1 DM) — so NO "just
# for u" / "ur on my special list" framing. Used ONLY by the "post to feed" path
# (ppv_library_config_api.post_ppv_to_feed); one line is random-picked + the
# {now}/{was}/{off} discount tokens filled, same as the message pools.
PPV_FEED_CAPTION_POOLS: dict[str, list[str]] = {
    "feed_new_drop": [
        "just posted somethin new 🙈 unlock it below babe",
        "new set is up 🔥 go unlock it, u wont regret it",
        "couldnt wait to share this one... its locked below, go see 👀",
        "fresh content just dropped 😏 tap to unlock",
        "posted somethin a lil spicy today 🙈 its all urs below",
    ],
    "feed_flash_sale": [
        "🔥 {off} OFF today only 🔥 was {was} now just {now}, unlock before i put it back up",
        "flash sale time 😈 {off} off, only {now} (was {was}). dont sleep on it",
        "{now} instead of {was} for the next few hours only 🙈 go go go",
        "spoilin u today 💋 {off} off, was {was} now {now}. unlock below",
        "puttin this on sale for a bit — {off} off, just {now} (was {was}) 👀",
    ],
    "feed_bundle_drop": [
        "huge bundle just dropped 🔥 everything in one unlock, way more than its worth",
        "dropped my biggest set yet 🙈 tons of content, one price below. go unlock",
        "new bundle is live 😈 all the pics + vids in one, dont miss it",
        "{off} off my full bundle today 🌟 was {was} now {now}, unlock everything below",
    ],
    "feed_teaser": [
        "this is just the preview... the full thing is locked below 👀",
        "lil taste of what i posted 🙈 unlock for the rest",
        "u want the full version? its right below babe, go unlock 😏",
        "consider this the soft version... the real one is locked below",
    ],
    "feed_video_drop": [
        "new video is up 🔥 unlock it below n press play",
        "posted a lil clip today 🙈 its locked below, go watch",
        "filmed somethin special... its all urs below, unlock to see 😈",
        "new vid just dropped 👀 worth every second i promise, unlock below",
    ],
    "feed_photoset": [
        "new photo set is up 🙈 starts cute... does NOT stay that way. unlock below",
        "posted a whole set today 🔥 it gets better with every pic, go unlock",
        "dropped a striptease set 😏 watch me lose the outfit one pic at a time, below",
        "new pics are live 👀 the last few r my fav, unlock to see",
    ],
}

# Spanish feed pools (es) — public-feed voice. Same keys; English per-key fallback.
PPV_FEED_CAPTION_POOLS_ES: dict[str, list[str]] = {
    "feed_new_drop": [
        "acabo de publicar algo nuevo 🙈 desbloquéalo abajo bebé",
        "nuevo set arriba 🔥 ve a desbloquearlo, no te vas a arrepentir",
        "no me pude aguantar a compartir este... está bloqueado abajo, ve a ver 👀",
        "contenido fresco recién soltado 😏 toca para desbloquear",
        "publiqué algo un poquito picante hoy 🙈 es todo tuyo abajo",
    ],
    "feed_flash_sale": [
        "🔥 {off} DE DESCUENTO solo hoy 🔥 era {was} ahora solo {now}, desbloquéalo antes de que lo vuelva a subir",
        "hora de sale 😈 {off} menos, solo {now} (era {was}). no te duermas",
        "{now} en vez de {was} solo por las próximas horas 🙈 corre corre corre",
        "consintiéndote hoy 💋 {off} menos, era {was} ahora {now}. desbloquea abajo",
        "poniendo esto en oferta un rato — {off} menos, solo {now} (era {was}) 👀",
    ],
    "feed_bundle_drop": [
        "bundle enorme recién soltado 🔥 todo en un solo desbloqueo, mucho más de lo que vale",
        "solté mi set más grande hasta ahora 🙈 montón de contenido, un solo precio abajo. ve a desbloquear",
        "nuevo bundle en vivo 😈 todas las fotos + videos en uno, no te lo pierdas",
        "{off} de descuento en mi bundle completo hoy 🌟 era {was} ahora {now}, desbloquea todo abajo",
    ],
    "feed_teaser": [
        "esto es solo el adelanto... lo completo está bloqueado abajo 👀",
        "una probadita de lo que publiqué 🙈 desbloquea por el resto",
        "quieres la versión completa? está justo abajo bebé, ve a desbloquear 😏",
        "considera esta la versión suave... la de verdad está bloqueada abajo",
    ],
    "feed_video_drop": [
        "nuevo video arriba 🔥 desbloquéalo abajo y dale play",
        "publiqué un clip hoy 🙈 está bloqueado abajo, ve a verlo",
        "grabé algo especial... es todo tuyo abajo, desbloquea para ver 😈",
        "nuevo video recién soltado 👀 vale cada segundo prometido, desbloquea abajo",
    ],
    "feed_photoset": [
        "nuevo set de fotos arriba 🙈 empieza tierno... NO se queda así. desbloquea abajo",
        "publiqué un set completo hoy 🔥 se pone mejor con cada foto, ve a desbloquear",
        "solté un set de striptease 😏 mírame perder el outfit foto por foto, abajo",
        "nuevas fotos en vivo 👀 las últimas son mis favoritas, desbloquea para ver",
    ],
}

# Slovenian feed pools (sl) — public-feed voice; female speaker (HER verbs feminine).
PPV_FEED_CAPTION_POOLS_SL: dict[str, list[str]] = {
    "feed_new_drop": [
        "pravkar sem objavila nekaj novega 🙈 odkleni spodaj srček",
        "nov set je gor 🔥 pojdi odkleni, ne boš obžaloval",
        "nisem se mogla zadržat da delim tega... zaklenjen je spodaj, pojdi poglej 👀",
        "sveža vsebina pravkar spuščena 😏 tapni za odklep",
        "danes sem objavila nekaj malo pikantnega 🙈 vse tvoje spodaj",
    ],
    "feed_flash_sale": [
        "🔥 {off} POPUSTA samo danes 🔥 bilo {was} zdaj samo {now}, odkleni preden dam nazaj gor",
        "čas za razprodajo 😈 {off} manj, samo {now} (bilo {was}). ne zaspi",
        "{now} namesto {was} samo za naslednjih par ur 🙈 pohiti pohiti pohiti",
        "razvajam te danes 💋 {off} manj, bilo {was} zdaj {now}. odkleni spodaj",
        "dajem tole na razprodajo za malo — {off} manj, samo {now} (bilo {was}) 👀",
    ],
    "feed_bundle_drop": [
        "ogromen bundle pravkar spuščen 🔥 vse v enem odklepu, veliko več kot je vredno",
        "spustila sem svoj največji set do zdaj 🙈 kup vsebine, ena cena spodaj. pojdi odkleni",
        "nov bundle v živo 😈 vse slike + videi v enem, ne zamudi",
        "{off} popusta na moj cel bundle danes 🌟 bilo {was} zdaj {now}, odkleni vse spodaj",
    ],
    "feed_teaser": [
        "tole je samo predogled... celota je zaklenjena spodaj 👀",
        "mala pokušina tega kar sem objavila 🙈 odkleni za ostalo",
        "hočeš celo verzijo? je prav spodaj srček, pojdi odkleni 😏",
        "vzemi tole kot nežno verzijo... prava je zaklenjena spodaj",
    ],
    "feed_video_drop": [
        "nov video je gor 🔥 odkleni spodaj in pritisni play",
        "danes sem objavila posnetek 🙈 zaklenjen je spodaj, pojdi poglej",
        "posnela sem nekaj posebnega... vse tvoje spodaj, odkleni da vidiš 😈",
        "nov video pravkar spuščen 👀 vreden vsake sekunde obljubim, odkleni spodaj",
    ],
    "feed_photoset": [
        "nov foto set je gor 🙈 začne se srčkano... NE ostane tako. odkleni spodaj",
        "danes sem objavila cel set 🔥 boljše je z vsako sliko, pojdi odkleni",
        "spustila sem striptease set 😏 glej me kako se slačim sliko za sliko, spodaj",
        "nove slike v živo 👀 zadnje so moje najljubše, odkleni da vidiš",
    ],
}
PPV_FEED_CAPTION_POOLS_BY_LANG: dict[str, dict[str, list[str]]] = {
    "en": PPV_FEED_CAPTION_POOLS, "es": PPV_FEED_CAPTION_POOLS_ES,
    "sl": PPV_FEED_CAPTION_POOLS_SL}


def round_to_99(amount_cents: float, bounds: tuple[int, int] | None = None) -> int:
    """Round to the nearest whole dollar, then end in .99, clamped into the
    operator's [min, max] price bounds (default $3/$200). A clamped price sits
    EXACTLY on the bound (no .99 restyle). e.g. 5750 → 5799 ($57.99)."""
    lo, hi = bounds if bounds is not None else (_PRICE_FLOOR_CENTS, _PRICE_CEIL_CENTS)
    dollars = round(amount_cents / 100)
    cents = 99 if dollars < 1 else dollars * 100 - 1
    return max(lo, min(cents, hi))


def _spend_band(cents: int) -> tuple[str, float]:
    c = max(0, int(cents or 0))
    for name, lo, hi, mult in SPEND_BANDS:
        if lo <= c < hi:
            return name, mult
    return "free", 0.5


def _recency_band(last_dt: datetime | None, now: datetime) -> tuple[str, float]:
    if last_dt is None:
        return "quiet", 0.55
    days = max(0.0, (now - last_dt).total_seconds() / 86_400)
    for name, hi_days, mult in RECENCY_BANDS:
        if days < hi_days:
            return name, mult
    return "quiet", 0.55


def _money(cents: int) -> str:
    return f"${cents // 100}" if cents % 100 == 0 else f"${cents / 100:.2f}"


def _anchor_price(now_cents: int) -> int:
    """A believable 'was' price for the discount framing: ~4x what the fan actually
    pays, rounded to end in .99. Display only (NOT a real OF price) so no $200 ceiling
    — this keeps EVERY segment's caption reading like a genuine ~75% off deal, even
    the whale cell where the real price is high. {now} < {was} always holds."""
    dollars = max(1, round(now_cents * 4 / 100))
    return dollars * 100 - 1


def _pct_off(now_cents: int, was_cents: int) -> int:
    if was_cents <= 0 or now_cents >= was_cents:
        return 0
    return round((1 - now_cents / was_cents) * 100)


def _pick_caption(ppv: dict, cell_price_cents: int, lang: str = "en") -> str:
    """Random caption (custom caption_texts win over the pool), discount tokens filled:
    {now} = this segment's price, {was} = an auto anchor ~4x above (always a discount),
    {off} = the resulting percent off (e.g. '75%'). `lang` selects the language pool
    (English per-key fallback); a custom caption_texts always wins, in any language."""
    from automations import _language
    texts = ppv.get("caption_texts")
    if not (isinstance(texts, list) and texts):
        key = str(ppv.get("caption_pool_key") or "")
        texts = _language.localized(PPV_CAPTION_POOLS_BY_LANG, lang, key) or []
    norm = _language.norm_lang(lang) or "en"
    line = random.choice(texts) if texts else _FALLBACK_CAPTION_BY_LANG.get(norm, _FALLBACK_CAPTION)
    if "{now}" in line or "{was}" in line or "{off}" in line:
        was = _anchor_price(cell_price_cents)
        line = (line.replace("{now}", _money(cell_price_cents))
                    .replace("{was}", _money(was))
                    .replace("{off}", f"{_pct_off(cell_price_cents, was)}%"))
    return line


def _rotate_preview(pool: list, idx: int) -> list[int]:
    """Deterministic round-robin so each RESEND (a different day) shows a DIFFERENT
    teaser with NO stored state — pool[idx % len]. A fan who didn't buy keeps getting
    the same locked content with a fresh preview each cycle. Empty pool → no preview."""
    return [pool[idx % len(pool)]] if pool else []


def pick_feed_caption(ppv: dict, base_cents: int, lang: str = "en") -> tuple[str, bool]:
    """Caption for the FEED post (public voice, NEVER the 1:1 message caption).
    Priority: the PPV's own feed_captions → its feed_caption_pool_key
    (PPV_FEED_CAPTION_POOLS) → the feed pool mapped from the message caption_pool_key
    via _MSG_TO_FEED_POOL (default 'feed_new_drop'). `lang` selects the language feed
    pool (English per-key fallback). Returns (caption, used_feed_specific) —
    used_feed_specific is always True; this never falls through to the message pool.
    Tokens are filled at the base price (a feed post is one price for everyone)."""
    from automations import _language
    feed_caps = [s for s in (ppv.get("feed_captions") or []) if isinstance(s, str) and s.strip()]
    if feed_caps:
        return _pick_caption({"caption_texts": feed_caps}, base_cents), True
    key = str(ppv.get("feed_caption_pool_key") or "").strip()
    if not (key and key in PPV_FEED_CAPTION_POOLS):
        key = _MSG_TO_FEED_POOL.get(str(ppv.get("caption_pool_key") or "").strip(), "feed_new_drop")
    pool = _language.localized(PPV_FEED_CAPTION_POOLS_BY_LANG, lang, key) or PPV_FEED_CAPTION_POOLS[key]
    return _pick_caption({"caption_texts": pool}, base_cents), True


async def post_to_feed(account_id: str, ppv: dict, *, employee_id: int | None = None,
                       caption: str | None = None,
                       media_files: list[int] | None = None,
                       previews_override: list[int] | None = None,
                       bounds: tuple[int, int] | None = None) -> dict:
    """Post ONE PPV to the OF FEED as a paid post at its BASE price, with the ⭐ free
    previews (preview_options) shown as the teaser and a feed-voice caption. Records a
    Post row. SHARED by the manual post-now endpoint and the auto 'also post to feed
    with each send' path in run(). No fan messaging — a feed post is one public drop.

    `caption`, when a non-empty string, is used VERBATIM (skips pick_feed_caption)
    with used_feed_caption forced True — this gives WYSIWYG: a caption shown in a
    preview step is exactly what gets posted on confirm.

    `media_files`, when a NON-EMPTY list, is the operator's chosen media in the EXACT
    order to post — it OVERRIDES the PPV's own media_ids, but is FILTERED to the PPV's
    own media_ids (never post ids the PPV doesn't own; OF rejects strays). An empty /
    absent list falls back to the PPV's media_ids (never post an empty media set).
    `previews_override`, when provided, replaces the preview_options-based previews;
    either way previews are filtered to a ⊆ subset of the media actually sent.
    `bounds` = the operator's (min_cents, max_cents) price limits (price_bounds(cfg));
    None → the hard OF defaults. The posted price is clamped into them."""
    import automation_executor as ax
    lo, hi = bounds if bounds is not None else (_PRICE_FLOOR_CENTS, _PRICE_CEIL_CENTS)
    base_cents = max(lo, min(int(ppv.get("base_price_cents") or 0), hi))
    ppv_media = [int(x) for x in (ppv.get("media_ids") or []) if str(x).strip()]
    if not ppv_media:
        return {"status": "skipped", "reason": "no_media"}
    ppv_set = set(ppv_media)
    # Operator override: chosen media in EXACT order, filtered to the PPV's own ids
    # (never post a stray). Empty/absent → the PPV's own media (never an empty set).
    if media_files:
        media_ids = [int(x) for x in media_files if str(x).strip() and int(x) in ppv_set]
    else:
        media_ids = list(ppv_media)
    if not media_ids:
        return {"status": "skipped", "reason": "no_media"}
    media_set = set(media_ids)
    # Previews must be ⊆ media_files (OF rejects/ignores stray ids), mirroring the message path.
    prev_src = previews_override if previews_override is not None else (ppv.get("preview_options") or [])
    previews = [int(x) for x in prev_src
                if str(x).strip() and int(x) in media_set]
    if isinstance(caption, str) and caption.strip():
        used_feed_caption = True
    else:
        from automations import _language
        _feed_lang = await _language.load_account_language(account_id)
        caption, used_feed_caption = pick_feed_caption(ppv, base_cents, _feed_lang)
    price = base_cents / 100   # OF wants dollars

    client = await asyncio.to_thread(ax._make_client, account_id)
    result = await ax.of_write_paced(
        account_id,
        lambda: client.create_post(text=caption, media_files=media_ids,
                                   price=price, previews=previews),
    )
    of_post_id = result.get("id") if isinstance(result, dict) else None
    if of_post_id is None:
        log.warning("post_to_feed account=%s ppv=%s create_post returned no id (%r)",
                    account_id, ppv.get("id"), result)
        return {"status": "error", "reason": "no_post_id", "caption": caption}
    of_post_id = int(of_post_id)

    now = datetime.utcnow()
    async with get_session() as s:
        s.add(Post(
            account_id=str(account_id), of_post_id=of_post_id, status="posted",
            text=caption, price_cents=base_cents, media_ids=json.dumps(media_ids),
            posted_at=now, created_by_employee_id=employee_id,
            raw_json=json.dumps(result, default=str)[:20000],
        ))
    log.info("post_to_feed account=%s ppv=%s of_post=%s price=%s previews=%d",
             account_id, ppv.get("id"), of_post_id, price, len(previews))
    return {
        "status": "ok", "of_post_id": of_post_id, "price": price, "caption": caption,
        "used_feed_caption": used_feed_caption, "media_count": len(media_ids),
        "preview_count": len(previews),
    }


def _segments(fan_rows: list, last_purchase: dict, now: datetime) -> dict[str, dict]:
    """Bucket fans into one spend×recency cell each. Spend = lifetime_spend_cents;
    recency = days since last PURCHASE (Transaction.occurred_at), NOT last message —
    a non-messaging buyer must still bucket by when they bought, and a never-buyer
    falls to 'quiet' (the cheapest intro price). Returns
    {cell_key: {spend, recency, spend_mult, rec_mult, fan_ids:[...]}}."""
    cells: dict[str, dict] = {}
    for fan_id, spend_cents in fan_rows:
        sname, smult = _spend_band(spend_cents)
        rname, rmult = _recency_band(last_purchase.get(int(fan_id)), now)
        key = f"{sname}:{rname}"
        cell = cells.get(key)
        if cell is None:
            cell = cells[key] = {
                "spend": sname, "recency": rname,
                "spend_mult": smult, "rec_mult": rmult, "fan_ids": [],
            }
        cell["fan_ids"].append(int(fan_id))
    return cells


# How long after the last PAID rung a fan still counts as "mid-sell". The ladder's
# own hot window is 45min (upsell.HOT_WINDOW_M) and it re-arms on every rung, so a
# 120min tail leaves room for the whole hot session to play out plus a slow reply —
# a mass blast landing 50min after he unlocked would still be talking over her.
_LADDER_HOT_TAIL_MIN = 120


async def _hot_ladder_fans(account_id: str, now: datetime) -> set[int]:
    """Fans the Offer Engine is CURRENTLY selling to 1:1 — status open/hot, or still
    inside the post-purchase hot tail. A mass PPV must not land on top of a live
    ladder: she is mid-negotiation at a fan-specific price, and a $12.99 blast of a
    different clip is the cheapest possible way to break that thread's momentum
    (worse: it re-anchors him low while she's asking for the escalated rung).

    The ladder is a NEW table. On an un-migrated prod DB (init_db's create_all vs
    alembic — the two diverge, see the migration notes) a missing `ladder_state`
    must degrade to "no ladder ⇒ no skips", never to a raised query that kills every
    PPV send on the account. Empty set = today's behavior, exactly."""
    cutoff = now - timedelta(minutes=_LADDER_HOT_TAIL_MIN)
    try:
        async with get_session() as s:
            rows = (await s.execute(
                select(LadderState.fan_id).where(
                    LadderState.account_id == account_id,
                    # BOTH halves are time-bounded, and that is load-bearing. An
                    # unbounded `status IN ('open','hot')` looks harmless but is a
                    # permanent revenue leak: a fan pays a rung → status='hot' → we
                    # deliver the unlocked media → the thread's last message is OURS →
                    # he never becomes an ai_chatter candidate again (the loop requires
                    # `last_dir == 'in'`) → nothing ever closes his ladder. He sits at
                    # 'hot' forever and is silently dropped from EVERY future mass PPV.
                    # He is a PROVEN PAYER — the exact fan the blast exists to reach.
                    # A ladder nobody has touched in _LADDER_HOT_TAIL_MIN is not live.
                    (LadderState.status.in_(("open", "hot"))
                     & LadderState.updated_at.is_not(None)
                     & (LadderState.updated_at > cutoff))
                    | (LadderState.hot_until.is_not(None) & (LadderState.hot_until > cutoff)),
                )
            )).scalars().all()
    except Exception as e:  # noqa: BLE001 — missing table / un-migrated DB
        log.warning("ppv_send ladder_state unreadable account=%s (%r) — "
                    "treating as no live ladders", account_id, e)
        return set()
    return {int(x) for x in rows if x is not None}


async def _offers_paused_fans(account_id: str, now: datetime) -> set[int]:
    """Fans whose 1:1 ladder is inside an offers-pause — a VOICED decline (soft
    'i'm broke' = 24h; hard chargeback/report/unsubscribe words = 72h, which since
    07-23 replaced the permanent skip_list('ladder_stop') row). "Stop selling to
    him for a while" must bind the blast too, or the pause is theatre: the
    hard-declining fan would simply get his next price from the mass lane instead.
    Same missing-table degradation as _hot_ladder_fans."""
    try:
        async with get_session() as s:
            rows = (await s.execute(
                select(LadderState.fan_id).where(
                    LadderState.account_id == account_id,
                    LadderState.offers_paused_until.is_not(None),
                    LadderState.offers_paused_until > now,
                )
            )).scalars().all()
    except Exception as e:  # noqa: BLE001 — missing table / un-migrated DB
        log.warning("ppv_send ladder_state unreadable account=%s (%r) — "
                    "treating as no offer pauses", account_id, e)
        return set()
    return {int(x) for x in rows if x is not None}


async def _eligible_fans(account_id: str):
    """All non-bot, non-blacklisted fans for the account + their last-PURCHASE time,
    MINUS anyone with a live 1:1 ladder. We filter bots/blacklisted HERE because
    ppv_send passes explicit included_users, and the send-side contact guard does NOT
    bot/blacklist-filter an explicit set. Recency is last purchase
    (Transaction.occurred_at), not last message.

    Returns (fan_rows, last_purchase, skipped_hot_ladder, skipped_offers_paused).
    The two counters are COUNTED and surfaced in the run stats on purpose: these
    filters silently remove paying fans from a blast, and nobody should have to
    attribute a revenue dip to an invisible line in here. cells_sent dropping while
    skipped_hot_ladder / skipped_offers_paused climb is the ladder doing its job;
    everything dropping together is a bug. skipped_offers_paused is TIME-VARYING
    (every soft decline parks a fan 24h, a hard decline 72h) — when declines spike,
    this counter is the explanation for the audience dip."""
    blacklisted = select(Blacklist.fan_id)
    now = datetime.utcnow()
    hot_ladder = await _hot_ladder_fans(account_id, now)
    # Hard skips (muted_creator / manual_restrict / of_restricted / ladder_stop).
    # ppv_send read NONE of these before — it filtered only bots + blacklist — so a
    # fan who told us "im disputing this charge, im reporting you" kept receiving
    # priced blasts, and as a past payer he landed in the HIGHEST spend-band cell.
    # A chargeback can take the whole OF account down; this is the cheapest possible
    # place to stop it. Since 07-23 the hard decline writes a 72h ladder
    # offers-pause instead of a skip_list row — filtered (and COUNTED) separately
    # below: same fan, same danger, different (now temporary) bookkeeping.
    hard_skip = await load_hard_skip_ids(account_id)
    offers_paused = await _offers_paused_fans(account_id, now)
    async with get_session() as s:
        fan_rows = (await s.execute(
            select(Fan.fan_id, Fan.lifetime_spend_cents).where(
                Fan.account_id == account_id,
                Fan.is_bot.is_(False),
                Fan.fan_id.not_in(blacklisted),
            )
        )).all()
        purchases = (await s.execute(
            select(Transaction.fan_id, func.max(Transaction.occurred_at)).where(
                Transaction.account_id == account_id,
                Transaction.amount_cents > 0,
                Transaction.fan_id.is_not(None),
            ).group_by(Transaction.fan_id)
        )).all()
    if hard_skip:
        fan_rows = [r for r in fan_rows if int(r[0]) not in hard_skip]
    skipped_offers_paused = 0
    if offers_paused:
        kept = [r for r in fan_rows if int(r[0]) not in offers_paused]
        skipped_offers_paused = len(fan_rows) - len(kept)
        fan_rows = kept
    skipped_hot_ladder = 0
    if hot_ladder:
        kept = [r for r in fan_rows if int(r[0]) not in hot_ladder]
        skipped_hot_ladder = len(fan_rows) - len(kept)
        fan_rows = kept
    last_purchase = {int(fid): dt for fid, dt in purchases if fid is not None}
    return fan_rows, last_purchase, skipped_hot_ladder, skipped_offers_paused


async def _all_fan_ids(account_id: str) -> list[int]:
    """EVERY fan id we know for the account (NO bot/blacklist/buyer filter). Used as
    the broadcast exclude: a 'message all subscribers' send skips everyone we've
    already handled per-fan above (tier-sent, or deliberately dropped as
    bot/blacklist/buyer), so ONLY the uncached followers get the default-price
    broadcast — no fan is messaged twice, the blacklist is honoured."""
    async with get_session() as s:
        rows = (await s.execute(
            select(Fan.fan_id).where(Fan.account_id == account_id)
        )).scalars().all()
    return [int(x) for x in rows]


# Media parsing + both ownership readers live in ownership.py (the one home
# for owned-media semantics — this used to be a "local mirror to avoid a
# circular import" situation; ownership.py is a dependency root, so the wart
# is gone). The `_owners_of_media` alias keeps this module's name for its
# call site and tests.
_owners_of_media = ownership.owners_of_media


async def _hero_media_ids(account_id: str, media_ids: list[int],
                          preview_ids: list[int]) -> list[int]:
    """Plain-id-list adapter over `ownership.hero_media_map` for the
    PPV-library blob shape — see it for the operator's hero/filler ruling."""
    return (await ownership.hero_media_map(
        account_id,
        {0: ([int(x) for x in media_ids],
             [int(x) for x in (preview_ids or [])])}))[0]


# ── Per-account cap: max PPV sends per rolling day / week / month ────────────
_CAP_WINDOWS = (("per_day", 86_400), ("per_week", 7 * 86_400), ("per_month", 30 * 86_400))

# Duplicate-fire gate: minimum minutes between two ACTUAL sends of the SAME ppv.
# Far below any real cadence (the rule sync's fastest is 604800/sends_per_week),
# so it only ever catches pathological double-fires: the executor's job retry,
# a double-enqueue, a manual fire landing on top of the rule's.
_DUP_GAP_MIN_DEFAULT = 60.0


async def _last_same_ppv_send(account_id: str, ppv_id: str, since: datetime) -> datetime | None:
    """completed_at of the newest ppv_send run of THIS ppv that ACTUALLY sent
    (cells_sent>0) since `since` — the duplicate-fire gate's ledger. Reads the
    per-run stats blob, so a PARTIAL send counts too (a run that broadcast 2 of
    3 cells before erroring is exactly the case the gate exists for)."""
    async with get_session() as s:
        return (await s.execute(
            select(func.max(AutomationRun.completed_at)).where(
                AutomationRun.account_id == account_id,
                AutomationRun.kind == "ppv_send",
                AutomationRun.completed_at.is_not(None),
                AutomationRun.completed_at >= since,
                func.json_extract(AutomationRun.stats_json, "$.ppv_id") == str(ppv_id),
                func.json_extract(AutomationRun.stats_json, "$.cells_sent") > 0,
            )
        )).scalar_one_or_none()


async def _recent_ppv_send_times(account_id: str, now: datetime) -> list[datetime]:
    """completed_at of this account's ppv_send fires that ACTUALLY sent (cells_sent>0)
    in the last 30 days — the cap counter. AutomationRun.status is always 'ok' so we
    key on the per-fire stats_json: a capped/skipped fire has no cells_sent."""
    since = now - timedelta(days=30)
    async with get_session() as s:
        rows = (await s.execute(
            select(AutomationRun.completed_at).where(
                AutomationRun.account_id == account_id,
                AutomationRun.kind == "ppv_send",
                AutomationRun.completed_at.is_not(None),
                AutomationRun.completed_at >= since,
                func.json_extract(AutomationRun.stats_json, "$.cells_sent") > 0,
            )
        )).scalars().all()
    return sorted(r for r in rows if r is not None)


def _cap_release(caps: dict, send_times: list, now: datetime):
    """Even-spread throttle. With cap N per window W, enforce a MINIMUM gap of W/N
    between PPV sends — so 2/day becomes one every 12h, 3/day every 8h, etc. — measured
    from the LAST send (the spacing restarts from the moment the previous one went /
    the cap lifted). The widest required gap across the day/week/month caps wins.
    Returns (capped, release_at, which_cap): release_at = the earliest the next send is
    allowed (last_send + that gap). Clear to send now → (False, None, None)."""
    if not send_times:
        return (False, None, None)
    last = max(send_times)
    release = None
    which = None
    for key, window_s in _CAP_WINDOWS:
        try:
            limit = int(caps.get(key) or 0)
        except (TypeError, ValueError):
            limit = 0
        if limit <= 0:
            continue
        earliest = last + timedelta(seconds=window_s / limit)   # one slot every W/N
        if now < earliest and (release is None or earliest > release):
            release, which = earliest, key
    return (release is not None, release, which)


async def _defer_capped(account_id: str, ppv_id: str, release_at: datetime, now: datetime,
                        *, is_resend: bool = False):
    """Re-enqueue a one-shot ppv_send for this ppv at the cap-free time, UNLESS one is
    already pending (dedup → no pile-up across the rule's repeated fires; keyed on
    ppv_id ONLY — never on is_resend). is_resend must survive the defer, or a capped
    monthly resend comes back as a non-resend and enqueues a SECOND monthly one-shot."""
    import automation_executor as ax
    async with get_session() as s:
        existing = (await s.execute(
            select(ScheduledJob.id).where(
                ScheduledJob.account_id == account_id,
                ScheduledJob.kind == "ppv_send",
                ScheduledJob.status == "pending",
                ScheduledJob.run_at > now,
                func.json_extract(ScheduledJob.payload_json, "$.ppv_id") == ppv_id,
            ).limit(1)
        )).scalar_one_or_none()
    if existing is not None:
        return None
    payload = {"account_id": account_id, "ppv_id": ppv_id}
    if is_resend:
        payload["is_resend"] = True
    return await ax.enqueue_job(account_id, "ppv_send", payload=payload,
                                run_at=release_at)


async def segment_preview(account_id: str, base_price_cents: int,
                          bounds: tuple[int, int] | None = None) -> dict:
    """Per-cell recipient counts + prices for a base price — the UI dry-run. Surfaces
    the 'everyone collapses into the cheap cell' case (thin data) BEFORE any send.
    `bounds` = the operator's (min, max) price limits; None → read them from the
    account's stored library config (so old callers stay correct)."""
    now = datetime.utcnow()
    if bounds is None:
        async with get_session() as s:
            row = await s.get(AccountAiConfig, account_id)
        stored: dict = {}
        if row is not None and row.ppv_library_config_json:
            try:
                stored = json.loads(row.ppv_library_config_json) or {}
            except Exception:
                stored = {}
        bounds = price_bounds(stored)
    fan_rows, last_purchase, skipped_hot_ladder, skipped_offers_paused = \
        await _eligible_fans(account_id)
    base = max(bounds[0], min(int(base_price_cents or 0), bounds[1]))
    cells = _segments(fan_rows, last_purchase, now)
    plan = [
        {
            "cell": key, "spend": c["spend"], "recency": c["recency"],
            "recipients": len(c["fan_ids"]),
            "price": round_to_99(base * c["spend_mult"] * c["rec_mult"], bounds) / 100,
        }
        for key, c in sorted(cells.items())
    ]
    return {"total_fans": len(fan_rows), "cells": plan,
            "skipped_hot_ladder": skipped_hot_ladder,
            "skipped_offers_paused": skipped_offers_paused}


def _load_ppv(cfg_json: str | None, ppv_id: str) -> tuple[dict, dict] | tuple[None, dict]:
    cfg = dict(_DEFAULTS)
    if cfg_json:
        try:
            cfg.update(json.loads(cfg_json) or {})
        except Exception:
            pass
    for p in cfg.get("ppvs") or []:
        if isinstance(p, dict) and str(p.get("id")) == str(ppv_id):
            return p, cfg
    return None, cfg


@register("ppv_send")
async def run(account_id: str, payload: dict, *, run_id: int) -> dict:
    payload = payload or {}
    ppv_id = payload.get("ppv_id")
    if not ppv_id:
        return {"status": "skipped", "reason": "no_ppv_id"}
    is_resend = bool(payload.get("is_resend"))
    dry_run = bool(payload.get("dry_run"))
    force_ids = {int(x) for x in (payload.get("force_ids") or [])}
    # only_fan_ids — the ai_chatter convention: scope the audience with EVERY gate
    # still armed (unlike force_ids, which is a human live-test escape hatch and
    # bypasses the cap + dup-fire gate). The Human Rhythm scheduler resumes a
    # deferred send by re-enqueueing THIS job scoped to one fan; if that resume
    # bypassed the gates it would happily re-sell media he bought in the meantime,
    # or re-blast a cell the original run already sent.
    only_fan_ids = {int(x) for x in (payload.get("only_fan_ids") or [])}

    now = datetime.utcnow()

    async with get_session() as s:
        cfg_row = await s.get(AccountAiConfig, account_id)
    ppv, cfg = _load_ppv(cfg_row.ppv_library_config_json if cfg_row else None, ppv_id)
    if ppv is None:
        return {"status": "skipped", "reason": "ppv_not_found"}
    # Account language for the DEFAULT caption pools (a PPV's own caption_texts still
    # win, in whatever language they were authored). cfg_row is already loaded above.
    from automations import _language
    account_lang = _language.norm_lang(getattr(cfg_row, "language", None)) or "en"
    # S8: a vault-AI-sourced draft (id `vai-…`) is a sendable offer ONLY once
    # explicitly armed — the adapter is the single arm gate (library master ON +
    # this entry `enabled`). An approved+exported-but-un-armed draft (enabled=False)
    # is invisible here; approval alone never sends (correction #2). Non-vault-AI
    # PPVs are untouched and fall through to the generic gate below.
    if (vault_ai_to_chatter.is_vault_ai_offer(ppv)
            and vault_ai_to_chatter.pickable_offer(cfg, ppv_id) is None):
        return {"status": "skipped", "reason": "not_armed"}
    if not cfg.get("enabled") or not ppv.get("enabled", True):
        return {"status": "skipped", "reason": "disabled"}

    # "Send to everyone": after the per-tier sends to KNOWN fans, fire ONE
    # default-price broadcast to ALL subscribers (OF-resolved, reaches uncached
    # followers too), excluding every known fan so nobody is double-sent. Off
    # until enabled per account (a deploy alone never mass-blasts a sub list).
    reach_all = bool(cfg.get("reach_all", False))
    # Pause = don't re-touch a fan messaged in the last N hours (the contact
    # guard). Default 0 = NO pause (send to everyone). Applies to both phases.
    try:
        pause_hours = max(0, int(cfg.get("pause_hours") or 0))
    except (TypeError, ValueError):
        pause_hours = 0

    # Cheap fail-fast checks before the fan scan.
    base_cents = int(ppv.get("base_price_cents") or 0)
    if base_cents <= 0:
        return {"status": "skipped", "reason": "no_base_price"}
    # Operator price limits — every COMPUTED price (per-cell, broadcast, feed)
    # is clamped into these; the authored base_price_cents is never rewritten.
    bounds = price_bounds(cfg)
    bcast_cents = max(bounds[0], min(base_cents, bounds[1]))
    media_ids = [int(x) for x in (ppv.get("media_ids") or []) if str(x).strip()]
    if not media_ids:
        return {"status": "skipped", "reason": "no_media"}
    media_set = set(media_ids)
    # OF `previews` are the attached media shown FREE as the teaser, so each preview
    # id MUST be one of media_files — anything else → OF 400 "Wrong preview". Keep
    # only valid ones; an empty pool = a fully-locked PPV (also valid).
    preview_pool = [int(x) for x in (ppv.get("preview_options") or [])
                    if str(x).strip() and int(x) in media_set]
    day_idx = now.toordinal()   # rotates the preview once per day → fresh on each resend

    # ── per-account cap: even-spread the day/week/month limit (one send every
    #    window/N, re-scheduling a too-soon send to its slot).
    # A blob with no ppv_caps key carries the house default (2/14/60 → 12h gap)
    # via the _DEFAULTS merge; an explicit all-zero save is the operator's off
    # switch and makes every any() below False.
    # (force_ids = an explicit live-test scope, bypasses the cap.)
    caps = cfg.get("ppv_caps") or {}
    if not dry_run and not force_ids and any((caps.get(k) or 0) for k in
                                             ("per_day", "per_week", "per_month")):
        capped, release_at, which = _cap_release(
            caps, await _recent_ppv_send_times(account_id, now), now)
        if capped:
            job_id = await _defer_capped(account_id, ppv_id, release_at, now,
                                         is_resend=is_resend)
            log.info("ppv_send capped account=%s ppv=%s cap=%s release=%s defer_job=%s",
                     account_id, ppv_id, which, release_at, job_id)
            return {"status": "skipped", "reason": f"capped_{which}",
                    "retry_at": release_at.isoformat() + "Z", "defer_job_id": job_id}

    # ── duplicate-fire gate: NEVER send the same ppv twice within the gap ─
    # Guards against the executor retrying a partially-failed job, a
    # double-enqueue, or a manual fire landing on the rule's — with the
    # default pause_hours=0 the contact guard is OFF, so this ledger check is
    # the real protection. Skipping is safe: a missed cycle self-heals at the
    # next cadence fire; a double-blast doesn't. (Runs AFTER the cap check so
    # a capped fire keeps its defer-to-slot semantics; a deferred job lands
    # past the gap, so the two never fight.)
    try:
        dup_gap_min = float(cfg.get("min_send_gap_minutes")
                            if cfg.get("min_send_gap_minutes") is not None
                            else _DUP_GAP_MIN_DEFAULT)
    except (TypeError, ValueError):
        dup_gap_min = _DUP_GAP_MIN_DEFAULT
    if not dry_run and not force_ids and dup_gap_min > 0:
        last = await _last_same_ppv_send(
            account_id, str(ppv_id), now - timedelta(minutes=dup_gap_min))
        if last is not None:
            log.warning("ppv_send duplicate-fire gate account=%s ppv=%s "
                        "last_sent=%s gap_min=%s — skipping",
                        account_id, ppv_id, last, dup_gap_min)
            return {"status": "skipped", "reason": "duplicate_recent_send",
                    "ppv_id": ppv_id, "last_sent_at": last.isoformat() + "Z",
                    "gap_minutes": dup_gap_min}

    fan_rows, last_purchase, skipped_hot_ladder, skipped_offers_paused = \
        await _eligible_fans(account_id)
    if force_ids:
        fan_rows = [r for r in fan_rows if int(r[0]) in force_ids]
    if only_fan_ids:
        fan_rows = [r for r in fan_rows if int(r[0]) in only_fan_ids]

    # ── ownership guard — UNCONDITIONAL, and deliberately NOT under exclude_buyers ─
    # Re-selling a fan a clip he ALREADY UNLOCKED is a money bug in both directions:
    # he pays twice for the same media (refund + chargeback risk, and chargebacks are
    # the one thing that kills an OF account), or he sees it's the same file at a
    # different price and stops trusting every future price we quote. There is no
    # config value for which that is the desired outcome — so there is no config flag
    # in front of it. `exclude_buyers` survives as a no-op for old configs.
    #
    # It runs AFTER the scopes, never inside an elif on one of them:
    #   force_ids may scope WHO we send to; it may NEVER license re-selling media a
    #   fan already owns. force_ids bypasses GATES (cap, dup-fire) — it is not a
    #   whitelist, and ownership is not a gate, it is a fact about the fan.
    # Nobody owns the media ⇒ owners is empty ⇒ the audience is untouched.
    #
    # HERO media only (operator ruling 07-23): the previews pool is the free-
    # visible tease slice, so a fan who merely owns a shared preview frame still
    # gets the blast — only owning the payoff (a video, or a non-preview image)
    # skips him. Preview-listed ids the vault mirror knows are VIDEOS stay hero
    # (a video is never mere filler). No previews ⇒ hero == full media set.
    hero_ids = await _hero_media_ids(account_id, media_ids, preview_pool)
    owners = await _owners_of_media(account_id, hero_ids)
    if owners:
        before = len(fan_rows)
        fan_rows = [r for r in fan_rows if int(r[0]) not in owners]
        if before != len(fan_rows):
            log.info("ppv_send owner-skip account=%s ppv=%s skipped=%d (already unlocked)",
                     account_id, ppv_id, before - len(fan_rows))

    # No known fans is only a hard stop when we're NOT also broadcasting to all
    # subscribers (reach_all): the broadcast can still reach the uncached list.
    # A fan-scoped run (force_ids/only_fan_ids) never broadcasts — the whole point of
    # a scope is that the audience is those fans and nobody else.
    broadcasting = reach_all and not force_ids and not only_fan_ids
    if not fan_rows and not broadcasting:
        # Carry the counters even here — the ladder eating the ENTIRE audience is the
        # single most expensive way these filters can misfire, and it lands exactly on
        # this branch. A bare "no_fans" would look identical to an empty account.
        if skipped_hot_ladder or skipped_offers_paused:
            log.info("ppv_send account=%s ppv=%s no_fans — %d skipped mid-ladder, "
                     "%d offers-paused", account_id, ppv_id, skipped_hot_ladder,
                     skipped_offers_paused)
        return {"status": "skipped", "reason": "no_fans",
                "skipped_hot_ladder": skipped_hot_ladder,
                "skipped_offers_paused": skipped_offers_paused}

    cells = _segments(fan_rows, last_purchase, now)

    # ── dry_run: the per-cell plan, no send, no enqueue ─────────────────
    if dry_run:
        plan = []
        for key, cell in sorted(cells.items()):
            price = round_to_99(base_cents * cell["spend_mult"] * cell["rec_mult"], bounds)
            plan.append({
                "cell": key, "recipients": len(cell["fan_ids"]),
                "price": price / 100,
                "caption": _pick_caption(ppv, price, account_lang)[:80],
                "preview": _rotate_preview(preview_pool, day_idx),
            })
        return {"dry_run": True, "ppv_id": ppv_id, "is_resend": is_resend,
                "cells": len(plan), "fans": len(fan_rows), "plan": plan, "sent": 0,
                "broadcast_all": broadcasting,
                "broadcast_price": (bcast_cents / 100) if broadcasting else None,
                "pause_hours": pause_hours,
                "skipped_hot_ladder": skipped_hot_ladder,
                "skipped_offers_paused": skipped_offers_paused}

    # ── send: one mass call per non-empty cell, matrix price + rotated preview
    from automations.send_mass_message import run as send_mass_run

    sent_cells = 0
    total_recipients = 0
    send_errors = 0
    results: list[dict] = []
    for key, cell in sorted(cells.items()):
        fan_ids = cell["fan_ids"]
        if not fan_ids:
            continue
        price = round_to_99(base_cents * cell["spend_mult"] * cell["rec_mult"], bounds)
        send_payload = {
            # Text is intentionally NOT locked — fans see the teaser caption free,
            # only the media sits behind the price (locked_text defaults off).
            "text": _pick_caption(ppv, price, account_lang),
            "media_files": media_ids,
            "previews": _rotate_preview(preview_pool, day_idx),
            "price": price / 100,                 # OF wants dollars
            "included_users": fan_ids,
            "automation_kind": "ppv_send",        # Mass Messages tab attribution
            # Pause = the contact guard. 0 → guard OFF (send to everyone, the
            # default); >0 → skip fans messaged in the last N hours.
            "exclude_replied_hours": pause_hours,
            "exclude_inbound_hours": pause_hours,
        }
        try:
            res = await send_mass_run(account_id, send_payload, run_id=run_id)
        except Exception as e:  # noqa: BLE001 — one cell must NOT fail the batch
            # A raise here fails the WHOLE job and the executor retries it —
            # re-broadcasting every cell that already went out (the live
            # double-send of 2026-07-01/03). Contain it: record the error,
            # move on; this cell's fans catch the next cadence fire.
            log.warning("ppv_send cell send failed account=%s ppv=%s cell=%s: %r",
                        account_id, ppv_id, key, e)
            send_errors += 1
            results.append({"cell": key, "price": price / 100,
                            "recipients": len(fan_ids), "status": "error",
                            "error": repr(e)[:200]})
            continue
        results.append({"cell": key, "price": price / 100,
                        "recipients": len(fan_ids), "status": res.get("status")})
        if res.get("status") not in ("skipped", "error"):
            sent_cells += 1
            total_recipients += len(fan_ids)

    # ── broadcast: ONE default-price send to ALL subscribers (OF-resolved), with
    #    EVERY known fan excluded (the per-tier sends above + buyers/bots/blacklist)
    #    → only the uncached followers get it, at the base "default" price. This is
    #    how a PPV reaches her whole free-page list without scraping it. ──────────
    broadcast = None
    if broadcasting:
        known_ids = await _all_fan_ids(account_id)
        broadcast_payload = {
            "text": _pick_caption(ppv, bcast_cents, account_lang),  # default-price caption
            "media_files": media_ids,
            "previews": _rotate_preview(preview_pool, day_idx),
            "price": bcast_cents / 100,               # the DEFAULT price, clamped
            "user_lists": ["fans"],                   # ALL active subscribers
            "excluded_users": known_ids,              # → Auto_Exclude (no double-send)
            "automation_kind": "ppv_send",
            "exclude_replied_hours": pause_hours,
            "exclude_inbound_hours": pause_hours,
        }
        try:
            res = await send_mass_run(account_id, broadcast_payload, run_id=run_id)
        except Exception as e:  # noqa: BLE001 — same containment as the cells
            log.warning("ppv_send broadcast send failed account=%s ppv=%s: %r",
                        account_id, ppv_id, e)
            send_errors += 1
            broadcast = "error"
            results.append({"cell": "broadcast:all", "price": bcast_cents / 100,
                            "recipients": 0, "status": "error",
                            "error": repr(e)[:200],
                            "excluded_known": len(known_ids)})
        else:
            broadcast = res.get("status")
            results.append({"cell": "broadcast:all", "price": bcast_cents / 100,
                            "recipients": res.get("recipients", 0), "status": broadcast,
                            "excluded_known": len(known_ids)})
            if broadcast not in ("skipped", "error"):
                sent_cells += 1   # counts as a send for the cap + 'ok' status

    # ── also post to feed: opt-in ("auto post to feed with mass"). Fire ONCE per
    #    run, only when a send actually went out (don't post to the feed on an
    #    all-empty/skipped cycle). Best-effort — a feed-post failure never fails the
    #    send. Same paid post + ⭐ preview + feed-voice caption as the manual button.
    feed_post = None
    if ppv.get("feed_enabled", True) and ppv.get("also_post_to_feed") and sent_cells:
        try:
            feed_post = await post_to_feed(account_id, ppv, bounds=bounds)
        except Exception:
            log.exception("ppv_send also_post_to_feed failed account=%s ppv=%s", account_id, ppv_id)
            feed_post = {"status": "error", "reason": "exception"}

    # ── monthly resend: one-shot +30d (a resend gets a fresh random preview) ─
    resend_job_id = None
    if ppv.get("resend_monthly") and not is_resend:
        import automation_executor as ax
        resend_job_id = await ax.enqueue_job(
            account_id, "ppv_send",
            payload={"account_id": account_id, "ppv_id": ppv_id, "is_resend": True},
            run_at=now + timedelta(days=30),
        )

    # The audience-shrink counters are logged on EVERY run, including 0 — a counter
    # you only see when it's non-zero is a counter nobody has a baseline for.
    log.info("ppv_send account=%s ppv=%s cells=%d recipients=%d errors=%d broadcast=%s "
             "resend_job=%s feed=%s hot_ladder_skipped=%d offers_paused_skipped=%d",
             account_id, ppv_id, sent_cells, total_recipients, send_errors, broadcast,
             resend_job_id, (feed_post or {}).get("status"), skipped_hot_ladder,
             skipped_offers_paused)
    # NOTE: an all-failed run returns status 'error' in the STATS only — it must
    # not raise, or the executor's job retry would re-broadcast any cell that
    # did go out. The failed cells simply wait for the next cadence fire.
    return {
        "status": "ok" if sent_cells else ("error" if send_errors else "skipped"),
        "reason": None if sent_cells else
                  ("all_sends_failed" if send_errors else "all_cells_empty"),
        "ppv_id": ppv_id, "is_resend": is_resend,
        "cells_sent": sent_cells, "recipients": total_recipients,
        "send_errors": send_errors,
        # Persisted into AutomationRun.stats_json → the ONLY way an operator can see
        # that a blast was quietly shrunk by the ladder rather than by a send failure.
        "skipped_hot_ladder": skipped_hot_ladder,
        "skipped_offers_paused": skipped_offers_paused,
        "broadcast": broadcast, "pause_hours": pause_hours,
        "resend_job_id": resend_job_id, "feed_post": feed_post, "results": results,
    }
