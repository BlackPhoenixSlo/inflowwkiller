/**
 * MoneyRail's persistence layer: every localStorage key the rail owns, the
 * persisted shapes, and their read/write/normalize pairs. No React here —
 * the component ([MoneyRail.tsx]) renders; this module remembers.
 *
 * The bell (NotificationBell) also writes rail settings (its "show the rail
 * here" restore button), which is why the settings live behind SETTINGS_EVENT
 * instead of component state.
 */

import { persistJSON, readJSON } from "./persist";

// ── Settings (rows + which models) ───────────────────────────────────────
// Deliberately NOT per-surface, unlike position: "watch 6 rows, for these
// models" is a preference about the work, not about this window's layout.
export const SETTINGS_KEY = "chatterly:money-rail:settings:v1";
/** 0 = collapsed shows ONLY the header bar — for chatters who want the dock
 *  reachable but zero rows of it on screen until they expand. */
export const SMALL_ROW_CHOICES = [0, 1, 2, 3, 4] as const;
export const BIG_ROW_CHOICES = [5, 6, 7, 8] as const;

/** The canonical surface universe — exported so the ⚙ picker derives its
 *  checkboxes from THIS list (a fifth surface added here is a compile error
 *  at the picker's label map, not a silently missing checkbox). */
export const SURFACE_VALUES = ["inbox", "popup", "group", "other"] as const;

/** The rail is mounted once in the root layout, but the same dock in the
 *  inbox, a chat pop-out, the group tab and everything else are different
 *  working surfaces — a spot that's out of the way on one covers something on
 *  another. So position (and open/collapsed) is persisted per surface, not
 *  globally. "other" is every non-chat page (Setup, Automations, Stats, …):
 *  one bucket, because none of them is a chatting surface — it exists so the
 *  rail can be kept on the chat surfaces but off the admin ones. */
export type Surface = (typeof SURFACE_VALUES)[number];

export function surfaceOf(pathname: string | null): Surface {
  if (pathname?.startsWith("/group")) return "group";
  if (pathname?.startsWith("/chat/")) return "popup";
  if (pathname?.startsWith("/inbox")) return "inbox";
  return "other";
}

export interface RailSettings {
  /** Rows visible while collapsed. */
  smallRows: number;
  /** Rows visible while expanded. */
  bigRows: number;
  /** Accounts to pull money notifications for. null = ALL active accounts —
   *  which is the default ON PURPOSE, and independent of the ScopeContext the
   *  rest of the UI follows: a chatter scoped to one model still wants to see
   *  the money landing on the others. */
  accounts: string[] | null;
  /** Surfaces the rail renders on. null = everywhere. Lets a chatter keep it
   *  on the chat surfaces but off Setup/Automations/Stats etc. ("other"), or
   *  any combination, instead of the old all-or-nothing hide. */
  surfaces: Surface[] | null;
}

export const DEFAULT_SETTINGS: RailSettings = {
  smallRows: 1,
  bigRows: 6,
  accounts: null,
  // House default: the chat surfaces only. On Setup/Automations/Stats pages
  // the rail is clutter over the forms — anyone who wants it there ticks
  // "Other pages" in ⚙.
  surfaces: ["inbox", "popup", "group"],
};

function clampChoice(v: unknown, choices: readonly number[], fallback: number): number {
  return typeof v === "number" && choices.includes(v) ? v : fallback;
}

/** Empty and complete picks both normalise to null ("everywhere") — a rail
 *  hidden on EVERY surface, whose only un-hide control lives inside the rail
 *  itself, would be gone for good. */
export function parseSurfaces(v: unknown): Surface[] | null {
  if (!Array.isArray(v)) return null;
  const picked = SURFACE_VALUES.filter((s) => v.includes(s));
  return picked.length > 0 && picked.length < SURFACE_VALUES.length ? picked : null;
}

export function railVisibleOn(settings: RailSettings, surface: Surface): boolean {
  return !settings.surfaces || settings.surfaces.includes(surface);
}

function normalizeSettings(p: Partial<RailSettings>): RailSettings {
  return {
    smallRows: clampChoice(p.smallRows, SMALL_ROW_CHOICES, DEFAULT_SETTINGS.smallRows),
    bigRows: clampChoice(p.bigRows, BIG_ROW_CHOICES, DEFAULT_SETTINGS.bigRows),
    // An EMPTY array would mean "no models", i.e. a permanently blank rail —
    // never a thing the user meant. Treat it as "all", same as null.
    accounts:
      Array.isArray(p.accounts) && p.accounts.length > 0
        ? p.accounts.filter((a): a is string => typeof a === "string")
        : null,
    // Three states on disk: an array = that pick; an explicit null = the
    // user chose "everywhere"; MISSING = a save from before surfaces
    // existed (or junk) → the house default, NOT "everywhere".
    surfaces:
      p.surfaces === null
        ? null
        : Array.isArray(p.surfaces)
          ? parseSurfaces(p.surfaces)
          : DEFAULT_SETTINGS.surfaces,
  };
}

/** A write that couldn't land in localStorage (full origin). Since
 *  writeSettings queues a SETTINGS_EVENT re-read, a silently-failed write
 *  used to snap every ⚙ pick straight back to the saved value. Held here so
 *  the pick still wins in this tab; cleared as soon as any write lands. (If
 *  another tab saves while this is set, this tab keeps its own pick —
 *  acceptable for a degraded mode.) */
let quotaFallback: RailSettings | null = null;

/** Test-only: module state would otherwise leak between quota tests. */
export function resetSettingsQuotaFallback(): void {
  quotaFallback = null;
}

export function readSettings(): RailSettings {
  if (quotaFallback) return normalizeSettings(quotaFallback);
  const p = readJSON<Partial<RailSettings>>(SETTINGS_KEY);
  return p ? normalizeSettings(p) : DEFAULT_SETTINGS;
}

export const SETTINGS_EVENT = "chatterly:money-rail:settings";

export function writeSettings(s: RailSettings): void {
  quotaFallback = persistJSON(SETTINGS_KEY, s) ? null : s;
  // Same-tab siblings (the bell's "show it here" restore button lives in a
  // different component) hear this and re-read; popout tabs get the native
  // storage event. Deferred so a subscriber's setState never fires inside
  // the caller's render/commit.
  try {
    queueMicrotask(() => {
      try { window.dispatchEvent(new CustomEvent(SETTINGS_EVENT)); } catch { /* ignore */ }
    });
  } catch { /* ignore */ }
}

/** Which accounts the rail actually queries. Defaults to every active account
 *  (NOT the current scope); a saved pick is intersected with what's live, so a
 *  removed model can't leave the rail permanently empty. */
export function resolveRailAccounts(
  settings: RailSettings,
  active: readonly { id: string }[],
): string[] {
  const activeIds = active.map((a) => a.id);
  if (!settings.accounts) return activeIds;
  const picked = activeIds.filter((id) => settings.accounts!.includes(id));
  return picked.length > 0 ? picked : activeIds;
}

// ── Dock position (per surface) ──────────────────────────────────────────

export interface DockState {
  open: boolean;
  /** Distance from the left edge. null (with y) = default bottom-right corner. */
  x: number | null;
  /** Distance from whichever edge `anchor` names. Growing the panel moves the
   *  OTHER edge, so the anchored one stays put. */
  y: number | null;
  /** Which horizontal edge the panel is pinned to. Chosen from where it was
   *  dropped: in the bottom half of the screen it pins to the BOTTOM, so
   *  expanding grows upward instead of shoving the panel off-screen. In the
   *  top half it pins to the top and grows downward. */
  anchor: "top" | "bottom";
}

export const DEFAULT_DOCK: DockState = { open: false, x: null, y: null, anchor: "bottom" };

function stateKey(surface: Surface): string {
  return `chatterly:money-rail:v2:${surface}`;
}

export function readDock(surface: Surface): DockState {
  const p = readJSON<Partial<DockState>>(stateKey(surface));
  if (!p) return DEFAULT_DOCK;
  return {
    open: p.open === true,
    x: typeof p.x === "number" && Number.isFinite(p.x) ? p.x : null,
    y: typeof p.y === "number" && Number.isFinite(p.y) ? p.y : null,
    // Positions saved before the anchor existed were top-based.
    anchor: p.anchor === "bottom" ? "bottom" : "top",
  };
}

export function writeDock(surface: Surface, s: DockState): void {
  persistJSON(stateKey(surface), s);
}

// ── Cold-start snapshot ──────────────────────────────────────────────────
// A refresh wipes the react-query cache, so without this the rail blanks to
// "Loading…" and rows trickle back in as each account's OF fan-out lands.
// Every successful list fetch is mirrored here; the next mount serves these
// rows as react-query placeholders — visible at once, dimmed, and replaced
// per (account, type) as each real fetch returns. Models revalidate
// independently: a slow account never holds the others' rows hostage.

const SNAPSHOT_KEY = "chatterly:money-rail:snapshot:v1";
// EVICT-BY: fetched-at. Older than this and the rows are pure archaeology.
const SNAPSHOT_TTL_MS = 3 * 24 * 60 * 60 * 1000;
/** Matches the fetch limit — persisting more than one response is waste. */
export const SNAPSHOT_MAX_ITEMS = 20;

export interface NotificationUser {
  id?: number;
  name?: string;
  username?: string;
  avatar?: string;
}
export interface NotificationItem {
  id?: number | string;
  type?: string;
  text?: string;
  description?: string;
  createdAt?: string;
  user?: NotificationUser;
  fromUser?: NotificationUser;
  replacePairs?: Record<string, string>;
  [k: string]: unknown;
}

export interface SnapshotEntry {
  /** dataUpdatedAt of the fetch these rows came from — doubles as the
   *  dirty-check so an unchanged response isn't re-serialised. */
  at: number;
  items: NotificationItem[];
}
export type SnapshotMap = Record<string, SnapshotEntry>;

export function snapshotKeyOf(aid: string, type: string): string {
  return `${aid}:${type}`;
}

function slimUser(u?: NotificationUser): NotificationUser | undefined {
  if (!u) return undefined;
  return { id: u.id, name: u.name, username: u.username, avatar: u.avatar };
}

/** Strip a notification to the fields the rail renders — raw OF rows drag
 *  along subscription/media payload we'd be persisting for nothing. */
export function slimNotif(n: NotificationItem): NotificationItem {
  return {
    id: n.id,
    type: n.type,
    text: n.text,
    description: n.description,
    createdAt: n.createdAt,
    user: slimUser(n.user),
    fromUser: slimUser(n.fromUser),
    replacePairs: n.replacePairs,
  };
}

export function readMoneySnapshot(): SnapshotMap {
  const parsed = readJSON<SnapshotMap>(SNAPSHOT_KEY);
  if (!parsed || typeof parsed !== "object") return {};
  const cutoff = Date.now() - SNAPSHOT_TTL_MS;
  const out: SnapshotMap = {};
  for (const [k, v] of Object.entries(parsed)) {
    if (!v || typeof v.at !== "number" || !Array.isArray(v.items)) continue;
    if (v.at < cutoff) continue;
    out[k] = { at: v.at, items: v.items.slice(0, SNAPSHOT_MAX_ITEMS) };
  }
  return out;
}

export function writeMoneySnapshot(map: SnapshotMap): void {
  persistJSON(SNAPSHOT_KEY, map);
}

// ── Unseen-sales watermark ───────────────────────────────────────────────
// The header shows how many money rows arrived since the user last had the
// rail's rows on screen — the count that makes bar-only collapse workable.
// "Seen" = the rows were visibly rendered (any size with ≥1 row); while the
// bar-only dock or a hidden surface accumulates, the number climbs. The
// watermark is a timestamp in localStorage: global across surfaces and tabs
// (money you saw in the inbox is not "new" again in the popout), and it
// survives refresh so the cold-start snapshot doesn't all count as new.

export const SEEN_KEY = "chatterly:money-rail:seen:v1";

export function readSeenTs(): number {
  const v = readJSON<unknown>(SEEN_KEY);
  return typeof v === "number" && Number.isFinite(v) && v > 0 ? v : 0;
}

export function writeSeenTs(ts: number): void {
  persistJSON(SEEN_KEY, ts);
}

export function unseenCountOf(items: readonly { ts: number }[], seenTs: number): number {
  let n = 0;
  for (const it of items) if (it.ts > seenTs) n++;
  return n;
}
