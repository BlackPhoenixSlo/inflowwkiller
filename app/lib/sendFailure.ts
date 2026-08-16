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
 * WHY the item is refused is still unknown, and one wrong answer is recorded
 * here so it is not re-derived. The five items in play that night split 599s
 * and 394s refused against 279s / 93s / 25s sent, which reads like a duration
 * ceiling near 5:00 — it is not one. `vault_sends` joined to `vault_items`
 * says 132 videos longer than 300s were DELIVERED in 2026-08 alone, the
 * longest 2029s, and 4,540s over the whole table. A pre-send warning built on
 * that inference shipped and was removed the same night.
 *
 * The one asymmetry that survives: both refused items have ZERO `vault_sends`
 * rows — they have never been delivered — while all three accepted ones had
 * been sent before. OF's `/vault/media/{id}` reports the two groups
 * identically (`canView`, `isReady`, `hasError: false`, no DRM, both
 * `videoSources`, same `listStates` shape), so whatever the condition is, the
 * vault API does not expose it and only the send path sees it. Until something
 * distinguishes them, the honest UI is to report what OF said and mark the id
 * it named — never to predict.
 */

/** What a failed send knows about itself. `reason` is for the operator to read;
 *  `refusedMediaIds` is for the bubble to act on. */
export interface SendFailure {
  /** OF's sentence, or the relay's own pre-flight refusal. */
  reason?: string;
  /** Vault ids OF named in `payload.removeFromInputMediaIds`. */
  refusedMediaIds?: number[];
}

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

