"use client";

/**
 * FunnelLaunchPanel — "Start" a funnel from the Mass Funnels tab.
 *
 * Fires the funnel's OPENING message to a chosen audience via the same path the
 * Mass composer uses (POST /api/of/v2/messages/queue with `funnel_id`), so the
 * mass_run is funnel-linked and reply_mass_funnel walks the reply/PPV steps for
 * every fan who answers. The audience controls map 1:1 to the relay's queue body:
 *   include lists  → user_lists           exclude lists → excluded_user_lists
 *   Online only    → online_only          Skip recently-messaged → exclude_replied_hours
 * (exclude_replied_hours drops fans we sent an OUTBOUND message to in the last N
 *  hours — see service/audiences.resolve_mass_audience.)
 *
 * Real send: a confirm() gate guards the blast. price=0 (the opener is free; the
 * paid PPV lives in a later funnel step).
 */

import { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { Megaphone, X } from "lucide-react";

import { Button, Card, Input } from "@/components/ui/primitives";
import { relay } from "@/lib/relay";
import { useEmployee } from "@/contexts/EmployeeContext";
import { useActiveAccounts } from "@/hooks/useAccounts";
import type { FunnelSummary } from "@/hooks/useFunnels";

interface FanList {
  id: string | number;
  name: string;
  type?: string;
}
type ListsResp = { list?: FanList[] } | FanList[];

// OF virtual audiences (not returned by /lists) — same two we expose elsewhere.
const SYSTEM_AUDIENCES: Array<{ id: string; label: string }> = [
  { id: "fans", label: "All fans" },
  { id: "following", label: "Following" },
];

// The standing "never mass PPV" exclude list, kept per account with this exact
// name. Auto-selected in the Exclude set on open so every funnel start skips it
// by default; the operator can untick it for a one-off. Matched case-insensitively.
export const MASS_EXCLUDE_LIST_NAME = "MASSPPVEXCLUDE";
// Default auto-unsend window for a funnel opener (hours). Pre-fills the field.
const DEFAULT_UNSEND_HOURS = "4";

export function FunnelLaunchPanel({
  funnel,
  accountId,
  onClose,
}: {
  funnel: FunnelSummary;
  accountId: string | null;
  onClose: () => void;
}) {
  const { current: currentEmployee } = useEmployee();
  const accounts = useActiveAccounts();
  const accountName = accounts.find((a) => a.id === accountId)?.nickname ?? accountId ?? "";

  const [includes, setIncludes] = useState<Set<string>>(() => new Set(["fans"]));
  const [excludes, setExcludes] = useState<Set<string>>(() => new Set());
  const [onlineOnly, setOnlineOnly] = useState(false);
  const [skipMessagedHours, setSkipMessagedHours] = useState("");
  // Auto-unsend the opener N hours after it sends. When it's unsent we also STOP
  // enrolling NEW repliers off this mass (the walker keeps going until a buy).
  // Pre-filled with the default; blank → no per-send auto-unsend.
  const [unsendHours, setUnsendHours] = useState(DEFAULT_UNSEND_HOURS);
  const [excludesSeeded, setExcludesSeeded] = useState(false);
  const [done, setDone] = useState<string | null>(null);

  const listsQ = useQuery<FanList[]>({
    queryKey: ["fan-lists", accountId ?? ""],
    enabled: !!accountId,
    staleTime: 5 * 60_000,
    queryFn: async () => {
      const r = await relay.get<ListsResp>("/api/of/v2/lists?limit=50", { accountId: accountId! });
      return Array.isArray(r) ? r : (r?.list ?? []);
    },
  });
  const customLists = useMemo(
    () => (listsQ.data ?? []).filter((l) => l.type !== "build-in" && l.type !== "built-in"),
    [listsQ.data],
  );

  // Default the standing MASSPPVEXCLUDE list into the exclude set once the
  // account's lists load (only if it exists; operator can untick it). Seeded
  // once per open so a manual untick isn't re-added on the next render.
  useEffect(() => {
    if (excludesSeeded || !listsQ.data) return;
    const match = (listsQ.data ?? []).find(
      (l) => String(l.name ?? "").trim().toUpperCase() === MASS_EXCLUDE_LIST_NAME,
    );
    if (match) setExcludes((prev) => new Set(prev).add(String(match.id)));
    setExcludesSeeded(true);
  }, [excludesSeeded, listsQ.data]);

  // Toggle `id` in `set`. When adding it, drop it from the opposite set so an
  // audience can't be both included and excluded (a contradictory queue body).
  function toggle(
    set: Set<string>,
    setter: (s: Set<string>) => void,
    other: Set<string>,
    otherSetter: (s: Set<string>) => void,
    id: string,
  ) {
    setDone(null);
    const next = new Set(set);
    if (next.has(id)) {
      next.delete(id);
    } else {
      next.add(id);
      if (other.has(id)) {
        const otherNext = new Set(other);
        otherNext.delete(id);
        otherSetter(otherNext);
      }
    }
    setter(next);
  }

  const send = useMutation({
    mutationFn: async () => {
      if (!accountId) throw new Error("Pick a model (account) above first");
      if (includes.size === 0) throw new Error("Pick at least one audience to include");
      const body: Record<string, unknown> = {
        text: funnel.opening_message,
        user_lists: Array.from(includes),
        user_ids: [],
        excluded_users: [],
        excluded_user_lists: Array.from(excludes),
        price: 0,
        locked_text: false,
        media_files: [],
        funnel_id: funnel.id,
      };
      if (onlineOnly) body.online_only = true;
      const h = Number(skipMessagedHours);
      if (skipMessagedHours.trim() !== "" && Number.isFinite(h) && h > 0) {
        body.exclude_replied_hours = h;
      }
      const uh = Number(unsendHours);
      if (unsendHours.trim() !== "" && Number.isFinite(uh) && uh > 0) {
        body.unsend_after_hours = uh;
      }
      return relay.post<{ _skipped_already_answered?: number }>(
        "/api/of/v2/messages/queue", body,
        { accountId, employeeId: currentEmployee?.id },
      );
    },
    onSuccess: (resp) => {
      const skipped = Number(resp?._skipped_already_answered) || 0;
      const base = "Funnel started — opener queued to OnlyFans. Replies will auto-walk the steps.";
      setDone(skipped > 0
        ? `${base} (${skipped} already answered this funnel — skipped.)`
        : base);
    },
  });

  function confirmAndSend() {
    setDone(null);
    const who = onlineOnly ? "ONLINE fans in" : "fans in";
    const ok = window.confirm(
      `Send "${funnel.name}" opener to ${who} the selected audience on ${accountName}?`,
    );
    if (ok) send.mutate();
  }

  const ChipRow = ({
    items, set, setter, other, otherSetter,
  }: {
    items: Array<{ id: string; label: string }>;
    set: Set<string>;
    setter: (s: Set<string>) => void;
    other: Set<string>;
    otherSetter: (s: Set<string>) => void;
  }) => (
    <div className="flex flex-wrap gap-1.5">
      {items.map((it) => {
        const on = set.has(it.id);
        return (
          <button
            key={it.id}
            type="button"
            onClick={() => toggle(set, setter, other, otherSetter, it.id)}
            className={
              "px-2.5 py-1 rounded-full text-xs border transition-colors " +
              (on
                ? "border-accent bg-accent/15 text-fg"
                : "border-border text-fg-dim hover:text-fg")
            }
          >
            {it.label}
          </button>
        );
      })}
    </div>
  );

  // OF sometimes tags the built-in "fans"/"following" lists with a type the
  // filter above misses, so they leak through and collide with SYSTEM_AUDIENCES
  // (duplicate React keys + duplicate chips). Drop any list whose id already
  // names a system audience.
  const systemIds = new Set(SYSTEM_AUDIENCES.map((a) => a.id));
  const listItems = customLists
    .map((l) => ({ id: String(l.id), label: l.name }))
    .filter((it) => !systemIds.has(it.id));

  return (
    <Card className="p-4 space-y-4 border-accent/40">
      <header className="flex items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          <Megaphone size={16} className="text-accent" />
          <h3 className="text-sm font-medium">Start funnel · {funnel.name}</h3>
        </div>
        <button type="button" onClick={onClose} className="text-fg-dim hover:text-fg">
          <X size={16} />
        </button>
      </header>

      <div className="text-[11px] text-fg-dim">
        Sends this opener now, then auto-replies to fans who answer:
        <div className="mt-1 rounded-md bg-bg border border-border px-3 py-2 text-fg text-xs">
          {funnel.opening_message}
        </div>
      </div>

      {!accountId && (
        <div className="text-xs text-amber-500">Pick a model (account) in the chips above first.</div>
      )}

      {/* Include */}
      <div className="space-y-1.5">
        <div className="text-xs text-fg-dim">Send to (include)</div>
        <ChipRow items={SYSTEM_AUDIENCES} set={includes} setter={setIncludes} other={excludes} otherSetter={setExcludes} />
        {listItems.length > 0 && (
          <ChipRow items={listItems} set={includes} setter={setIncludes} other={excludes} otherSetter={setExcludes} />
        )}
      </div>

      {/* Exclude */}
      <div className="space-y-1.5">
        <div className="text-xs text-fg-dim">Exclude</div>
        <ChipRow items={[...SYSTEM_AUDIENCES, ...listItems]} set={excludes} setter={setExcludes} other={includes} otherSetter={setIncludes} />
      </div>

      {/* Online + skip-recent */}
      <div className="flex flex-wrap items-end gap-4">
        <label className="flex items-center gap-2 cursor-pointer">
          <input
            type="checkbox"
            className="h-4 w-4 accent-[var(--accent)]"
            checked={onlineOnly}
            onChange={(e) => { setDone(null); setOnlineOnly(e.target.checked); }}
          />
          <span className="text-xs">Online only</span>
        </label>
        <label className="space-y-1">
          <div className="text-[11px] text-fg-dim">Skip fans I messaged in the last … hours</div>
          <Input
            type="number"
            min={0}
            className="w-28"
            placeholder="e.g. 2"
            value={skipMessagedHours}
            onChange={(e) => { setDone(null); setSkipMessagedHours(e.target.value); }}
          />
        </label>
        <label className="space-y-1">
          <div className="text-[11px] text-fg-dim">Auto-unsend opener after … hours</div>
          <Input
            type="number"
            min={0}
            className="w-28"
            placeholder="e.g. 4"
            value={unsendHours}
            onChange={(e) => { setDone(null); setUnsendHours(e.target.value); }}
          />
        </label>
      </div>
      <p className="text-[11px] text-fg-dim -mt-1">
        Fans who already answered this funnel are skipped automatically, and your{" "}
        <span className="text-fg-dim font-medium">{MASS_EXCLUDE_LIST_NAME}</span>{" "}
        list is excluded by default. When the opener is unsent, new repliers stop
        enrolling — but anyone already chatting keeps going until they buy.
      </p>

      <div className="flex items-center gap-3 pt-1">
        <Button variant="primary" onClick={confirmAndSend} disabled={send.isPending || !accountId}>
          {send.isPending ? "Sending…" : "▶ Start funnel"}
        </Button>
        {done && <span className="text-xs text-emerald-500">{done}</span>}
        {send.isError && (
          <span className="text-xs text-err">{(send.error as Error)?.message || "Send failed"}</span>
        )}
      </div>
    </Card>
  );
}
