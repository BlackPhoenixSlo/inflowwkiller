import { describe, expect, it } from "vitest";

import { runStatsChunks, welcomeStatsChunks } from "./runStats";

/** The rendered line an operator actually reads. */
function line(stats: Record<string, unknown> | null): string {
  return runStatsChunks(stats).map((c) => c.text).join(" · ");
}

/** A stats bag from a preview THAT RAN THE GATE.
 *
 *  `no_price_skipped` is the marker, and it was sprinkled through these cases as
 *  a bare `no_price_skipped: 0` — a magic key with no name, easily read as an
 *  incidental counter rather than the precondition the whole forecast chunk hangs
 *  on. Only a gate-running preview emits it; an older relay fills `would_follow`
 *  with the raw, UN-gated pool, and rendering that as "8 would notify" would
 *  restate the six-day lie with more confidence than the wording it replaced. So
 *  every case that expects a forecast to RENDER has to say this out loud. */
function gated(stats: Record<string, unknown>): Record<string, unknown> {
  return { no_price_skipped: 0, ...stats };
}

describe("runStatsChunks — dry-run reporting", () => {
  it("leads with what the run WOULD do, not how big the pool was", () => {
    // The production shape that hid a six-day no-op: auto_follow aimed at fans
    // it already followed. The old line read "8 candidates · dry run", which
    // an operator reads as "8 follows ready to go".
    const s = line({
      action: "follow", dry_run: true, candidates: 8, examined: 8,
      would_follow: [], already_following: 8,
      paid_profile_skipped: 0, no_price_skipped: 0, errors: 0, cap: 50,
    });
    expect(s).toContain("0 would notify");
    expect(s).toContain("8 already followed");
  });

  it("flags a zero forecast so it cannot be skimmed past", () => {
    // `no_price_skipped` is the marker that says the gate actually ran. Without
    // it the chunk is withheld on purpose (see runStats.tsx) — so every case that
    // asserts a forecast IS rendered has to carry it.
    const chunks = runStatsChunks(gated({ would_follow: [], candidates: 8, examined: 8 }));
    expect(chunks.find((c) => c.text === "0 would notify")?.tone).toBe("warn");
  });

  it("does not flag a forecast that will actually act", () => {
    const chunks = runStatsChunks(gated({ would_ping: [1, 2, 3], candidates: 40, examined: 40 }));
    const w = chunks.find((c) => c.text === "3 would notify");
    expect(w).toBeDefined();
    expect(w?.tone).toBeUndefined();
  });

  it("reads would_ping as well as would_follow", () => {
    expect(line(gated({ would_ping: [7001, 7002], examined: 2 })))
      .toContain("2 would notify");
  });

  it("says when only part of the pool was checked", () => {
    expect(line({ candidates: 250, examined: 50 }))
      .toContain("50 of 250 candidates checked");
    expect(line({ candidates: 8, examined: 8 })).toContain("8 candidates");
  });

  it("still reports a live run's real counters", () => {
    const s = line({
      action: "ping", dry_run: false, pinged: 12, stranded: 0,
      paid_profile_skipped: 3, no_price_skipped: 2, candidates: 148,
    });
    expect(s).toContain("pinged 12");
    expect(s).toContain("3 paid profiles skipped");
    expect(s).toContain("2 price unreadable, skipped");
  });

  it("is silent on an empty bag", () => {
    expect(runStatsChunks(null)).toEqual([]);
    expect(runStatsChunks({})).toEqual([]);
  });
  it("says nothing about a forecast an older relay did not gate", () => {
    // A relay without the gated preview still fills would_follow with the raw
    // pool. Reporting that as "8 would notify" is the original defect wearing a
    // more confident label, so the chunk is withheld until the gate marker
    // proves the gate ran.
    //
    // ONE case, not two: this was written twice, once here and once as "says
    // nothing at all when it cannot prove the gate ran", with the same bag and
    // the same assertion — and the second copy then contradicted its own name by
    // asserting that `money_gate: false` DOES say something. That half was the
    // interesting half and it was checking the weakest thing available
    // (`toContain("would notify")`), never the loud red chunk it exists for; it
    // now has its own case below.
    const s = line({ dry_run: true, candidates: 8, would_follow: [1, 2, 3, 4, 5, 6, 7, 8] });
    expect(s).not.toContain("would notify");
    expect(s).toContain("8 candidates");
  });
});

describe("runStatsChunks — an UNGATED run, which spends real money", () => {
  it("leads with the charge, in the loudest tone, naming the count", () => {
    // ⚠️ MONEY, and the one chunk an operator must not skim past: an ungated run
    // reads no profile, so nothing here can name the price — only that these
    // follows WILL be charged. It has to be FIRST (the row is read left to right)
    // and it has to be `err` (every other tone in this file is advisory).
    const chunks = runStatsChunks({
      action: "follow", dry_run: true, money_gate: false,
      would_follow: [1, 2, 3, 4, 5, 6, 7, 8], unpriced_follows: 8,
      candidates: 8, examined: 8, cap: 50,
    });
    expect(chunks[0]?.text).toBe("money gate OFF — 8 follows will be charged");
    expect(chunks[0]?.tone).toBe("err");
    // The forecast still renders — `money_gate: false` is the OTHER way to prove
    // the numbers describe the run that will actually happen.
    expect(chunks.map((c) => c.text)).toContain("8 would notify");
  });

  it("still says the charge is coming when it cannot count it", () => {
    const chunks = runStatsChunks({ action: "follow", money_gate: false });
    expect(chunks[0]?.text).toBe("money gate OFF — paid profiles WILL be charged");
    expect(chunks[0]?.tone).toBe("err");
  });
});

describe("runStatsChunks — a tick that threw", () => {
  // R3-2. `auto_follow` emits `errors` from all four actions and
  // `promo_reactivate` from its only one, and the growth formatter rendered
  // none of them: a follow tick that threw on 12 fans printed
  // `last run ok · followed 0 · 20 candidates` on AutoFollowTab and on the
  // Overview card — the line a healthy quiet day prints. The welcome card was
  // given this exact chunk one pass earlier; the paid lane was left without it.
  it("NEVER lets a failed follow tick read as a quiet day", () => {
    const chunks = runStatsChunks({
      action: "follow", dry_run: false, source: "all_stored",
      candidates: 20, followed: 0, already_following: 8, errors: 12, cap: 30,
    });
    expect(chunks[0]?.text).toBe("12 failed");
    expect(chunks[0]?.tone).toBe("err");
  });

  it("covers promo_reactivate, the other kind this formatter renders", () => {
    // Those two kinds are the whole audience of `runStatsChunks`
    // (OverviewTab.GROWTH_KINDS, AutoFollowTab, PromoReactivateCard), and both
    // emit `errors` — so one chunk closes the hole on both.
    const chunks = runStatsChunks({ reactivated: 0, errors: 3, deferred: 1, dry_run: false });
    expect(chunks[0]?.text).toBe("3 failed");
    expect(chunks[0]?.tone).toBe("err");
  });

  it("outranks even the money-gate chunk", () => {
    // A run we could not READ is worse news than a run we read without a price
    // check — and the gate chunk keeps its own `err` tone, so neither is lost.
    const chunks = runStatsChunks({
      action: "follow", money_gate: false, followed: 2, errors: 5,
    });
    expect(chunks.map((c) => c.text).slice(0, 2)).toEqual([
      "5 failed", "money gate OFF — 2 follows will be charged",
    ]);
    expect(chunks.slice(0, 2).every((c) => c.tone === "err")).toBe(true);
  });

  it("says nothing on a clean tick", () => {
    expect(line({ action: "follow", followed: 3, errors: 0 })).not.toContain("failed");
  });
});

describe("runStatsChunks — a live run that notified nobody", () => {
  it("names the pinned window instead of letting it read as a quiet day", () => {
    // `pool_exhausted` is the server's own bit (auto_follow._run_follow): the run
    // walked everything it looked at and notified nobody. It was emitted with no
    // consumer at all, so the operator saw `followed 0 · 15 candidates` — the
    // exact line a healthy quiet day prints.
    const s = line({
      action: "follow", dry_run: false, candidates: 15, followed: 0,
      already_following: 15, pool_exhausted: true, errors: 0, cap: 3,
    });
    expect(s).toContain("nobody new to notify in this batch");
    expect(runStatsChunks({ pool_exhausted: true })[0]?.tone).toBe("warn");
  });

  it("stays quiet on a run that did notify someone", () => {
    const s = line({
      action: "follow", dry_run: false, candidates: 15, followed: 3,
      already_following: 12, pool_exhausted: false, errors: 0, cap: 3,
    });
    expect(s).not.toContain("nobody new to notify");
    expect(line({ action: "follow", followed: 3 })).not.toContain("nobody new to notify");
  });

  it("does not claim the table is finished while it also reports fans left", () => {
    // The two chunks are computed over DIFFERENT populations and used to
    // contradict each other in one line. `pool_exhausted` is measured over the
    // headroom-truncated slice (`[:_headroom(cap)]`), so on a backfill it means
    // "this tick's window", while `eligible` counts the whole remaining pool —
    // and this bag is an ordinary healthy draining tick whose head happens to be
    // already-followed or priced, not a rare one. The old copy rendered
    // "whole pool checked, nobody new to notify · 200 eligible still to walk",
    // which teaches the operator to distrust one of the two numbers.
    const s = line({
      action: "follow", dry_run: false, source: "all_stored", candidates: 25,
      followed: 0, already_following: 25, pool_exhausted: true, eligible: 200,
      errors: 0, cap: 5,
    });
    expect(s).toContain("nobody new to notify in this batch");
    expect(s).toContain("200 eligible still to walk");
    // No chunk may claim the pool/table is done while another says 200 remain.
    expect(s).not.toContain("whole pool");
    expect(s).not.toMatch(/pool checked|nothing left|finished/i);
  });
});

describe("welcomeStatsChunks — the send_welcome follow-back lane", () => {
  // ⚠️ MONEY. `grep -rn "followed_back" app/` returned NOTHING before this: the
  // engine emitted five counters and two knobs and the UI read none of them, so
  // the only automation lane that can buy a subscription was also the only one
  // with no run feedback anywhere in the app. Nothing distinguished a free
  // follow-back from a purchased one.
  //
  // R3-3. These cases used to drive `runStatsChunks`, which called
  // `followBackChunks` in violation of its own file header — and that call was
  // DEAD: `follow_back` reaches neither kind that renders the growth formatter.
  // So the only money lane in the app was tested exclusively through a code path
  // production never takes, while the path it does take (`welcomeStatsChunks`,
  // via `<RunStats kind="send_welcome">`) had one case. They now run through the
  // production path, and the dead call is gone.
  const lane = (extra: Record<string, unknown> = {}) => ({
    welcomes_sent: 2, follow_back: true, follow_back_gate: true,
    followed_back: 2, follow_back_already: 0, follow_back_paid_skipped: 0,
    follow_back_no_price: 0, follow_back_errors: 0, ...extra,
  });
  /** The lane's own chunks, with the welcome card's headline chunks stripped —
   *  so a case about the follow-back lane asserts about the follow-back lane and
   *  not about where `welcomed 2` sits relative to it. */
  const laneChunks = (stats: Record<string, unknown>) =>
    welcomeStatsChunks(stats).filter((c) => /follow|charged/.test(c.text));
  const laneLine = (stats: Record<string, unknown>) =>
    welcomeStatsChunks(stats).map((c) => c.text).join(" · ");

  it("reports what the lane did", () => {
    const s = laneLine(lane({
      followed_back: 1, follow_back_already: 1, follow_back_paid_skipped: 3,
      follow_back_no_price: 2, follow_back_errors: 1,
    }));
    expect(s).toContain("followed back 1");
    expect(s).toContain("1 already followed back");
    expect(s).toContain("3 paid subs not followed back");
    expect(s).toContain("2 follow-back prices unreadable");
    expect(s).toContain("1 follow-backs failed");
  });

  it("leads with the charge when the price-check is off", () => {
    // Same tone, same position and the same class of fact as auto_follow's
    // `money gate OFF` chunk — two voices for one hazard would make the quieter
    // one read as the smaller problem, and this one fires on a TIMER rather than
    // on a button the operator pressed.
    const chunks = laneChunks(lane({ follow_back_gate: false, followed_back: 4 }));
    expect(chunks[0]?.text).toBe("follow-back gate OFF — 4 follow-backs charged blind");
    expect(chunks[0]?.tone).toBe("err");
  });

  it("says the charge is coming even on a tick that followed nobody", () => {
    const chunks = laneChunks(lane({ follow_back_gate: false, followed_back: 0 }));
    expect(chunks[0]?.text).toBe("follow-back gate OFF — paid subscribers WILL be charged");
    expect(chunks[0]?.tone).toBe("err");
  });

  it("flags a tick that welcomed people and followed nobody back", () => {
    // The lane was ON, welcomes landed, and not one follow went out. That is
    // either a broken lane or an account we already follow entirely, and the
    // qualifiers beside it say which — but it must not print in the same voice
    // as a working tick.
    const chunks = laneChunks(lane({ followed_back: 0, follow_back_already: 2 }));
    expect(chunks.find((c) => c.text === "followed back 0")?.tone).toBe("warn");
    expect(chunks.find((c) => c.text === "followed back 2")).toBeUndefined();
  });

  it("says nothing at all when the lane is switched off", () => {
    // `followed_back: 0` is a switched-off lane AND a quiet one AND a broken one.
    // Reporting a permanent zero on an account that never turned the lane on is
    // how operators learn to stop reading the row.
    expect(laneLine(lane({ follow_back: false, followed_back: 0 }))).not.toContain("followed back");
  });

  it("says nothing when the run did not vouch for the knob", () => {
    // An older relay emits the counters without `follow_back`. Absent is "cannot
    // say", not "off" — and inventing a lane report out of five zeroes it did not
    // vouch for is the mistake `would_follow` already made once.
    const s = laneLine({ welcomes_sent: 2, followed_back: 0, follow_back_paid_skipped: 0 });
    expect(s).not.toContain("followed back");
  });

  it("stays quiet on a tick that welcomed nobody", () => {
    // The card still prints its own `welcomed 0` warning — that is the headline,
    // not the lane. The LANE says nothing, because every counter is 0 for the
    // uninteresting reason.
    expect(laneChunks(lane({ welcomes_sent: 0, followed_back: 0 }))).toEqual([]);
    expect(laneLine(lane({ welcomes_sent: 0, followed_back: 0 }))).toContain("welcomed 0");
  });

  it("does not assert a charge on a run that cannot spend a cent", () => {
    // R2-5. `send_welcome` follows nobody on a dry run — `if ctx.follow_back
    // and not ctx.dry_run` — so the five counters are structural zeroes and the
    // gate describes what WOULD happen. The red "paid subscribers WILL be
    // charged" was landing on a run whose own `· dry run` marker sat three
    // chunks to its right. Same mistake C7 fixed in the panel, one file over.
    const all = welcomeStatsChunks(
      lane({ dry_run: true, follow_back_gate: false, followed_back: 0 }));
    expect(all.map((c) => c.text).join(" · ")).not.toContain("WILL be charged");
    expect(all.map((c) => c.text).join(" · ")).not.toContain("charged blind");
    expect(all.some((c) => c.tone === "err")).toBe(false);
    expect(laneChunks(lane({ dry_run: true, follow_back_gate: false, followed_back: 0 }))[0]?.text)
      .toBe("follow-back gate off — a live run would follow blind");
  });

  it("does not report counters a dry run never measured", () => {
    const s = laneLine(lane({ dry_run: true, followed_back: 0 }));
    expect(s).not.toContain("followed back 0");
    expect(s).toContain("follow-back would run (dry)");
  });
  it("the GROWTH formatter says nothing about this lane at all", () => {
    // R3-3, the other half. `runStatsChunks` renders `auto_follow` and
    // `promo_reactivate`; neither emits `follow_back`, so its old call into this
    // lane could only ever fire on a bag it never receives. If a welcome key
    // reappears in that formatter, this is where it goes red.
    expect(runStatsChunks(lane({ follow_back_gate: false, followed_back: 4 })))
      .toEqual([]);
  });
});

describe("welcomeStatsChunks — the welcome card's OWN last-run line", () => {
  // R2-6. The card rendered `runStatsChunks`, which knows only Growth keys. The
  // two bags share the follow-back keys and nothing else, so a welcome tick's
  // headline and its errors both rendered as the empty string.
  const wl = (extra: Record<string, unknown> = {}) => ({
    subscribers_seen: 12, new_subscribers: 5, welcomes_sent: 5, errors: 0,
    ...extra,
  });

  it("shows the headline the growth formatter rendered as nothing", () => {
    expect(runStatsChunks(wl()).map((c) => c.text).join(" · ")).toBe("");
    expect(welcomeStatsChunks(wl()).map((c) => c.text).join(" · "))
      .toContain("welcomed 5");
  });

  it("NEVER lets a failed tick read as a bare ok", () => {
    // The executor finalises a paced welcome run as `ok` even when every fan in
    // the batch threw (§A3: one fan's escape must not kill five other bursts),
    // so `errors` is the only place a broken tick is visible. It rendered
    // nowhere, and the card said "Last run: ok".
    const chunks = welcomeStatsChunks(wl({ welcomes_sent: 0, errors: 9 }));
    expect(chunks[0]?.text).toBe("9 failed");
    expect(chunks[0]?.tone).toBe("err");
  });

  it("warns on a tick that welcomed nobody, and says why", () => {
    const chunks = welcomeStatsChunks(wl({
      welcomes_sent: 0, skipped_existing: 4, skipped_cooldown: 1, cap_hit: true,
    }));
    expect(chunks.find((c) => c.text === "welcomed 0")?.tone).toBe("warn");
    const s = chunks.map((c) => c.text).join(" · ");
    expect(s).toContain("4 already welcomed");
    expect(s).toContain("1 on cooldown");
    expect(s).toContain("daily LLM cap hit");
  });

  it("suppresses the qualifiers that are zero", () => {
    const s = welcomeStatsChunks(wl()).map((c) => c.text).join(" · ");
    expect(s).not.toContain("already welcomed");
    expect(s).not.toContain("cooldown");
    expect(s).not.toContain("failed");
  });

  it("carries the follow-back lane, which is shared with the growth bag", () => {
    const s = welcomeStatsChunks(wl({
      follow_back: true, follow_back_gate: true, followed_back: 3,
    })).map((c) => c.text).join(" · ");
    expect(s).toContain("followed back 3");
  });

  it("reports the stop_on_reply outcomes nothing else in the app shows", () => {
    const s = welcomeStatsChunks(wl({
      aborted_on_reply: 2, handoff_enqueued: 1, reply_guard_off: 3,
    })).map((c) => c.text).join(" · ");
    expect(s).toContain("2 stopped, he replied");
    expect(s).toContain("1 handed to the chat engine");
    expect(s).toContain("3 sent without the reply guard");
  });
});

describe("runStatsChunks — a backfill preview that is standing still", () => {
  it("names the pool the dry run is not draining", () => {
    // A dry tick prints the same plan forever (why: `auto_follow._run_follow`),
    // so `20 candidates` on a 3663-fan account looked exactly like a finished
    // 20-fan account.
    const chunks = runStatsChunks(gated({
      action: "follow", dry_run: true, candidates: 20, examined: 20,
      eligible: 3663, would_follow: [], already_following: 20,
    }));
    const e = chunks.find((c) => c.text.startsWith("3663 eligible"));
    expect(e).toBeDefined();
    expect(e?.tone).toBe("warn");
  });

  it("stays quiet when the pool fits in one look", () => {
    // Nothing is standing still if the whole eligible pool IS the candidate list.
    expect(line(gated({ source: "all_stored", candidates: 9, examined: 9, eligible: 9 })))
      .not.toContain("eligible");
  });

  it("does not call a LIVE tick's remaining pool a dry run", () => {
    // R3-4 emitted `eligible` on the live path too, which is where the number is
    // actually progress. Saying "a dry run re-checks this same head each tick"
    // over it would be false, and `warn` over it would be crying wolf about a
    // rule that IS draining.
    const chunks = runStatsChunks({
      action: "follow", dry_run: false, source: "all_stored",
      candidates: 150, followed: 30, eligible: 3663, errors: 0,
    });
    const e = chunks.find((c) => c.text.includes("eligible"));
    expect(e?.text).toBe("3663 eligible still to walk");
    expect(e?.tone).toBeUndefined();
  });

  it("still reads an OLD stored row that stacked the two, without comparing them", () => {
    // S-N12, and read the name literally: this is HISTORY-DEFENCE, not live
    // behaviour. `auto_follow` now emits `eligible` only when `all_stored` is
    // the whole rule, so the relay cannot produce this bag any more
    // (`test_auto_follow.case_eligible_is_not_emitted_for_a_stacked_pool`).
    // `last_run.stats` is stored JSON though, and a row written before that fix
    // still carries it. `candidates` there is the merged pool across every
    // source while `eligible` counts the all_stored backfill alone, so
    // comparing them compares different populations — and would hide the number
    // on exactly the rule whose progress is hardest to read.
    const chunks = runStatsChunks(gated({
      // dry, because that is what the "standing still" wording describes — see
      // the live case above for the same bag on a rule that is draining.
      dry_run: true,
      source: "expired,all_stored", candidates: 40, examined: 40, eligible: 12,
    }));
    const e = chunks.find((c) => c.text.includes("eligible"));
    expect(e?.text).toBe(
      "12 eligible in the backfill pool — a dry run re-checks this same head each tick");
    expect(e?.tone).toBe("warn");
  });
});
