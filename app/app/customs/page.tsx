"use client";

/**
 * /customs — the owed-customs queue.
 *
 * A fan tips $100-$200 and gets back a voice note (later, a short video) made
 * for him. Fulfilment is MANUAL, so this page is the work list: who paid, how
 * much, how long ago, and a button that says it went out.
 *
 * WHY IT LIVES AT /customs AND NOT /admin/customs
 * Next file-system routes win over rewrites, so a page at /admin/customs would
 * shadow the relay endpoint of the same name and this page's own fetch would
 * resolve to its own HTML. Same trap /admin/manage documents.
 *
 * THE PAGE HOLDS NO STATE. "Owed" is the ` Custom` suffix on the fan's OF
 * nickname (see automations/_customs) — the same string the operator sees in the
 * OnlyFans inbox. This is a view over that, so the page, the OF app and the
 * automation can never disagree. Clearing here strips the suffix and pushes it
 * to OF; deleting it by hand in OF works exactly as well, and `customs_watch`
 * clears it too when it sees the voice note go out.
 *
 * Cross-account by default: the queue exists so nobody has to remember to look,
 * and a per-model page is a surface you only visit if you already suspect
 * something is on it.
 */

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";

import { relay } from "@/lib/relay";

/** An account taking custom-sized tips with no watcher on it. */
interface UntrackedRow {
  account_id: string;
  account_name: string;
  qualifying_tips: number;
  biggest_cents: number;
}

interface CustomRow {
  account_id: string;
  account_name: string;
  fan_id: number;
  nickname: string | null;
  display_name: string;
  tip_cents: number | null;
  tipped_at: string | null;
  lifetime_spend_cents: number;
  chat_href: string;
}

function money(cents: number | null | undefined): string {
  if (cents == null) return "—";
  return `$${(cents / 100).toFixed(2)}`;
}

/** How long he has been waiting. The number that should make someone move. */
function waited(iso: string | null): { label: string; hours: number } {
  if (!iso) return { label: "unknown", hours: 0 };
  const ms = Date.now() - Date.parse(iso);
  if (!Number.isFinite(ms) || ms < 0) return { label: "unknown", hours: 0 };
  const h = ms / 3_600_000;
  if (h < 1) return { label: `${Math.round(h * 60)}m`, hours: h };
  if (h < 48) return { label: `${Math.round(h)}h`, hours: h };
  return { label: `${Math.round(h / 24)}d`, hours: h };
}

export default function CustomsPage() {
  const [rows, setRows] = useState<CustomRow[] | null>(null);
  const [untracked, setUntracked] = useState<UntrackedRow[]>([]);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const res = await relay.get<{
        customs: CustomRow[];
        untracked?: UntrackedRow[];
      }>("/admin/customs");
      setRows(res.customs || []);
      setUntracked(res.untracked || []);
      setErr(null);
    } catch (e) {
      setErr(e instanceof Error ? e.message : "failed to load");
      setRows([]);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const clear = useCallback(
    async (r: CustomRow) => {
      const key = `${r.account_id}:${r.fan_id}`;
      setBusy(key);
      try {
        await relay.post("/admin/customs/clear", {
          account_id: r.account_id,
          fan_id: r.fan_id,
        });
        // Drop it locally rather than refetching — the row is gone either way,
        // and an operator working a list of ten should not watch it flicker.
        setRows((prev) =>
          (prev || []).filter(
            (x) => !(x.account_id === r.account_id && x.fan_id === r.fan_id),
          ),
        );
      } catch (e) {
        setErr(e instanceof Error ? e.message : "failed to clear");
      } finally {
        setBusy(null);
      }
    },
    [],
  );

  const owedTotal = (rows || []).reduce((n, r) => n + (r.tip_cents || 0), 0);

  return (
    <main className="mx-auto max-w-5xl px-4 py-8">
      <header className="mb-6 flex flex-wrap items-baseline justify-between gap-3">
        <div>
          <h1 className="text-2xl font-semibold">Customs owed</h1>
          <p className="mt-1 text-sm opacity-70">
            Paid for, not yet sent. Clearing a row also removes the{" "}
            <code className="rounded bg-black/10 px-1">Custom</code> tag from the
            fan&rsquo;s OnlyFans nickname.
          </p>
        </div>
        <button
          onClick={() => void load()}
          className="rounded border px-3 py-1.5 text-sm hover:bg-black/5"
        >
          Refresh
        </button>
      </header>

      {err && (
        <div className="mb-4 rounded border border-red-500/40 bg-red-500/10 px-3 py-2 text-sm">
          {err}
        </div>
      )}

      {/* The one hole in a per-account switch: an eligible account never gets
          the rule and nobody notices — which looks exactly like a healthy empty
          queue. Shown ABOVE the table, and on the empty state too. */}
      {untracked.length > 0 && (
        <div className="mb-5 rounded border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-sm">
          <p className="font-medium">
            Not tracking customs on {untracked.length}{" "}
            {untracked.length === 1 ? "account" : "accounts"}
          </p>
          <p className="mt-1 opacity-80">
            These took tips big enough to be a custom order in the last 14 days,
            with no <code>customs_watch</code> automation enabled. If those tips
            were orders, nothing is recording them.
          </p>
          <ul className="mt-2 space-y-0.5">
            {untracked.map((u) => (
              <li key={u.account_id} className="tabular-nums">
                <span className="font-medium">{u.account_name}</span> —{" "}
                {u.qualifying_tips}{" "}
                {u.qualifying_tips === 1 ? "tip" : "tips"}, biggest{" "}
                {money(u.biggest_cents)}
              </li>
            ))}
          </ul>
        </div>
      )}

      {rows === null && <p className="text-sm opacity-60">Loading…</p>}

      {rows !== null && rows.length === 0 && !err && (
        <div className="rounded border border-dashed px-4 py-10 text-center">
          <p className="font-medium">Nothing owed.</p>
          <p className="mt-1 text-sm opacity-60">
            A qualifying tip adds a fan here automatically.
          </p>
        </div>
      )}

      {rows !== null && rows.length > 0 && (
        <>
          <div className="mb-3 text-sm opacity-70">
            {rows.length} owed · {money(owedTotal)} taken
          </div>
          <div className="overflow-x-auto">
            <table className="w-full border-collapse text-sm">
              <thead>
                <tr className="border-b text-left opacity-60">
                  <th className="py-2 pr-3 font-medium">Waiting</th>
                  <th className="py-2 pr-3 font-medium">Fan</th>
                  <th className="py-2 pr-3 font-medium">Model</th>
                  <th className="py-2 pr-3 font-medium">Paid</th>
                  <th className="py-2 pr-3 font-medium">Lifetime</th>
                  <th className="py-2 font-medium" />
                </tr>
              </thead>
              <tbody>
                {rows.map((r) => {
                  const w = waited(r.tipped_at);
                  const key = `${r.account_id}:${r.fan_id}`;
                  return (
                    <tr key={key} className="border-b last:border-0">
                      {/* Past two days reads as a problem, not a queue item. */}
                      <td
                        className={`py-2 pr-3 tabular-nums ${
                          w.hours >= 48 ? "font-semibold text-red-500" : ""
                        }`}
                      >
                        {w.label}
                      </td>
                      <td className="py-2 pr-3">
                        <Link
                          href={r.chat_href}
                          className="underline underline-offset-2 hover:opacity-70"
                        >
                          {r.display_name}
                        </Link>
                        {r.nickname && (
                          <div className="text-xs opacity-50">{r.nickname}</div>
                        )}
                      </td>
                      <td className="py-2 pr-3 opacity-80">{r.account_name}</td>
                      <td className="py-2 pr-3 tabular-nums font-medium">
                        {money(r.tip_cents)}
                      </td>
                      <td className="py-2 pr-3 tabular-nums opacity-70">
                        {money(r.lifetime_spend_cents)}
                      </td>
                      <td className="py-2 text-right">
                        <button
                          disabled={busy === key}
                          onClick={() => void clear(r)}
                          className="rounded border px-3 py-1 text-xs hover:bg-black/5 disabled:opacity-40"
                        >
                          {busy === key ? "…" : "Sent"}
                        </button>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </>
      )}
    </main>
  );
}
