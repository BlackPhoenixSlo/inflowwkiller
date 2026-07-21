"use client";

/**
 * ChatSearch — in-chat history search panel backed by OF's native
 * `/chats/{id}/messages/search?query=` endpoint.
 *
 * The OF endpoint returns just an array of message IDs (newest first).
 * Previews come from whatever messages we already have in the React
 * Query cache. For IDs not yet loaded, the row shows "(load context)"
 * and clicking pages older messages back until the target is reached
 * (capped to keep OF happy with its cursor rate limit).
 */

import { useEffect, useMemo, useRef, useState } from "react";

import { relay, type OFMessage } from "@/lib/relay";
import { stripHtmlPreview } from "@/lib/htmlPreview";

export interface ChatSearchProps {
  accountId: string;
  fanId: number;
  messages: OFMessage[];
  ownerUserId: number | null;
  hasOlder: boolean;
  loadingOlder: boolean;
  onLoadOlder: () => Promise<{ added: number; hasMore: boolean }> | void;
  onPick: (messageId: number) => void;
  onClose: () => void;
}

const MAX_PAGES_TO_LOAD = 12; // cap older-page pulls per search session
const AUTO_FILL_COUNT = 3;    // how many top hits we auto-page back to preview

export function ChatSearch(props: ChatSearchProps) {
  const {
    accountId, fanId, messages, ownerUserId,
    hasOlder, loadingOlder, onLoadOlder, onPick, onClose,
  } = props;
  const [q, setQ] = useState("");
  const [debounced, setDebounced] = useState("");
  const [ids, setIds] = useState<number[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [searching, setSearching] = useState(false);
  // Tracks a per-result "loading context" state so the user gets feedback
  // when clicking a not-yet-loaded match triggers a page-back loop.
  const [jumpingId, setJumpingId] = useState<number | null>(null);
  // True while the auto-fill loop is pulling pages to preview the top hits.
  const [autoFilling, setAutoFilling] = useState(false);
  // Sentinel: only auto-fill once per (query, top-ids) tuple — without this,
  // every byId update re-triggers the effect and we'd spam loadOlder.
  const autoFillKeyRef = useRef<string>("");
  const inputRef = useRef<HTMLInputElement | null>(null);
  // Debounce so each keystroke doesn't slam OF's search endpoint.
  useEffect(() => {
    const t = setTimeout(() => setDebounced(q.trim()), 250);
    return () => clearTimeout(t);
  }, [q]);

  useEffect(() => {
    inputRef.current?.focus();
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") onClose();
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  // Run the OF search on the debounced query. Empty query clears state
  // without firing a request.
  useEffect(() => {
    let cancelled = false;
    if (!debounced) {
      setIds([]);
      setError(null);
      setSearching(false);
      return;
    }
    setSearching(true);
    setError(null);
    relay
      .get<unknown>(
        `/api/of/v2/chats/${fanId}/messages/search?query=${encodeURIComponent(debounced)}`,
        { accountId },
      )
      .then((resp) => {
        if (cancelled) return;
        // OF responds with either a flat array of ids OR an envelope like
        // {list: [...]}. Accept both so we don't silently break if OF
        // tweaks the shape.
        const list = Array.isArray(resp)
          ? resp
          : Array.isArray((resp as { list?: unknown[] })?.list)
          ? (resp as { list: unknown[] }).list
          : [];
        const numeric = list
          .map((x) => (typeof x === "number" ? x : Number(x)))
          .filter((n) => Number.isFinite(n));
        setIds(numeric as number[]);
      })
      .catch((err) => {
        if (cancelled) return;
        console.warn("[chat-search] failed", err);
        setError((err as Error)?.message || "Search failed");
        setIds([]);
      })
      .finally(() => {
        if (!cancelled) setSearching(false);
      });
    return () => { cancelled = true; };
  }, [debounced, accountId, fanId]);

  // Build a quick lookup so each result row can render its preview from
  // the messages we already have on screen.
  const byId = useMemo(() => {
    const m = new Map<number, OFMessage>();
    for (const msg of messages) {
      const id = Number(msg.id);
      if (Number.isFinite(id) && id > 0) m.set(id, msg);
    }
    return m;
  }, [messages]);

  // Auto-fill the top hits' previews by paging older messages in until
  // the first AUTO_FILL_COUNT ids are present. Keyed on (query, top-ids)
  // so it runs exactly once per result set even though byId changes each
  // time a page lands.
  useEffect(() => {
    if (!debounced || ids.length === 0 || !hasOlder) return;
    const top = ids.slice(0, AUTO_FILL_COUNT);
    const key = `${debounced}|${top.join(",")}`;
    if (autoFillKeyRef.current === key) return;
    // If everything is already on screen, mark this set as done without
    // running the loop — cheap path for re-searches over already-loaded history.
    if (top.every((id) => byId.has(id))) {
      autoFillKeyRef.current = key;
      return;
    }
    autoFillKeyRef.current = key;
    let cancelled = false;
    setAutoFilling(true);
    (async () => {
      for (let i = 0; i < MAX_PAGES_TO_LOAD && !cancelled; i++) {
        // We can't rely on `byId` here — closure has a stale snapshot. Read
        // the DOM (Bubble stamps `data-msg-id` on each bubble) so we can
        // bail out the moment every target id is rendered.
        const allLoaded = top.every((id) =>
          document.querySelector(`[data-msg-id="${CSS.escape(String(id))}"]`),
        );
        if (allLoaded) break;
        const res = await onLoadOlder();
        const ok = res && typeof res === "object" && "added" in res ? res : null;
        if (!ok || ok.added === 0 || !ok.hasMore) break;
      }
      if (!cancelled) setAutoFilling(false);
    })();
    return () => { cancelled = true; };
    // We intentionally don't depend on byId — the DOM check inside the
    // loop handles "did the previous page bring our targets in?" itself.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [debounced, ids, hasOlder]);

  async function handlePick(id: number) {
    // If the bubble's already on screen, just jump.
    if (byId.has(id)) {
      onPick(id);
      return;
    }
    // Otherwise page back until found (or we exhaust history / cap).
    if (!hasOlder) {
      setError("Match is older than what's been loaded, and OF says there is no more history.");
      return;
    }
    setJumpingId(id);
    try {
      for (let i = 0; i < MAX_PAGES_TO_LOAD; i++) {
        const res = await onLoadOlder();
        const ok = res && typeof res === "object" && "added" in res ? res : null;
        // `byId` is recomputed each render — read it via the latest message
        // snapshot we got back. We can't see updated state in this loop
        // closure, so check via the DOM hook: just look at the current
        // messages prop on next tick. Simplest reliable approach: peek the
        // result counts and break if either we got nothing back or we
        // exhausted history.
        if (!ok) break;
        if (ok.added === 0) break;
        if (!ok.hasMore) break;
        // Quick exit — if the new page contained the id we're after, jump.
        // We check via document because state hasn't propagated to us yet.
        const el = document.querySelector(`[data-msg-id="${CSS.escape(String(id))}"]`);
        if (el) break;
      }
      onPick(id);
    } finally {
      setJumpingId(null);
    }
  }

  return (
    <div className="border-b border-border bg-bg-elev-1/40 px-3 py-2 space-y-2">
      <div className="flex items-center gap-2">
        <input
          ref={inputRef}
          value={q}
          onChange={(e) => setQ(e.target.value)}
          placeholder="Search this conversation…"
          className="flex-1 bg-bg border border-border rounded-md px-2.5 py-2.5 md:py-1.5 text-base md:text-sm focus:outline-none focus:border-accent placeholder:text-muted"
        />
        {(searching || jumpingId != null || autoFilling || loadingOlder) && (
          <span className="text-[11px] text-fg-dim">
            {searching
              ? "Searching…"
              : jumpingId != null
              ? "Loading context…"
              : autoFilling
              ? "Loading top previews…"
              : "Loading…"}
          </span>
        )}
        <button
          type="button"
          onClick={onClose}
          className="text-fg-dim hover:text-fg w-10 h-10 md:w-auto md:h-auto grid md:block place-items-center text-base md:text-[11px] md:px-1"
          title="Close"
          aria-label="Close search"
        >
          ✕
        </button>
      </div>
      {error && (
        <div className="text-[11px] text-err">{error}</div>
      )}
      {debounced && !searching && (
        <div className="text-[10px] text-fg-dim">
          {ids.length} match{ids.length === 1 ? "" : "es"} across the full chat history
          {byId.size > 0 && ids.length > 0 &&
            ` · ${ids.filter((id) => byId.has(id)).length} previewable here`}
        </div>
      )}
      {debounced && ids.length > 0 && (
        <div className="max-h-64 overflow-y-auto rounded-md border border-border bg-bg divide-y divide-border">
          {ids.map((id) => {
            const msg = byId.get(id);
            const outgoing = msg && ownerUserId != null && msg.fromUser?.id === ownerUserId;
            const preview = msg
              ? stripHtmlPreview(msg.text || (msg.mediaCount ? `(${msg.mediaCount} media)` : "(message)"), 160)
              : null;
            return (
              <button
                key={id}
                type="button"
                onClick={() => handlePick(id)}
                disabled={jumpingId != null}
                className="w-full text-left px-2.5 py-3 md:py-1.5 hover:bg-bg-elev-1 text-[12px] disabled:opacity-60"
              >
                <div className="flex items-center gap-2">
                  {msg ? (
                    <span
                      className={
                        outgoing
                          ? "text-accent text-[10px] font-semibold"
                          : "text-info text-[10px] font-semibold"
                      }
                    >
                      {outgoing ? "you" : "fan"}
                    </span>
                  ) : (
                    <span className="text-[10px] font-semibold text-fg-dim">
                      not loaded
                    </span>
                  )}
                  <span className="text-[10px] text-fg-dim">
                    {msg?.createdAt ? fmt(msg.createdAt) : `id ${id}`}
                  </span>
                </div>
                <div className="truncate">
                  {preview
                    ? highlightMatch(preview, debounced)
                    : <span className="italic text-fg-dim">Click to load context…</span>}
                </div>
              </button>
            );
          })}
        </div>
      )}
    </div>
  );
}

function highlightMatch(text: string, needle: string) {
  if (!needle) return text;
  const idx = text.toLowerCase().indexOf(needle.toLowerCase());
  if (idx < 0) return text;
  return (
    <>
      {text.slice(0, idx)}
      <mark className="bg-info/30 text-fg rounded-sm px-0.5">
        {text.slice(idx, idx + needle.length)}
      </mark>
      {text.slice(idx + needle.length)}
    </>
  );
}

function fmt(iso?: string): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  return d.toLocaleString([], {
    month: "short", day: "numeric", hour: "2-digit", minute: "2-digit",
  });
}
