/**
 * dateRange.ts — shared date-boundary helpers for the date-range pickers.
 *
 * These three primitives (daysAgoISO / todayISO / toLocalIso) were copied
 * verbatim between app/app/messages/page.tsx and app/app/stats/page.tsx.
 * Two copies of UTC/local day-boundary math drift: a fix in one page
 * silently leaves the other wrong, and the boundary logic is
 * correctness-sensitive (it keeps a Pacific user's "today" aligned with
 * their wall clock). Centralized here so both consumers share one copy.
 */

/** Local YYYY-MM-DD for `n` days ago (picker output format). */
export function daysAgoISO(n: number): string {
  const d = new Date();
  d.setDate(d.getDate() - n);
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, "0");
  const dd = String(d.getDate()).padStart(2, "0");
  return `${y}-${m}-${dd}`;
}

/** Local YYYY-MM-DD for today (picker output format). */
export function todayISO(): string {
  const d = new Date();
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, "0");
  const dd = String(d.getDate()).padStart(2, "0");
  return `${y}-${m}-${dd}`;
}

// Convert a bare YYYY-MM-DD (picker output) into a full UTC-instant ISO
// string with millisecond precision. `end=true` picks 23:59:59.999 local.
// Backend `_parse_date` (stats.py) converts the tzinfo back to UTC; this
// keeps a Pacific user's "today" boundary aligned with their wall clock.
// Anything that already has a time component is passed through.
export function toLocalIso(date: string | null, end: boolean): string | null {
  if (!date) return null;
  if (date.includes("T")) return date;
  const parts = date.split("-").map(Number);
  if (parts.length !== 3 || parts.some((n) => !Number.isFinite(n))) return date;
  const [y, m, d] = parts;
  const local = end
    ? new Date(y, m - 1, d, 23, 59, 59, 999)
    : new Date(y, m - 1, d, 0, 0, 0, 0);
  return local.toISOString();
}
