"use client";

/**
 * errorReporter — central client → server error pipeline.
 *
 * `reportError(err, kind, extra?)` POSTs a row to /admin/errors. Called
 * from:
 *   • the global handlers installed by `installGlobalErrorHandlers()`
 *     (mounted once at the app root via useErrorReporter)
 *   • our React ErrorBoundary's componentDidCatch
 *   • any ad-hoc catch block that wants to record a non-fatal warn
 *
 * Two safety nets so the reporter can never make things worse:
 *   1. Dedupe — identical (kind, message, stack-head) within 30s only
 *      sends once. A render loop that throws on every paint would
 *      otherwise hammer /admin/errors.
 *   2. POST failures are swallowed. We do NOT log them through the
 *      same path (would loop) or even console.error (already too noisy);
 *      a single console.warn keeps the dev tab informed.
 */

import { relay } from "./relay";

interface ReportPayload {
  source: "browser";
  kind: string;
  message: string;
  stack?: string;
  url?: string;
  user_agent?: string;
  context?: Record<string, unknown>;
}

const DEDUPE_WINDOW_MS = 30_000;
const _recent = new Map<string, number>();

function dedupeKey(p: ReportPayload): string {
  const head = (p.stack || "").split("\n").slice(0, 2).join("\n");
  return `${p.kind}::${p.message}::${head}`;
}

export function reportError(
  err: unknown,
  kind = "error",
  extra?: Record<string, unknown>,
): void {
  if (typeof window === "undefined") return;
  let message = "(unknown error)";
  let stack: string | undefined;
  if (err instanceof Error) {
    message = err.message || err.name || message;
    stack = err.stack;
  } else if (typeof err === "string") {
    message = err;
  } else if (err && typeof err === "object") {
    try { message = JSON.stringify(err).slice(0, 2000); } catch { /* keep default */ }
  }

  const payload: ReportPayload = {
    source: "browser",
    kind,
    message: message.slice(0, 4000),
    stack,
    url: window.location.href,
    user_agent: navigator.userAgent,
    context: extra,
  };

  // Last-line defense against feedback loops: never report network-layer
  // failures, since the POST itself goes over the network.
  if (shouldSkip(payload.message) || shouldSkip(payload.stack || "")) return;

  const key = dedupeKey(payload);
  const now = Date.now();
  const prev = _recent.get(key);
  if (prev && now - prev < DEDUPE_WINDOW_MS) return;
  _recent.set(key, now);

  // Fire-and-forget. Don't await — we don't want to slow the page down
  // on unrelated work, and we never want to surface the report's own
  // failure to the user.
  relay.post("/admin/errors", payload).catch((postErr: unknown) => {
    // Single console.warn so the dev tab notices, but DON'T route this
    // through reportError again (would loop on relay outages).
    console.warn("[errorReporter] failed to POST", postErr);
  });
}

// Skip-list of message substrings that should NEVER be reported.
// These are network-layer failures (relay restart, transient fetch
// errors, our own /admin/errors POST failing) where reporting them
// just creates a feedback loop: relay slow → fetch fails → report →
// relay slower → more fails → ...
const _SKIP_PATTERNS = [
  "ECONNRESET",
  "socket hang up",
  "Failed to fetch",
  "Failed to proxy",
  "NetworkError",
  "Load failed",
  "/admin/errors",      // our own endpoint failing → must not loop
];

function shouldSkip(message: string): boolean {
  return _SKIP_PATTERNS.some((p) => message.includes(p));
}

let _installed = false;

/** Idempotent: mounts window-level error + unhandledrejection forwarders
 *  exactly once per page load. Safe to call from multiple components.
 *
 *  Deliberately does NOT patch console.error — that was tried briefly
 *  and created a feedback loop when the relay was under load (every
 *  failed fetch logged via console.error → POST to /admin/errors →
 *  added load → more failed fetches). Window-level errors + unhandled
 *  rejections cover real bugs; transient network errors should stay
 *  in the console and the Network tab where they belong. */
export function installGlobalErrorHandlers(): void {
  if (typeof window === "undefined" || _installed) return;
  _installed = true;

  window.addEventListener("error", (event) => {
    if (!event.error) return;
    const msg = String(event.error?.message ?? event.error);
    if (shouldSkip(msg)) return;
    reportError(event.error, "window-error", {
      filename: event.filename,
      lineno: event.lineno,
      colno: event.colno,
    });
  });

  window.addEventListener("unhandledrejection", (event) => {
    const reason = event.reason;
    const msg = String(reason?.message ?? reason);
    if (shouldSkip(msg)) return;
    reportError(reason, "unhandled-rejection");
  });
}
