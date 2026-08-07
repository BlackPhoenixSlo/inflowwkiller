import { type ClassValue, clsx } from "clsx";
import { twMerge } from "tailwind-merge";

/**
 * cn() — class merger used by every Tailwind component in this app.
 * Pattern: `cn("base classes", condition && "extras", props.className)`.
 * twMerge resolves conflicts (the *later* wins), so callers can override
 * any base class by passing the same utility with a different value.
 */
export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}

/** OF returns several string fields (chat title, fan name, message body)
 *  HTML-encoded — "&amp; & &lt; &gt; &quot; &#39; &nbsp;" leak straight
 *  into the DOM as literal `&amp;` etc. when rendered as text. Decode the
 *  common entities so chat headers + inbox rows show what humans see on
 *  OF web. */
export function decodeHtmlEntities(s: string | null | undefined): string {
  if (!s) return "";
  return s
    .replace(/&nbsp;/g, " ")
    .replace(/&amp;/g, "&")
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">")
    .replace(/&quot;/g, "\"")
    .replace(/&#39;/g, "'");
}

/** Relative-time formatter that handles BOTH past and future timestamps.
 *  Past → "5m ago", future → "in 5m". Returns "—" for null/invalid input
 *  and falls back to a date string at the 30-day boundary so we don't
 *  render "in 47d" / "182d ago" — those are noisy enough that an absolute
 *  date is more useful. */
export function fmtRelTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  const diffMs = d.getTime() - Date.now();
  const future = diffMs > 0;
  const sec = Math.floor(Math.abs(diffMs) / 1000);
  const fmt = (n: number, unit: string) =>
    future ? `in ${n}${unit}` : `${n}${unit} ago`;
  if (sec < 60) return future ? "in <1m" : "just now";
  const min = Math.floor(sec / 60);
  if (min < 60) return fmt(min, "m");
  const hr = Math.floor(min / 60);
  if (hr < 24) return fmt(hr, "h");
  const day = Math.floor(hr / 24);
  if (day < 30) return fmt(day, "d");
  return d.toLocaleDateString([], { month: "short", day: "numeric" });
}

/** A dead OF session PAUSES every automation for that account until it is
 *  re-captured (service/account_health.py). Map the relay's raw reason onto the
 *  repair the operator actually has to perform — the two reasons need different
 *  ones, and a paused account is otherwise indistinguishable from an idle one.
 *  Shared so the Setup table and the model picker never drift on the wording. */
export function describeDeadSession(reason: string | null | undefined): string {
  if (reason === "wrong_user") {
    return "OnlyFans rejected this login — the creator logged in elsewhere or " +
      "unlinked it. Automations stay paused until it is re-linked in Setup.";
  }
  if (reason === "no_session") {
    return "No captured session for this account on this host. Automations stay " +
      "paused until the session is captured in Setup.";
  }
  return "This account's OnlyFans session is not working. Automations stay " +
    "paused until it is re-captured in Setup.";
}

export interface SubStatusDisplay {
  /** Short label for chips ("Expired", "Won't renew", "Active"…). */
  short: string;
  /** Tailwind tone class so chips can color the status (warn / ok / fg-dim). */
  tone: "ok" | "warn" | "err" | "fg-dim";
  /** Long label for the drawer Stats grid. */
  long: string;
  /** True when the expiry is in the past. */
  expired: boolean;
}

/** Normalize OF's raw `subscribedOnData.status` into something a human can
 *  parse at a glance. OF reports:
 *    • null / "" while we're not subscribed at all
 *    • "active" — sub is on auto-renew
 *    • "Set to Expire" — sub is on but auto-renew was disabled (will end at expiry)
 *    • other free-form strings on rare account states
 *  …combined with `expiredAt` (which can be in the past for already-expired
 *  fans). We collapse to four buckets: active, won't-renew, expired, none. */
export function interpretSubStatus(
  rawStatus: string | null | undefined,
  expiredAtIso: string | null | undefined,
): SubStatusDisplay {
  const expiry = expiredAtIso ? new Date(expiredAtIso) : null;
  const expired = !!(expiry && !Number.isNaN(expiry.getTime()) && expiry.getTime() < Date.now());
  const status = (rawStatus || "").toLowerCase();
  if (expired) {
    return {
      short: "Expired",
      tone: "err",
      long: expiredAtIso ? `Expired ${fmtRelTime(expiredAtIso)}` : "Expired",
      expired: true,
    };
  }
  if (!rawStatus) {
    return { short: "—", tone: "fg-dim", long: "—", expired: false };
  }
  if (status === "active") {
    return {
      short: "Active",
      tone: "ok",
      long: expiredAtIso ? `Active · renews ${fmtRelTime(expiredAtIso)}` : "Active",
      expired: false,
    };
  }
  if (status.includes("expire")) {
    // "set to expire" / "Set to Expire" → renewal off, will lapse at expiry.
    return {
      short: "Won't renew",
      tone: "warn",
      long: expiredAtIso ? `Won't renew · ends ${fmtRelTime(expiredAtIso)}` : "Won't renew",
      expired: false,
    };
  }
  return { short: rawStatus, tone: "fg-dim", long: rawStatus, expired: false };
}
