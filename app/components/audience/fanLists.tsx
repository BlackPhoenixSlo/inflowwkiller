"use client";

import { useMemo } from "react";
import { useQuery } from "@tanstack/react-query";

import { relay } from "@/lib/relay";

/**
 * The house fan-list picker parts, shared by every surface that targets an OF
 * audience (the Mass Messages composer, the PPV Library broadcast). Extracted
 * from MassMessageComposer so there is ONE definition of each rule below — a
 * second copy drifts, and the drift is silent: both versions keep sending.
 *
 * ⚠️ The include/exclude halves are NOT symmetric, and the asymmetry is OF's:
 *
 *   • `userLists` (include) accepts the built-in audience NAMES "fans" /
 *     "following" as well as custom list ids.
 *   • `excludedLists` (exclude) accepts list IDS ONLY. A built-in name placed
 *     there is silently DROPPED by OF — the send succeeds, and the exclusion
 *     protects nobody.
 *
 * So system audiences are include-only (no exclude state to offer), and a
 * caller that persists an exclusion must refuse a name rather than store one
 * that cannot work.
 *
 * IDS COME FROM OF, NOT FROM US. `/api/of/v2/lists` returns OF's own list ids —
 * the ones `userLists`/`excludedLists` resolve against. `/admin/lists` returns
 * our LOCAL primary keys, which are also plain integers, so swapping the
 * endpoint would be accepted silently by every layer while naming a different
 * folder (or none at all). Do not swap it.
 */

export interface FanList {
  id: number | string;
  name?: string;
  /** OF tags lists as 'custom' or 'build-in' (sic) — case-spelling varies. */
  type?: string;
  usersCount?: number;
}

/** OF returns either `{list:[…]}` or a bare array depending on params. */
export type ListsResp = { list?: FanList[]; hasMore?: boolean } | FanList[];

/** OF-recognised virtual audience IDs. The /lists endpoint does NOT
 *  return these — they're hardcoded server-side names that `userLists`
 *  accepts. We expose two OF guarantees on every account: All fans and
 *  Following. The desktop app advertises five (adding `recent`, `online`,
 *  `tagged`) but those depend on per-account state and `online` was
 *  intentionally dropped on user request — the "currently online" filter
 *  isn't actionable for scheduled / mass sends. */
export const SYSTEM_AUDIENCES: Array<{ id: string; label: string }> = [
  { id: "fans",      label: "All fans"  },
  { id: "following", label: "Following" },
];

/** What a broadcast targets when its audience was never configured — the
 *  historical fans + following. Mirrors `audiences.BROADCAST_LISTS` server-side,
 *  and is DERIVED from the chips above so the picker and the default can't drift
 *  into disagreeing about which built-ins exist. */
export const DEFAULT_BROADCAST_LISTS: string[] = SYSTEM_AUDIENCES.map((a) => a.id);

/** Names that should float to the top of the custom-list columns, in
 *  this priority order. Exact lowercase match only — a list named
 *  "fans" sorts first, but variations like "allFans" / "All fans"
 *  stay in alphabetical so they don't crowd the top. */
const TOP_LIST_NAMES: string[] = [
  "fans",
  "following",
];

function topRank(name: string): number {
  const i = TOP_LIST_NAMES.indexOf(name.toLowerCase());
  return i === -1 ? TOP_LIST_NAMES.length : i;
}

/** The account's CUSTOM lists — OF's built-ins filtered out and sorted the
 *  house way. Shared cache key, so opening the composer and the PPV tab in one
 *  session costs one OF call, not two. */
export function useFanLists(accountId: string | null | undefined) {
  const listsQ = useQuery<FanList[]>({
    queryKey: ["fan-lists", accountId ?? ""],
    enabled: !!accountId,
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
    // OF sometimes tags the built-in "fans"/"following" lists with a type the
    // type-filter misses; drop any whose id already names a system audience so
    // they don't show as redundant custom chips sharing a SYSTEM_AUDIENCES id.
    const systemIds = new Set(SYSTEM_AUDIENCES.map((a) => a.id));
    const filtered = (listsQ.data ?? []).filter(
      (l) =>
        l.type !== "build-in" &&
        l.type !== "built-in" &&
        !systemIds.has(String(l.id)),
    );
    // Sort: Fans → Following first (mirrors system-audience order), then
    // alphabetical. Catches lists the user manually re-created under those
    // names too.
    return filtered.slice().sort((a, b) => {
      const an = (a.name ?? "").toLowerCase();
      const bn = (b.name ?? "").toLowerCase();
      const ra = topRank(an);
      const rb = topRank(bn);
      if (ra !== rb) return ra - rb;
      return an.localeCompare(bn);
    });
  }, [listsQ.data]);

  // `data`/`isFetching` ride along for callers that need the RAW list (the
  // composer seeds its MASSPPVEXCLUDE list from it, and must not read a stale
  // account's lists while a refetch is in flight).
  return {
    customLists,
    isLoading: listsQ.isLoading,
    isFetching: listsQ.isFetching,
    data: listsQ.data,
  };
}

/** A system-audience chip. Include-only by construction: OF ignores these
 *  inside `excludedLists`, so there is no exclude state to render. */
export function SysChip({
  state, onClick, label, disabled = false,
}: {
  state: "off" | "in";
  onClick: () => void;
  label: string;
  disabled?: boolean;
}) {
  const stateStyles = {
    off: "bg-transparent text-fg-dim border-border hover:border-border-light",
    in:  "bg-accent/15 text-accent border-accent/30",
  };
  const prefix = state === "in" ? "+ " : "";
  const hint = state === "off" ? "OF built-in" : "included";
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      title="Click to toggle: off → include → off"
      className={
        "px-2 py-1 rounded-full border text-[11px] flex items-center gap-1.5 transition-colors " +
        "disabled:opacity-40 " + stateStyles[state]
      }
    >
      <span>{prefix}{label}</span>
      <span className="opacity-60 no-underline">· {hint}</span>
    </button>
  );
}

/** One checkbox column of custom lists (the include or the exclude side). */
export function ListColumn({
  lists, loading, selected, onToggle, emptyLabel, disabled = false,
}: {
  lists: FanList[];
  loading: boolean;
  selected: Set<string>;
  onToggle: (id: string) => void;
  emptyLabel: string;
  disabled?: boolean;
}) {
  if (loading) return <div className="text-[11px] text-fg-dim">Loading…</div>;
  if (lists.length === 0) {
    return <div className="text-[11px] text-fg-dim italic">{emptyLabel}</div>;
  }
  return (
    <div className="flex flex-col gap-1 max-h-none sm:max-h-32 overflow-visible sm:overflow-y-auto pr-1">
      {lists.map((l) => {
        const id = String(l.id);
        const checked = selected.has(id);
        return (
          <label
            key={id}
            className="flex items-center gap-1.5 text-xs cursor-pointer hover:bg-bg-elev-1/40 rounded px-1 min-h-[40px] sm:min-h-auto py-2 sm:py-0.5"
          >
            <input
              type="checkbox"
              checked={checked}
              disabled={disabled}
              onChange={() => onToggle(id)}
            />
            <span className="flex-1 truncate">{l.name || `List #${id}`}</span>
            <span className="text-[10px] text-fg-dim shrink-0">({l.usersCount ?? 0})</span>
          </label>
        );
      })}
    </div>
  );
}
