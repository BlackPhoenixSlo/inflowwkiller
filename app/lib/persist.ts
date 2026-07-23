/**
 * localStorage JSON round-trip, safe on SSR and a full origin.
 *
 * The react-query persister shares the origin's ~5MB with every feature
 * that saves UI state, so any setItem can throw QuotaExceededError. Writers
 * that need to react (e.g. keep the value in memory) branch on the returned
 * boolean instead of each hand-rolling a try/catch.
 */

/** Write `value` as JSON. Returns false when the write didn't land (SSR,
 *  quota, private-mode restrictions) so the caller can degrade explicitly. */
export function persistJSON(key: string, value: unknown): boolean {
  if (typeof window === "undefined") return false;
  try {
    window.localStorage.setItem(key, JSON.stringify(value));
    return true;
  } catch {
    return false;
  }
}

/** Read and parse `key`. null = missing, unparseable, or SSR. */
export function readJSON<T>(key: string): T | null {
  if (typeof window === "undefined") return null;
  try {
    const raw = window.localStorage.getItem(key);
    return raw === null ? null : (JSON.parse(raw) as T);
  } catch {
    return null;
  }
}
