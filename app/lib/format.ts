/**
 * format.ts — shared formatters for the stats surface.
 *
 * Lives outside `cn`/utils because these are tiny number/date primitives
 * that 3+ components share verbatim. Behavior matches the original
 * inline copies in PerModelKpiGrid / PerEmployeeTable / OrphanTipsCard.
 */

/** Cents → "$1.23" / "$1,234". `null` / `undefined` / `NaN` → "—". */
export function fmtCents(c: number | null | undefined): string {
  if (c == null || Number.isNaN(c)) return "—";
  const d = c / 100;
  return d >= 1000
    ? `$${d.toLocaleString(undefined, { maximumFractionDigits: 0 })}`
    : `$${d.toFixed(2)}`;
}

/** Same as fmtCents but renders 0 as "—" — matches the PerEmployeeTable
 *  "no data" convention where empty cells stay quiet. */
export function fmtCentsBlankZero(c: number | null | undefined): string {
  if (c == null || c === 0 || Number.isNaN(c)) return "—";
  return fmtCents(c);
}

export function fmtInt(n: number | null | undefined): string {
  if (n == null || Number.isNaN(n)) return "—";
  return n.toLocaleString();
}

/** Seconds → "9:59". Absent / zero / negative → "" so a caller can render the
 *  pill only when there is a duration to show (`dur && …`, `dur || "▶"`).
 *
 *  This was three identical private copies — VaultPicker's `fmtDur`,
 *  VaultTile's `fmtDuration`, and a fourth written for the composer's
 *  overlong-video warning — before they were collapsed here. */
export function fmtDuration(sec: number | null | undefined): string {
  if (!sec || sec <= 0) return "";
  const m = Math.floor(sec / 60);
  const s = Math.floor(sec % 60);
  return `${m}:${String(s).padStart(2, "0")}`;
}

/** Automation cadence: seconds → "every 5 min" / "every 2 h" / "every 90 s".
 *  `null` / 0 → "—", which is what a rule on a `daily_at` trigger (no interval at
 *  all) renders as — the schedule then lives in `trigger.daily_at`, so inventing
 *  an interval here would print a cadence the rule does not run on.
 *
 *  Was private to AutomationsPanel until a second automation surface needed it. */
export function fmtEvery(secs: number | null | undefined): string {
  if (!secs || secs <= 0) return "—";
  if (secs % 3600 === 0) return `every ${secs / 3600} h`;
  if (secs % 60 === 0) return `every ${secs / 60} min`;
  return `every ${secs} s`;
}

/** Past ISO timestamp → "12s ago" / "5 min ago" / "3 h ago" / "2 d ago".
 *  Absent / unparseable → `null` (render nothing); a FUTURE stamp → "soon".
 *
 *  The third copy of this was being written when it moved here — AutomationsPanel
 *  and ReadyMadePanel each carried a byte-identical private `timeAgo`. Same story
 *  as `fmtDuration` above. (The `${h}h ago` family in MoneyRail / NotificationBell
 *  / IngestHealthBanner is a DIFFERENT wording with a 30-day cutoff — deliberately
 *  left alone rather than silently restyled.) */
export function fmtAgo(iso: string | null | undefined): string | null {
  if (!iso) return null;
  const then = new Date(iso).getTime();
  if (!Number.isFinite(then)) return null;
  const secs = Math.round((Date.now() - then) / 1000);
  if (secs < 0) return "soon";
  if (secs < 60) return `${secs}s ago`;
  const mins = Math.round(secs / 60);
  if (mins < 60) return `${mins} min ago`;
  const hrs = Math.round(mins / 60);
  if (hrs < 24) return `${hrs} h ago`;
  return `${Math.round(hrs / 24)} d ago`;
}

export function fmtDateShort(iso: string): string {
  return new Date(iso).toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

export function fmtDateTime(iso: string | null): string {
  if (!iso) return "—";
  return new Date(iso).toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

/** "HH:MM" in the user's locale — used for "data through 13:45" captions. */
export function fmtTimeOnly(iso: string | null): string {
  if (!iso) return "—";
  return new Date(iso).toLocaleTimeString(undefined, {
    hour: "2-digit",
    minute: "2-digit",
  });
}
