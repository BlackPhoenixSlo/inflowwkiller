/**
 * lib/ofMedia — the browser half of the still-picking rule.
 *
 * The cases are NOT written here: they come from
 * `service/tests/fixtures/of_media_shapes.json`, the same table the python suite
 * asserts `of_shapes.still_url` against. Two implementations of one rule can't
 * share code across the wire, so they share the fixture — add a payload there and
 * whichever side drifts goes red.
 *
 * Read at runtime rather than imported so the fixture stays where python's test
 * data lives, outside the Next tsconfig's rootDir.
 */
import { readFileSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

import { hasDescribableStill, stillUrl } from "./ofMedia";
import type { OFMedia } from "@/lib/relay";

interface FixtureCase {
  name: string;
  media: Record<string, unknown>;
  still: string | null;
}
const fixture = JSON.parse(
  readFileSync(
    join(__dirname, "..", "..", "service", "tests", "fixtures", "of_media_shapes.json"),
    "utf8",
  ),
) as { sig: string; cases: FixtureCase[] };

/** OF's signed query is long and irrelevant to the decision — appending it to
 *  every url is what proves the extension test reads the PATH, not the string. */
function withSig<T>(value: T): T {
  return JSON.parse(
    JSON.stringify(value)
      .replace(/\.jpg/g, `.jpg${fixture.sig}`)
      .replace(/\.mp4/g, `.mp4${fixture.sig}`)
      .replace(/\.mp3/g, `.mp3${fixture.sig}`),
  ) as T;
}

describe("stillUrl — shared fixture with of_shapes.still_url", () => {
  it("has cases to check", () => {
    expect(fixture.cases.length).toBeGreaterThan(10);
  });

  for (const c of fixture.cases) {
    it(c.name, () => {
      const media = withSig(c.media) as unknown as OFMedia;
      const expected = c.still === null ? null : `${c.still}${fixture.sig}`;
      expect(stillUrl(media)).toBe(expected);
      // …and the button-visibility wrapper must agree with the resolver.
      expect(hasDescribableStill([media])).toBe(expected !== null);
    });
  }
});

describe("hasDescribableStill — bubble-level rollup", () => {
  const readable = { id: 1, files: { preview: { url: "https://cdn/a.jpg" } } } as OFMedia;
  const locked = { id: 2, type: "video", files: { full: { url: "https://cdn/a.mp4" } } } as OFMedia;

  it("offers the read when ANY item in a mixed bubble is readable", () => {
    expect(hasDescribableStill([locked, readable])).toBe(true);
    expect(hasDescribableStill([locked, locked])).toBe(false);
  });

  it("withholds it when there is no media at all", () => {
    expect(hasDescribableStill([])).toBe(false);
    expect(hasDescribableStill(undefined)).toBe(false);
  });
});
