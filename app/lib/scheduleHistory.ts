/**
 * scheduleHistory — per-scope log of the user's recent schedule picks +
 * a `predictNext()` that projects forward by the learned cadence.
 *
 * Ported from the desktop app's `src/utils/scheduleHistory.ts` so the
 * smart-suggestion chip in the new-post / mass-message composers
 * behaves identically across the two apps.
 *
 * Storage shape (per scope):
 *   chatterly:schedule:history:<scope> -> Array<HistoryEntry>, newest
 *   first, capped at HISTORY_CAP.
 *
 * Scopes: callers pass a stable string — typically the OF account id —
 * so an agency managing several creators keeps each cadence isolated.
 */

const STORAGE_PREFIX = "chatterly:schedule:history:";
const HISTORY_CAP = 8;        // hold extras so deletes don't immediately collapse the prediction
const DELTA_SAMPLE = 3;       // average over up to the last 3 deltas — recent cadence wins
const DAY_MS = 24 * 60 * 60 * 1000;
const MIN_DELTA_MS = 5 * 60 * 1000;     // ignore <5min deltas (accidental double-picks)
const MAX_DELTA_MS = 30 * DAY_MS;       // ignore >30d deltas (stale history)

export interface HistoryEntry {
  /** Local datetime in "YYYY-MM-DDTHH:MM" form — what <input type="datetime-local"> takes. */
  value: string;
  /** Epoch ms when the user clicked submit. */
  savedAt: number;
}

export interface Prediction {
  value: string;          // "YYYY-MM-DDTHH:MM" — pluggable straight into the input
  intervalLabel: string;  // "" when not delta-derived, e.g. "1d 4h" otherwise
  fromDelta: boolean;
}

function key(scope: string | null | undefined): string {
  return STORAGE_PREFIX + (scope || "default");
}

export function loadHistory(scope: string | null | undefined): HistoryEntry[] {
  if (typeof window === "undefined") return [];
  try {
    const raw = window.localStorage.getItem(key(scope));
    if (!raw) return [];
    const parsed = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    return parsed.filter(
      (e): e is HistoryEntry =>
        e && typeof e.value === "string" && e.value.length >= 16 && typeof e.savedAt === "number",
    );
  } catch {
    return [];
  }
}

function saveHistory(scope: string | null | undefined, entries: HistoryEntry[]): void {
  if (typeof window === "undefined") return;
  try {
    // EVICT-BY: created-at — see plan/17_cache_strategy.md §7
    window.localStorage.setItem(key(scope), JSON.stringify(entries.slice(0, HISTORY_CAP)));
  } catch { /* quota — silent */ }
}

/** Push a new pick to the front, dedup against an exact-same most-recent
 *  value (so a double-submit doesn't bloat the list). */
export function recordSchedule(scope: string | null | undefined, value: string): void {
  if (typeof value !== "string" || value.length < 16) return;
  const existing = loadHistory(scope);
  if (existing[0]?.value === value) return;
  existing.unshift({ value, savedAt: Date.now() });
  saveHistory(scope, existing);
}

/** Parse "YYYY-MM-DDTHH:MM[:SS]" as a local-time Date. We deliberately
 *  avoid `new Date(s)` because browsers interpret it as local but some
 *  Node versions read it as UTC — we want consistent local behavior. */
function parseLocal(value: string): Date | null {
  const m = value.match(/^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})(?::(\d{2}))?$/);
  if (!m) return null;
  const [, y, mo, d, h, mi, s] = m;
  const date = new Date(Number(y), Number(mo) - 1, Number(d), Number(h), Number(mi), Number(s || 0));
  return isNaN(date.getTime()) ? null : date;
}

/** Format a Date as "YYYY-MM-DDTHH:MM" in local time — what
 *  <input type="datetime-local"> expects. `toISOString()` would shift
 *  the displayed time when the user's UTC offset isn't zero. */
export function toLocalDatetimeInput(d: Date): string {
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

/** Returns null when there's no history (no chip to show).
 *
 *  With 1 entry, we *infer* the cadence from `value - savedAt` (the gap
 *  between when the user submitted and when they scheduled for). If that
 *  delta is sensible (5min..30d), we use it — so picking "post 6h from
 *  now" produces a chip suggesting "+6h" instead of the desktop app's
 *  daily-only fallback. Outside that range, fall back to +1 day.
 *
 *  With ≥2 entries, average the most recent DELTA_SAMPLE deltas
 *  (filtered to MIN_DELTA_MS..MAX_DELTA_MS) and project last + avg.
 *  Roll forward in multiples of the cadence if the projection is past —
 *  keeps the chip actionable.
 */
export function predictNext(history: HistoryEntry[], now: Date = new Date()): Prediction | null {
  if (history.length === 0) return null;
  const times = history
    .map((e) => parseLocal(e.value))
    .filter((d): d is Date => d !== null);
  if (times.length === 0) return null;
  const last = times[0];

  // 1-entry case: try to infer cadence from the "scheduled at vs saved
  // at" gap before falling back to +1 day. Lets a single 6h-out pick
  // suggest a 6h cadence going forward.
  if (times.length === 1) {
    // |scheduled - saved| — the magnitude of the gap is the cadence; a
    // past-dated pick (negative raw delta) still implies that spacing.
    const inferred = Math.abs(last.getTime() - history[0].savedAt);
    if (inferred >= MIN_DELTA_MS && inferred <= MAX_DELTA_MS) {
      let next = new Date(last.getTime() + inferred);
      while (next.getTime() <= now.getTime()) next = new Date(next.getTime() + inferred);
      return { value: toLocalDatetimeInput(next), intervalLabel: formatInterval(inferred), fromDelta: true };
    }
    let next = new Date(last.getTime() + DAY_MS);
    while (next.getTime() <= now.getTime()) next = new Date(next.getTime() + DAY_MS);
    return { value: toLocalDatetimeInput(next), intervalLabel: "", fromDelta: false };
  }

  const deltas: number[] = [];
  for (let i = 0; i < times.length - 1 && deltas.length < DELTA_SAMPLE; i++) {
    const d = times[i].getTime() - times[i + 1].getTime();
    if (d >= MIN_DELTA_MS && d <= MAX_DELTA_MS) deltas.push(d);
  }
  if (deltas.length === 0) {
    let next = new Date(last.getTime() + DAY_MS);
    while (next.getTime() <= now.getTime()) next = new Date(next.getTime() + DAY_MS);
    return { value: toLocalDatetimeInput(next), intervalLabel: "", fromDelta: false };
  }
  const avg = Math.round(deltas.reduce((s, x) => s + x, 0) / deltas.length);
  let next = new Date(last.getTime() + avg);
  while (next.getTime() <= now.getTime()) next = new Date(next.getTime() + avg);
  return { value: toLocalDatetimeInput(next), intervalLabel: formatInterval(avg), fromDelta: true };
}

/** "1d", "1d 4h", "4h", "30m" — short form for the chip subtitle. Rounds
 *  minutes to the nearest 5 to keep noisy intervals readable. */
function formatInterval(ms: number): string {
  const totalMin = Math.max(1, Math.round(ms / 60000 / 5) * 5);
  const days = Math.floor(totalMin / (60 * 24));
  const hours = Math.floor((totalMin - days * 60 * 24) / 60);
  const mins = totalMin - days * 60 * 24 - hours * 60;
  const parts: string[] = [];
  if (days) parts.push(`${days}d`);
  if (hours) parts.push(`${hours}h`);
  if (!days && mins) parts.push(`${mins}m`);
  return parts.join(" ") || "0m";
}

/** "Tue, May 25 · 9:00 PM" — chip face. Uses the host locale. */
export function describePrediction(value: string): string {
  const d = parseLocal(value);
  if (!d) return value;
  const date = d.toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric" });
  const time = d.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
  return `${date} · ${time}`;
}

/** Convert a "YYYY-MM-DDTHH:MM" local-time string into an ISO string
 *  with the host UTC offset — what the relay's `scheduled_date` field
 *  expects. Returns null if the input fails to parse. */
export function localDatetimeToIso(value: string): string | null {
  const d = parseLocal(value);
  return d ? d.toISOString() : null;
}
