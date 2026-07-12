"use client";

/**
 * groupChannel — coordination layer for the /group tab.
 *
 * The "👥 group" button in chat headers needs to:
 *   • Push a slot into a live /group tab without navigating it.
 *   • Detect if the fan is already in ANY live group tab and just
 *     focus that specific one (no duplicate adds across tabs).
 *   • Pick a tab with room when more than one is alive.
 *   • Spawn a fresh tab only when no live tab has room.
 *
 * Mechanism:
 *   • Each /group tab gets a unique tabId (sessionStorage-persisted) and
 *     writes its own localStorage entry under `chatterly:group-tab:<tabId>`
 *     with {createdAt, heartbeat, slots}. Cleared on unmount/pagehide.
 *   • A source tab scans all live entries with a fresh heartbeat to find
 *     which (if any) already has this fan; if not, picks the most
 *     recently active tab that still has room.
 *   • BroadcastChannel messages carry the target tabId — only the
 *     addressed tab acts on them. Other group tabs ignore.
 *
 * Race handling:
 *   • Cold-start race (two source tabs simultaneously see live=∅):
 *     pre-spawn defer-and-recheck (50ms) gives a competing spawner time
 *     to register so we route to it instead. /group reconciles on mount
 *     by createdAt as a secondary safety net.
 *   • Overflow drop (target tab fills between snapshot and broadcast):
 *     adds carry a reqId; the target acks {ok:true} on accept or
 *     {ok:false, reason:"full"} on reject. Source awaits ACK with a
 *     short timeout and spawns an overflow tab on nack/timeout.
 *   • Frozen-tab unfreeze: /group runs a duplicate-reconcile pass on
 *     visibilitychange when it detects its own heartbeat went stale —
 *     drops slots a younger live tab has also taken on.
 *   • Cross-target focus: source tracks groupRef.tabId via a `claim`
 *     message from the spawned /group tab. Focus only uses the held
 *     Window ref when its tabId matches the routing target; otherwise
 *     falls back to a broadcast-focus.
 */

export const GROUP_CHANNEL_NAME = "chatterly-group";
/** Set on the /group page's `window.name`. Used to identify when a
 *  caller of openGroupTab is itself a group tab (defensive: never
 *  re-enter via window.open from inside the group). */
export const GROUP_WINDOW_NAME = "chatterly-group-tab";
export const GROUP_HEARTBEAT_MS = 2000;
export const GROUP_HEARTBEAT_TTL_MS = 6000;
/** Per-tab registry key prefix. Each /group tab writes
 *  `chatterly:group-tab:<tabId>` with its current state. */
export const GROUP_TAB_KEY_PREFIX = "chatterly:group-tab:";
/** Mirrors the cap in app/group/page.tsx. Source tabs need to know
 *  this independently so they can overflow into a fresh tab when all
 *  alive tabs are at cap. */
export const GROUP_SLOT_CAP = 8;
/** How long to wait for another spawner to register before we spawn
 *  ourselves. Long enough to absorb a near-simultaneous cold-start
 *  race; short enough to not feel laggy. */
const SPAWN_DEFER_MS = 60;
/** How long to wait for an add-ack from the target tab before treating
 *  the add as failed and spawning an overflow tab. Generous enough to
 *  cover BroadcastChannel + a state-set round trip on a busy renderer. */
const ADD_ACK_TIMEOUT_MS = 400;

export interface GroupSlot { accountId: string; fanId: number }

export interface GroupTabRegistry {
  tabId: string;
  /** When this tab first registered. Used as a deterministic tiebreaker
   *  when two tabs end up holding the same fan — the younger one yields. */
  createdAt: number;
  heartbeat: number;
  slots: GroupSlot[];
}

export type GroupChannelMessage =
  | { type: "add"; tabId: string; reqId: string; accountId: string; fanId: number }
  | { type: "add-ack"; tabId: string; reqId: string; ok: boolean }
  /** Replace the target tab's ENTIRE slot list (money rail's "open last 8
   *  buyers"). Unlike `add`, this is destructive by design: clicking the
   *  rail button again re-seeds the same tab with the newest 8 rather than
   *  appending or spawning a second tab. Acked with `add-ack`. */
  | { type: "replace"; tabId: string; reqId: string; slots: GroupSlot[] }
  | { type: "focus"; tabId: string }
  | { type: "claim"; tabId: string; spawnToken: string };

/** Same-tab replace. BroadcastChannel does NOT deliver to the posting
 *  context, so when the money rail is clicked from inside the /group tab
 *  itself (the rail is in the root layout, so it renders there too) we hand
 *  the slots over via a DOM event instead. */
export const GROUP_REPLACE_EVENT = "chatterly:group-replace";

/** Scan localStorage for all live /group tab registry entries (heartbeat
 *  within TTL). Stale or malformed entries are skipped. */
export function readAllLiveGroupTabs(): GroupTabRegistry[] {
  if (typeof window === "undefined") return [];
  const out: GroupTabRegistry[] = [];
  try {
    const now = Date.now();
    for (let i = 0; i < window.localStorage.length; i++) {
      const key = window.localStorage.key(i);
      if (!key || !key.startsWith(GROUP_TAB_KEY_PREFIX)) continue;
      const raw = window.localStorage.getItem(key);
      if (!raw) continue;
      try {
        const parsed = JSON.parse(raw) as Partial<GroupTabRegistry>;
        if (
          typeof parsed.tabId !== "string" ||
          typeof parsed.heartbeat !== "number" ||
          !Array.isArray(parsed.slots)
        ) {
          continue;
        }
        if (now - parsed.heartbeat > GROUP_HEARTBEAT_TTL_MS) continue;
        const slots = parsed.slots.filter((s): s is GroupSlot =>
          !!s && typeof s === "object" &&
          typeof (s as GroupSlot).accountId === "string" &&
          typeof (s as GroupSlot).fanId === "number",
        );
        const createdAt = typeof parsed.createdAt === "number" && parsed.createdAt > 0
          ? parsed.createdAt
          : parsed.heartbeat;
        out.push({ tabId: parsed.tabId, createdAt, heartbeat: parsed.heartbeat, slots });
      } catch { /* skip malformed entry */ }
    }
  } catch { /* localStorage unavailable */ }
  return out;
}

export function writeGroupTabRegistry(tabId: string, createdAt: number, slots: GroupSlot[]): void {
  if (typeof window === "undefined") return;
  try {
    const r: GroupTabRegistry = { tabId, createdAt, heartbeat: Date.now(), slots };
    window.localStorage.setItem(GROUP_TAB_KEY_PREFIX + tabId, JSON.stringify(r));
  } catch { /* quota / private mode */ }
}

export function removeGroupTabRegistry(tabId: string): void {
  if (typeof window === "undefined") return;
  try { window.localStorage.removeItem(GROUP_TAB_KEY_PREFIX + tabId); } catch {}
}

/** Module-level Window ref to the /group tab we opened FROM this
 *  source tab. tabId is resolved once the spawned tab broadcasts its
 *  `claim` matching our spawnToken — only after that pairing is known
 *  do we use the ref for focus, otherwise we fall back to broadcast. */
let groupRef: { tabId: string | null; spawnToken: string | null; win: Window } | null = null;

/** Long-lived BroadcastChannel for the SOURCE side. Receives add-ack
 *  responses and claim announcements. Kept open so per-click handlers
 *  don't have to race against channel construction. */
let sourceChannel: BroadcastChannel | null = null;
const pendingAcks = new Map<string, (ok: boolean) => void>();

function ensureSourceChannel(): BroadcastChannel | null {
  if (typeof window === "undefined") return null;
  if (sourceChannel) return sourceChannel;
  try {
    sourceChannel = new BroadcastChannel(GROUP_CHANNEL_NAME);
    sourceChannel.onmessage = (e: MessageEvent<GroupChannelMessage>) => {
      const msg = e.data;
      if (!msg) return;
      if (msg.type === "add-ack") {
        const resolver = pendingAcks.get(msg.reqId);
        if (resolver) {
          pendingAcks.delete(msg.reqId);
          resolver(!!msg.ok);
        }
        return;
      }
      if (msg.type === "claim") {
        // Bind the held window ref to its tabId once the spawned tab
        // identifies itself. Only the source tab whose spawnToken matches
        // accepts the claim — multiple source tabs can be claiming
        // different spawns at once.
        if (groupRef && groupRef.spawnToken && groupRef.spawnToken === msg.spawnToken) {
          groupRef.tabId = msg.tabId;
        }
        return;
      }
    };
  } catch {
    sourceChannel = null;
  }
  return sourceChannel;
}

function broadcast(msg: GroupChannelMessage): void {
  const ch = ensureSourceChannel();
  if (!ch) return;
  try { ch.postMessage(msg); } catch { /* ignore */ }
}

export type OpenGroupResult =
  | { kind: "broadcast" }   // pushed a fresh slot into a live tab
  | { kind: "focused" }     // fan was already in some live tab — focused it
  | { kind: "opened" }      // spawned a fresh tab (cold or all-full overflow)
  | { kind: "self" };       // caller was already inside /group

function tryFocusGroupTab(tabId: string): void {
  // Same-BCG Window ref is the reliable focus path, but only safe when
  // we KNOW the ref belongs to the requested target tab. Otherwise we'd
  // focus the wrong group tab. Falls back to broadcast `focus` — the
  // target tab calls window.focus() on itself (may be denied by browser
  // policy when not driven by a recent user gesture; in that case the
  // user alt-tabs manually).
  if (groupRef && !groupRef.win.closed && groupRef.tabId === tabId) {
    try { groupRef.win.focus(); } catch { /* policy denial */ }
  }
  broadcast({ type: "focus", tabId });
}

/** Wait for `add-ack` matching reqId, or timeout. Returns true if the
 *  target accepted the add, false otherwise (rejected or timed out). */
function waitForAddAck(reqId: string): Promise<boolean> {
  return new Promise<boolean>((resolve) => {
    pendingAcks.set(reqId, resolve);
    window.setTimeout(() => {
      if (pendingAcks.delete(reqId)) resolve(false);
    }, ADD_ACK_TIMEOUT_MS);
  });
}

function genReqId(): string {
  return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
}

function spawnGroupTab(accountId: string, fanId: number, overflow: boolean): void {
  // spawnToken lets us pair the held Window ref with its eventual tabId
  // when the spawned tab broadcasts a `claim`. Without this we'd focus
  // the wrong tab on overflow.
  const spawnToken = genReqId();
  const url = `/group?add=${encodeURIComponent(accountId)}:${fanId}&spawn=${encodeURIComponent(spawnToken)}`;
  // Ensure source channel exists BEFORE the spawned tab boots — otherwise
  // its `claim` broadcast may land before we're listening.
  ensureSourceChannel();
  if (overflow) {
    // Existing groupRef points at a different (full) tab — keep it for
    // now but allow the new tab to take over once it claims.
    const win = window.open(url, "_blank");
    if (win) groupRef = { tabId: null, spawnToken, win };
  } else {
    const win = window.open(url, GROUP_WINDOW_NAME);
    if (win) groupRef = { tabId: null, spawnToken, win };
  }
}

function encodeSlots(slots: GroupSlot[]): string {
  return slots.map((s) => `${encodeURIComponent(s.accountId)}:${s.fanId}`).join(",");
}

/** Parse the `?slots=acct:fan,acct:fan` seed. Exported so /group and its
 *  tests share one parser with the writer above. */
export function parseSlotsParam(raw: string): GroupSlot[] {
  const out: GroupSlot[] = [];
  for (const part of raw.split(",")) {
    const idx = part.lastIndexOf(":");
    if (idx <= 0) continue;
    const accountId = decodeURIComponent(part.slice(0, idx));
    const fanId = Number(part.slice(idx + 1));
    if (!accountId || !Number.isFinite(fanId) || fanId <= 0) continue;
    if (out.some((s) => s.accountId === accountId && s.fanId === fanId)) continue;
    out.push({ accountId, fanId });
  }
  return out.slice(0, GROUP_SLOT_CAP);
}

/**
 * Open the group tab seeded with an exact slot list, replacing whatever
 * it currently holds. Used by the money rail's "open the last 8 buyers"
 * button, where the point is a snapshot of who just paid — so a second
 * click must REFRESH the existing tab, not append to it or spawn another.
 *
 *   • A live group tab exists  → broadcast `replace` to the freshest one
 *                                and focus it. On nack/timeout, spawn.
 *   • We ARE the group tab     → hand the slots over via a DOM event
 *                                (BroadcastChannel skips the sender).
 *   • No live tab              → spawn one seeded with `?slots=`.
 *
 * Never navigates the calling tab.
 */
export async function openGroupTabWithSlots(slots: GroupSlot[]): Promise<OpenGroupResult> {
  if (typeof window === "undefined") return { kind: "opened" };
  const wanted = slots.slice(0, GROUP_SLOT_CAP);
  if (wanted.length === 0) return { kind: "self" };

  if (window.location?.pathname === "/group") {
    window.dispatchEvent(new CustomEvent(GROUP_REPLACE_EVENT, { detail: { slots: wanted } }));
    return { kind: "self" };
  }

  const live = readAllLiveGroupTabs().sort((a, b) => b.heartbeat - a.heartbeat);
  const target = live[0];
  if (target) {
    const reqId = genReqId();
    const ackPromise = waitForAddAck(reqId);
    broadcast({ type: "replace", tabId: target.tabId, reqId, slots: wanted });
    tryFocusGroupTab(target.tabId);
    if (await ackPromise) return { kind: "broadcast" };
    // Tab died between the heartbeat scan and the broadcast.
  }

  const spawnToken = genReqId();
  ensureSourceChannel();
  const url = `/group?slots=${encodeSlots(wanted)}&spawn=${encodeURIComponent(spawnToken)}`;
  const win = window.open(url, GROUP_WINDOW_NAME);
  if (win) groupRef = { tabId: null, spawnToken, win };
  return { kind: "opened" };
}

/**
 * Add a fan to a live /group tab, focus the one that already has it,
 * or open a fresh group tab if none can accept. Never navigates the
 * source tab.
 *
 * Routing across multiple live group tabs:
 *   1. If the fan is already in ANY live tab → focus that tab.
 *   2. Else pick the live tab with the freshest heartbeat that still
 *      has room → broadcast an add, await ack, spawn overflow on
 *      rejection/timeout.
 *   3. Else (no live tab, or all alive ones are full) → spawn a new
 *      tab seeded with just this fan. On cold-start, defer briefly so
 *      a competing source tab's spawn has time to register.
 */
export async function openGroupTab(accountId: string, fanId: number): Promise<OpenGroupResult> {
  if (typeof window === "undefined") return { kind: "opened" };

  // Defensive re-entry guard: if the caller is itself inside /group.
  // Keyed off pathname only — window.name persists across page reloads
  // in the same tab, so a tab that ever loaded /group keeps the
  // GROUP_WINDOW_NAME stamp forever and would false-positive here even
  // after navigating to /inbox.
  if (typeof window.location !== "undefined" &&
      window.location.pathname === "/group") {
    return { kind: "self" };
  }

  const live = readAllLiveGroupTabs();

  // 1. Already in some live tab? Focus that specific one.
  const owner = live.find((t) =>
    t.slots.some((s) => s.accountId === accountId && s.fanId === fanId),
  );
  if (owner) {
    tryFocusGroupTab(owner.tabId);
    return { kind: "focused" };
  }

  // 2. Find a tab with room — prefer the most recently active.
  const withRoom = live
    .filter((t) => t.slots.length < GROUP_SLOT_CAP)
    .sort((a, b) => b.heartbeat - a.heartbeat);
  const target = withRoom[0];
  if (target) {
    const reqId = genReqId();
    const ackPromise = waitForAddAck(reqId);
    broadcast({ type: "add", tabId: target.tabId, reqId, accountId, fanId });
    tryFocusGroupTab(target.tabId);
    const accepted = await ackPromise;
    if (accepted) return { kind: "broadcast" };
    // Target filled / dead / unreachable since the snapshot — overflow.
    spawnGroupTab(accountId, fanId, /*overflow*/ true);
    return { kind: "opened" };
  }

  // 3. Cold-start path. Defer briefly to absorb a concurrent spawn from
  //    another source tab; if one shows up during the wait, route to it.
  if (live.length === 0) {
    await new Promise<void>((r) => window.setTimeout(r, SPAWN_DEFER_MS));
    const recheck = readAllLiveGroupTabs();
    if (recheck.length > 0) {
      // Another source beat us. Re-enter normal routing.
      return openGroupTab(accountId, fanId);
    }
  }

  spawnGroupTab(accountId, fanId, /*overflow*/ live.length > 0);
  return { kind: "opened" };
}
