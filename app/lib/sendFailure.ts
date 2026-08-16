/**
 * sendFailure — why a chat send bounced, and which attachment to blame.
 *
 * OF's rejection is precise and we were reading only the vague half of it:
 *
 *   {"error":{"code":0,
 *             "message":"Something wrong with attached media, please try to
 *                        upload it again",
 *             "payload":{"removeFromInputMediaIds":[3468787295]}},
 *    "errors":{"mediaFiles":["Something wrong with attached media…"]}}
 *
 * The sentence says "media", plural and anonymous; `removeFromInputMediaIds`
 * names the ONE item to drop. On 2026-08-16 that gap cost one live account five
 * sends in thirteen minutes — the same refused item went into three separate
 * bundles ($200, $200, $144) because nothing on screen said which of the
 * near-identical tiles OF meant.
 *
 * Both answers come out of ONE parse. The reason and the ids are the same
 * string wrapped the same way — OF's json inside `_proxy`'s
 * `detail.upstream_body` inside RelayError's `body` — so a second reader would
 * be a second place to keep that envelope shape correct.
 *
 * The last export is the same question asked BEFORE the round-trip. Of the five
 * vault items in play that night, OF's own `/vault/media/{id}` describes the
 * refused pair and the accepted trio identically — `canView: true`,
 * `hasError: false`, `isReady: true`, no DRM, both `videoSources` present.
 * Duration is the only field that separates them:
 *
 *      599s  refused        279s  sent
 *      394s  refused         93s  sent
 *                            25s  sent
 *
 * So the ceiling lies somewhere in (279, 394] and 5:00 is the round number
 * inside it. That is an INFERENCE from five points, which is exactly why it
 * warns and never blocks: a hard stop at a guessed threshold would refuse sends
 * OF would have taken, and OF is the only authority on its own limit.
 */

/** What a failed send knows about itself. `reason` is for the operator to read;
 *  `refusedMediaIds` is for the bubble to act on. */
export interface SendFailure {
  /** OF's sentence, or the relay's own pre-flight refusal. */
  reason?: string;
  /** Vault ids OF named in `payload.removeFromInputMediaIds`. */
  refusedMediaIds?: number[];
}

/** Seconds past which a video usually comes back as "Something wrong with
 *  attached media". Advisory — see the module note on why this is not a block. */
export const MESSAGE_VIDEO_LIMIT_S = 300;

/** Everything the relay's error body will tell us about a failed send.
 *
 *  Takes `unknown` because it is fed a RelayError's `body`, which is whatever
 *  the relay answered with; every step is guarded rather than typed. Two
 *  shapes reach here:
 *    • the relay's OWN pre-flight refusal (409 already-owned) — `detail.message`,
 *      no OF round-trip happened and so no ids;
 *    • OF's rejection, re-wrapped by `_proxy` as `detail.upstream_body`.
 *  `_proxy` truncates that string at 2000 chars, so a pathologically long OF
 *  error parses to nothing — the operator still gets `HTTP 400` from the
 *  caller's fallback, just no tile markers. */
export function parseSendFailure(body: unknown): SendFailure {
  const detail = (body as { detail?: { message?: unknown; upstream_body?: unknown } })?.detail;
  // Prefer the relay's own words verbatim; without them the operator sees only
  // "HTTP 409" for a block we chose to apply ourselves.
  const own = detail?.message;
  if (typeof own === "string" && own) return { reason: own };

  const raw = detail?.upstream_body;
  if (typeof raw !== "string") return {};
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    return {};
  }
  const error = (parsed as {
    error?: { message?: unknown; payload?: { removeFromInputMediaIds?: unknown } };
  })?.error;
  const ids = error?.payload?.removeFromInputMediaIds;
  return {
    reason: typeof error?.message === "string" ? error.message : undefined,
    refusedMediaIds: Array.isArray(ids)
      ? ids.map(Number).filter((n) => Number.isFinite(n) && n > 0)
      : undefined,
  };
}

/** True when this attachment is long enough that OF is likely to refuse it.
 *
 *  Only videos carry a duration worth testing; a photo has none and an audio
 *  note is short, so both fall through to false without a type check that would
 *  have to be kept in step with OF's type strings. */
export function isOverlongForMessage(
  media: { duration?: number | null } | null | undefined,
): boolean {
  const d = media?.duration;
  return typeof d === "number" && d > MESSAGE_VIDEO_LIMIT_S;
}
