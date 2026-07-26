/**
 * PersonaClaims — the two pure rules behind "what she told him".
 *
 * The ledger is written by the chat engines and read by the pre-send consistency
 * check; this panel is the first place an operator can see it. Both helpers guard
 * against a real shape the server can send: a claim row with no text (the
 * extractor recorded a topic but nothing usable) and an `at` that isn't a date.
 */
import { describe, expect, it } from "vitest";

import {
  canonFacts, claimDate, supersededClaims, visibleClaims, visibleResolved,
} from "./PersonaClaims";
import type { PersonaClaim, ResolvedClaim } from "@/lib/relay";

describe("visibleClaims", () => {
  it("keeps real claims in stored order — newest last, as the engines write them", () => {
    const claims: PersonaClaim[] = [
      { topic: "current location", claim: "estoy acá en Chile", at: "2026-07-25T19:00:00" },
      { topic: "nationality", claim: "soy argentina de verdad", at: "2026-07-25T19:46:42" },
      { topic: "city", claim: "en córdoba", at: "2026-07-25T20:44:39" },
    ];
    expect(visibleClaims(claims).map((c) => c.claim)).toEqual([
      "estoy acá en Chile", "soy argentina de verdad", "en córdoba",
    ]);
  });

  it("drops rows with no usable claim text", () => {
    // A topic with an empty claim tells the operator nothing and only makes the
    // real rows harder to scan.
    expect(visibleClaims([
      { topic: "job", claim: "" },
      { topic: "job", claim: "   " },
      { topic: "city", claim: "en córdoba" },
      {},
    ])).toHaveLength(1);
  });

  it("treats absent/undefined as empty rather than throwing", () => {
    expect(visibleClaims(undefined)).toEqual([]);
    expect(visibleClaims([])).toEqual([]);
  });
});

describe("canonFacts — step 1, what is pinned into every prompt", () => {
  const fields = [
    { key: "age", label: "Age" },
    { key: "job", label: "Job" },
    { key: "height", label: "Height" },
  ];

  it("keeps the contract's order, not the object's — it is 'what fans ask most'", () => {
    const facts = { job: "barista", age: "22", height: "165 cm" };
    expect(canonFacts(facts, fields).map((f) => f.label)).toEqual(["Age", "Job", "Height"]);
  });

  it("shows only filled slots", () => {
    // A blank slot is not something she was told to say — it is a gap she will
    // improvise into, and that improvisation shows up in the per-fan ledger.
    // Listing every empty row would bury the ones that are actually set.
    expect(canonFacts({ age: "22", job: "", height: "   " }, fields)
      .map((f) => f.key)).toEqual(["age"]);
  });

  it("survives an unconfigured model without throwing", () => {
    expect(canonFacts(undefined, fields)).toEqual([]);
    expect(canonFacts({ age: "22" }, undefined)).toEqual([]);
  });
});

describe("visibleResolved", () => {
  it("keeps the server's merge and order — it is the prompt's own line-up", () => {
    // Deliberately NOT re-sorted or re-merged here: the override rule lives in
    // _persona.resolved_claims, and a second implementation is what made the
    // browser and the server disagree about media stills.
    const rows: ResolvedClaim[] = [
      { label: "where you are", value: "en rosario", column: "persona_location_claimed", at: null },
      { label: "nationality", value: "soy argentina", column: null, at: "2026-07-25T19:46:42" },
    ];
    expect(visibleResolved(rows).map((r) => r.label)).toEqual(["where you are", "nationality"]);
  });

  it("drops valueless rows and survives an absent field", () => {
    expect(visibleResolved([
      { label: "job", value: "  ", column: "persona_job_claimed", at: null },
    ])).toEqual([]);
    expect(visibleResolved(undefined)).toEqual([]);
  });
});

describe("supersededClaims", () => {
  it("shows only what is NO LONGER the current answer", () => {
    // She said Chile, then Córdoba. Córdoba is live; Chile is history. Printing
    // Córdoba in both lists would just be the same sentence twice.
    const claims: PersonaClaim[] = [
      { topic: "current location", claim: "estoy acá en Chile", at: "2026-07-25T19:00:00" },
      { topic: "city", claim: "en córdoba", at: "2026-07-25T20:44:39" },
    ];
    const resolved: ResolvedClaim[] = [
      { label: "where you are", value: "en córdoba", column: "persona_location_claimed", at: null },
    ];
    expect(supersededClaims(claims, resolved).map((c) => c.claim))
      .toEqual(["estoy acá en Chile"]);
  });

  it("is empty when every claim is still current", () => {
    const claims: PersonaClaim[] = [{ topic: "city", claim: "en córdoba", at: "1" }];
    const resolved: ResolvedClaim[] = [
      { label: "city", value: "en córdoba", column: null, at: "1" },
    ];
    expect(supersededClaims(claims, resolved)).toEqual([]);
  });
});

describe("claimDate", () => {
  it("renders a real timestamp as a date", () => {
    expect(claimDate("2026-07-25T20:44:39")).not.toBe("");
  });

  it("renders nothing for junk instead of 'Invalid Date'", () => {
    // The column is free-text on the server; a bad value must degrade to blank,
    // never paint "Invalid Date" into the drawer.
    expect(claimDate("not-a-date")).toBe("");
    expect(claimDate("")).toBe("");
    expect(claimDate(undefined)).toBe("");
  });
});
