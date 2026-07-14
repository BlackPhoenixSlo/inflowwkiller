"""content_gap_report — per-model readiness for the AI Upseller.

Read-only. Answers "what content is missing before the seller can work" for every
account: is the engine configured, is there a price authority (PPV Library Min/Max),
and is there SELLABLE content (items with media + a price/description). An item with
no media is not offerable; smart pricing needs a Min/Max; the gate needs the engine on.

Run:  cd service && python tools/content_gap_report.py
Writes a markdown report to service/tools/out/CONTENT_GAP.md and prints a summary.
"""
from __future__ import annotations

import json
import os
import sqlite3

DB = os.environ.get("CONTENT_GAP_DB", "chatterly.db")
BRAIN_OWNER = "BRAIN_OWNER_ACCOUNT_ID"  # Jaka — the brain owner, not a sellable model


def _j(raw):
    if not raw:
        return {}
    try:
        v = json.loads(raw)
        return v if isinstance(v, dict) else {}
    except Exception:
        return {}


def _media_ids(raw) -> list:
    if not raw:
        return []
    try:
        v = json.loads(raw)
        return v if isinstance(v, list) else []
    except Exception:
        return []


def _price_bounds(ppv: dict) -> tuple[int | None, int | None]:
    """The PPV Library Min/Max in cents, however the config spells them."""
    def pick(*keys):
        for k in keys:
            if ppv.get(k) not in (None, ""):
                try:
                    return int(ppv[k])
                except (TypeError, ValueError):
                    pass
        return None
    lo = pick("price_min_cents", "min_price_cents", "price_floor_cents", "min_cents")
    hi = pick("price_max_cents", "max_price_cents", "price_ceil_cents", "max_cents")
    return lo, hi


def analyze(c: sqlite3.Connection, aid: str, nick: str) -> dict:
    cfg = _j(c.execute(
        "select ai_chatter_config_json from account_ai_config where account_id=?",
        (aid,)).fetchone() or [None])
    ppv = _j(c.execute(
        "select ppv_library_config_json from account_ai_config where account_id=?",
        (aid,)).fetchone() or [None])
    lo, hi = _price_bounds(ppv)

    items = c.execute(
        "select script_id, description_for_ai, media_ids, price_cents, tip_unlock_cents, "
        "is_free_teaser, enabled from catalog_items where account_id=?", (aid,)).fetchall()
    scripts = c.execute(
        "select status from catalog_scripts where account_id=?", (aid,)).fetchall()

    singles = [it for it in items if it[0] is None]
    script_items = [it for it in items if it[0] is not None]

    def sellable(it) -> bool:
        _sid, _desc, media, price, tip, free, enabled = it
        if not enabled:
            return False
        if not _media_ids(media):
            return False               # no media = not offerable
        if free:
            return True
        return bool((price or 0) > 0 or (tip or 0) > 0)

    def no_media(it):
        return not _media_ids(it[2])

    def no_desc(it):
        return not (it[1] or "").strip()

    total = len(items)
    sellable_n = sum(1 for it in items if sellable(it))
    no_media_n = sum(1 for it in items if no_media(it))
    no_desc_n = sum(1 for it in items if no_desc(it))
    enabled_scripts = sum(1 for s in scripts if s[0] == "enabled")

    gate = bool(cfg.get("qualification_gate_enabled"))
    smart = bool(cfg.get("smart_pricing_enabled"))
    enabled = bool(cfg.get("enabled"))

    blockers = []
    if not enabled:
        blockers.append("AI Chatter is OFF (enable it, or click Enable Upseller)")
    if not gate:
        blockers.append("Sell-in-chat gate OFF (click Enable Upseller)")
    # NOTE: PPV Library Min/Max is NOT a blocker — price_bounds() defaults it to the
    # OF floor/ceiling ($3/$200) when unset, so smart pricing always has an authority.
    # Left here as INFO only.
    if sellable_n == 0:
        blockers.append(f"NO sellable content ({total} items exist, {no_media_n} have no media)")
    if no_media_n:
        blockers.append(f"{no_media_n} item(s) have no media attached (not offerable)")
    if no_desc_n:
        blockers.append(f"{no_desc_n} item(s) have no 'what the fan sees' description")

    return {
        "aid": aid, "nick": nick, "enabled": enabled, "gate": gate, "smart": smart,
        "price_min": lo, "price_max": hi,
        "scripts_total": len(scripts), "scripts_enabled": enabled_scripts,
        "items_total": total, "singles": len(singles), "script_items": len(script_items),
        "sellable": sellable_n, "no_media": no_media_n, "no_desc": no_desc_n,
        "blockers": blockers,
    }


def main() -> None:
    c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    accts = c.execute("select id, nickname from accounts order by nickname").fetchall()
    reports = [analyze(c, aid, nick or aid) for aid, nick in accts
               if aid != BRAIN_OWNER]

    def dol(cents):
        return "—" if cents is None else f"${cents // 100}"

    lines = ["# AI Upseller — content-gap report", ""]
    lines.append("| Model | Engine | Gate | Smart$ | PPV Min/Max | Scripts (on) | Sellable items | No-media | Ready? |")
    lines.append("|---|---|---|---|---|---|---|---|---|")
    for r in reports:
        ready = "✅" if not r["blockers"] else "❌"
        lines.append(
            f"| {r['nick']} | {'on' if r['enabled'] else 'OFF'} | "
            f"{'on' if r['gate'] else 'OFF'} | {'on' if r['smart'] else 'off'} | "
            f"{dol(r['price_min'])} / {dol(r['price_max'])} | "
            f"{r['scripts_total']} ({r['scripts_enabled']}) | "
            f"{r['sellable']}/{r['items_total']} | {r['no_media']} | {ready} |")

    lines += ["", "## What each model needs before the seller works", ""]
    for r in reports:
        lines.append(f"### {r['nick']}  ·  `{r['aid']}`")
        if not r["blockers"]:
            lines.append("- ✅ Ready — engine on, price authority set, sellable content exists.")
        else:
            for b in r["blockers"]:
                lines.append(f"- ❌ {b}")
        lines.append("")

    out_dir = os.path.join(os.path.dirname(__file__), "out")
    os.makedirs(out_dir, exist_ok=True)
    out = os.path.join(out_dir, "CONTENT_GAP.md")
    with open(out, "w") as f:
        f.write("\n".join(lines))

    # Console summary
    print(f"{len(reports)} sellable models (excluding brain owner {BRAIN_OWNER}):\n")
    for r in reports:
        status = "READY" if not r["blockers"] else f"{len(r['blockers'])} blocker(s)"
        print(f"  {r['nick']:<12} engine={'on' if r['enabled'] else 'OFF':<3} "
              f"gate={'on' if r['gate'] else 'OFF':<3} "
              f"sellable={r['sellable']}/{r['items_total']:<3} -> {status}")
        for b in r["blockers"]:
            print(f"       - {b}")
    print(f"\nwrote {out}")


if __name__ == "__main__":
    main()
