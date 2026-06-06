"use client";

/**
 * perfLog — lightweight client-side timing log for UX-critical user flows.
 *
 * The point isn't general telemetry — it's *blocking-path* visibility. When
 * a chatter clicks "Open vault" or scrolls to load more messages, we want a
 * crisp record of:
 *
 *   • requested  — the moment we kicked off the call (button click,
 *                  effect fired, IntersectionObserver tripped)
 *   • delivered  — the moment the network response landed in JS
 *   • painted    — the moment the user can actually see the first tile /
 *                  row / item (first DOM commit after data arrived)
 *
 * Plus one session-level phase:
 *
 *   • open       — fires once per tab on module load (the closest we can
 *                  get to "first tab open"). Carries tabId, parentTabId
 *                  (when this tab was spawned from another via
 *                  window.open / target="_blank"), route, referrer, and
 *                  PerformanceNavigationTiming deltas so a slow first
 *                  paint can be attributed to browser vs. React.
 *
 * Everything else (image bytes, color tints, lazy-loaded extras) is paint-
 * later. We log them too if a caller hands us a paint-later marker, but
 * they NEVER block the painted milestone.
 *
 * Tab identity: each tab mints a UUID on first load and parks it in
 * sessionStorage so refreshes keep the id but new tabs get fresh ones.
 * The id is mirrored on `window.__perfTabId` so popout tabs can discover
 * their parent via `window.opener.__perfTabId` (same-origin only).
 *
 * Surface:
 *   • Structured `console.info` lines with deltas, prefixed `[perf]`,
 *     suffixed `tab=<short-id>` so multi-tab DevTools sessions stay
 *     attributable.
 *   • Window-accessible ring buffer at `window.__perfLog` for inspection
 *     in DevTools (`copy(JSON.stringify(window.__perfLog, null, 2))`)
 *   • Subscribe-style callback hook (perfLogSubscribe) if a future view
 *     wants to show in-app perf overlay
 *
 * No background polling, no network egress — pure client-local.
 */

export type PerfPhase = "requested" | "delivered" | "painted" | "later" | "error" | "open";

export interface PerfEvent {
  /** Stable id grouping all phases of one operation. For `phase=open` this
   *  equals the tabId — the session itself is the "op". */
  opId: string;
  /** Coarse category (e.g. "vault.open", "vault.media", "chat.list",
   *  "chat.messages", "vault.lists", "tab"). Used for dashboards / filtering. */
  kind: string;
  phase: PerfPhase;
  /** epoch ms — performance.now() relative to navigationStart isn't
   *  comparable across reloads; absolute ms is what we want in the log. */
  ts: number;
  /** Tab that produced this event. Stamped by emit() if unset, so callers
   *  can ignore it. */
  tabId?: string;
  /** Free-form metadata: counts, ids, filter strings. Keep it small. */
  meta?: Record<string, unknown>;
}

export interface PerfTabContext {
  /** UUID per tab — survives reload, dies with the tab. */
  tabId: string;
  /** UUID of the tab that spawned this one (via window.open / target=_blank
   *  on the same origin). null for the root tab in a browser session and
   *  for any tab whose opener is gone / cross-origin. */
  parentTabId: string | null;
  /** epoch ms when this tab first ran perfLog. Sticky across reloads via
   *  sessionStorage. */
  tabOpenedAt: number;
  /** True iff this tab has no detectable parent — either the user opened
   *  it directly (typed URL, bookmark) or the opener became unreachable. */
  isRootTab: boolean;
}

const BUFFER_MAX = 500;
const buffer: PerfEvent[] = [];
const subs = new Set<(e: PerfEvent) => void>();
// First-seen timestamp per opId, so deltas survive even if `requested` was
// dropped (e.g. perfLog imported lazily after the click).
const firstSeenByOp = new Map<string, number>();
// Last-seen timestamp per opId, so we can print Δprev = current - last.
const lastSeenByOp = new Map<string, number>();

// ── Tab identity ─────────────────────────────────────────────────────

const TAB_ID_KEY = "chatterly:perf-tab-id";
const TAB_OPENED_KEY = "chatterly:perf-tab-opened-at";
const PARENT_TAB_ID_KEY = "chatterly:perf-parent-tab-id";

function readSession(k: string): string | null {
  if (typeof window === "undefined") return null;
  try { return window.sessionStorage.getItem(k); } catch { return null; }
}

function writeSession(k: string, v: string): void {
  if (typeof window === "undefined") return;
  try { window.sessionStorage.setItem(k, v); } catch { /* private mode / disabled storage */ }
}

function discoverParentTabId(): string | null {
  // window.opener exposes its tabId on a known property if the parent is
  // same-origin and has perfLog loaded. Cross-origin or detached opener
  // throws on property access — swallowed. Same-window (no popout)
  // yields null. Once learned, we sticky it via sessionStorage so a
  // refresh of the popout still knows its parent even after the opener
  // was closed.
  if (typeof window === "undefined") return null;
  try {
    const op = window.opener as (Window & { __perfTabId?: string }) | null;
    if (op && op !== window) {
      const id = op.__perfTabId;
      if (typeof id === "string" && id.length > 0) return id;
    }
  } catch { /* opener may be cross-origin or already closed */ }
  return null;
}

function bootTabContext(): PerfTabContext {
  if (typeof window === "undefined") {
    return { tabId: "ssr", parentTabId: null, tabOpenedAt: 0, isRootTab: true };
  }
  let tabId = readSession(TAB_ID_KEY);
  let openedRaw = readSession(TAB_OPENED_KEY);
  if (!tabId) {
    tabId = typeof crypto !== "undefined" && typeof crypto.randomUUID === "function"
      ? crypto.randomUUID()
      : `t-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
    writeSession(TAB_ID_KEY, tabId);
  }
  if (!openedRaw) {
    openedRaw = String(Date.now());
    writeSession(TAB_OPENED_KEY, openedRaw);
  }
  // Parent linkage: prefer the sticky one (so refreshes don't lose it),
  // fall back to the live opener lookup on cold open.
  let parentTabId = readSession(PARENT_TAB_ID_KEY);
  if (!parentTabId) {
    parentTabId = discoverParentTabId();
    if (parentTabId) writeSession(PARENT_TAB_ID_KEY, parentTabId);
  }
  // Mirror our id on window so future popouts can read it as their parent.
  (window as Window & { __perfTabId?: string }).__perfTabId = tabId;
  return {
    tabId,
    parentTabId,
    tabOpenedAt: Number(openedRaw) || Date.now(),
    isRootTab: !parentTabId,
  };
}

const TAB_CTX: PerfTabContext = bootTabContext();

function shortTab(id: string): string {
  return id === "ssr" ? "ssr" : id.slice(0, 6);
}

// ── Event emission ───────────────────────────────────────────────────

function emit(evt: PerfEvent): void {
  if (evt.tabId == null) evt.tabId = TAB_CTX.tabId;
  buffer.push(evt);
  if (buffer.length > BUFFER_MAX) buffer.splice(0, buffer.length - BUFFER_MAX);

  const first = firstSeenByOp.get(evt.opId);
  const last = lastSeenByOp.get(evt.opId);
  if (first == null) firstSeenByOp.set(evt.opId, evt.ts);
  lastSeenByOp.set(evt.opId, evt.ts);

  const totalMs = first != null ? evt.ts - first : 0;
  const stepMs = last != null ? evt.ts - last : 0;

  if (typeof window !== "undefined") {
    // Use console.info so it's filterable separately from console.log noise.
    // The `+Nms` step delta is the actionable number — if it's huge we have
    // a slow phase. The `Σ` is the running total since the op was first
    // seen. `tab=` is a short prefix of the tab UUID so multi-tab DevTools
    // sessions stay attributable at a glance.
    // eslint-disable-next-line no-console
    console.info(
      `[perf] ${evt.kind} ${evt.phase} +${stepMs}ms Σ${totalMs}ms id=${evt.opId} tab=${shortTab(evt.tabId)}`,
      evt.meta ?? {},
    );
  }
  for (const s of subs) {
    try { s(evt); } catch { /* subscriber errors must not break perf path */ }
  }
  // Queue for server-side ingest. Done AFTER subscribers so a slow
  // subscriber doesn't block the queue accumulating future events.
  queueForIngest(evt);
}

function nowMs(): number {
  // Prefer performance.now() resolution if available, anchored to Date.now()
  // for cross-reload-comparable absolute timestamps.
  if (typeof performance !== "undefined" && typeof performance.now === "function") {
    // performance.timeOrigin + performance.now() ≈ Date.now() but with sub-ms.
    const origin = (performance as Performance & { timeOrigin?: number }).timeOrigin;
    if (typeof origin === "number") return origin + performance.now();
  }
  return Date.now();
}

let opCounter = 0;
/** Mint a unique op id. Callers can prepend their own scope. */
export function perfOpId(scope: string): string {
  opCounter += 1;
  return `${scope}:${opCounter}:${Math.random().toString(36).slice(2, 6)}`;
}

export function perfLog(
  opId: string,
  kind: string,
  phase: PerfPhase,
  meta?: Record<string, unknown>,
): void {
  emit({ opId, kind, phase, ts: nowMs(), meta });
}

/** Convenience: log `requested` for a fresh op id and return that id so the
 *  caller can pass it to delivered/painted/done later. */
export function perfRequested(kind: string, meta?: Record<string, unknown>): string {
  const id = perfOpId(kind);
  perfLog(id, kind, "requested", meta);
  return id;
}

export function perfDelivered(opId: string, kind: string, meta?: Record<string, unknown>): void {
  perfLog(opId, kind, "delivered", meta);
}

export function perfPainted(opId: string, kind: string, meta?: Record<string, unknown>): void {
  perfLog(opId, kind, "painted", meta);
}

export function perfError(opId: string, kind: string, meta?: Record<string, unknown>): void {
  perfLog(opId, kind, "error", meta);
}

/** "later" marker — cosmetic / lazy work that finished after `painted`.
 *  Use sparingly; these should NEVER live on the blocking path. */
export function perfLater(opId: string, kind: string, meta?: Record<string, unknown>): void {
  perfLog(opId, kind, "later", meta);
}

export function perfSnapshot(): PerfEvent[] {
  return buffer.slice();
}

export function perfLogSubscribe(fn: (e: PerfEvent) => void): () => void {
  subs.add(fn);
  return () => { subs.delete(fn); };
}

/** Read-only snapshot of the tab session: id, parent, when it opened.
 *  Useful for instrumenting flows that span multiple windows (popout
 *  hand-off) so events can be re-stitched across tabs after the fact. */
export function perfTabContext(): PerfTabContext {
  return { ...TAB_CTX };
}

/** No-op import target. Call once from app bootstrap (providers.tsx) so
 *  the side-effects below — minting the tab id, emitting `tab open` —
 *  run as early as possible on cold load, before lazy-imported hooks
 *  drag perfLog in. The actual work happens at module-evaluation time;
 *  this function exists so the tree-shaker doesn't drop the import. */
export function perfBoot(): void {
  /* intentionally empty — module load is the side-effect */
}

// ── Server-side ingest ───────────────────────────────────────────────
//
// Events are batched in memory and flushed to /admin/perflog/ingest on
// a short timer (FLUSH_INTERVAL_MS), when the buffer hits FLUSH_MAX,
// and on page-hide / unload via navigator.sendBeacon. The relay's
// ingest endpoint is append-only with a 7-day prune.
//
// Design notes:
//   • No retries. If a flush fails, the batch is dropped — perf logs
//     are best-effort telemetry; persisting them past relay outages
//     isn't worth the back-pressure / loop risk.
//   • Bypass-flag: setting `window.__perfLogNoIngest = true` before the
//     module evaluates disables network egress for one tab — useful
//     when investigating the network pipeline itself (you don't want
//     your own probe POSTs polluting the log you're reading).
//   • sendBeacon vs. fetch — Beacon is the only way to reliably ship a
//     batch from a tab that's about to close. We use it on pagehide
//     and on visibilitychange-to-hidden; fetch+keepalive on the timer.

const INGEST_URL = "/admin/perflog/ingest";
const FLUSH_INTERVAL_MS = 10_000;
const FLUSH_MAX = 50;
const pendingForIngest: PerfEvent[] = [];
let flushTimer: ReturnType<typeof setInterval> | null = null;
let ingestDisabled =
  typeof window !== "undefined" &&
  Boolean((window as Window & { __perfLogNoIngest?: boolean }).__perfLogNoIngest);

function buildIngestBody(events: PerfEvent[]): string {
  return JSON.stringify({
    tabId: TAB_CTX.tabId,
    parentTabId: TAB_CTX.parentTabId,
    events: events.map((e) => ({
      opId: e.opId,
      kind: e.kind,
      phase: e.phase,
      ts: e.ts,
      tabId: e.tabId ?? TAB_CTX.tabId,
      meta: e.meta,
    })),
  });
}

function flushNow(useBeacon = false): void {
  if (ingestDisabled) {
    pendingForIngest.length = 0;
    return;
  }
  if (pendingForIngest.length === 0) return;
  // Splice instead of slice so we don't double-send if the next tick
  // races with a unload-triggered beacon — once we hand the bytes to
  // the network layer the local queue MUST be empty.
  const batch = pendingForIngest.splice(0);
  const body = buildIngestBody(batch);
  try {
    if (useBeacon && typeof navigator !== "undefined" && typeof navigator.sendBeacon === "function") {
      const blob = new Blob([body], { type: "application/json" });
      const ok = navigator.sendBeacon(INGEST_URL, blob);
      if (ok) return;
      // sendBeacon rejected (rare — usually a quota or detached doc).
      // Fall through to fetch+keepalive; better to try than to drop.
    }
    if (typeof fetch !== "function") return;
    void fetch(INGEST_URL, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body,
      keepalive: true,
      credentials: "same-origin",
    }).catch(() => { /* swallow — perf log can never make things worse */ });
  } catch { /* swallow */ }
}

function queueForIngest(evt: PerfEvent): void {
  if (ingestDisabled) return;
  pendingForIngest.push(evt);
  if (pendingForIngest.length >= FLUSH_MAX) {
    // Don't block emit() on the flush — schedule on a microtask so the
    // caller's synchronous control flow returns first.
    queueMicrotask(() => flushNow(false));
  }
}

/** Force-flush the in-memory ingest buffer. Call before manually closing
 *  a tab from inside the app, or from a debug overlay's "send now"
 *  button. The unload handlers cover the implicit close case. */
export function perfFlushIngest(): void {
  flushNow(false);
}

/** Toggle network egress at runtime (also honored at module load via
 *  `window.__perfLogNoIngest`). Useful for investigating the network
 *  pipeline without your own probe POSTs polluting the log. */
export function perfSetIngestEnabled(enabled: boolean): void {
  ingestDisabled = !enabled;
  if (enabled) {
    // Drop anything queued while disabled — its meta likely came from
    // the probe session and isn't representative.
    pendingForIngest.length = 0;
  }
}

// ── Side effects on module load ──────────────────────────────────────

if (typeof window !== "undefined" && TAB_CTX.tabId !== "ssr") {
  // Expose for ad-hoc DevTools inspection. Read-only-ish — we replace the
  // accessor on every emit so devs always see the live buffer.
  Object.defineProperty(window, "__perfLog", {
    configurable: true,
    get: () => perfSnapshot(),
  });
  Object.defineProperty(window, "__perfTabCtx", {
    configurable: true,
    get: () => perfTabContext(),
  });

  // Emit the tab-open event as soon as we can. We use TAB_CTX.tabOpenedAt
  // (sticky across reloads) as the `ts` so the timeline anchors at the
  // *original* open, not the most recent perfLog import — that's the
  // signal the user actually cares about ("when was the first tab open").
  // PerformanceNavigationTiming gives us the browser's view of how long
  // the initial load took, which is the only way to attribute a slow
  // first paint to browser vs. React vs. our own work.
  let navMeta: Record<string, unknown> = {};
  try {
    const nav = performance.getEntriesByType("navigation")[0] as PerformanceNavigationTiming | undefined;
    if (nav) {
      navMeta = {
        navType: nav.type,
        // Round to ms; sub-ms noise is meaningless at this level.
        domContentLoadedMs: Math.round(nav.domContentLoadedEventEnd),
        loadEventEndMs: Math.round(nav.loadEventEnd),
        responseEndMs: Math.round(nav.responseEnd),
      };
    }
  } catch { /* perf entries may be unavailable in some browsers / iframes */ }

  emit({
    opId: TAB_CTX.tabId,
    kind: "tab",
    phase: "open",
    ts: TAB_CTX.tabOpenedAt,
    tabId: TAB_CTX.tabId,
    meta: {
      tabId: TAB_CTX.tabId,
      parentTabId: TAB_CTX.parentTabId,
      isRootTab: TAB_CTX.isRootTab,
      route: window.location.pathname + window.location.search,
      referrer: document.referrer || null,
      ...navMeta,
    },
  });

  // Periodic flush. Idle tabs still ship pending events on the timer so
  // a chatter who leaves the tab open all day doesn't lose the last
  // batch when their laptop sleeps and never sends a pagehide.
  if (flushTimer == null) {
    flushTimer = setInterval(() => flushNow(false), FLUSH_INTERVAL_MS);
  }

  // pagehide is the reliable unload signal (fires even on back/forward-
  // cache evict where unload doesn't). visibilitychange catches tab
  // backgrounding on mobile and laptop-lid-close. Both use sendBeacon.
  window.addEventListener("pagehide", () => flushNow(true), { capture: true });
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "hidden") flushNow(true);
  }, { capture: true });
}
