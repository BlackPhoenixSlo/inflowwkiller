/**
 * answerLinks — click paths resolve, and prose does not.
 *
 * Two jobs here. The first is the resolver itself: the shapes the bot actually
 * writes ("Growth → ❤️ Auto-follow", a three-segment Automations path, a label
 * with the sentence running on past it) have to land on the right route with
 * the right slice of text underlined.
 *
 * The second is the DRIFT GUARD at the bottom. answerLinks.ts copies four
 * TABS tables rather than importing four page components into the widget's
 * bundle, so the copies can rot. These cases read the owning source files and
 * assert every key still exists — renaming a tab fails here instead of quietly
 * sending readers to a default tab.
 */
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { describe, expect, it } from "vitest";

import {
  GROWTH_TABS,
  MESSAGES_TABS,
  READY_MADE_TABS,
  SETTINGS_TABS,
  findPaths,
} from "./answerLinks";

/** The single hit in `text`, or null. Fails loudly on more than one. */
function only(text: string) {
  const hits = findPaths(text);
  expect(hits.length).toBeLessThanOrEqual(1);
  if (hits.length === 0) return null;
  return { href: hits[0].href, text: text.slice(hits[0].start, hits[0].end) };
}

describe("findPaths", () => {
  it("resolves a two-segment growth path, emoji and all", () => {
    expect(only("In Growth → ❤️ Auto-follow, set Action.")).toEqual({
      href: "/growth?tab=auto_follow",
      text: "Growth → ❤️ Auto-follow",
    });
  });

  it("resolves a three-segment automations path through its intermediate card", () => {
    expect(
      only("Go to Automations → Ready-made posts & broadcasts → 🗓️ Auto Posts."),
    ).toEqual({
      href: "/automations?ready=auto_posts",
      text: "Automations → Ready-made posts & broadcasts → 🗓️ Auto Posts",
    });
  });

  it("resolves settings tabs whose key differs from their label", () => {
    expect(only("It lives at Settings → Auto stories (owner/admin only).")).toEqual({
      href: "/settings?tab=stories",
      text: "Settings → Auto stories",
    });
  });

  it("stops at the label when the sentence runs on past it", () => {
    // The failure this prevents: underlining "Auto Posts and press + New auto
    // post" and resolving it to nothing.
    expect(
      only("Go to Automations → 🗓️ Auto Posts and press + New auto post"),
    ).toEqual({
      href: "/automations?ready=auto_posts",
      text: "Automations → 🗓️ Auto Posts",
    });
  });

  it("accepts AI Seller as a name for the AI Chatter tab", () => {
    expect(only("Automations → 🤖 AI Seller")?.href).toBe(
      "/automations?ready=ai_seller",
    );
  });

  it("falls back to the section page when the last segment is unknown", () => {
    expect(only("Growth → 🛸 Something We Never Shipped")?.href).toBe("/growth");
  });

  it("links a bare mention of an unambiguous section", () => {
    expect(only("Ask an owner to open Automations for you.")).toEqual({
      href: "/automations",
      text: "Automations",
    });
  });

  it("resolves the customs queue as a tab of Stuff", () => {
    // The queue used to be its own nav entry at /customs. Both the path shape
    // and the bare mention have to land on the tab it became.
    expect(only("Open Stuff → Customs to clear it.")).toEqual({
      href: "/messages?tab=customs",
      text: "Stuff → Customs",
    });
    expect(only("A fan on the Customs queue is withheld from selling.")).toEqual({
      href: "/messages?tab=customs",
      text: "Customs",
    });
  });

  it("still answers to the old name for the Stuff page", () => {
    // The manual is mid-rename and operators say "Messages" out of habit.
    expect(only("Messages → Top posts sorts by comment count.")?.href).toBe(
      "/messages?tab=top",
    );
  });

  it("leaves ordinary prose alone", () => {
    // "growth" lowercase is a word; "Messages"/"Stats" are words that happen to
    // match a nav item, so they only link as the head of an arrow path.
    expect(findPaths("the growth of an account")).toEqual([]);
    expect(findPaths("Messages are stored for 30 days.")).toEqual([]);
    expect(findPaths("Stats update hourly.")).toEqual([]);
  });

  it("finds several paths in one answer without overlapping", () => {
    const text =
      "Use Automations → 👋 Nudge Online for that, or Settings → Scheduled for a one-off.";
    const hits = findPaths(text);
    expect(hits.map((h) => h.href)).toEqual([
      "/automations?ready=nudge_online",
      "/settings?tab=scheduled",
    ]);
    expect(hits[0].end).toBeLessThanOrEqual(hits[1].start);
  });

  it("does not trail a bold marker into the link", () => {
    // AnswerText strips `**` before calling this, but the function is exported
    // and has to be right on its own — a hit ending in "Promotion**" would
    // underline the asterisks the whole change exists to remove.
    expect(only("go to **Growth → 📣 Promotion**. There, set a Name")).toEqual({
      href: "/growth?tab=promotion",
      text: "Growth → 📣 Promotion",
    });
  });

  it("never returns a range outside the string", () => {
    const text = "Growth → ❤️ Auto-follow";
    for (const h of findPaths(text)) {
      expect(h.start).toBeGreaterThanOrEqual(0);
      expect(h.end).toBeLessThanOrEqual(text.length);
      expect(h.end).toBeGreaterThan(h.start);
    }
  });
});

// ── Drift guard ─────────────────────────────────────────────────────

/** Every `key: "..."` inside the TABS array literal of `file`. */
function tabKeysIn(file: string): Set<string> {
  const src = readFileSync(resolve(__dirname, "..", "..", file), "utf-8");
  const start = src.indexOf("const TABS");
  expect(start, `${file} has no TABS array`).toBeGreaterThan(-1);
  const end = src.indexOf("];", start);
  const body = src.slice(start, end);
  const keys = new Set<string>();
  for (const m of body.matchAll(/key:\s*"([^"]+)"/g)) keys.add(m[1]);
  expect(keys.size, `${file} TABS parsed empty`).toBeGreaterThan(0);
  return keys;
}

describe("the copied tab tables still match the pages that own them", () => {
  const cases: Array<[string, Record<string, string>, string]> = [
    ["app/growth/page.tsx", GROWTH_TABS, "GROWTH_TABS"],
    ["app/messages/page.tsx", MESSAGES_TABS, "MESSAGES_TABS"],
    ["app/settings/page.tsx", SETTINGS_TABS, "SETTINGS_TABS"],
    ["components/automations/ReadyMadePanel.tsx", READY_MADE_TABS, "READY_MADE_TABS"],
  ];

  it.each(cases)("%s", (file, table, name) => {
    const real = tabKeysIn(file);
    for (const [label, key] of Object.entries(table)) {
      expect(
        real.has(key),
        `${name}["${label}"] → "${key}" is not a tab in ${file}`,
      ).toBe(true);
    }
  });
});
