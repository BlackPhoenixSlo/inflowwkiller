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
