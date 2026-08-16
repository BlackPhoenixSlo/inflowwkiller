/**
 * AiStatusStrip — the daily-quota chip (item 21c).
 *
 * The three payloads below are not hand-written: they were captured verbatim from a
 * live `GET /admin/fans/{account}/{fan}/ai-status` against a seeded DB, one fan per
 * verdict the gate can reach with a ceiling in play — runway, held, whale. Keeping the
 * real wire shape is the point: the chip used to INFER the state from which fields
 * happened to be null, and a hand-rolled fixture is exactly where that kind of bug
 * hides, because the author writes the shape the code already expects.
 *
 * `relay.get` is mocked rather than `useAiStatus`, so the real hook, the real query and
 * the real component all run.
 */
import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactElement } from "react";

import AiStatusStrip from "./AiStatusStrip";
import type { AiStatus } from "@/hooks/useAiStatus";

const mockGet = vi.fn();
vi.mock("@/lib/relay", () => ({ relay: { get: (...a: unknown[]) => mockGet(...a) } }));

/** Everything outside `cadence.daily`, identical across the three captures. */
const BASE: Omit<AiStatus, "cadence" | "state" | "label" | "detail"> = {
  until: null,
  engine: "ai_chatter",
  graduated: null,
  ladder: { status: "idle", rung: 0 },
  language: "en",
  language_source: "account",
  open_ask: null,
  last_paid_at: null,
  force_ask: false,
  offer_caps_ok: null,
  break_proof: false,
  next_action: null,
  gate: { enabled: false },
  spend_7d: { paid_cents: 0, cap_cents: null, capped: false },
  llm: { last_at: null, model: null, purpose: null, calls_24h: 0, cost_24h_cents: 0 },
};

const BURST = {
  enabled: true, tier: "baseline", used: 2, cap: 50, left: 48,
  stopped: false, resets_after_minutes: 60, last_message_at: null,
};

/** Fan 7001 — 2 of his 5 free replies spent, so no ceiling reaches him yet. */
const RUNWAY: AiStatus = {
  ...BASE, state: "active", label: "Active", detail: "The AI answers his next message",
  cadence: {
    ...BURST,
    daily: {
      reason: "runway", used: 2, quota: null, runway_left: 3,
      held: false, enforced: true, backoff_hours: null, dry_days: null,
      ladder_hours: null, rung: null, free_at: null,
    },
  },
};

/** Fan 7002 — 6 replies against a ration of 3, past the runway, inside the backoff. */
const HELD: AiStatus = {
  ...BASE, state: "paused", label: "Quiet for 3.6h · daily cap reached (6/3)", detail: "…",
  cadence: {
    ...BURST, used: 6,
    daily: {
      reason: "held", used: 6, quota: 3, runway_left: null,
      held: true, enforced: true, backoff_hours: 3.5858745856403846, dry_days: 0.0,
      ladder_hours: [4, 12, 24, 72], rung: 0, free_at: null,
    },
  },
};

/** Fan 7003 — $600 in 7 days. A whale is never rationed. */
const WHALE: AiStatus = {
  ...BASE, state: "active", label: "Active", detail: "The AI answers his next message",
  cadence: {
    ...BURST, used: 6,
    daily: {
      reason: "unlimited", used: 6, quota: null, runway_left: null,
      held: false, enforced: true, backoff_hours: null, dry_days: null,
      ladder_hours: null, rung: null, free_at: null,
    },
  },
};

function draw(status: AiStatus): ReactElement {
  mockGet.mockResolvedValue(status);
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return (
    <QueryClientProvider client={qc}>
      <AiStatusStrip accountId="909090909" fanId={7001} />
    </QueryClientProvider>
  );
}

/** The chip is the only element whose text starts with the calendar glyph. */
async function chip(): Promise<HTMLElement | null> {
  const found = await screen.findAllByText(/📅/);
  return found[0] ?? null;
}

beforeEach(() => mockGet.mockReset());
afterEach(cleanup);

describe("AiStatusStrip — daily quota chip", () => {
  it("counts down the runway instead of claiming there is no cap", async () => {
    render(draw(RUNWAY));
    const el = await chip();
    // The bug this replaces: every fan inside his runway — nearly the whole roster —
    // rendered "no daily cap", which says nothing about the fan you are looking at.
    expect(el?.textContent).toBe("📅 runway 3 left");
    expect(el?.textContent).not.toContain("no daily cap");
    expect(el?.getAttribute("title")).toContain("3 more replies are owed before one applies");
  });

  it("keeps the counter but does not retype the badge's sentence", async () => {
    render(draw(HELD));
    const el = await chip();
    expect(el?.textContent).toBe("📅 6/3 replies · step 1 of 4");
    expect(el?.className).toContain("amber");
    // The wait belongs in the title; the state badge beside it already says it aloud.
    expect(el?.textContent).not.toContain("Quiet");
    // "today" is a calendar word for a window that tumbles — and on a held fan whose
    // day rolled over it put "0/25 today" next to a hold, which reads as a broken strip.
    expect(el?.textContent).not.toContain("today");
    // …and it is rounded. `%g` on the jittered rung printed "3.58587h" on a live thread.
    expect(el?.getAttribute("title")).toContain("Quiet for 3.6h");
  });

  it("draws the ladder so the longest rung doesn't read as a jump to it", async () => {
    // A fan 20.9 days dry sits on the LAST rung — and every operator who saw "72h"
    // asked whether it skipped 4/12/24. It didn't: the bands are as wide as their own
    // rungs, so the last one owns 72 of the 112 hours in a lap.
    render(draw({
      ...HELD,
      cadence: {
        ...HELD.cadence,
        daily: {
          ...HELD.cadence.daily!, rung: 3, backoff_hours: 64.996,
          free_at: "2026-07-30T03:54:43Z",
        },
      },
    }));
    const el = await chip();
    expect(el?.textContent).toContain("step 4 of 4");
    // The ladder is drawn with HIS step bracketed, and the next one named — the
    // cycle is the whole point: 4 of 4 is followed by 1 of 4, not by more.
    expect(el?.getAttribute("title")).toContain("Quiet steps: 4h · 12h · 24h · [72h]");
    expect(el?.getAttribute("title")).toContain("after this one comes 4h");
    // A manual reply does NOT move the clock, and the strip must not imply it does.
    expect(el?.getAttribute("title")).toContain("from the last reply");
  });

  it("explains a hold sitting next to a counter that already rolled over", async () => {
    // The live report this replaces: "0/25 today" beside "Daily quota reached (0/25)".
    // The quota day tumbles, so it resets under a fan mid-backoff — his ALLOWANCE comes
    // back, his wait does not — and the chip has to say that or it reads as broken.
    render(draw({
      ...HELD,
      cadence: {
        ...HELD.cadence,
        daily: { ...HELD.cadence.daily!, used: 0, quota: 25, rung: 3, backoff_hours: 66 },
      },
    }));
    const el = await chip();
    expect(el?.textContent).toContain("📅 0/25 replies · step 4 of 4");
    expect(el?.getAttribute("title")).toContain("already rolled over");
    expect(el?.getAttribute("title")).toContain("never his wait");
  });

  it("dates the release, because a 72h hold is not 'this morning'", async () => {
    render(draw({
      ...HELD,
      cadence: {
        ...HELD.cadence,
        daily: { ...HELD.cadence.daily!, rung: 3, free_at: "2026-07-30T03:54:43Z" },
      },
    }));
    const el = await chip();
    // Rendered in the viewer's zone, so assert the SHAPE — a weekday must be present,
    // never a bare clock that reads as today.
    expect(el?.textContent).toMatch(/→ (Mon|Tue|Wed|Thu|Fri|Sat|Sun) \d{2}:\d{2}/);
  });

  it("dates the STATE badge's wake time too — it carries the 72h backoff", async () => {
    // The badge used `untilLabel`, which prints a bare clock. That is right for the
    // pauses it was built for (they lift within the hour) and wrong for the quota
    // hold, which runs to three days: the thread header read "→ 12:31 PM" over a wait
    // that ended on Tuesday. The chip beside it had been dating its copy since 07-28.
    render(draw({ ...HELD, until: "2026-07-30T03:54:43Z" }));
    const badge = await screen.findByText(/daily cap reached/);
    expect(badge.textContent).toMatch(/→ (Mon|Tue|Wed|Thu|Fri|Sat|Sun) \d{2}:\d{2}/);
  });

  it("counts his messages down to the next tease and names the ask", async () => {
    render(draw({
      ...RUNWAY,
      teaser: {
        after: 20, msgs_since: 13, remaining: 7, rung: 4, rungs: 7,
        adaptive: true, cents: 5472, cents_max: 5472, softened: true,
      },
    }));
    const el = await screen.findByText(/🎁/);
    expect(el.textContent).toBe("🎁 tease in 7 · $54.72");
    expect(el.getAttribute("title")).toContain("13 of 20");
    expect(el.getAttribute("title")).toContain("SOFTENED");
  });

  it("shows a RANGE when the soften roll is what decides the ask", async () => {
    // 65-73% of the last ask. One end quoted as "the" price is a number the operator
    // would watch the engine contradict.
    render(draw({
      ...RUNWAY,
      teaser: {
        after: 20, msgs_since: 20, remaining: 0, rung: 3, rungs: 7,
        adaptive: true, cents: 5200, cents_max: 5840, softened: true,
      },
    }));
    const el = await screen.findByText(/🎁/);
    expect(el.textContent).toBe("🎁 tease due · $52–$58.40");
    expect(el.className).toContain("amber");
  });

  it("says free rather than $0, which reads as a broken price", async () => {
    render(draw({
      ...RUNWAY,
      teaser: {
        after: 20, msgs_since: 2, remaining: 18, rung: 4, rungs: 7,
        adaptive: true, cents: 0, cents_max: 0, softened: true,
      },
    }));
    expect((await screen.findByText(/🎁/)).textContent).toBe("🎁 tease in 18 · free");
  });

  it("still names a whale, which is the only fan the green pill should mean", async () => {
    render(draw(WHALE));
    const el = await chip();
    expect(el?.textContent).toBe("📅 no daily cap");
    expect(el?.getAttribute("title")).toContain("whale");
  });

  it("renders no chip at all when the quota is switched off", async () => {
    render(draw({ ...RUNWAY, cadence: { ...BURST, daily: { ...RUNWAY.cadence.daily!, reason: "off" } } }));
    // Wait for SOMETHING to have rendered before asserting the absence.
    expect(await screen.findByText(/AI Chatter/)).toBeTruthy();
    expect(screen.queryByText(/📅/)).toBeNull();
  });
});
