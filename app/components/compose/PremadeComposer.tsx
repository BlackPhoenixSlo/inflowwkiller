"use client";

/**
 * PremadeComposer — modal for the two automation-backed "ready-made" features:
 *
 *   kind="auto_posts"   (P1/P2) — a list of feed posts, sent one by one; each
 *                        optionally auto-deletes after N hours (a self-cleaning
 *                        feed). Submits {posts:[…]}.
 *   kind="mass_premade" (P3)    — a list of ready broadcasts; each can resend
 *                        later and/or auto-unsend on a timer. Submits {messages:[…]}.
 *
 * Unlike MassMessageComposer (which hits /api/of/v2/messages/queue for an
 * immediate send), this enqueues a one-shot automation job via
 * POST /admin/automation/enqueue — the worker runs it on the next tick and
 * chains the follow-up delete/resend jobs itself. The whole list rides in the
 * job payload; the automation drips it out one item at a time.
 *
 * `dry_run` enqueues a plan-only job (no real post/send) — the plan lands in
 * the run's stats on the Automations page.
 */

import { useMemo, useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";

import { useEmployee } from "@/contexts/EmployeeContext";
import { relay, type VaultMedia } from "@/lib/relay";
import { VaultPicker } from "@/components/chat/VaultPicker";
import { useActiveAccounts } from "@/hooks/useAccounts";
import { useAllModelsInclude } from "@/hooks/useAllModelsInclude";
import { useVaultLists } from "@/hooks/useVaultMedia";

import { AccountPicker } from "./AccountPicker";

type Kind = "auto_posts" | "mass_premade";

/** One per-account result of an all-models fan-out enqueue. */
interface FanOutResult {
  accountId: string;
  ok: boolean;
  error?: string;
}

/** OF built-in audiences usable across any account (sent as `userLists`). Same
 *  pair the Mass Online composer exposes. */
const SYSTEM_AUDIENCES: Array<{ id: string; label: string }> = [
  { id: "fans",      label: "All fans"  },
  { id: "following", label: "Following" },
];
const TOP_LIST_NAMES: string[] = ["fans", "following"];
function topRank(name: string): number {
  const i = TOP_LIST_NAMES.indexOf(name.toLowerCase());
  return i === -1 ? TOP_LIST_NAMES.length : i;
}

interface FanList {
  id: number | string;
  name?: string;
  /** OF tags lists 'custom' or 'build-in' (sic) — spelling varies. */
  type?: string;
  usersCount?: number;
}
type ListsResp = { list?: FanList[]; hasMore?: boolean } | FanList[];

interface Row {
  // Text variant POOL — one is picked at random per fire. Always ≥1 entry.
  texts: string[];
  // Image POOL (pre-picked vault media) — mediaCount are sampled per fire.
  media: VaultMedia[];
  // Vault FOLDER pool — non-DRM photos from this folder join the pool at fire
  // time (backend `media_folder_id`). "" = none. Combines with `media` above.
  folderId: string;
  mediaCount: string; // images per fire (default 1)
  // shared timer
  delayMinutes: string;
  // auto_posts
  hoursToLive: string;
  // mass_premade
  onlineOnly: boolean;
  unsendAfterHours: string;
  resendAfterHours: string;
  resendCount: string;
  // mass_premade audience (Mass Online parity). `includes`/`excludes` hold a mix
  // of system-audience ids ('fans'/'following') and custom-list ids — they go to
  // OF as `user_lists` / `excluded_user_lists`. The hour/limit knobs resolve
  // server-side via audiences.resolve_mass_audience (DB-sourced fan ids).
  includes: string[];
  excludes: string[];
  excludeRepliedHours: string; // → exclude_replied_hours (fans we replied to)
  excludeInboundHours: string; // → exclude_inbound_hours (fans who messaged us)
  unreadLimit: string;         // → unread_limit (fans with unread msgs)
  recentChatHours: string;     // → recent_chat_hours (chatted-with → include)
  recentChatLimit: string;     // → recent_chat_limit (cap the recent-chat add)
}

function blankRow(): Row {
  return {
    texts: [""],
    media: [],
    folderId: "",
    mediaCount: "",
    delayMinutes: "",
    hoursToLive: "",
    onlineOnly: true,
    unsendAfterHours: "",
    resendAfterHours: "",
    resendCount: "",
    // Default audience mirrors Mass Online: fans + following both included.
    includes: ["fans", "following"],
    excludes: [],
    excludeRepliedHours: "",
    excludeInboundHours: "",
    unreadLimit: "",
    recentChatHours: "",
    recentChatLimit: "",
  };
}

/** A blank/zero/garbage string → undefined; a positive number → the number.
 *  Keeps unset knobs out of the payload so the automation treats them as
 *  "not applied" (e.g. no hours_to_live = keep the post forever). */
function posNum(s: string): number | undefined {
  const n = Number(s);
  return s.trim() !== "" && Number.isFinite(n) && n > 0 ? n : undefined;
}
function nonNegInt(s: string): number | undefined {
  const n = Number(s);
  return s.trim() !== "" && Number.isFinite(n) && n >= 0 ? Math.floor(n) : undefined;
}

/** Modal wrapper around the shared form — used from TopNav's "+ New" menu. */
export function PremadeComposer({
  open,
  onClose,
  kind,
}: {
  open: boolean;
  onClose: () => void;
  kind: Kind;
}) {
  if (!open) return null;
  const title =
    kind === "auto_posts"
      ? "Auto Posts (self-cleaning feed)"
      : "Premade Mass (send · resend · unsend)";
  return (
    <div
      className="fixed inset-0 z-50 bg-black/50 grid place-items-center p-4"
      onClick={onClose}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        className="w-full max-w-[640px] max-h-[90vh] flex flex-col bg-panel border border-border rounded-xl shadow-2xl"
      >
        <header className="px-4 py-3 border-b border-border flex items-center justify-between">
          <h2 className="text-sm font-semibold">{title}</h2>
          <button
            type="button"
            onClick={onClose}
            className="text-fg-dim hover:text-fg text-lg leading-none"
            title="Close"
          >×</button>
        </header>
        <div className="flex-1 overflow-y-auto p-4">
          <PremadeForm kind={kind} onDone={onClose} />
        </div>
      </div>
    </div>
  );
}

/** The inline form (account picker + repeating rows + dry-run + submit). Used
 *  both inside the modal above and embedded in the Automations page panel.
 *
 *  Return-payload mode: pass `onComposePayload` and the form STOPS enqueuing —
 *  the submit button becomes "Use in rule" and calls back with the same
 *  `{posts:[…]}` / `{messages:[…]}` payload it would have enqueued, so the
 *  RuleEditor can store it on an automation_rule. `forcedAccountId` pins the
 *  account (a rule is single-account) and hides the picker + all-models switch. */
export function PremadeForm({
  kind,
  onDone,
  forcedAccountId,
  onComposePayload,
}: {
  kind: Kind;
  /** Optional — called after a successful enqueue (the modal uses it to close). */
  onDone?: () => void;
  /** Pin the account (return-payload mode); hides the picker + all-models. */
  forcedAccountId?: string;
  /** Return-payload mode: receive the built payload instead of enqueuing. */
  onComposePayload?: (payload: Record<string, unknown>) => void;
}) {
  const posts = kind === "auto_posts";
  const composeMode = !!onComposePayload;
  const { current: currentEmployee } = useEmployee();
  const [accountId, setAccountId] = useState<string | null>(forcedAccountId ?? null);
  const [rows, setRows] = useState<Row[]>([blankRow()]);
  const [dryRun, setDryRun] = useState(false);
  // Auto Posts repost CYCLE (list-level, not per-row): re-post the whole list
  // after it finishes. Baked into the auto_posts payload, not the rule cadence.
  const [postsResendAfterHours, setPostsResendAfterHours] = useState("");
  const [postsResendCount, setPostsResendCount] = useState("");
  const [pickerFor, setPickerFor] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [okMsg, setOkMsg] = useState<string | null>(null);

  // All-models fan-out — enqueue the same job for every active model at once
  // (mirrors MassMessageComposer). Text-only: images are per-account vault ids,
  // so there's no single id that means the same media across every model.
  const [allModels, setAllModels] = useState(false);
  const activeAccounts = useActiveAccounts();
  const { isIncluded, toggle: toggleAllModelsInclude } = useAllModelsInclude();
  const allAccountIds = useMemo(() => activeAccounts.map((a) => a.id), [activeAccounts]);
  const broadcastAccounts = useMemo(
    () => activeAccounts.filter((a) => isIncluded(a.id)),
    [activeAccounts, isIncluded],
  );

  // Custom lists for the mass_premade audience picker (same source + sort as the
  // Mass Online composer). Only fetched for broadcasts, once an account is set.
  const listsQ = useQuery<FanList[]>({
    queryKey: ["fan-lists", accountId ?? ""],
    enabled: !!accountId && !posts,
    staleTime: 5 * 60_000,
    queryFn: async () => {
      const r = await relay.get<ListsResp>(
        "/api/of/v2/lists?limit=50",
        { accountId: accountId! },
      );
      return Array.isArray(r) ? r : (r?.list ?? []);
    },
  });
  const customLists = useMemo(() => {
    const filtered = (listsQ.data ?? []).filter(
      (l) => l.type !== "build-in" && l.type !== "built-in",
    );
    return filtered.slice().sort((a, b) => {
      const an = (a.name ?? "").toLowerCase();
      const bn = (b.name ?? "").toLowerCase();
      const ra = topRank(an);
      const rb = topRank(bn);
      if (ra !== rb) return ra - rb;
      return an.localeCompare(bn);
    });
  }, [listsQ.data]);

  // Vault folders for the per-row folder pool (same hook as AutoStoriesTab).
  // Only folders WITH photos — posts/mass attach photos. Disabled in all-models.
  const foldersQ = useVaultLists(accountId, !allModels);
  const photoFolders = useMemo(
    () => (foldersQ.data?.list ?? []).filter((f) => (f.photosCount ?? 0) > 0),
    [foldersQ.data],
  );

  function patchRow(i: number, patch: Partial<Row>) {
    setRows((prev) => prev.map((r, idx) => (idx === i ? { ...r, ...patch } : r)));
  }
  // ── Per-row audience toggles (mass_premade). includes/excludes mix system
  //    ids + custom-list ids, mutually exclusive per id (same UX as Mass Online).
  function mutAudience(i: number, fn: (inc: Set<string>, exc: Set<string>) => void) {
    setRows((prev) => prev.map((r, idx) => {
      if (idx !== i) return r;
      const inc = new Set(r.includes);
      const exc = new Set(r.excludes);
      fn(inc, exc);
      return { ...r, includes: [...inc], excludes: [...exc] };
    }));
  }
  /** System chip tri-state: off → +include → −exclude → off. */
  function cycleSystem(i: number, id: string) {
    mutAudience(i, (inc, exc) => {
      if (inc.has(id)) { inc.delete(id); exc.add(id); }
      else if (exc.has(id)) { exc.delete(id); }
      else { inc.add(id); }
    });
  }
  function toggleListInclude(i: number, id: string) {
    mutAudience(i, (inc, exc) => {
      if (inc.has(id)) inc.delete(id);
      else { inc.add(id); exc.delete(id); }
    });
  }
  function toggleListExclude(i: number, id: string) {
    mutAudience(i, (inc, exc) => {
      if (exc.has(id)) exc.delete(id);
      else { exc.add(id); inc.delete(id); }
    });
  }
  function addRow() {
    setRows((prev) => [...prev, blankRow()]);
  }
  function removeRow(i: number) {
    setRows((prev) => (prev.length <= 1 ? prev : prev.filter((_, idx) => idx !== i)));
  }
  // Text-variant pool helpers (one row → many text variants, random one sent).
  function patchText(i: number, ti: number, val: string) {
    setRows((prev) => prev.map((r, idx) =>
      idx === i ? { ...r, texts: r.texts.map((t, k) => (k === ti ? val : t)) } : r));
  }
  function addText(i: number) {
    setRows((prev) => prev.map((r, idx) => (idx === i ? { ...r, texts: [...r.texts, ""] } : r)));
  }
  function removeText(i: number, ti: number) {
    setRows((prev) => prev.map((r, idx) =>
      idx === i
        ? { ...r, texts: r.texts.length <= 1 ? r.texts : r.texts.filter((_, k) => k !== ti) }
        : r));
  }
  function reset() {
    setRows([blankRow()]);
    setDryRun(false);
    setPostsResendAfterHours("");
    setPostsResendCount("");
    setError(null);
    setOkMsg(null);
  }

  /** Build the per-item payload entry from a row, dropping unset knobs. The
   *  text/image POOLS go to the backend, which randomises per fire. */
  function rowToItem(r: Row): Record<string, unknown> {
    const media = r.media.map((m) => m.id).filter((id): id is number => typeof id === "number");
    const texts = r.texts.map((t) => t.trim()).filter(Boolean);
    const item: Record<string, unknown> = {};
    // One variant → a plain `text`; many → a `texts` pool (random per fire).
    if (texts.length === 1) item.text = texts[0];
    else if (texts.length > 1) item.texts = texts;
    // Images are per-account vault ids — omit them entirely in all-models mode.
    if (!allModels) {
      const folderId = r.folderId ? Number(r.folderId) : null;
      if (media.length) item.media_files = media;        // pre-picked pool
      if (folderId) item.media_folder_id = folderId;     // whole-folder pool
      if (media.length || folderId) {
        const mc = posNum(r.mediaCount);
        if (mc) item.media_count = Math.floor(mc);       // sampled per fire (default 1)
      }
    }
    const delay = posNum(r.delayMinutes);
    if (delay) item.delay_minutes = delay;
    if (posts) {
      const htl = posNum(r.hoursToLive);
      if (htl) item.hours_to_live = htl;
    } else {
      if (r.onlineOnly) item.online_only = true;
      const uah = posNum(r.unsendAfterHours);
      if (uah) item.unsend_after_hours = uah;
      const rc = nonNegInt(r.resendCount);
      const rah = posNum(r.resendAfterHours);
      if (rc && rc > 0 && rah) {
        item.resend_count = rc;
        item.resend_after_hours = rah;
      }
      // Audience (Mass Online parity): system + custom list ids → user_lists /
      // excluded_user_lists; the hour/limit knobs resolve server-side. In
      // all-models mode custom-list ids don't map across accounts, so keep only
      // the system audiences (fans/following).
      const sysIds = new Set(SYSTEM_AUDIENCES.map((a) => a.id));
      const inc = allModels ? r.includes.filter((id) => sysIds.has(id)) : r.includes;
      const exc = allModels ? r.excludes.filter((id) => sysIds.has(id)) : r.excludes;
      if (inc.length) item.user_lists = inc;
      if (exc.length) item.excluded_user_lists = exc;
      const erh = posNum(r.excludeRepliedHours);
      if (erh) item.exclude_replied_hours = erh;
      const eih = posNum(r.excludeInboundHours);
      if (eih) item.exclude_inbound_hours = eih;
      const ul = nonNegInt(r.unreadLimit);
      if (ul && ul > 0) item.unread_limit = ul;
      const rch = posNum(r.recentChatHours);
      if (rch) item.recent_chat_hours = rch;
      const rcl = nonNegInt(r.recentChatLimit);
      if (rcl && rcl > 0) item.recent_chat_limit = rcl;
    }
    return item;
  }

  /** Auto Posts list-level repost cycle, dropped from the payload when unset. */
  function postsCyclePayload(): Record<string, number> {
    if (!posts) return {};
    const rah = posNum(postsResendAfterHours);
    const rc = nonNegInt(postsResendCount);
    return rc && rc > 0 && rah ? { resend_count: rc, resend_after_hours: rah } : {};
  }

  /** Enqueue one job for one account; returns the new job id. */
  async function enqueueFor(aid: string, items: Record<string, unknown>[]) {
    const payload = posts
      ? { posts: items, dry_run: dryRun, ...postsCyclePayload() }
      : { messages: items, dry_run: dryRun };
    const resp = await relay.post<{ enqueued_job_id: number }>(
      "/admin/automation/enqueue",
      { account_id: aid, kind, payload },
      { accountId: aid, employeeId: currentEmployee?.id },
    );
    return resp.enqueued_job_id;
  }

  const submit = useMutation({
    mutationFn: async () => {
      const items = rows
        .map(rowToItem)
        // In all-models mode media is stripped, so an item is valid on text alone.
        .filter((it) => !!it.text || Array.isArray(it.texts)
          || Array.isArray(it.media_files) || it.media_folder_id != null);
      if (items.length === 0) {
        throw new Error(
          allModels ? "Add at least one post with text" : "Add at least one post with text or media",
        );
      }

      if (allModels) {
        const targets = broadcastAccounts.map((a) => a.id);
        if (targets.length === 0) throw new Error("No active models to broadcast from");
        const fan: FanOutResult[] = [];
        for (const aid of targets) {
          try {
            await enqueueFor(aid, items);
            fan.push({ accountId: aid, ok: true });
          } catch (e) {
            fan.push({ accountId: aid, ok: false, error: (e as Error).message });
          }
        }
        return { fan, count: items.length };
      }

      if (!accountId) throw new Error("Pick an account");
      const jobId = await enqueueFor(accountId, items);
      return { jobId, count: items.length };
    },
    onSuccess: ({ jobId, fan, count }) => {
      setError(null);
      const noun = `${count} ${posts ? "post" : "message"}${count === 1 ? "" : "s"}`;
      const dry = dryRun ? " (dry run — plan only, nothing sent)" : "";
      if (fan) {
        const okN = fan.filter((r) => r.ok).length;
        const failed = fan.filter((r) => !r.ok);
        setOkMsg(
          `Enqueued ${noun} on ${okN}/${fan.length} models${dry}. The worker runs them within ~30s.` +
            (failed.length ? ` Failed: ${failed.map((f) => f.accountId).join(", ")}.` : ""),
        );
      } else {
        setOkMsg(`Enqueued job #${jobId} — ${noun}${dry}. The worker runs it within ~30s.`);
      }
      setRows([blankRow()]);
    },
    onError: (err: Error) => {
      setOkMsg(null);
      setError(err.message);
    },
  });

  /** Build the same `{posts|messages:[…]}` payload the enqueue path sends, but
   *  return it (return-payload mode) instead of POSTing — null if no usable row.
   *  No `dry_run` here: that's a separate rule knob in the editor. */
  function buildComposePayload(): Record<string, unknown> | null {
    const items = rows
      .map(rowToItem)
      .filter((it) => !!it.text || Array.isArray(it.texts)
        || Array.isArray(it.media_files) || it.media_folder_id != null);
    if (items.length === 0) return null;
    return posts ? { posts: items, ...postsCyclePayload() } : { messages: items };
  }

  function useInRule() {
    setError(null);
    const payload = buildComposePayload();
    if (!payload) {
      setError(posts ? "Add at least one post with text or media"
                     : "Add at least one message with text or media");
      return;
    }
    onComposePayload?.(payload);
  }

  // Surface the optional onDone after a successful enqueue (modal closes; the
  // embedded panel passes nothing and just shows the success line).
  void onDone;

  return (
    <>
      <div className="space-y-3">
        {/* All-models switch — fan the same job out to every active model.
         *  Text-only (images are per-account vault ids). Hidden in return-payload
         *  mode: a rule targets exactly one account (forcedAccountId). */}
        {!composeMode && (
          <label className="flex items-center gap-2 text-xs cursor-pointer select-none">
            <input
              type="checkbox"
              checked={allModels}
              onChange={(e) => setAllModels(e.target.checked)}
            />
            <span className="font-medium">{posts ? "Post from ALL models" : "Broadcast from ALL models"}</span>
            <span className="text-fg-dim">
              ({broadcastAccounts.length}/{activeAccounts.length} included)
            </span>
          </label>
        )}

        {composeMode ? null : allModels ? (
          <div className="flex flex-wrap gap-1.5">
            {activeAccounts.map((a) => {
              const inc = isIncluded(a.id);
              return (
                <button
                  key={a.id}
                  type="button"
                  onClick={() => toggleAllModelsInclude(a.id, allAccountIds)}
                  title={inc ? "Click to exclude from broadcast" : "Click to include"}
                  className={
                    "flex items-center gap-1.5 px-2 py-1 rounded-full text-[11px] border transition-colors " +
                    (inc
                      ? "bg-bg-elev-1 border-border text-fg"
                      : "bg-bg border-border/60 text-fg-dim line-through opacity-60")
                  }
                >
                  <span className="w-2 h-2 rounded-full shrink-0" style={{ background: a.color || "#666" }} />
                  <span>{a.nickname || a.id}</span>
                </button>
              );
            })}
            {activeAccounts.length === 0 && (
              <span className="text-[11px] text-fg-dim">No active sessions.</span>
            )}
          </div>
        ) : (
          <AccountPicker value={accountId} onChange={setAccountId} />
        )}

          <div className="text-[11px] text-fg-dim border border-dashed border-border rounded-md py-2 px-3 leading-relaxed">
            {allModels
              ? "Text only — a random text variant is picked each time it fires (images are per-account, so they're off for all-models). "
              : "Add text variants and an image pool — pick individual images and/or a whole vault folder; a random text + random image(s) are picked each time it fires. "}
            {posts
              ? "Each post goes out one at a time (per-post delay staggers them). Set hours-to-live to auto-delete; blank = keep."
              : "Each broadcast goes to fans online now (toggle off for all fans). Set auto-unsend to clean up later; set resend to re-send on a timer (keep gaps ≥ a few minutes — OF rate-limits back-to-back sends)."}
          </div>

          {rows.map((r, i) => (
            <div key={i} className="border border-border rounded-md p-3 space-y-2 relative">
              <div className="flex items-center justify-between">
                <span className="text-[11px] uppercase tracking-wide text-fg-dim">
                  {posts ? "Post" : "Message"} {i + 1}
                </span>
                {rows.length > 1 && (
                  <button
                    type="button"
                    onClick={() => removeRow(i)}
                    className="text-[11px] text-err hover:underline"
                  >remove</button>
                )}
              </div>

              {/* Text variant POOL — a random one is sent each fire. */}
              <div className="space-y-1.5">
                {r.texts.map((t, ti) => (
                  <div key={ti} className="flex items-start gap-1.5">
                    <textarea
                      value={t}
                      onChange={(e) => patchText(i, ti, e.target.value)}
                      placeholder={
                        r.texts.length > 1
                          ? `Variant ${ti + 1} (random one sent)`
                          : posts ? "Post caption…" : "Broadcast text…"
                      }
                      rows={2}
                      className="flex-1 bg-bg border border-border rounded-md px-3 py-2 text-sm placeholder:text-muted focus:outline-none focus:border-accent resize-y"
                    />
                    {r.texts.length > 1 && (
                      <button
                        type="button"
                        onClick={() => removeText(i, ti)}
                        title="Remove this variant"
                        className="text-fg-dim hover:text-err text-sm leading-none mt-2"
                      >×</button>
                    )}
                  </div>
                ))}
                <button
                  type="button"
                  onClick={() => addText(i)}
                  className="text-[11px] text-accent hover:underline"
                >
                  + add text variant{r.texts.length > 1 ? ` (${r.texts.length})` : ""}
                </button>
              </div>

              {/* Image POOL — pre-picked vault media AND/OR a whole vault folder.
               *  Both feed one pool; `media_count` is sampled from it per fire.
               *  Hidden in all-models mode (vault ids are per-account). */}
              {allModels ? (
                <div className="text-[11px] text-fg-dim italic">
                  Text only — images aren’t available when {posts ? "posting" : "broadcasting"} from all models.
                </div>
              ) : (
                <div className="space-y-2">
                  {/* Whole-folder pool. */}
                  <div className="flex items-center gap-2 flex-wrap">
                    <span className="text-[11px] text-fg-dim">📁 Folder pool</span>
                    <select
                      value={r.folderId}
                      onChange={(e) => patchRow(i, { folderId: e.target.value })}
                      disabled={!accountId || foldersQ.isLoading}
                      className="text-[11px] bg-bg border border-border rounded-md px-2 py-1 text-fg focus:outline-none focus:border-accent disabled:opacity-50 max-w-[260px]"
                    >
                      <option value="">
                        {!accountId
                          ? "— pick an account first —"
                          : foldersQ.isLoading
                            ? "Loading folders…"
                            : photoFolders.length === 0 ? "— no photo folders —" : "— none —"}
                      </option>
                      {photoFolders.map((f) => (
                        <option key={f.id} value={String(f.id)}>
                          {f.name} ({f.photosCount} photos)
                        </option>
                      ))}
                    </select>
                  </div>

                  {/* Pre-picked image pool. */}
                  <div className="flex items-center gap-2 flex-wrap">
                    <button
                      type="button"
                      onClick={() => setPickerFor(i)}
                      disabled={!accountId}
                      className="text-[11px] px-2 py-1 rounded border border-border hover:border-border-light text-fg-dim hover:text-fg disabled:opacity-50"
                    >
                      {r.media.length ? `🖼 ${r.media.length} in pool` : "🖼 Pick image pool"}
                    </button>
                    {r.media.length > 0 && (
                      <button
                        type="button"
                        onClick={() => patchRow(i, { media: [] })}
                        className="text-[11px] text-fg-dim hover:text-err"
                      >clear</button>
                    )}
                  </div>

                  {/* How many to attach per fire — sampled from the combined pool
                   *  (picked images ∪ folder). Shown once either source is set. */}
                  {(r.media.length > 0 || r.folderId) && (
                    <label className="flex items-center gap-1 text-[11px] text-fg-dim">
                      <span>send</span>
                      <input
                        type="text"
                        inputMode="numeric"
                        value={r.mediaCount}
                        onChange={(e) => patchRow(i, { mediaCount: e.target.value.replace(/[^0-9]/g, "") })}
                        placeholder="1"
                        className="w-12 bg-bg border border-border rounded-md px-2 py-1 text-xs text-fg text-center focus:outline-none focus:border-accent"
                      />
                      <span>
                        image{r.mediaCount === "1" ? "" : "s"} at random per fire
                        {r.media.length > 0 && r.folderId
                          ? ` (from ${r.media.length} picked + folder)`
                          : r.media.length > 0 ? ` (of ${r.media.length} picked)` : " (from folder)"}
                      </span>
                    </label>
                  )}
                </div>
              )}

              <div className="grid grid-cols-2 gap-2">
                {posts ? (
                  <NumField
                    label="Auto-delete after" suffix="hrs · blank = keep"
                    value={r.hoursToLive}
                    onChange={(v) => patchRow(i, { hoursToLive: v })}
                    placeholder="e.g. 6"
                  />
                ) : (
                  <NumField
                    label="Auto-unsend after" suffix="hrs · blank = keep"
                    value={r.unsendAfterHours}
                    onChange={(v) => patchRow(i, { unsendAfterHours: v })}
                    placeholder="e.g. 8"
                  />
                )}
                <NumField
                  label={i === 0 ? "Delay before posting" : "Delay after previous"}
                  suffix="minutes"
                  value={r.delayMinutes}
                  onChange={(v) => patchRow(i, { delayMinutes: v })}
                  placeholder={i === 0 ? "0 (now)" : "e.g. 30"}
                />
                {!posts && (
                  <>
                    <NumField
                      label="Resend this many times" suffix="0 = once"
                      value={r.resendCount}
                      onChange={(v) => patchRow(i, { resendCount: v })}
                      placeholder="0"
                    />
                    <NumField
                      label="…waiting between resends" suffix="hrs"
                      value={r.resendAfterHours}
                      onChange={(v) => patchRow(i, { resendAfterHours: v })}
                      placeholder="e.g. 24"
                    />
                  </>
                )}
              </div>

              {!posts && (
                <div className="border border-border/70 rounded-md p-2.5 space-y-2.5">
                  <div className="text-[11px] uppercase tracking-wide text-fg-dim">
                    Audience
                  </div>

                  <label className="flex items-center gap-2 text-xs cursor-pointer select-none">
                    <input
                      type="checkbox"
                      checked={r.onlineOnly}
                      onChange={(e) => patchRow(i, { onlineOnly: e.target.checked })}
                    />
                    <span>Only fans online right now</span>
                    <span className="text-fg-dim">(OF resolves at send time)</span>
                  </label>

                  {/* System audiences — tri-state chips (off → +include → −exclude). */}
                  <div className="flex flex-wrap gap-1.5">
                    {SYSTEM_AUDIENCES.map((b) => {
                      const state = r.includes.includes(b.id)
                        ? "in"
                        : r.excludes.includes(b.id) ? "ex" : "off";
                      return (
                        <SysChip
                          key={b.id}
                          state={state}
                          onClick={() => cycleSystem(i, b.id)}
                          label={b.label}
                        />
                      );
                    })}
                  </div>

                  {/* Custom lists — include / exclude columns. Per-account, so
                   *  hidden in all-models mode (only system audiences apply). */}
                  {!allModels && (
                    <div className="grid grid-cols-2 gap-3 pt-1 border-t border-border/50">
                      <div>
                        <div className="text-[11px] text-fg-dim mb-1">Custom lists — include</div>
                        <ListColumn
                          lists={customLists}
                          loading={listsQ.isLoading}
                          selected={new Set(r.includes)}
                          onToggle={(id) => toggleListInclude(i, id)}
                          emptyLabel={accountId ? "No custom lists." : "Pick an account."}
                        />
                      </div>
                      <div>
                        <div className="text-[11px] text-fg-dim mb-1">Custom lists — exclude</div>
                        <ListColumn
                          lists={customLists}
                          loading={listsQ.isLoading}
                          selected={new Set(r.excludes)}
                          onToggle={(id) => toggleListExclude(i, id)}
                          emptyLabel="—"
                        />
                      </div>
                    </div>
                  )}

                  {/* DB-sourced add/exclude knobs (resolved server-side). */}
                  <div className="grid grid-cols-2 gap-2 pt-1 border-t border-border/50">
                    <NumField
                      label="Exclude I replied to" suffix="last N hrs"
                      value={r.excludeRepliedHours}
                      onChange={(v) => patchRow(i, { excludeRepliedHours: v })}
                      placeholder="e.g. 2"
                    />
                    <NumField
                      label="Exclude who messaged me" suffix="last N hrs"
                      value={r.excludeInboundHours}
                      onChange={(v) => patchRow(i, { excludeInboundHours: v })}
                      placeholder="e.g. 2"
                    />
                    <NumField
                      label="Include chatted-with" suffix="last N hrs"
                      value={r.recentChatHours}
                      onChange={(v) => patchRow(i, { recentChatHours: v })}
                      placeholder="(optional)"
                    />
                    <NumField
                      label="…cap recent-chat to" suffix="N fans"
                      value={r.recentChatLimit}
                      onChange={(v) => patchRow(i, { recentChatLimit: v })}
                      placeholder="(optional)"
                    />
                    <NumField
                      label="Include fans with unread" suffix="up to N"
                      value={r.unreadLimit}
                      onChange={(v) => patchRow(i, { unreadLimit: v })}
                      placeholder="(optional)"
                    />
                  </div>
                </div>
              )}
            </div>
          ))}

          <button
            type="button"
            onClick={addRow}
            className="w-full text-xs py-2 rounded border border-dashed border-border text-fg-dim hover:text-fg hover:border-border-light"
          >
            + Add another {posts ? "post" : "message"}
          </button>

          {/* How multiple entries combine — no behaviour, just guidance. */}
          <div className="text-[10px] text-fg-dim leading-relaxed">
            {posts ? (
              <>
                Each <b>post</b> here is published on its own, one after another (staggered by its delay) —
                they’re independent, not combined into one post. Inside a post, the <b>text variants</b> are
                alternatives: one is picked at random per fire, not all sent.
              </>
            ) : (
              <>
                Each <b>message</b> here is sent as its own broadcast to its own audience — they
                <b> don’t exclude each other</b>, so a fan who matches two messages gets both. To avoid
                overlap, narrow each audience or use the “exclude” knobs. Inside a message, the
                <b> text variants</b> are alternatives: one is picked at random per send, not all sent.
              </>
            )}
          </div>

          {/* Auto Posts repost cycle — list-level, re-posts the WHOLE list after
           *  it finishes. Shown in both enqueue + return-payload (rule) modes. */}
          {posts && (
            <div className="border border-border/70 rounded-md p-2.5 space-y-2">
              <div className="text-[11px] uppercase tracking-wide text-fg-dim">
                Repost cycle
              </div>
              <div className="grid grid-cols-2 gap-2">
                <NumField
                  label="Repost whole list after" suffix="hrs · blank = once"
                  value={postsResendAfterHours}
                  onChange={setPostsResendAfterHours}
                  placeholder="e.g. 24"
                />
                <NumField
                  label="Repost this many times" suffix="0 = once"
                  value={postsResendCount}
                  onChange={setPostsResendCount}
                  placeholder="0"
                />
              </div>
              <div className="text-[10px] text-fg-dim leading-relaxed">
                After the last post goes out, the whole list re-posts after the gap —
                this many extra times. Leave blank to post once. Pair with each
                post’s auto-delete for a self-refreshing feed.
              </div>
            </div>
          )}

          {/* Dry-run is an enqueue-time choice; in return-payload mode the rule
           *  carries its own `dry_run` knob, so hide it here. */}
          {!composeMode && (
            <label className="flex items-center gap-2 text-xs cursor-pointer select-none">
              <input type="checkbox" checked={dryRun} onChange={(e) => setDryRun(e.target.checked)} />
              <span className="font-medium">Dry run</span>
              <span className="text-fg-dim">(plan only — nothing is posted or sent)</span>
            </label>
          )}

          {okMsg && (
            <div className="text-[11px] text-ok border border-ok/30 bg-ok/5 rounded-md px-3 py-2">
              {okMsg}
            </div>
          )}

          <div className="flex items-center justify-end gap-2 pt-1">
            {error && <span className="text-err text-[11px] mr-auto">{error}</span>}
            <button
              type="button"
              onClick={reset}
              disabled={submit.isPending}
              className="text-xs px-3 py-1.5 rounded border border-border hover:border-border-light text-fg-dim hover:text-fg disabled:opacity-50"
            >
              Reset
            </button>
            {composeMode ? (
              <button
                type="button"
                onClick={useInRule}
                className="text-xs px-4 py-1.5 rounded bg-accent text-white font-medium hover:bg-accent-hover disabled:opacity-50"
              >
                Use in rule
              </button>
            ) : (
              <button
                type="button"
                onClick={() => {
                  setError(null);
                  const real = rows.filter((r) => r.texts.some((t) => t.trim()) || (!allModels && (r.media.length || r.folderId))).length;
                  const verb = posts ? "post" : "broadcast";
                  const note = dryRun ? " (dry run)" : "";
                  const scope = allModels ? ` on ${broadcastAccounts.length} model${broadcastAccounts.length === 1 ? "" : "s"}` : "";
                  if (!confirm(`Enqueue ${real} ${verb}${real === 1 ? "" : "s"}${scope}${note}?`)) return;
                  submit.mutate();
                }}
                disabled={submit.isPending || (allModels ? broadcastAccounts.length === 0 : !accountId)}
                className="text-xs px-4 py-1.5 rounded bg-accent text-white font-medium hover:bg-accent-hover disabled:opacity-50"
              >
                {submit.isPending ? "Enqueuing…" : dryRun ? "Enqueue (dry run)" : "Enqueue"}
              </button>
            )}
          </div>
      </div>

      <VaultPicker
        open={pickerFor !== null}
        onClose={() => setPickerFor(null)}
        accountId={accountId}
        fanId={null}
        initialSelectedIds={pickerFor !== null ? rows[pickerFor].media.map((m) => m.id) : []}
        onConfirm={(picked) => {
          if (pickerFor !== null) patchRow(pickerFor, { media: picked });
          setPickerFor(null);
        }}
      />
    </>
  );
}

/** Tri-state chip for system audiences: off / include (+) / exclude (−).
 *  Mirrors the Mass Online composer's SysChip. */
function SysChip({
  state, onClick, label,
}: {
  state: "off" | "in" | "ex";
  onClick: () => void;
  label: string;
}) {
  const stateStyles = {
    off: "bg-transparent text-fg-dim border-border hover:border-border-light",
    in:  "bg-accent/15 text-accent border-accent/30",
    ex:  "bg-err/15 text-err border-err/30 line-through",
  };
  const prefix = state === "in" ? "+ " : state === "ex" ? "− " : "";
  return (
    <button
      type="button"
      onClick={onClick}
      title="Click to cycle: off → include → exclude → off"
      className={
        "px-2 py-1 rounded-full border text-[11px] flex items-center gap-1.5 transition-colors " +
        stateStyles[state]
      }
    >
      <span>{prefix}{label}</span>
    </button>
  );
}

/** A scrollable checkbox column of custom lists (include OR exclude). Mirrors
 *  the Mass Online composer's ListColumn. */
function ListColumn({
  lists, loading, selected, onToggle, emptyLabel,
}: {
  lists: FanList[];
  loading: boolean;
  selected: Set<string>;
  onToggle: (id: string) => void;
  emptyLabel: string;
}) {
  if (loading) return <div className="text-[11px] text-fg-dim">Loading…</div>;
  if (lists.length === 0) {
    return <div className="text-[11px] text-fg-dim italic">{emptyLabel}</div>;
  }
  return (
    <div className="flex flex-col gap-1 max-h-32 overflow-y-auto pr-1">
      {lists.map((l) => {
        const id = String(l.id);
        return (
          <label
            key={id}
            className="flex items-center gap-1.5 text-xs cursor-pointer hover:bg-bg-elev-1/40 rounded px-1 py-0.5"
          >
            <input type="checkbox" checked={selected.has(id)} onChange={() => onToggle(id)} />
            <span className="flex-1 truncate">{l.name || `List #${id}`}</span>
            <span className="text-[10px] text-fg-dim shrink-0">({l.usersCount ?? 0})</span>
          </label>
        );
      })}
    </div>
  );
}

/** Compact labelled numeric input — blank = the knob isn't applied. */
function NumField({
  label, suffix, value, onChange, placeholder,
}: {
  label: string;
  suffix: string;
  value: string;
  onChange: (v: string) => void;
  placeholder?: string;
}) {
  return (
    <label className="flex flex-col gap-1 text-[11px] text-fg-dim">
      <span className="leading-tight">{label} <span className="opacity-60">({suffix})</span></span>
      <input
        type="text"
        inputMode="numeric"
        value={value}
        onChange={(e) => onChange(e.target.value.replace(/[^0-9.]/g, ""))}
        placeholder={placeholder}
        className="bg-bg border border-border rounded-md px-2 py-1.5 text-xs text-fg focus:outline-none focus:border-accent"
      />
    </label>
  );
}
