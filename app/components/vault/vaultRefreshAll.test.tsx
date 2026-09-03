/**
 * `refreshAll()` must invalidate the GRID, not just the chrome around it.
 *
 * Adding media to a Fansly album succeeded upstream and the folder rail's
 * count went to 2, but the tiles underneath kept showing the pre-add page —
 * the one thing the user actually looks at was the one thing that never
 * refetched. The cause: refreshAll invalidated "vault-mirror-items",
 * "vault-lists" and "vault-cache-summary", but the grid reads
 * ["vault-media", accountId, type, listId, sort, query] and that key was
 * absent. It reads as fixed (counts move!) while the content is stale.
 *
 * These pin the property that matters — after refreshAll, a seeded grid entry
 * is invalidated — for a numeric OnlyFans list id AND a Fansly string
 * snowflake, and for an album OTHER than the one on screen.
 */
import { QueryClient } from "@tanstack/react-query";
import { describe, expect, it } from "vitest";

import type { FanId } from "@/lib/fanId";

const AID = "789937824869654528";
const FANSLY_ALBUM: FanId = "951605470987055104"; // the real "asdf" album
const OF_LIST: FanId = 29271410;

/** Exactly what the component's refreshAll does. */
function refreshAll(qc: QueryClient, accountId: string) {
  qc.invalidateQueries({ queryKey: ["vault-mirror-items", accountId] });
  qc.invalidateQueries({ queryKey: ["vault-lists", accountId] });
  qc.invalidateQueries({ queryKey: ["vault-cache-summary", accountId] });
  qc.invalidateQueries({ queryKey: ["vault-media", accountId] });
}

/** A grid page cached under the full key useVaultMedia builds. */
function seedGrid(qc: QueryClient, listId: FanId | null) {
  qc.setQueryData(["vault-media", AID, "all", listId, "newest", ""], { list: [] });
}

const isInvalidated = (qc: QueryClient, key: unknown[]) =>
  qc.getQueryCache().find({ queryKey: key })?.state.isInvalidated ?? false;

describe("vault refreshAll", () => {
  it("invalidates the grid for a Fansly album snowflake", () => {
    const qc = new QueryClient();
    seedGrid(qc, FANSLY_ALBUM);
    refreshAll(qc, AID);
    expect(isInvalidated(qc, ["vault-media", AID, "all", FANSLY_ALBUM, "newest", ""])).toBe(true);
  });

  it("invalidates the grid for a numeric OnlyFans list id", () => {
    const qc = new QueryClient();
    seedGrid(qc, OF_LIST);
    refreshAll(qc, AID);
    expect(isInvalidated(qc, ["vault-media", AID, "all", OF_LIST, "newest", ""])).toBe(true);
  });

  it("also refreshes an album that is NOT the one on screen", () => {
    // You add to "asdf" while viewing the unfiltered vault; both must refetch,
    // or opening the folder afterwards shows the stale page.
    const qc = new QueryClient();
    seedGrid(qc, null);
    seedGrid(qc, FANSLY_ALBUM);
    refreshAll(qc, AID);
    expect(isInvalidated(qc, ["vault-media", AID, "all", null, "newest", ""])).toBe(true);
    expect(isInvalidated(qc, ["vault-media", AID, "all", FANSLY_ALBUM, "newest", ""])).toBe(true);
  });

  it("proves the OLD refreshAll left the grid stale", () => {
    const qc = new QueryClient();
    seedGrid(qc, FANSLY_ALBUM);
    // The pre-fix body: everything except the grid's own key.
    qc.invalidateQueries({ queryKey: ["vault-mirror-items", AID] });
    qc.invalidateQueries({ queryKey: ["vault-lists", AID] });
    qc.invalidateQueries({ queryKey: ["vault-cache-summary", AID] });
    expect(isInvalidated(qc, ["vault-media", AID, "all", FANSLY_ALBUM, "newest", ""])).toBe(false);
  });
});
