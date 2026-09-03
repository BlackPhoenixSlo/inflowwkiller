/**
 * fanId.ts — who a message is from, and which fan a route means.
 *
 * One module for platform identity because the two platforms disagree about
 * what an id IS: OnlyFans sends numbers, the Fansly shim sends string
 * snowflakes (`of_message` → `"fromUser": {"id": str(senderId)}`). Every bug
 * these functions exist to prevent came from a layer picking one of those and
 * quietly converting the other:
 *
 *   • `Number(id)` — a Fansly snowflake is larger than Number.MAX_SAFE_INTEGER,
 *     so `Number("951404711209099264")` is 951404711209099300, a fan that does
 *     not exist. This is what made "↗ pop out" open an empty thread.
 *   • `a === b` across the split — a string id can never `===` a number one,
 *     so "did we reply?" answered "no" forever and pinned the owe-a-reply dot.
 *   • `a - b` for sorting — subtracting strings is NaN, so id tiebreaks
 *     silently stopped ordering anything.
 *
 * The rule this module encodes: ids are OPAQUE. Compare them as text, keep
 * them in the shape the wire sent, and never do arithmetic on one.
 */

/** A fan's id as the wire actually sends it: numeric on OnlyFans, a string
 *  snowflake on Fansly (too big for float64 — see `fanIdFromParam`). Every
 *  signature that carries a fan id along the chat path uses this, so no layer
 *  can quietly re-narrow it to `number` and reintroduce the rounding bug. */
export type FanId = number | string;

/** Do two ids name the same account, across the number/string split?
 *
 *  Compare through String() on both sides — never `Number()`, which silently
 *  collapses snowflakes past 2^53. A raw `===` between the two forms is ALWAYS
 *  false, which is how `lastMessage.fromUser.id !== Number(accountId)` read
 *  "the fan spoke last" for every Fansly chat. */
export function sameUserId(
  a: FanId | null | undefined,
  b: FanId | null | undefined,
): boolean {
  if (a == null || b == null) return false;
  return String(a) === String(b);
}

/** Order two ids without arithmetic.
 *
 *  `b - a` is the obvious thing and it is wrong: these ids are as often
 *  strings as numbers, and subtracting strings yields NaN — a comparator that
 *  calls everything equal, so any sort using it silently stops sorting. Longest
 *  first so a 19-digit snowflake still ranks above a 9-digit OnlyFans id;
 *  same-length ids compare lexically, which for equal-width digits IS numeric
 *  order. Ascending; for descending call `compareFanIds(b, a)` rather than
 *  negating the result. */
export function compareFanIds(a: FanId | null | undefined, b: FanId | null | undefined): number {
  // PRECONDITION: ids carry no leading zeros. Neither platform mints them —
  // OnlyFans sends integers and Fansly sends snowflake digits — and
  // `fanIdFromParam` is the only parser, so nothing introduces one. This
  // matters because length-first ordering would otherwise disagree with
  // `sameUserId` ("007" vs "7" are the same account but different lengths),
  // and a comparator whose "equal" contradicts the identity predicate is not
  // a total order. Normalizing here instead would be worse: it would make
  // compareFanIds treat as equal two ids sameUserId calls different.
  const sa = a == null ? "" : String(a);
  const sb = b == null ? "" : String(b);
  // Length first so a 19-digit snowflake outranks a 9-digit OnlyFans id;
  // equal-width digit strings compare lexically, which IS numeric order.
  if (sa.length !== sb.length) return sa.length - sb.length;
  return sa.localeCompare(sb);
}

/** Is this a usable account id, whatever shape the wire sent it in?
 *
 *  The validity test for an opaque id — `Number.isFinite` was being used for
 *  this and is wrong twice over: it rejects a perfectly good string snowflake
 *  unless you truncate it first, and it accepts `1e21` and `-5`. An id is a
 *  run of digits that is not all zeros; that is the whole contract. */
export function isValidFanId(v: unknown): v is FanId {
  if (typeof v !== "number" && typeof v !== "string") return false;
  if (typeof v === "number" && !Number.isSafeInteger(v)) return false;
  const s = String(v);
  return /^\d+$/.test(s) && !/^0+$/.test(s);
}

/** A `[fanId]` route param -> the id to key caches and requests on, WITHOUT
 *  losing precision.
 *
 *  Keeps an OnlyFans-sized id a number so its query keys stay identical to the
 *  inbox's (which passes `withUser.id` straight through from JSON), and keeps
 *  anything that would not survive the float64 round trip as the exact string.
 *
 *  Returns null for a param that is not a positive integer id at all, which is
 *  the caller's cue to render "invalid fan id" instead of querying. */
export function fanIdFromParam(param: string | null | undefined): FanId | null {
  const raw = (param ?? "").trim();
  // At least one non-zero digit: "0" and "000" parse as integers but are not
  // ids, and callers rely on this to reject them (parseSlotsParam used to
  // carry its own `fanId <= 0` guard for exactly this).
  if (!isValidFanId(raw)) return null;
  const n = Number(raw);
  // The round trip is the test: if String(Number(raw)) !== raw the value does
  // not fit float64 exactly, so the string is the only faithful form.
  return Number.isSafeInteger(n) && String(n) === raw ? n : raw;
}

/** The inbox's "ball is in our court" rule, in ONE place: the thread's newest
 *  message came from someone other than us, so it is unanswered.
 *
 *  Both the yellow row dot and the "Owe reply" chip read this, and the relay's
 *  roster badge folds the SAME rule server-side (`_roster_counts_from_rows`) —
 *  they must never disagree about a row. */
export function lastMessageFromFan(
  chat: { lastMessage?: { fromUser?: { id?: FanId } } | null } | null | undefined,
  accountId: string | null | undefined,
): boolean {
  const from = chat?.lastMessage?.fromUser?.id;
  if (from == null) return false;
  return !sameUserId(from, accountId);
}
