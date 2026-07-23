import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  SETTINGS_KEY,
  parseSurfaces,
  railVisibleOn,
  readDock,
  readMoneySnapshot,
  readSeenTs,
  readSettings,
  resetSettingsQuotaFallback,
  resolveRailAccounts,
  slimNotif,
  snapshotKeyOf,
  surfaceOf,
  unseenCountOf,
  writeDock,
  writeMoneySnapshot,
  writeSeenTs,
  writeSettings,
} from "./moneyRailStorage";

describe("surfaceOf", () => {
  it("routes each working surface to its own bucket", () => {
    expect(surfaceOf("/inbox")).toBe("inbox");
    expect(surfaceOf("/group")).toBe("group");
    expect(surfaceOf("/chat/5551/999")).toBe("popup");
    expect(surfaceOf("/chat/5551/u/someone")).toBe("popup");
  });

  it("buckets every non-chat page (and no path) as 'other'", () => {
    // Setup/Automations/Stats/… are one bucket: none is a chatting surface,
    // and the bucket exists so the rail can be kept off the admin pages.
    expect(surfaceOf("/")).toBe("other");
    expect(surfaceOf("/stats")).toBe("other");
    expect(surfaceOf("/automations")).toBe("other");
    expect(surfaceOf("/setup")).toBe("other");
    expect(surfaceOf(null)).toBe("other");
  });
});

describe("dock persistence", () => {
  beforeEach(() => window.localStorage.clear());

  it("keeps each surface's position independent", () => {
    writeDock("inbox", { open: true, x: 40, y: 60, anchor: "top" });
    writeDock("group", { open: false, x: 700, y: 12, anchor: "bottom" });

    expect(readDock("inbox")).toEqual({ open: true, x: 40, y: 60, anchor: "top" });
    expect(readDock("group")).toEqual({ open: false, x: 700, y: 12, anchor: "bottom" });
    // Untouched surface stays on the default bottom-right corner.
    expect(readDock("popup")).toEqual({ open: false, x: null, y: null, anchor: "bottom" });
  });

  it("falls back to the default corner on malformed or partial state", () => {
    window.localStorage.setItem("chatterly:money-rail:v2:inbox", "{not json");
    expect(readDock("inbox")).toEqual({ open: false, x: null, y: null, anchor: "bottom" });

    window.localStorage.setItem(
      "chatterly:money-rail:v2:group",
      JSON.stringify({ open: true, x: "left", y: NaN }),
    );
    // A bad coordinate must not pin the panel at NaN — it re-anchors.
    expect(readDock("group")).toMatchObject({ open: true, x: null, y: null });
  });

  it("reads a position saved before anchors existed as top-based", () => {
    window.localStorage.setItem(
      "chatterly:money-rail:v2:inbox",
      JSON.stringify({ open: false, x: 40, y: 60 }),
    );
    // The old y WAS a distance from the top; re-reading it as a bottom offset
    // would teleport the panel across the screen.
    expect(readDock("inbox").anchor).toBe("top");
  });
});

describe("rail settings", () => {
  beforeEach(() => window.localStorage.clear());

  it("defaults to 1 small row, 6 big rows, all models, chat surfaces only", () => {
    expect(readSettings()).toEqual({
      smallRows: 1, bigRows: 6, accounts: null, surfaces: ["inbox", "popup", "group"],
    });
  });

  it("keeps an explicit 'everywhere' (null) distinct from a pre-surfaces save", () => {
    // Explicit null = the user ticked all four surfaces.
    writeSettings({ smallRows: 1, bigRows: 6, accounts: null, surfaces: null });
    expect(readSettings().surfaces).toBeNull();
    // Missing key = a save from before surfaces existed → the house default,
    // NOT everywhere.
    window.localStorage.setItem(
      "chatterly:money-rail:settings:v1",
      JSON.stringify({ smallRows: 2, bigRows: 6, accounts: null }),
    );
    expect(readSettings().surfaces).toEqual(["inbox", "popup", "group"]);
  });

  it("round-trips a saved pick", () => {
    writeSettings({ smallRows: 3, bigRows: 8, accounts: ["a", "b"], surfaces: ["inbox", "group"] });
    expect(readSettings()).toEqual({
      smallRows: 3, bigRows: 8, accounts: ["a", "b"], surfaces: ["inbox", "group"],
    });
  });

  it("accepts 0 small rows — collapsed becomes just the header bar", () => {
    writeSettings({ smallRows: 0, bigRows: 6, accounts: null, surfaces: null });
    expect(readSettings().smallRows).toBe(0);
  });

  it("rejects row counts outside the offered choices", () => {
    // A hand-edited or stale value must not stretch the dock to 900 rows.
    writeSettings({ smallRows: 99, bigRows: 2, accounts: null, surfaces: null } as never);
    expect(readSettings()).toMatchObject({ smallRows: 1, bigRows: 6 });
  });

  it("reads an empty model list as 'all', never as a blank rail", () => {
    window.localStorage.setItem(
      "chatterly:money-rail:settings:v1",
      JSON.stringify({ smallRows: 1, bigRows: 6, accounts: [] }),
    );
    expect(readSettings().accounts).toBeNull();
  });
});

describe("rail settings under a full localStorage", () => {
  // The react-query persister shares the origin's ~5MB with these writes, so
  // setItem can throw QuotaExceededError. The persister now sheds old queries
  // on quota (removeOldestQuery), but a rail write can still land in the
  // window before that trim — and since writeSettings queues a SETTINGS_EVENT
  // re-read, a silently-failed write would snap every ⚙ pick back to the
  // saved value. These tests pin the survival path.
  const quotaError = () => new DOMException("quota", "QuotaExceededError");

  beforeEach(() => window.localStorage.clear());
  afterEach(() => {
    vi.restoreAllMocks();
    resetSettingsQuotaFallback();
    window.localStorage.clear();
  });

  it("keeps the new pick when the write fails — no snap-back to the saved value", async () => {
    writeSettings({ smallRows: 1, bigRows: 6, accounts: null, surfaces: null });
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => { throw quotaError(); });

    writeSettings({ smallRows: 3, bigRows: 8, accounts: ["a"], surfaces: null });
    // Let the queued SETTINGS_EVENT fire — this is the re-read that used to
    // restore the old value.
    await new Promise<void>((r) => queueMicrotask(r));

    expect(readSettings()).toEqual({ smallRows: 3, bigRows: 8, accounts: ["a"], surfaces: null });
  });

  it("hands back to localStorage once a later write lands", () => {
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => { throw quotaError(); });
    writeSettings({ smallRows: 4, bigRows: 5, accounts: null, surfaces: null });
    vi.restoreAllMocks();

    writeSettings({ smallRows: 0, bigRows: 6, accounts: null, surfaces: null });
    expect(readSettings().smallRows).toBe(0);
    expect(JSON.parse(window.localStorage.getItem(SETTINGS_KEY)!).smallRows).toBe(0);
  });
});

describe("surface visibility", () => {
  it("keeps a genuine subset — including the chats-only house default", () => {
    expect(parseSurfaces(["inbox", "group"])).toEqual(["inbox", "group"]);
    expect(parseSurfaces(["popup"])).toEqual(["popup"]);
    expect(parseSurfaces(["inbox", "popup", "group"])).toEqual(["inbox", "popup", "group"]);
  });

  it("normalises 'none' and 'all' to everywhere — a rail hidden on every surface has no way back", () => {
    expect(parseSurfaces([])).toBeNull();
    expect(parseSurfaces(["inbox", "popup", "group", "other"])).toBeNull();
  });

  it("drops junk values instead of persisting them", () => {
    expect(parseSurfaces(["inbox", "sidebar", 42])).toEqual(["inbox"]);
    expect(parseSurfaces("inbox")).toBeNull();
  });

  it("railVisibleOn honours the pick and treats null as everywhere", () => {
    const s = { smallRows: 1, bigRows: 6, accounts: null, surfaces: ["inbox", "group"] as ("inbox" | "group")[] };
    expect(railVisibleOn(s, "inbox")).toBe(true);
    expect(railVisibleOn(s, "group")).toBe(true);
    expect(railVisibleOn(s, "popup")).toBe(false);
    expect(railVisibleOn(s, "other")).toBe(false);
    expect(railVisibleOn({ ...s, surfaces: null }, "other")).toBe(true);
  });

  it("hides the rail on admin pages under the house default", () => {
    window.localStorage.clear();
    expect(railVisibleOn(readSettings(), "other")).toBe(false);
    expect(railVisibleOn(readSettings(), "inbox")).toBe(true);
  });
});

describe("unseen-sales watermark", () => {
  beforeEach(() => window.localStorage.clear());

  it("round-trips and rejects garbage", () => {
    expect(readSeenTs()).toBe(0);
    writeSeenTs(1_700_000_000_000);
    expect(readSeenTs()).toBe(1_700_000_000_000);
    window.localStorage.setItem("chatterly:money-rail:seen:v1", "yesterday");
    expect(readSeenTs()).toBe(0);
  });

  it("counts only rows newer than the watermark", () => {
    const items = [{ ts: 300 }, { ts: 200 }, { ts: 100 }];
    expect(unseenCountOf(items, 0)).toBe(3);
    expect(unseenCountOf(items, 200)).toBe(1);
    expect(unseenCountOf(items, 300)).toBe(0);
    expect(unseenCountOf([], 0)).toBe(0);
  });
});

describe("cold-start snapshot", () => {
  beforeEach(() => window.localStorage.clear());

  const item = (id: number) => ({
    id,
    type: "tip",
    text: `<p>tipped $${id}</p>`,
    createdAt: "2026-07-23T09:00:00Z",
    user: { id: 1, name: "Jim", username: "jim", avatar: "a.jpg" },
  });

  it("round-trips per (account, type)", () => {
    const key = snapshotKeyOf("5551", "tip");
    writeMoneySnapshot({ [key]: { at: Date.now(), items: [item(1), item(2)] } });
    const back = readMoneySnapshot();
    expect(back[key]?.items).toHaveLength(2);
    expect(back[key]?.items[0]).toMatchObject({ id: 1, type: "tip" });
  });

  it("drops entries older than the TTL — last week's money is not 'just now'", () => {
    const old = Date.now() - 4 * 24 * 60 * 60 * 1000;
    writeMoneySnapshot({
      [snapshotKeyOf("5551", "tip")]: { at: old, items: [item(1)] },
      [snapshotKeyOf("5551", "purchases")]: { at: Date.now(), items: [item(2)] },
    });
    const back = readMoneySnapshot();
    expect(back[snapshotKeyOf("5551", "tip")]).toBeUndefined();
    expect(back[snapshotKeyOf("5551", "purchases")]).toBeDefined();
  });

  it("survives malformed storage", () => {
    window.localStorage.setItem("chatterly:money-rail:snapshot:v1", "{not json");
    expect(readMoneySnapshot()).toEqual({});
    window.localStorage.setItem(
      "chatterly:money-rail:snapshot:v1",
      JSON.stringify({ bad: { at: "yesterday", items: "nope" } }),
    );
    expect(readMoneySnapshot()).toEqual({});
  });

  it("slimNotif keeps only what the rail renders", () => {
    const slim = slimNotif({
      ...item(9),
      subscriber: { huge: "payload" },
      canGoToProfile: true,
    });
    expect(slim).toEqual({
      id: 9,
      type: "tip",
      text: "<p>tipped $9</p>",
      description: undefined,
      createdAt: "2026-07-23T09:00:00Z",
      user: { id: 1, name: "Jim", username: "jim", avatar: "a.jpg" },
      fromUser: undefined,
      replacePairs: undefined,
    });
    expect("subscriber" in slim).toBe(false);
  });
});

describe("resolveRailAccounts", () => {
  const active = [{ id: "a" }, { id: "b" }, { id: "c" }];
  const base = { smallRows: 1, bigRows: 6, surfaces: null };

  it("watches every active model by default — regardless of UI scope", () => {
    expect(resolveRailAccounts({ ...base, accounts: null }, active))
      .toEqual(["a", "b", "c"]);
  });

  it("honours a narrowed pick", () => {
    expect(resolveRailAccounts({ ...base, accounts: ["b"] }, active))
      .toEqual(["b"]);
  });

  it("ignores picked models that are no longer active", () => {
    expect(resolveRailAccounts({ ...base, accounts: ["b", "gone"] }, active))
      .toEqual(["b"]);
  });

  it("falls back to all when the pick no longer matches any live model", () => {
    // Otherwise removing a model would leave the rail permanently empty with
    // no hint as to why.
    expect(resolveRailAccounts({ ...base, accounts: ["gone"] }, active))
      .toEqual(["a", "b", "c"]);
  });
});
