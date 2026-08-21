"""service/funnel_stats_api.py — read-only per-step SENT→PURCHASED rollup for a
funnel ("per-script tracking"). The write half already exists: every funnel
send is stamped `funnel_step` + `mass_run_id` by write_outbound_attribution
(attribution.py), and a PPV unlock flips `is_paid=True` / `purchased_at` on that
SAME outbound `messages` row (transaction_ingest / the ai_chatter isOpened
fast-path). So the message row IS the purchase→step join — no new linker.

HOW A PURCHASE JOINS BACK TO ITS STEP
  reply_mass_funnel._send_step writes the PPV offer as one outbound `messages`
  row carrying `funnel_step=N`, `mass_run_id=R`, `price_cents=P`, `is_paid=False`.
  When the fan unlocks it, transaction_ingest._link_ppv_payment picks THAT row
  (offer→payment linkage) and sets `is_paid=True`. The row still carries its
  original `funnel_step`, so `is_paid=True` on a funnel-stamped row is precisely
  "the offer made at step N was purchased". Revenue is that row's `price_cents`
  (a PPV is paid at its offer price). This is the exact signal reply_mass_funnel
  itself reads for its #30 "offer taken" halt (`Message.is_paid.is_(True)`).

SCOPING
  A funnel is a GLOBAL definition, but its sends live on account-scoped
  `mass_runs` (MassRun.funnel_id). We roll up over `messages JOIN mass_runs` and
  gate the account set with `clamp_account_filter` (auth.py) — the stats-endpoint
  cousin of `assert_account_owned`: a signed-in owner/chatter only sees THEIR
  accounts' runs of the funnel (403 on an unowned explicit account_id); an
  unauthed system/cron call stays global. No writes.

Route (covered by the existing `/admin/:path*` Next rewrite — no next.config
change, no new app/admin page):

  GET /admin/funnel-stats?funnel_id=[&account_id=]
     → {"funnel_id": id,
        "steps": [{"step", "sent", "bought", "conv_pct", "revenue_cents"}, ...]}

COUNTING
  Per `funnel_step`, over this funnel's runs' outbound rows:
    • sent          = COUNT(DISTINCT fan_id) — fans who received the step. Distinct
                      fans (not raw rows) so a 2-bubble text step doesn't read as
                      "2 sends", and it's the conversion denominator ops expect
                      ("of the fans offered step 4, what % bought").
    • bought        = COUNT(DISTINCT fan_id) among rows with is_paid=True — fans who
                      unlocked the step's PPV. Non-PPV steps have is_paid NULL → 0.
    • revenue_cents = SUM(price_cents) over is_paid=True rows — true dollars from
                      purchases attributed to the step.
    • conv_pct      = round(100 * bought / sent, 1); 0.0 when sent==0 (no /0).
  Every step DEFINED in the funnel appears even with zero sends (a step with no
  sends returns zeros); any observed step no longer in the definition is appended.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException
from sqlalchemy import case, distinct, func, select

from auth import clamp_account_filter
from db.engine import get_session
from db.models import MassMessageFunnel, MassRun, Message
from funnels_api import _safe_steps

log = logging.getLogger("of-relay.funnel_stats_api")

router = APIRouter()


def _defined_steps(raw: str | None) -> list[int]:
    """The funnel's declared step numbers, in order, deduped. Drives which steps
    appear in the rollup even when they've had zero sends (so a brand-new /
    never-triggered step reads as zeros rather than vanishing). Reuses funnels_api's
    canonical step parser; only the whole-int gate is stats-specific (a step number
    must be a real integer to join back to Message.funnel_step)."""
    out: list[int] = []
    seen: set[int] = set()
    for st in _safe_steps(raw):
        v = st.get("step")
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            continue
        n = int(v)
        if n != v or n in seen:  # whole ints only; first occurrence wins
            continue
        seen.add(n)
        out.append(n)
    return out


@router.get("/admin/funnel-stats")
async def funnel_stats(
    funnel_id: int,
    account_id: str | None = None,
) -> dict:
    # Plain scalar params → FastAPI reads them as query string (?funnel_id=&
    # account_id=). Plain defaults (not Query() sentinels) keep the coroutine
    # directly unit-testable, matching funnels_api's route fns.
    # clamp_account_filter is the stats-endpoint owner gate: [account_id] when
    # given+owned (403 otherwise), the principal's whole account set when signed
    # in with no account_id, or None (global) for unauthed system/cron callers.
    account_ids = clamp_account_filter(account_id)

    async with get_session() as s:
        fn = await s.get(MassMessageFunnel, int(funnel_id))
        if fn is None:
            raise HTTPException(404, "funnel not found")
        defined = _defined_steps(fn.steps_json)

        # The purchase→step join: outbound funnel rows (funnel_step stamped at
        # send) whose is_paid was flipped True when the fan unlocked the PPV — all
        # on the SAME row. Scoped to THIS funnel's runs via the mass_runs join.
        q = (
            select(
                Message.funnel_step.label("step"),
                func.count(distinct(Message.fan_id)).label("sent"),
                func.count(distinct(
                    case((Message.is_paid.is_(True), Message.fan_id))
                )).label("bought"),
                func.coalesce(func.sum(
                    case((Message.is_paid.is_(True), Message.price_cents), else_=0)
                ), 0).label("revenue_cents"),
            )
            .join(MassRun, Message.mass_run_id == MassRun.id)
            .where(
                MassRun.funnel_id == int(funnel_id),
                Message.direction == "out",
                Message.funnel_step.is_not(None),
            )
            .group_by(Message.funnel_step)
        )
        if account_ids is not None:
            q = q.where(MassRun.account_id.in_(account_ids))
        rows = (await s.execute(q)).all()

    agg = {
        int(r.step): (int(r.sent or 0), int(r.bought or 0), int(r.revenue_cents or 0))
        for r in rows if r.step is not None
    }
    # Declared steps first (in funnel order), then any observed step that's no
    # longer in the definition (an edited-out step with historical sends).
    order = list(defined) + sorted(k for k in agg if k not in defined)

    steps_out: list[dict] = []
    for step in order:
        sent, bought, revenue_cents = agg.get(step, (0, 0, 0))
        conv_pct = round(100.0 * bought / sent, 1) if sent else 0.0
        steps_out.append({
            "step": step,
            "sent": sent,
            "bought": bought,
            "conv_pct": conv_pct,
            "revenue_cents": revenue_cents,
        })

    return {"funnel_id": int(funnel_id), "steps": steps_out}
