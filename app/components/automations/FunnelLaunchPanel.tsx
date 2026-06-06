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

import { useMemo, useState } from "react";
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

  function toggle(set: Set<string>, setter: (s: Set<string>) => void, id: string) {
    const next = new Set(set);
    if (next.has(id)) next.delete(id);
    else next.add(id);
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
      return relay.post("/api/of/v2/messages/queue", body, {
        accountId,
        employeeId: currentEmployee?.id,
      });
    },
    onSuccess: () => setDone("Funnel started — opener queued to OnlyFans. Replies will auto-walk the steps."),
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
    items, set, setter,
  }: {
    items: Array<{ id: string; label: string }>;
    set: Set<string>;
    setter: (s: Set<string>) => void;
  }) => (
    <div className="flex flex-wrap gap-1.5">
      {items.map((it) => {
        const on = set.has(it.id);
        return (
          <button
            key={it.id}
            type="button"
            onClick={() => toggle(set, setter, it.id)}
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

  const listItems = customLists.map((l) => ({ id: String(l.id), label: l.name }));

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
        <ChipRow items={SYSTEM_AUDIENCES} set={includes} setter={setIncludes} />
        {listItems.length > 0 && <ChipRow items={listItems} set={includes} setter={setIncludes} />}
      </div>

      {/* Exclude */}
      <div className="space-y-1.5">
        <div className="text-xs text-fg-dim">Exclude</div>
        <ChipRow items={[...SYSTEM_AUDIENCES, ...listItems]} set={excludes} setter={setExcludes} />
      </div>

      {/* Online + skip-recent */}
      <div className="flex flex-wrap items-end gap-4">
        <label className="flex items-center gap-2 cursor-pointer">
          <input
            type="checkbox"
            className="h-4 w-4 accent-[var(--accent)]"
            checked={onlineOnly}
            onChange={(e) => setOnlineOnly(e.target.checked)}
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
            onChange={(e) => setSkipMessagedHours(e.target.value)}
          />
        </label>
      </div>

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
