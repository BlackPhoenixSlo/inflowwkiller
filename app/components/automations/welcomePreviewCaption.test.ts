/**
 * THE PREVIEW CAPTION MUST NOT NAME A BUBBLE THAT IS NOT THERE.
 *
 * A welcome burst is VARIABLE-LENGTH. Two shipped shapes drop the middle bubble
 * — the `skip_time_bubble` checkbox, and a time-of-day slot the creator never
 * filled in (the activity line composes to nothing, which is already a live case
 * in the sender's own suite) — and in both the operator's QUESTION becomes
 * `bubbles[1]`.
 *
 * The caption used to count bubbles. It was taught about the checkbox and left
 * blind to the unfilled slot, so that burst's caption told the operator his own
 * word-for-word question was "the plain template — turn on AI restyle to preview
 * the shipped line": advice to hand his question to the model, printed under a
 * preview he is being asked to approve. It now reads the server's per-bubble
 * roles, which cannot be wrong about either shape.
 */
import { describe, expect, it } from "vitest";
import { welcomeGapIndex, welcomePinIndex, welcomePreviewCaption } from "@/components/automations/BrainPanel";
import type { AutomationPreviewResult } from "@/hooks/useAutomations";

const P = (o: Partial<AutomationPreviewResult>): AutomationPreviewResult =>
  ({ text: "", ...o }) as AutomationPreviewResult;

describe("welcomePreviewCaption", () => {
  it("names the time line, the question and the GIF in the full burst", () => {
    const c = welcomePreviewCaption(P({
      bubbles: ["hey", "it's Sunday afternoon", "what's yours?"],
      bubble_roles: ["opener", "gap", "tail"],
      gif_id: "abc",
    }))!;
    expect(c).toContain("4 bubbles");
    expect(c).toContain("the time line is the plain template");
    expect(c).toContain("then your question, word-for-word");
    expect(c).toContain("then the GIF, on its own");
  });

  it("does not call the QUESTION a template when the slot has no activity line", () => {
    // The regression. Same two bubbles as `skip_time_bubble`, but no checkbox is
    // ticked — the slot simply has nothing to say, so the server composed
    // greeting + question and said so in the roles.
    // (No `skip_time_bubble: false` here any more — the server does not echo the
    // flag and nothing ever read it. The ROLES are the whole input.)
    const c = welcomePreviewCaption(P({
      bubbles: ["hey", "what's yours?"],
      bubble_roles: ["opener", "tail"],
    }))!;
    expect(c).toContain("no time bubble");
    expect(c).toContain("then your question, word-for-word");
    expect(c).not.toContain("plain template");
    expect(c).not.toContain("AI restyle");
  });

  it("says the line is pinned, or restyled, when it is", () => {
    const roles = ["opener", "gap"] as const;
    expect(welcomePreviewCaption(P({ bubbles: ["hey", "line"], bubble_roles: [...roles], pinned: true })))
      .toContain("your pinned line (sends as-is)");
    expect(welcomePreviewCaption(P({ bubbles: ["hey", "line"], bubble_roles: [...roles], restyled: true })))
      .toContain("the AI-restyled line that ships");
  });

  it("still captions a greeting + GIF burst, and stays silent on a lone greeting", () => {
    expect(welcomePreviewCaption(P({
      bubbles: ["hey"], bubble_roles: ["opener"], gif_id: "abc",
    }))).toContain("then the GIF, on its own");
    expect(welcomePreviewCaption(P({ bubbles: ["hey"], bubble_roles: ["opener"] }))).toBeNull();
  });

  it("falls back to the old positional reading when a response carries no roles", () => {
    // A cached mutation result from before roles shipped. The pre-role burst
    // always had the time line at index 1, so that is the honest fallback.
    const c = welcomePreviewCaption(P({
      bubbles: ["hey", "it's Sunday afternoon"],
    }))!;
    expect(c).toContain("the time line is the plain template");
  });
});

/**
 * THE PIN BUTTON MUST NOT OFFER TO PIN A BUBBLE THAT IS NOT THE ACTIVITY LINE.
 *
 * A pin REPLACES the slot's activity line. The gate used to be five clauses plus
 * a hardcoded `bubbles[1]`, and three of those clauses read LIVE form state
 * (`welcomeTimeOnly` / `welcomeSkipTimeBubble` / `welcomeQuestion`) against a
 * preview from the last Preview click — so clearing the question box after
 * previewing made the button appear and pin the QUESTION as the activity line.
 * The fan then received it twice: once as the pin, once as the tail.
 *
 * `bubble_roles` says which bubble is the `gap`, on the same object as the
 * bubbles, so it cannot go stale against them.
 */
describe("welcomeGapIndex", () => {
  it("finds the activity line in the full burst", () => {
    expect(welcomeGapIndex(P({
      bubbles: ["hey", "it's Sunday afternoon", "what's yours?"],
      bubble_roles: ["opener", "gap", "tail"],
    }))).toBe(1);
  });

  it("🔴 refuses the QUESTION on a slot with no activity line", () => {
    // The regression, and the shape the index-1 guess got wrong: the server
    // composed greeting + question, so bubble 1 IS the question.
    expect(welcomeGapIndex(P({
      bubbles: ["hey", "hey whats your name?"],
      bubble_roles: ["opener", "tail"],
    }))).toBe(-1);
  });

  it("refuses a skip_time_bubble burst without reading the checkbox", () => {
    expect(welcomeGapIndex(P({
      bubbles: ["hey babe"],
      bubble_roles: ["opener"],
    }))).toBe(-1);
  });

  it("finds the activity line even when the question is gone", () => {
    // The mirror of the regression: the operator cleared the question box after
    // previewing. The gate must follow the PREVIEW, not the form.
    expect(welcomeGapIndex(P({
      bubbles: ["hey", "it's Sunday afternoon"],
      bubble_roles: ["opener", "gap"],
    }))).toBe(1);
  });

  it("refuses a response from before roles shipped, and a mismatched pair", () => {
    expect(welcomeGapIndex(P({ bubbles: ["hey", "it's Sunday afternoon"] }))).toBe(-1);
    expect(welcomeGapIndex(P({
      bubbles: ["hey", "it's Sunday afternoon"],
      bubble_roles: ["opener", "gap", "tail"],
    }))).toBe(-1);
    expect(welcomeGapIndex(null)).toBe(-1);
  });

  it("refuses a gap role with an empty bubble behind it", () => {
    expect(welcomeGapIndex(P({
      bubbles: ["hey", ""],
      bubble_roles: ["opener", "gap"],
    }))).toBe(-1);
  });
});

/**
 * 🔴 THE ROLES ALONE DO NOT SAY WHETHER A LINE MAY BE PINNED.
 *
 * `welcomeGapIndex` answers WHICH bubble is the activity line. It cannot answer
 * whether that bubble is pinnable, because `time_only` composes a short CLOCK
 * line into the very same slot wearing the very same `gap` role (the server must
 * pace it as one). Gating the button on the role alone offered "Keep this line"
 * on a time-only preview and pinned the clock: a pin that never ships while the
 * checkbox is on, that the panel cannot un-pin (`pinned` stays False, so Unpin
 * never renders), and that becomes the slot's permanent activity line the moment
 * the checkbox comes off.
 *
 * So the SERVER decides it, where the knobs are, and echoes one bit —
 * `pinnable`. This block is the client half of
 * `test_send_welcome.case_preview_pinnable_is_false_for_time_only`, which drives
 * the real `_compose_bubbles` and proves the roles really are identical across
 * the two shapes below.
 */
describe("welcomePinIndex", () => {
  it("offers the activity line when the server says it is pinnable", () => {
    expect(welcomePinIndex(P({
      bubbles: ["hey", "just got back from the beach... it's Sunday afternoon", "what's yours?"],
      bubble_roles: ["opener", "gap", "tail"],
      pinnable: true,
    }))).toBe(1);
  });

  it("🔴 refuses the CLOCK line on a time_only preview", () => {
    // Byte-for-byte the same roles and the same gap index as the case above —
    // only `pinnable` differs, which is the whole point.
    expect(welcomePinIndex(P({
      bubbles: ["hey", "it's Sunday afternoon", "what's yours?"],
      bubble_roles: ["opener", "gap", "tail"],
      pinnable: false,
    }))).toBe(-1);
  });

  it("still refuses a slot with no activity line, pinnable or not", () => {
    // The server cannot report `pinnable` on a burst with no gap bubble, but the
    // index guard is what makes that a checked fact rather than a trusted one.
    expect(welcomePinIndex(P({
      bubbles: ["hey", "hey whats your name?"],
      bubble_roles: ["opener", "tail"],
      pinnable: true,
    }))).toBe(-1);
    expect(welcomePinIndex(P({
      bubbles: ["hey", ""],
      bubble_roles: ["opener", "gap"],
      pinnable: true,
    }))).toBe(-1);
  });

  it("refuses a response from before `pinnable` shipped", () => {
    // No button is the safe side of storing a line that ships to fans forever.
    expect(welcomePinIndex(P({
      bubbles: ["hey", "it's Sunday afternoon"],
      bubble_roles: ["opener", "gap"],
    }))).toBe(-1);
    expect(welcomePinIndex(null)).toBe(-1);
  });
});
