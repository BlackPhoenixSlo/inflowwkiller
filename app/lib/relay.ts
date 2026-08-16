/**
 * relay.ts — typed fetch wrapper around the Python relay.
 *
 * Auto-injects:
 *   • `X-Employee-Id` from EmployeeContext (so every mutation gets audited)
 *   • `X-Account-Id` from ScopeContext when scope is a single model
 *   • `?t=<share token>` query param on every URL (relay's share-link gate)
 *
 * Returns parsed JSON for 2xx, throws `RelayError` for 4xx/5xx with the
 * upstream body attached. Callers (and React Query) get clean rejection
 * paths instead of having to inspect `.ok` themselves.
 */

const RELAY_BASE = ""; // Next rewrites front the relay; same-origin.

export class RelayError extends Error {
  status: number;
  body: unknown;
  constructor(status: number, body: unknown, message?: string) {
    super(message || `relay ${status}`);
    this.status = status;
    this.body = body;
  }
}

export interface RelayContext {
  shareToken?: string | null;
  employeeId?: number | null;
  accountId?: string | null;
  /** Sent as `X-Priority`. The relay reserves slots in its per-account
   *  upstream concurrency pool for "user" callers — anything tagged
   *  "background" (chat-list enrichment, periodic refresh, prefetch)
   *  may queue behind user-initiated work. Default: "user". */
  priority?: "user" | "background";
}

/**
 * Read the share token from the URL or from a previously-stored localStorage
 * value. Mirrors the existing /ui/'s behavior so a user who pastes
 * `?t=...&...` once doesn't have to repeat it.
 */
export function resolveShareToken(): string | null {
  if (typeof window === "undefined") return null;
  const fromUrl = new URLSearchParams(window.location.search).get("t");
  if (fromUrl) {
    try {
      window.localStorage.setItem("chatterly:share_token", fromUrl);
    } catch {
      /* ignore quota / safari private */
    }
    return fromUrl;
  }
  try {
    return window.localStorage.getItem("chatterly:share_token");
  } catch {
    return null;
  }
}

function buildUrl(path: string, ctx?: RelayContext, opts?: { refresh?: boolean }): string {
  const url = new URL(path, RELAY_BASE || window.location.origin);
  const tok = ctx?.shareToken ?? resolveShareToken();
  if (tok && !url.searchParams.has("t")) url.searchParams.set("t", tok);
  // Stage C backend caches honour `?refresh=1` as a per-request bypass.
  // Refresh-affordances on the frontend (Refresh Inbox, settings refresh,
  // etc.) pass `{ refresh: true }` to force the next call past the cache.
  if (opts?.refresh && !url.searchParams.has("refresh")) {
    url.searchParams.set("refresh", "1");
  }
  return url.pathname + url.search;
}

function buildHeaders(init?: RequestInit, ctx?: RelayContext): HeadersInit {
  const headers = new Headers(init?.headers);
  if (!headers.has("Accept")) headers.set("Accept", "application/json");
  if (ctx?.employeeId != null) headers.set("X-Employee-Id", String(ctx.employeeId));
  if (ctx?.accountId) headers.set("X-Account-Id", String(ctx.accountId));
  if (ctx?.priority === "background") headers.set("X-Priority", "background");
  // Only set content-type for JSON bodies. For FormData the browser
  // needs to set Content-Type itself so it can include the multipart
  // boundary — overriding here would corrupt the upload.
  if (init?.body && !(init.body instanceof FormData) && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  return headers;
}

async function parseResponse<T>(r: Response): Promise<T> {
  const text = await r.text();
  let body: unknown = text;
  if (text) {
    try { body = JSON.parse(text); } catch { /* keep as text */ }
  }
  if (!r.ok) {
    const rawDetail =
      (typeof body === "object" && body && "detail" in body && (body as Record<string, unknown>).detail) ||
      text || `HTTP ${r.status}`;
    // FastAPI returns `detail` as an object/array for upstream proxy
    // errors (`{upstream_status, upstream_body}`) and validation errors
    // (list of {loc, msg, type}). `String(obj)` yields "[object Object]"
    // which surfaces as an unreadable error chip — flatten to JSON instead.
    let detail: string;
    if (typeof rawDetail === "string") detail = rawDetail;
    else if (rawDetail && typeof rawDetail === "object") {
      const d = rawDetail as Record<string, unknown>;
      if (typeof d.upstream_status === "number" && typeof d.upstream_body === "string") {
        detail = `upstream ${d.upstream_status}: ${String(d.upstream_body).slice(0, 200)}`;
      } else if (Array.isArray(rawDetail)) {
        detail = rawDetail.map((e) => {
          if (e && typeof e === "object" && "msg" in (e as Record<string, unknown>)) {
            return String((e as Record<string, unknown>).msg);
          }
          return JSON.stringify(e);
        }).join("; ");
      } else {
        try { detail = JSON.stringify(rawDetail); } catch { detail = `HTTP ${r.status}`; }
      }
    } else {
      detail = `HTTP ${r.status}`;
    }
    throw new RelayError(r.status, body, detail);
  }
  return body as T;
}

/**
 * Turn one of the error strings built above into a line an operator can act on.
 *
 * It lives HERE, next to the code that formats `upstream <status>: <body>`,
 * because the two have to agree on that shape — a humaniser parked in one
 * component drifts the moment this formatter changes, and every other error
 * surface keeps printing the raw blob.
 *
 * What the operator actually sees today is the upstream body verbatim:
 * `upstream 404: {"error":{"code":0,"message":"User not found"}}` — which reads
 * like a crash and buries the only fact that matters. The permanent case is
 * separated from the transient ones on purpose: "retry" is the right instinct
 * for a timeout and a waste of time on a 404.
 */
export function describeLoadError(
  error: { message?: string } | null | undefined,
  /** What a 404 means HERE. The generic default is deliberate: only the caller
   *  knows whether the missing thing is a fan, a thread or a vault list, and
   *  "this fan's account no longer exists" is simply false on a list query. */
  notFound = "OnlyFans no longer has this.",
): string {
  const raw = error?.message || "";
  if (/User not found/i.test(raw) || /upstream 404/.test(raw)) return notFound;
  if (/proxy_unreachable|network/i.test(raw)) return "Can't reach OnlyFans right now.";
  if (/timeout/i.test(raw)) return "OnlyFans timed out.";
  if (/upstream 401|upstream 403/.test(raw)) return "This account's OnlyFans session expired.";
  return `Couldn't refresh: ${raw || "unknown error"}`;
}

/**
 * Core fetch wrapper. Most callers use the verb helpers (get/post/patch/delete)
 * but `request()` is here for the long tail of mixed verbs / streaming bodies.
 *
 * `opts.refresh=true` appends `?refresh=1` so Stage C backend caches bypass.
 */
export async function request<T = unknown>(
  path: string,
  init?: RequestInit,
  ctx?: RelayContext,
  opts?: { refresh?: boolean },
): Promise<T> {
  const url = buildUrl(path, ctx, opts);
  const headers = buildHeaders(init, ctx);
  const r = await fetch(url, { ...init, headers, credentials: "same-origin" });
  return parseResponse<T>(r);
}

/**
 * Wrap an OF CDN URL so it loads through the relay's `/img` proxy. We have
 * to do this because OF signs CDN URLs with `AWS:SourceIp=<egress IP>/32` —
 * the browser's IP doesn't match the account's proxy egress, so the image
 * 403s. `/img` tunnels the fetch via the right account's HTTP client.
 *
 * Returns the original URL if either `url` or `accountId` is missing.
 */
export function proxyImage(url: string | null | undefined, accountId: string | null | undefined): string {
  if (!url) return "";
  // Browser-local URLs (blob:/data:) aren't fetchable through the relay
  // — they're already in the browser, just pass them through.
  if (url.startsWith("blob:") || url.startsWith("data:")) return url;
  if (!accountId) return url;
  const tok = resolveShareToken();
  const params = new URLSearchParams();
  params.set("u", url);
  params.set("account_id", accountId);
  if (tok) params.set("t", tok);
  return `/img?${params.toString()}`;
}

/**
 * Build a URL for the i-th scrub frame (0..11) of a video. The relay
 * lazily extracts a 12-frame storyboard for each video on first hit;
 * subsequent fetches serve cached JPGs straight from disk. Returns ""
 * if we don't have everything we need to build the URL.
 *
 * Passing `duration` (seconds, from VaultMedia.duration which OF already
 * tells us in the vault listing) lets the relay skip its own ffprobe
 * step and start the extraction immediately.
 */
export function proxyScrubFrame(
  url: string | null | undefined,
  accountId: string | null | undefined,
  frameIdx: number,
  duration?: number | null,
  /** Per-hover session id. The cancel POST that fires on hover-end
   *  carries the SAME id so the server can scope the abort to this
   *  exact hover session — a delayed cancel from a previous hover of
   *  the same video can't abort a freshly-started build. */
  sessionId?: string | null,
): string {
  if (!url || !accountId) return "";
  if (url.startsWith("blob:") || url.startsWith("data:")) return "";
  const tok = resolveShareToken();
  const params = new URLSearchParams();
  params.set("u", url);
  params.set("account_id", accountId);
  params.set("i", String(frameIdx));
  if (typeof duration === "number" && duration > 0) {
    params.set("dur", String(duration));
  }
  if (sessionId) params.set("sid", sessionId);
  if (tok) params.set("t", tok);
  return `/img/scrub?${params.toString()}`;
}

export const relay = {
  /** `opts.refresh=true` appends `?refresh=1` to bust Stage C backend
   *  caches (list_chats / init / notifications_count / my_settings /
   *  get_messages_tail / tagged_friend_users). Use for Refresh-inbox
   *  buttons and similar user-driven "force fresh" affordances. */
  get<T = unknown>(
    path: string,
    ctx?: RelayContext,
    signal?: AbortSignal,
    opts?: { refresh?: boolean },
  ): Promise<T> {
    return request<T>(path, { method: "GET", signal }, ctx, opts);
  },
  /** Multipart POST — caller hands us the File; we wrap it in FormData
   *  and skip the default JSON Content-Type so the browser can set the
   *  multipart boundary. */
  uploadFile<T = unknown>(path: string, file: File, fieldName = "file", ctx?: RelayContext): Promise<T> {
    const form = new FormData();
    form.append(fieldName, file, file.name);
    return request<T>(path, { method: "POST", body: form }, ctx);
  },
  post<T = unknown>(path: string, body?: unknown, ctx?: RelayContext): Promise<T> {
    return request<T>(path, {
      method: "POST",
      body: body == null ? undefined : JSON.stringify(body),
    }, ctx);
  },
  patch<T = unknown>(path: string, body?: unknown, ctx?: RelayContext): Promise<T> {
    return request<T>(path, {
      method: "PATCH",
      body: body == null ? undefined : JSON.stringify(body),
    }, ctx);
  },
  put<T = unknown>(path: string, body?: unknown, ctx?: RelayContext): Promise<T> {
    return request<T>(path, {
      method: "PUT",
      body: body == null ? undefined : JSON.stringify(body),
    }, ctx);
  },
  delete<T = unknown>(path: string, ctx?: RelayContext, body?: unknown): Promise<T> {
    return request<T>(path, {
      method: "DELETE",
      body: body == null ? undefined : JSON.stringify(body),
    }, ctx);
  },
};

// ── Typed payload helpers ───────────────────────────────────────────
// These mirror the existing relay endpoints we'll consume in Phase A.8.
// Add new helpers when a screen needs them; resist building speculative
// surface area.

export interface Employee {
  id: number;
  display_name: string;
  color: string | null;
  is_active: boolean;
  /** Non-null = this row is a per-(chatter, owner) mirror auto-created
   * for a chatter login. The owner's "Who's chatting?" picker hides
   * these; orphan-tip / attribution flows keep them visible. */
  chatter_id?: string | null;
  created_at?: string | null;
}

export interface AccountMeta {
  id: string;
  nickname?: string | null;
  color?: string | null;
  incogniton_profile_id?: string | null;
  created_at?: string | null;
  last_used_at?: string | null;
  has_session?: boolean;
  /** Set while the account's OF session is flagged dead — EVERY automation for
   *  it is PAUSED until the session is re-captured (service/account_health.py).
   *  ISO instant it was flagged; absent/null means healthy. Load-bearing for the
   *  UI: without it a parked account is indistinguishable from an idle one. */
  session_dead_at?: string | null;
  /** Which repair it needs: "wrong_user" (the creator unlinked or logged in
   *  elsewhere — re-link) or "no_session" (no captured session on this host —
   *  run the bootstrap). */
  session_dead_reason?: string | null;
}

export interface ProxyMeta {
  label: string;
  scheme: string;
  host: string;
  port: number;
  username?: string | null;
  password?: string | null;
  notes?: string;
  verified_ip?: string | null;
  verified_at?: string | null;
  /** Canonical: many accounts may share one proxy. Sourced from
   *  `proxies.json :: assigned_account_ids`. The UI flags >1 with a
   *  shared-egress warning because OF correlates same-IP behaviour. */
  assigned_account_ids?: string[];
  assigned_accounts?: Array<{ id: string; nickname?: string | null; color?: string | null }>;
  /** @deprecated Mirror of `assigned_accounts[0]` — kept so legacy callers
   *  still compile. New code should read `assigned_accounts`. */
  assigned_account_id?: string | null;
  /** @deprecated Mirror of `assigned_accounts[0]`. */
  assigned_account?: { id: string; nickname?: string | null } | null;
}

export interface DriftReport {
  live_rev: string | null;
  live_known: boolean;
  live_fetched_at?: number | null;
  live_error?: string | null;
  any_drift: boolean;
  accounts: Array<{
    account_id: string;
    nickname?: string | null;
    has_session: boolean;
    session_rev: string | null;
    live_rev?: string | null;
    live_known?: boolean;
    drift: boolean;
    stale: boolean;
    captured_at?: string | null;
    reason?: string;
  }>;
}

export interface BootstrapResponse {
  ok: true;
  session_file: string;
  account_id: string;
  user_id: string;
  x_of_rev: string;
}

// ── OF chat / message shapes ────────────────────────────────────────
// These mirror what OF returns through the relay. Optional fields stay
// optional because OF's payloads vary by chat state. Keep field names
// in OF's camelCase (we don't re-shape on the relay).

export interface OFMediaVariant {
  url: string;
}
/** The size variants OF hangs off one media item.
 *
 *  All five are modelled because picking a still is a search DOWN this list (see
 *  lib/ofMedia.stillUrl): typing only `thumb`/`source` is what forced the chat
 *  pane to guess describability from `type` instead of resolving a real URL — and
 *  guessing gets a viewable clip that ships `preview` but no `thumb` wrong in one
 *  direction, and a gif whose only file is the .mp4 wrong in the other. */
export interface OFMediaFiles {
  thumb?: OFMediaVariant | null;
  squarePreview?: OFMediaVariant | null;
  preview?: OFMediaVariant | null;
  full?: OFMediaVariant | null;
  source?: OFMediaVariant | null;
}
export interface OFMedia {
  id: number;
  type?: string;
  url?: string | null;
  files?: OFMediaFiles | null;
  hasError?: boolean;
  /** Natural pixel dimensions of the media's source/full asset (NOT the
   *  square thumb). Populated lazily by the relay: from OF's REST
   *  `files`/`info` width/height on the live message fetch, the stored
   *  `message_media` row on the DB seed, or a client onload→PATCH for items
   *  OF never sized. Both are NULLABLE by design — the skeleton/grid must
   *  tolerate null and fall back to a neutral box. See 18_chat_render_stability §1.1. */
  width?: number | null;
  height?: number | null;
}

export interface OFUserMini {
  id: number;
  name?: string;
  username?: string;
  avatar?: string | null;
  /** Team-set nickname from our SQLite, stitched onto /users/list by the
   *  relay. Overrides OF's display name in chat-list / pane / popout UIs
   *  so chatters see the label they chose everywhere. */
  customNickname?: string | null;
  /** OF's own per-subscription rename — what the OF.com UI shows above
   *  the original `name`. Editable via PUT /subscriptions/{id} with
   *  `{displayName}`. Returned on /users/list and /users/{id}. */
  displayName?: string | null;
  /** OF's private creator-side note on this fan (PUT with `{notice}`). */
  notice?: string | null;
  /** Stamped by the relay on /chats rows when WE restricted this user on
   *  OnlyFans (durable `of_restricted` registry) — the thin skip_users=all
   *  rows never carry OF's own flag. Restricted creators are excluded from
   *  every unread / owe-reply count (server badge fold + inbox chips). */
  isRestricted?: boolean;
}

export interface OFMessage {
  id: number | string;       // negative for optimistic
  text: string;
  fromUser: OFUserMini;
  toUser?: OFUserMini;
  createdAt: string;
  changedAt?: string;
  isFree?: boolean;
  isOpened?: boolean;
  /** Ledger-confirmed purchase, stamped by our transaction ingest onto the
   *  local `messages` row (`is_paid`). A second unlock signal ALONGSIDE OF's
   *  `isOpened`: the payouts ledger flips this minutes before a fresh OF fetch
   *  would echo `isOpened`, so the PPV bubble can render unlocked immediately.
   *  Carried by the DB seed (/admin/messages); OF's live payload omits it. */
  isPaid?: boolean;
  /** OF echoes a heart-react on chat messages via `isLiked`. We toggle
   *  it locally for optimistic feedback; the next message-list refetch
   *  reconciles with truth. */
  isLiked?: boolean;
  /** Pin state — flipped optimistically by useTogglePinMessage and
   *  reconciled by the next /messages refetch. OF returns it on every
   *  message in /chats/{id}/messages. */
  isPinned?: boolean;
  /** OF native quote-reply. Outgoing sends carry this through to OF's
   *  send body; incoming messages receive the field populated when the
   *  fan tapped "Reply" on one of our bubbles. */
  replyToMessageId?: number;
  /** Embedded snapshot of the quoted message (text/from/createdAt). OF
   *  sometimes populates this on read, sometimes only the id — when only
   *  the id is present we look up the original from the local cache. */
  replyToMessage?: {
    id: number;
    text?: string;
    fromUser?: OFUserMini;
    createdAt?: string;
    mediaCount?: number;
  } | null;
  price?: number;            // dollars
  isTip?: boolean;
  /** True when the message text is locked behind the PPV price. Free
   *  messages and PPVs sent with `lockedText=false` echo this as false /
   *  omit it; either way the text was visible to the fan immediately. */
  lockedText?: boolean;
  media: OFMedia[];
  mediaCount?: number;
  /** Vault ids that were sent UNLOCKED (free preview) when `price > 0`.
   *  OF echoes the same `previews` field we sent in the request body. */
  previews?: number[];
  /** Giphy id echoed by OF on GIF messages. Sibling of `media` — when
   *  set, the message body is a GIF, not vault media. The renderer fetches
   *  the GIF via `https://media.giphy.com/media/<id>/giphy.gif`. */
  giphyId?: string;
  // Optimistic fields
  _pending?: boolean;
  _failed?: boolean;
  _failedReason?: string;    // human-readable upstream reason (OF error)
  /** Vault ids OF named in `payload.removeFromInputMediaIds` — the exact
   *  attachments it refused. The bubble marks those tiles so the operator
   *  drops the right one; see lib/sendFailure. */
  _failedMediaIds?: number[];
  _tempId?: number;
  /** Set on pseudo-rows synthesized from a scheduled send (the executor-fired
   *  `scheduled_jobs` queue). `_fireAt` is the ISO timestamp it delivers at;
   *  `_scheduleJobId` is the server job id used to cancel it. The MessageList
   *  renders these distinctly + offers a cancel button. */
  _isFutureScheduled?: boolean;
  _fireAt?: string;
  _scheduleJobId?: number;
  /** The pseudo-row is a pending AI reply (Human Rhythm paused ai_chatter and
   *  scheduled its own wake), not a human's queued send. It has NO body — the
   *  draft doesn't exist yet at defer time — so the bubble is time-only, and
   *  `_scheduleJobId` is deliberately left unset so no cancel button renders:
   *  cancelling the wake job would strand `rhythm_state.wake_at`. */
  _aiPending?: boolean;
}

export interface OFChatItem {
  id?: number;
  withUser: OFUserMini & { id: number };
  lastMessage?: {
    id: number;
    text?: string;
    fromUser?: OFUserMini;
    createdAt?: string;
    mediaCount?: number;
    isFree?: boolean;
    isTip?: boolean;
    lockedText?: boolean;
    isOpened?: boolean;
  };
  hasUnread?: boolean;
  /** OF's source-of-truth field. We mirror it to `hasUnread` in the
   *  chat-list normalizer so downstream code can keep using the boolean,
   *  but the count is handy when we want a "5" badge instead of a dot. */
  unreadMessagesCount?: number;
  lastReadMessageId?: number;
  isOnline?: boolean;
  /** OF gates outgoing sends per-chat (e.g., unsubscribed fans). When
   *  `canSendMessage` is false, sending the standard message endpoint
   *  returns 400 — `canNotSendReason` is OF's human-readable reason. */
  canSendMessage?: boolean;
  canNotSendReason?: string | null;
  isMutedNotifications?: boolean;
  // We attach this client-side after a fan-out so each row knows
  // which model account it came from (drives the colored dot).
  __accountId?: string;
}

export interface OFChatsResp {
  list?: OFChatItem[];
  chats?: OFChatItem[]; // some OF shapes return `list`, others `chats`
  hasMore: boolean;
}

export interface OFMessagesResp {
  list: OFMessage[];
  hasMore: boolean;
}

export interface AuditAction {
  id: number;
  employee_id: number | null;
  account_id: string | null;
  action: string;          // "POST /admin/accounts/..."
  target_type: string | null;
  target_id: string | null;
  payload: unknown;
  at: string | null;
}

export interface FanRecord {
  account_id: string;
  fan_id: number;
  of_username: string | null;
  of_display_name: string | null;
  avatar_url: string | null;
  custom_nickname: string | null;
  generated_nickname: string | null;
  real_name: string | null;
  his_age: string | null;
  home_country: string | null;
  home_city: string | null;
  hobbies: string | null;
  fetishes: string | null;
  self_description: string | null;
  description: string | null;
  notes: string | null;
  tags: string[];
  lifetime_spend_cents: number;
  bought_amount: number;
  subscription_status: string | null;
  subscribed_at: string | null;
  last_message_received_at: string | null;
  source: string;
  is_followed: boolean;
  /** ISO 639-1 the fan writes in (per-fan override/detection); null = use account default. */
  language: string | null;
  /** 'manual' when an operator pinned language (then AI detection can't overwrite it). */
  language_source: string | null;
  // ── gen_info-extracted facts (for the AI-Profile card) ─
  occupation?: string | null;
  employer?: string | null;
  relationship_status?: string | null;
  relationship_stage?: string | null;
  has_kids?: boolean | null;
  pets?: unknown[];
  // ── Persona continuity (Tier B) — what SHE told THIS fan ─
  /** The resolved answer per topic. Operator-writable: these are what the prompt
   *  reads, so a correction here is what actually changes her next reply. */
  persona_age_claimed?: string | null;
  persona_location_claimed?: string | null;
  persona_job_claimed?: string | null;
  /** The raw ledger behind those three — every self-claim she has made to this
   *  fan, newest last. Read-only: it is the audit trail, not the setting. */
  persona_claims?: PersonaClaim[];
  /** The MERGED view: one row per topic, scalars overriding, in the order the
   *  prompt prints them. Built server-side by the same function that renders the
   *  prompt block, so the drawer shows what the AI is told — not a second
   *  implementation of the override rule. */
  persona_claims_resolved?: ResolvedClaim[];
  communication_style?: Record<string, unknown>;
  recent_events_timeline?: { date?: string; event?: string }[];
  /** The joined gen_info profile: rich note + openers. */
  profile?: FanProfileBlock;
  created_at: string | null;
  updated_at: string | null;
}

/** One line of the merged "you already told him this" block. */
export interface ResolvedClaim {
  /** The prompt's own wording — a topic she chose ("nationality") or an override
   *  label ("where you are"). */
  label: string;
  value: string;
  /** The PATCH-editable field behind this row, or null when the topic has no
   *  mirror column. Null means read-only, NOT that it misses the prompt. */
  column: "persona_age_claimed" | "persona_location_claimed" | "persona_job_claimed" | null;
  /** When she said it. Null for an operator override — it was never "said". */
  at: string | null;
}

/** One self-claim she made to this fan, as recorded at the time she said it. */
export interface PersonaClaim {
  /** Free-text topic the extractor chose ("city", "nationality", "job", …).
   *  Deliberately not an enum — it is whatever she actually talked about. */
  topic?: string;
  claim?: string;
  /** ISO timestamp of the reply that contained it. */
  at?: string;
}

export interface FanProfileBlock {
  rich_note: string;
  short_bio: string | null;
  nickname: string | null;
  q: string[];
  tease: string[];
  last_generated_at: string | null;
}

export interface FanUpdate {
  custom_nickname?: string | null;
  notes?: string | null;
  tags?: string[];
  real_name?: string | null;
  home_country?: string | null;
  home_city?: string | null;
  his_age?: string | null;
  hobbies?: string | null;
  fetishes?: string | null;
  /** ISO 639-1 code, or "" / null to clear the manual override. */
  language?: string | null;
  /** Persona continuity (Tier B) — correct what she told this fan. These are the
   *  values the chat prompt reads, so editing one changes her next reply. The
   *  server has accepted them since they were added; only the client contract
   *  was missing, which is why nothing could write them. `persona_claims` is
   *  deliberately absent: the ledger is an audit trail, not a setting. */
  persona_age_claimed?: string | null;
  persona_location_claimed?: string | null;
  persona_job_claimed?: string | null;
}

export interface SendMessageBody {
  text: string;
  locked_text?: boolean;
  price?: number;
  media_files?: Array<number | Record<string, unknown>>;
  /** Subset of `media_files` (numeric vault ids only) that ride along
   *  UNLOCKED when `price > 0`. Empty / omitted = everything locked.
   *  Fresh-claim dicts can't appear here — they have no id yet. */
  previews?: number[];
  is_couple_people_media?: boolean;
  is_forward?: boolean;
  /** OF native quote-reply target id. When set, the receiver renders
   *  the quoted message as a card above this bubble (matches OF web). */
  reply_to_message_id?: number;
  /** OF user ids of creators to @-tag in this message. Source must be
   *  GET /api/of/v2/self/tagged-friend-users — OF 400s on ids that
   *  aren't on the caller's tagged-friend list. */
  tagged_users?: number[];
  /** Giphy id (e.g. "0ndspgUFbm9iQtyX2Q"). Forwarded as top-level
   *  `giphyId` in the OF body — sibling of `media_files`. One per send. */
  giphy_id?: string;
}

// ── Vault ───────────────────────────────────────────────────────────

/** One OF-extracted poster frame for a video (lives on `preview.options`).
 *  For DRM-only videos these frames are the ONLY renderable representation
 *  we have — the segments are FairPlay-encrypted (SAMPLE-AES), so ffmpeg
 *  and <video>/hls.js cannot decode them. */
export interface VaultFrameOption { id?: number; url: string; selected?: boolean; custom?: boolean }
export interface VaultFile {
  /** Absent on a DRM-only `full` entry (OF ships `sources: []` and no url). */
  url?: string;
  width?: number;
  height?: number;
  /** Progressive sources for `full`; empty array on DRM-only videos. */
  sources?: unknown[];
  /** OF-extracted poster frames on `preview` (3–9 typically). */
  options?: VaultFrameOption[];
}
export interface VaultFiles {
  thumb?: VaultFile | null;
  preview?: VaultFile | null;
  squarePreview?: VaultFile | null;
  full?: VaultFile | null;
  /** Present only on DRM (FairPlay) videos: HLS/DASH manifests, license-gated.
   *  Its existence + a null `videoSources`/empty `full.sources` is how we
   *  detect "DRM-only, no progressive mp4". */
  drm?: { manifest?: { hls?: string | null; dash?: string | null } } | null;
}
export interface VaultMedia {
  id: number;
  type: "photo" | "video" | "gif" | "audio" | string;
  isReady?: boolean;
  canView?: boolean;
  hasError?: boolean;
  createdAt?: string;
  duration?: number;
  files?: VaultFiles | null;
  videoSources?: { "720"?: string | null; "240"?: string | null } | null;
  /** When this attachment came from a fresh upload that OF hasn't finished
   *  transcoding, `_claim` holds the `{processId,host,name,extra}` dict that
   *  the message-send endpoint accepts in place of a vault id. */
  _claim?: Record<string, unknown>;
  /** Local blob URL for the original file — used by the optimistic
   *  outgoing bubble until reconcile pulls real CDN urls. */
  _localPreview?: string;
  /** True while the upload is still in flight — composer renders a
   *  spinner overlay on the chip so the user knows the file is
   *  attaching, not stuck. Cleared once `mutateAsync` resolves. */
  _uploading?: boolean;
}
export interface VaultMediaResp {
  list: VaultMedia[];
  hasMore: boolean;
}

export interface VaultList {
  id: number;
  type: "custom" | "media_stickers" | string;
  name: string;
  hasMedia: boolean;
  videosCount?: number;
  photosCount?: number;
  gifsCount?: number;
  audiosCount?: number;
}

export interface VaultListsResp {
  list: VaultList[];
  hasMore?: boolean;
}

/** Response from POST /api/of/v2/upload. `send_with` is the only field
 *  callers should write into a message body's `media_files`. */
/** OF's message-template / saved-reply.
 *  `template: "reply_on_subscribe"` is the magical welcome-message slot —
 *  exactly one per account. Everything else is a regular saved reply. */
export interface OFMessageTemplate {
  id: string;
  template?: string | null;
  text: string;
  displayText?: string;
  price?: number;
  lockedText?: boolean;
  mediaCount?: number;
  media?: Array<{
    id: number;
    type?: string;
    files?: VaultFiles | null;
  }>;
  /** Welcome slot only — does NOT track the master `replyOnSubscribe`
   *  switch (verified live: settings had replyOnSubscribe=true while the
   *  template still returned isActive=false). Read the actual flag from
   *  `/users/me/settings.replyOnSubscribe` instead. */
  isActive?: boolean;
  previews?: number[];
}

/** A saved reply stored in our local DB (NOT in OF). Saved replies
 *  share the editor with OF's welcome message, but the welcome is the
 *  only one OF actually accepts via /messages/templates — everything
 *  else is local. The media array is the same VaultMedia shape we
 *  carry through the rest of the app. */
export interface SavedReply {
  id: number;
  account_id: string;
  title?: string | null;
  text: string;
  price: number;          // dollars
  locked_text: boolean;
  media: Array<{
    id: number;
    type?: string;
    files?: VaultFiles | null;
  }>;
  /** OF user ids to @-tag when this template is applied. Persisted so
   *  picking a template fully restores the tagging state. */
  tagged_users?: number[];
  /** Subset of `media`'s ids that ride along UNLOCKED when `price > 0`. */
  previews?: number[];
  /** Optional Giphy id stored on the template. When set, picking this
   *  template seeds the composer's GIF state so the GIF is sent alongside.
   *  GIF-only templates (no images/videos) survive the image-template
   *  filter applied in mass/post/model-to-models composers. */
  gif_id?: string | null;
  /** Animated preview URL — cached so the picker chip and apply flow don't
   *  need a fresh Giphy roundtrip just to render the GIF. */
  gif_url?: string | null;
  /** Optional script grouping. When `script_id` + `script_step` are both
   *  set, sending this template advances a per-chat cursor and the chat
   *  composer surfaces the next step (same `script_id`, `script_step + 1`)
   *  as a one-tap suggestion bubble. */
  script_id?: string | null;
  script_step?: number | null;
  created_at?: string | null;
  updated_at?: string | null;
}

/** A message queued for future delivery via OF's /messages/queue.
 *  Both direct (`userIds: [fanId]`) and mass (`userLists` / `groups`)
 *  sends land in the same queue; we surface both with a recipient hint. */
export interface OFScheduledMessage {
  id: number;
  text?: string;
  scheduledDate?: string;
  price?: number;
  lockedText?: boolean;
  mediaCount?: number;
  media?: Array<{
    id: number;
    type?: string;
    files?: VaultFiles | null;
  }>;
  /** Direct sends populate userIds; mass sends use userLists/groups. */
  userIds?: number[];
  userLists?: string[];
  groups?: string[];
  /** Echoed from relay so the UI can label which account a queued send belongs to. */
  __accountId?: string;
}

export interface VaultUploadResp {
  ready: boolean;
  deduped: boolean;
  vault_id: number | null;
  send_with: Array<number | Record<string, unknown>>;
  size?: number;
  filename?: string;
  note?: string;
  existing?: { id?: number; type?: string; files?: VaultFiles | null };
}

/** One entry from GET /api/of/v2/posts/tagged-friend-users. Shape mirrors
 *  OF's response: top-level `{id, name, type:"user", user:{...}}` plus
 *  a nested `user` block with the avatar + verification details. */
export interface TaggedFriendUser {
  id: number;
  name?: string;
  type?: string;
  user: {
    id: number;
    name?: string;
    username?: string;
    avatar?: string | null;
    avatarThumbs?: { c50?: string; c144?: string } | null;
    isIdentityVerified?: boolean;
  };
}

export interface TaggedFriendUsersResp {
  items: TaggedFriendUser[];
  hasMore: boolean;
}

// ── Giphy proxy (via OF) ────────────────────────────────────────────
// OF exposes Giphy under /api2/v2/giphy/proxy/gifs/{trending,search}. The
// relay proxies these without modification; the response shape is Giphy's
// native one. We only type the fields the picker actually reads.

export interface GiphyImageVariant {
  url?: string;
  mp4?: string;
  webp?: string;
  width?: string;
  height?: string;
}

export interface GiphyImages {
  original?: GiphyImageVariant;
  fixed_height?: GiphyImageVariant;
  fixed_height_small?: GiphyImageVariant;
  fixed_width?: GiphyImageVariant;
  fixed_width_small?: GiphyImageVariant;
  downsized?: GiphyImageVariant;
  preview_gif?: GiphyImageVariant;
  preview_webp?: GiphyImageVariant;
  // Catch-all so unmapped variants are still indexable.
  [k: string]: GiphyImageVariant | undefined;
}

export interface GiphyItem {
  type: "gif";
  id: string;
  url?: string;
  slug?: string;
  title?: string;
  rating?: string;
  images: GiphyImages;
}

export interface GiphyResponse {
  data: GiphyItem[];
  pagination?: { total_count?: number; count?: number; offset?: number };
}

export function gifTrending(
  limit = 20, offset = 0, ctx?: RelayContext, signal?: AbortSignal,
): Promise<GiphyResponse> {
  const qs = new URLSearchParams({ limit: String(limit), offset: String(offset) });
  return relay.get<GiphyResponse>(`/api/of/v2/giphy/proxy/gifs/trending?${qs}`, ctx, signal);
}

export function gifSearch(
  q: string, limit = 20, offset = 0, ctx?: RelayContext, signal?: AbortSignal,
): Promise<GiphyResponse> {
  const qs = new URLSearchParams({ q, limit: String(limit), offset: String(offset) });
  return relay.get<GiphyResponse>(`/api/of/v2/giphy/proxy/gifs/search?${qs}`, ctx, signal);
}
