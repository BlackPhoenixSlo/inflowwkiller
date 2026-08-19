"use client";

/**
 * /customs — the owed-customs queue.
 *
 * A fan tips for a custom and gets back a VOICE NOTE made for him — audio,
 * only ever audio. Orders are TRACKED from $50 (stacked tips count as one
 * order); the price the chatter QUOTES stays in the $100-$200 band.
 * Fulfilment is MANUAL, so this page is the work list: who paid, how much,
 * how long ago, and a button that says it went out.
 *
 * WHY IT LIVES AT /customs AND NOT /admin/customs
 * Next file-system routes win over rewrites, so a page at /admin/customs would
 * shadow the relay endpoint of the same name and this page's own fetch would
 * resolve to its own HTML. Same trap /admin/manage documents.
 *
 * THE PAGE HOLDS NO STATE. "Owed" is the
 * column `fans.customs_owed_at` (see automations/_customs). This page is the
 * ONLY operator surface for it: the marker used to live on the fan's OnlyFans
 * nickname, but that field is rewritten from structured facts by the chat engines
 * on every tick, so it was erased about a minute after it was written. Clearing
 * here blanks the column and stamps it settled; `customs_watch` clears it too
 * when it sees the voice note go out.
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
  /** true = tagged Custom and owed. false = a qualifying tip nobody watched;
   *  a REVIEW row, which may equally have been plain generosity. */
  marked?: boolean;
  account_id: string;
  account_name: string;
  fan_id: number;
  display_name: string;
  tip_cents: number | null;
  tipped_at: string | null;
  lifetime_spend_cents: number;
  chat_href: string;
}

interface ContextMsg {
  message_id: number;
  from_fan: boolean;
  text: string;
  at: string | null;
  is_tip: boolean;
  price_cents: number;
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


function Section({
  title,
  note,
  rows,
  busy,
  onClear,
}: {
  title: string;
  note: string;
  rows: CustomRow[];
  busy: string | null;
  onClear: (r: CustomRow) => void;
}) {
  const isReview = rows[0]?.marked === false;
  // Which row is expanded, and its thread. ONE at a time: this is a read-to-
  // decide surface, and two open threads is two things to hold in your head.
  const [openKey, setOpenKey] = useState<string | null>(null);
  const [ctx, setCtx] = useState<ContextMsg[] | null>(null);

  const toggle = useCallback(
    async (r: CustomRow) => {
      const key = `${r.account_id}:${r.fan_id}`;
      if (openKey === key) {
        setOpenKey(null);
        return;
      }
      setOpenKey(key);
      setCtx(null);
      try {
        const q = new URLSearchParams({
          account_id: r.account_id,
          fan_id: String(r.fan_id),
        });
        if (r.tipped_at) q.set("at", r.tipped_at);
        const res = await relay.get<{ messages: ContextMsg[] }>(
          `/admin/customs/context?${q.toString()}`,
        );
        setCtx(res.messages || []);
      } catch {
        setCtx([]);
      }
    },
    [openKey],
  );

  return (
    <section className="mb-8">
      <h2 className="mb-2 text-sm font-semibold">
        {title} <span className="ml-1 font-normal opacity-60">{note}</span>
      </h2>
      <div className="overflow-x-auto">
        <table className="w-full border-collapse text-sm">
          <thead>
            <tr className="border-b text-left opacity-60">
              {/* The owed mark. It lives in fans.customs_owed_at now, so this
                  column IS the ledger made visible — there is no longer a copy of
                  it on OnlyFans to cross-check against. */}
              <th className="w-6 py-2 pr-2 font-medium" aria-label="Owed" />
              <th className="py-2 pr-3 font-medium">
                {isReview ? "Tipped" : "Waiting"}
              </th>
              <th className="py-2 pr-3 font-medium">Fan</th>
              <th className="py-2 pr-3 font-medium">Model</th>
              <th className="py-2 pr-3 font-medium">Paid</th>
              <th className="py-2 pr-3 font-medium">Lifetime</th>
              <th className="py-2 font-medium" />
            </tr>
          </thead>
          <tbody>
            {rows.flatMap((r) => {
              const w = waited(r.tipped_at);
              const key = `${r.account_id}:${r.fan_id}`;
              const open = openKey === key;
              const cells = (
                <tr key={key} className="border-b last:border-0">
                  {/* ✓ = tagged owed by customs_watch. ? = a qualifying tip that
                      nothing was watching for, which may equally be generosity —
                      so it gets a different glyph, not a quieter one. */}
                  <td
                    className="py-2 pr-2 text-center"
                    title={
                      isReview
                        ? "Qualifying tip, never tagged — check the thread"
                        : "Owed a custom"
                    }
                  >
                    <span
                      aria-hidden="true"
                      className={isReview ? "opacity-40" : "text-emerald-600"}
                    >
                      {isReview ? "?" : "✓"}
                    </span>
                    <span className="sr-only">
                      {isReview ? "Needs review" : "Owed"}
                    </span>
                  </td>
                  {/* Two days of an UNPAID debt is a problem. Two days since a
                      tip nobody tagged is just a date. */}
                  <td
                    className={`py-2 pr-3 tabular-nums ${
                      !isReview && w.hours >= 48 ? "font-semibold text-red-500" : ""
                    }`}
                  >
                    {w.label}
                  </td>
                  <td className="py-2 pr-3">
                    {/* ?msg=<tip id> — the thread opens on the tip itself, the
                        message that has to be read to decide. */}
                    <Link
                      href={r.chat_href}
                      className="underline underline-offset-2 hover:opacity-70"
                    >
                      {r.display_name}
                    </Link>
                  </td>
                  <td className="py-2 pr-3 opacity-80">{r.account_name}</td>
                  <td className="py-2 pr-3 tabular-nums font-medium">
                    {money(r.tip_cents)}
                  </td>
                  <td className="py-2 pr-3 tabular-nums opacity-70">
                    {money(r.lifetime_spend_cents)}
                  </td>
                  <td className="py-2 text-right whitespace-nowrap">
                    {/* Read the thread WITHOUT leaving the page. The tip says a
                        man paid; the messages around it say what he paid FOR,
                        which is the whole decision on a review row. */}
                    <button
                      onClick={() => void toggle(r)}
                      className="mr-2 rounded border px-2 py-1 text-xs hover:bg-black/5"
                      title="Read the conversation around this tip"
                    >
                      {openKey === key ? "Hide" : "Read"}
                    </button>
                    <button
                      disabled={busy === key}
                      onClick={() => onClear(r)}
                      className="rounded border px-3 py-1 text-xs hover:bg-black/5 disabled:opacity-40"
                      title={
                        isReview
                          ? "Nothing owed here — settle it and stop showing it"
                          : "Mark delivered; also clears the Custom tag on OnlyFans"
                      }
                    >
                      {busy === key ? "…" : isReview ? "Dismiss" : "Sent"}
                    </button>
                  </td>
                </tr>
              );
              if (!open) return [cells];
              return [
                cells,
                <tr key={`${key}:ctx`} className="border-b last:border-0">
                  <td colSpan={7} className="bg-black/[0.03] px-3 py-3">
                    {ctx === null && (
                      <p className="text-xs opacity-60">Reading…</p>
                    )}
                    {ctx !== null && ctx.length === 0 && (
                      <p className="text-xs opacity-60">
                        No messages stored around this tip.
                      </p>
                    )}
                    {ctx !== null && ctx.length > 0 && (
                      <div className="space-y-1.5">
                        {ctx.map((m) => (
                          <div
                            key={m.message_id}
                            className={`flex gap-2 text-xs ${
                              m.from_fan ? "" : "opacity-70"
                            }`}
                          >
                            <span className="w-10 shrink-0 font-medium">
                              {m.from_fan ? "fan" : "us"}
                            </span>
                            <span className="min-w-0">
                              {/* A tip row has no body — show the money, which
                                  is the thing being explained. */}
                              {m.is_tip ? (
                                <em className="not-italic font-semibold">
                                  💸 tipped{" "}
                                  {m.text.match(/\$[\d,.]+/)?.[0] ?? ""}
                                </em>
                              ) : (
                                m.text || <span className="opacity-40">(media)</span>
                              )}
                              {m.price_cents > 0 && (
                                <span className="ml-1 opacity-60">
                                  [{money(m.price_cents)} locked]
                                </span>
                              )}
                            </span>
                          </div>
                        ))}
                      </div>
                    )}
                  </td>
                </tr>,
              ];
            })}
          </tbody>
        </table>
      </div>
    </section>
  );
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

  const owed = (rows || []).filter((r) => r.marked !== false);
  const review = (rows || []).filter((r) => r.marked === false);
  const owedTotal = owed.reduce((n, r) => n + (r.tip_cents || 0), 0);

  return (
    <main className="mx-auto max-w-5xl px-4 py-8">
      <header className="mb-6 flex flex-wrap items-baseline justify-between gap-3">
        <div>
          <h1 className="text-2xl font-semibold">Customs owed</h1>
          <p className="mt-1 text-sm opacity-70">
            Paid for, not yet sent. Every custom is a voice note. Clearing a row
            marks it delivered and stops the AI selling to him again &mdash; this
            page is the only place that happens.
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

      {/* TWO LISTS, not one table with a flag. A marked row is DEBT — money
          taken, work owed, red past two days. An unmarked row is a QUESTION —
          a tip that may equally have been generosity. Sorting them into one
          oldest-first list buries a real 12-hour debt under a 50-day maybe,
          and the "Waiting" column means a different thing in each. */}
      {rows !== null && owed.length > 0 && (
        <Section
          title="Owed"
          note={`${owed.length} · ${money(owedTotal)} taken`}
          rows={owed}
          busy={busy}
          onClear={clear}
        />
      )}

      {rows !== null && review.length > 0 && (
        <Section
          title="Worth a look"
          note={`${review.length} · tips big enough to be an order, never tagged`}
          rows={review}
          busy={busy}
          onClear={clear}
        />
      )}

    </main>
  );
}
