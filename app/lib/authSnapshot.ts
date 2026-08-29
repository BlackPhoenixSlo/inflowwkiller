/**
 * authSnapshot — last confirmed /auth/me principal, mirrored to
 * localStorage so a reload doesn't blank the whole app on a network RTT.
 *
 * AuthGate renders nothing while the principal probes are in flight
 * ([AuthGate.tsx] `if (userLoading || chatterLoading) return null`), and
 * UserContext's probe is a bare fetch outside react-query — so unlike the
 * chatter principal (persisted under the `chatter` query prefix) it had no
 * warm-boot path at all. Every reload paid a full round-trip of blank
 * before the persisted query caches underneath were even allowed to mount.
 *
 * The snapshot only ever OPENS the gate early. It is not an auth decision:
 * the live probe still runs on every boot and its answer always wins —
 * same id is a no-op, a different id or a failed probe wipes this and
 * falls through to exactly the state the app would have reached without
 * a snapshot. The relay authorises every request by cookie regardless of
 * what this says, so a stale snapshot costs a wasted render, never access.
 *
 * The `chatterly:` prefix is load-bearing: wipeIdentityStorage() sweeps
 * that namespace on every login / logout / register / impersonation flip,
 * which is what guarantees one principal's snapshot can't survive into
 * another's session.
 */

import { persistJSON, readJSON } from "./persist";

import type { AuthedUserDTO } from "@/contexts/UserContext";

const SNAPSHOT_KEY = "chatterly:auth-snapshot:v1";
/** EVICT-BY: confirmed-at. Past this we'd rather pay the RTT than paint a
 *  principal nobody has re-confirmed in days. */
const SNAPSHOT_TTL_MS = 3 * 24 * 60 * 60 * 1000;

interface AuthSnapshot {
  /** epoch ms of the /auth/me response this came from. */
  at: number;
  user: AuthedUserDTO;
}

/** The last confirmed principal, or null when missing / expired / SSR.
 *  Never returns an impersonation overlay — see writeAuthSnapshot. */
export function readAuthSnapshot(): AuthedUserDTO | null {
  const parsed = readJSON<AuthSnapshot>(SNAPSHOT_KEY);
  if (!parsed || typeof parsed.at !== "number") return null;
  if (parsed.at < Date.now() - SNAPSHOT_TTL_MS) return null;
  const u = parsed.user;
  if (!u || typeof u.user_id !== "string" || !u.user_id) return null;
  // Defence in depth: a snapshot carrying an overlay should never have been
  // written, so treat one as corrupt rather than paint it.
  if (u.impersonating) return null;
  return u;
}

/** Mirror a SERVER-CONFIRMED principal. Callers must not pass synthesized
 *  or optimistic users — a snapshot that was never confirmed would slide
 *  its own TTL forward on every boot and outlive the session it describes.
 *
 *  Impersonation is deliberately not persisted: the overlay is founder-only
 *  and short-lived, and painting a stale one on next boot would misreport
 *  who the founder is acting as until the probe lands. Clearing keeps the
 *  founder on the (correct) blocking path for that one reload. */
export function writeAuthSnapshot(user: AuthedUserDTO | null): void {
  if (!user || !user.user_id || user.impersonating) {
    clearAuthSnapshot();
    return;
  }
  persistJSON(SNAPSHOT_KEY, { at: Date.now(), user } satisfies AuthSnapshot);
}

export function clearAuthSnapshot(): void {
  if (typeof window === "undefined") return;
  try { window.localStorage.removeItem(SNAPSHOT_KEY); } catch { /* private mode */ }
}
