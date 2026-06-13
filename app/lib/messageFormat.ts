/**
 * messageFormat.ts — formatters shared by the /messages row components
 * (MessageRow, MessageRowGeneric, TipRow, TopTippersCard).
 *
 * These used to live as per-file `fmtPrice` / `stripHtml` copies that had
 * drifted apart — large amounts rendered differently between lists. One
 * copy here keeps every dollar figure and body preview identical.
 */

/** Cents → "$1.23"; amounts of $1,000+ drop the decimals and gain a
 *  thousands separator ("$1,234") so big numbers stay readable. */
export function fmtCents(cents: number): string {
  const d = cents / 100;
  return d >= 1000
    ? `$${d.toLocaleString(undefined, { maximumFractionDigits: 0 })}`
    : `$${d.toFixed(2)}`;
}

/** OF bodies arrive wrapped in <p>…</p>; trim the common cases for a denser
 *  preview. A full parser pass isn't worth it for short previews. */
export function stripHtml(s: string): string {
  return s.replace(/<\/?p[^>]*>/gi, "").replace(/<br\s*\/?>/gi, " ").trim();
}
