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
  ...BASE, state: "active", label: "Active", detail: "She'll answer his next message",
  cadence: {
    ...BURST,
    daily: {
      reason: "runway", used: 2, quota: null, runway_left: 3,
      held: false, enforced: true, backoff_hours: null, dry_days: null,
    },
  },
};

/** Fan 7002 — 6 replies against a ration of 3, past the runway, inside the backoff. */
const HELD: AiStatus = {
  ...BASE, state: "paused", label: "Daily quota reached (6/3)", detail: "…",
  cadence: {
    ...BURST, used: 6,
    daily: {
      reason: "held", used: 6, quota: 3, runway_left: null,
      held: true, enforced: true, backoff_hours: 3.5858745856403846, dry_days: 0.0,
    },
  },
};

/** Fan 7003 — $600 in 7 days. A whale is never rationed. */
const WHALE: AiStatus = {
  ...BASE, state: "active", label: "Active", detail: "She'll answer his next message",
  cadence: {
    ...BURST, used: 6,
    daily: {
      reason: "unlimited", used: 6, quota: null, runway_left: null,
      held: false, enforced: true, backoff_hours: null, dry_days: null,
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
    expect(el?.getAttribute("title")).toContain("3 more replies before one applies");
  });

  it("keeps the counter but does not retype the badge's sentence", async () => {
    render(draw(HELD));
    const el = await chip();
    expect(el?.textContent).toBe("📅 6/3 today");
    expect(el?.className).toContain("amber");
    // The wait belongs in the title; the state badge beside it already says it aloud.
    expect(el?.textContent).not.toContain("quiet");
    // …and it is rounded. `%g` on the jittered rung printed "3.58587h" on a live thread.
    expect(el?.getAttribute("title")).toContain("She's quiet for 3.6h");
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
