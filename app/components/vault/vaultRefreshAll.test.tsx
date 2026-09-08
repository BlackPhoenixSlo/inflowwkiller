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
 *
 * It calls the REAL `invalidateVaultGrid`, and that is deliberate. This suite
 * used to re-type the body of `refreshAll` at the top of the file, which pins a
 * copy of the list rather than the list: the panel could drop a key (it had
 * already dropped one) and every case here would still pass. Importing the
 * shipped function also covers the mirror-derived folder counts and the summary
 * flag that CHOOSES the mirror, neither of which the hand-written copy had.
 */
import { QueryClient } from "@tanstack/react-query";
import { describe, expect, it } from "vitest";

import { invalidateVaultGrid, vaultGridKeys } from "@/hooks/useVaultCache";
import type { FanId } from "@/lib/fanId";

const AID = "789937824869654528";
const FANSLY_ALBUM: FanId = "951605470987055104"; // the real "asdf" album
const OF_LIST: FanId = 29271410;

/** The shipped one, not a transcription of it. */
const refreshAll = invalidateVaultGrid;

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

  it("covers the folder counts and the mirror flag too, not just the grid", () => {
    // The panel's hand-written copy of this list was missing
    // `vault-of-folders-mirror` from the day it was added, so after an
    // add-to-folder the rail's counts came from a stale mirror read. And
    // `vault-cache-summary` is not cosmetic — it is what decides whether the
    // grid reads the mirror at all, so a just-collected vault stays on the
    // live-OF path until something invalidates it.
    const qc = new QueryClient();
    qc.setQueryData(["vault-of-folders-mirror", AID], { list: [] });
    qc.setQueryData(["vault-cache-summary", AID], { count: 0 });
    refreshAll(qc, AID);
    expect(isInvalidated(qc, ["vault-of-folders-mirror", AID])).toBe(true);
    expect(isInvalidated(qc, ["vault-cache-summary", AID])).toBe(true);
  });

  it("names every root the vault surface reads, and each is account-scoped", () => {
    // The `[name, accountId]` shape is load-bearing: index 1 is what keeps one
    // model's invalidation off another model's cache.
    const keys = vaultGridKeys(AID);
    expect(keys.map((k) => k[0])).toEqual([
      "vault-media",
      "vault-lists",
      "vault-mirror-items",
      "vault-cache-summary",
      "vault-of-folders-mirror",
    ]);
    for (const k of keys) expect(k[1]).toBe(AID);
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
