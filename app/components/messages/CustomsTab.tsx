"use client";

/**
 * CustomsTab — the owed-customs queue, rendered as a tab of /messages ("Stuff").
 *
 * A fan tips for a custom and gets back a VOICE NOTE made for him — audio,
 * only ever audio. Orders are TRACKED from $50 (stacked tips count as one
 * order); the price the chatter QUOTES stays in the $100-$200 band.
 * Fulfilment is MANUAL, so this tab is the work list: who paid, how much,
 * how long ago, and a button that says it went out.
 *
 * WHY IT IS A TAB AND NOT ITS OWN NAV ENTRY
 * The queue is money already taken against work not yet done — it belongs
 * beside the other money surfaces (PPV, Tips), not as a tenth item competing
 * with them in the nav strip. `/customs` still resolves: the route redirects
 * to `?tab=customs`, so every link the manual, the settings copy and the help
 * bot already print keeps landing here.
 *
 * THE TAB HOLDS NO STATE. "Owed" is the
 * column `fans.customs_owed_at` (see automations/_customs). This is the
 * ONLY operator surface for it: the marker used to live on the fan's OnlyFans
 * nickname, but that field is rewritten from structured facts by the chat engines
 * on every tick, so it was erased about a minute after it was written. Clearing
 * here blanks the column and stamps it settled; `customs_watch` clears it too
 * when it sees the voice note go out.
 *
 * Cross-account by default, and deliberately ignores the page's date range:
 * the queue exists so nobody has to remember to look, and a debt does not stop
 * being owed because it fell out of a 30-day window.
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
  /** How many SEPARATE orders make up `tip_cents`. Two $100 tips a day apart
   *  are two voice notes, and the queue used to show one. 0 on a row whose
   *  amount could not be reconstructed from transactions. */
  order_count?: number;
  tipped_at: string | null;
  lifetime_spend_cents: number;
  chat_href: string;
}

/** The recording brief. Amounts come from the DB, never from the model —
 *  see `/admin/customs/brief`. */
interface Brief {
  ok: boolean;
  reason?: string;
  paid_cents?: number;
  order_count?: number;
  /** false = the model could not see him ask for anything in this excerpt.
   *  Rendered as a warning, never as a work order. */
  found_request?: boolean;
  summary?: string;
  deliverables?: string[];
  say_by_name?: string | null;
  uncertain?: string[];
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


/** The fan's own words, most likely to BE the work order.
 *
 *  His longest inbound message in the pane. No model, no inference, no
 *  paraphrase — a summary that quietly rewrites "25 back 25 front" is worse
 *  than no summary at all, because the operator films against it. This is
 *  verbatim or nothing, and when it guesses wrong the raw thread is directly
 *  underneath it.
 *
 *  Tip rows carry no body of their own (the money is the content), so they are
 *  skipped; very short replies ("ok", "yes") never carry a brief. */
function workOrderQuote(msgs: ContextMsg[]): string | null {
  let best: string | null = null;
  for (const m of msgs) {
    if (!m.from_fan || m.is_tip) continue;
    const t = (m.text || "").trim();
    if (t.length < 25) continue;
    if (best === null || t.length > best.length) best = t;
  }
  return best;
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
  // Which row is mid-pull. The re-scrape is synchronous (~1-2s) and the pane
  // must not look merely slow while it runs.
  const [pulling, setPulling] = useState<string | null>(null);
  // The brief for the open row. `undefined` = never asked (the operator has to
  // click), `null` = in flight.
  const [brief, setBrief] = useState<Brief | null | undefined>(undefined);

  /** Load the thread around this row's anchor tip into the pane. */
  const loadCtx = useCallback(async (r: CustomRow) => {
    setCtx(null);
    setBrief(undefined);
    try {
      const q = new URLSearchParams({
        account_id: r.account_id,
        fan_id: String(r.fan_id),
        // The endpoint accepts up to 60 either side and we were sending
        // neither, so every Read silently took the 18-before default. A fan
        // with two orders on different days needs to reach back past the
        // newer tip to the conversation that placed the older one.
        before: "50",
        after: "10",
      });
      if (r.tipped_at) q.set("at", r.tipped_at);
      const res = await relay.get<{ messages: ContextMsg[] }>(
        `/admin/customs/context?${q.toString()}`,
      );
      setCtx(res.messages || []);
    } catch {
      setCtx([]);
    }
  }, []);

  const toggle = useCallback(
    async (r: CustomRow) => {
      const key = `${r.account_id}:${r.fan_id}`;
      if (openKey === key) {
        setOpenKey(null);
        return;
      }
      setOpenKey(key);
      await loadCtx(r);
    },
    [openKey, loadCtx],
  );

  /** Ask the model what to record. ON CLICK ONLY — a call per row on queue
   *  load would be money spent on rows nobody opens. Best-effort: a failure
   *  renders as a line in the pane, never as the page-wide error banner, and
   *  the raw thread underneath is unaffected. */
  const askBrief = useCallback(async (r: CustomRow) => {
    setBrief(null);
    try {
      const res = await relay.post<Brief>("/admin/customs/brief", {
        account_id: r.account_id,
        fan_id: r.fan_id,
        at: r.tipped_at,
      });
      setBrief(res);
    } catch (e) {
      setBrief({ ok: false, reason: e instanceof Error ? e.message : "failed" });
    }
  }, []);

  /** Re-scrape this fan's thread from OF, then re-read the pane.
   *
   *  The context pane can only show what we have STORED, so on a thread the
   *  scraper never reached the work order simply is not there. This is the
   *  existing chat ↻ endpoint (`rescrape-now`) — synchronous, ~1-2s, and
   *  best-effort: it returns ok=false rather than throwing, so a failed pull
   *  leaves the pane exactly as it was instead of blanking it. */
  const pull = useCallback(
    async (r: CustomRow) => {
      const key = `${r.account_id}:${r.fan_id}`;
      setPulling(key);
      try {
        await relay.post(
          `/admin/messages/${r.account_id}/${r.fan_id}/rescrape-now`,
          {},
        );
        setOpenKey(key);
        await loadCtx(r);
      } catch {
        // Best-effort by design — keep whatever the pane already had.
      } finally {
        setPulling(null);
      }
    },
    [loadCtx],
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
                    {/* TWO $100 tips a day apart are TWO voice notes. The total
                        alone reads as one big custom, so the count is what makes
                        it legible. Shown only when it is more than one — a "1"
                        on every row is noise. */}
                    {(r.order_count || 0) > 1 && (
                      <span
                        className="ml-1 font-normal opacity-60"
                        title={`${r.order_count} separate orders — ${r.order_count} voice notes owed`}
                      >
                        · {r.order_count} tips
                      </span>
                    )}
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
                    {/* When the stored thread is thin (or the work order sits
                        further back than we ever scraped), pull it from OF and
                        re-read. Same endpoint the chat ↻ uses. */}
                    <button
                      disabled={pulling === key}
                      onClick={() => void pull(r)}
                      className="mr-2 rounded border px-2 py-1 text-xs hover:bg-black/5 disabled:opacity-40"
                      title="Re-fetch this chat from OnlyFans, then re-read it"
                    >
                      {pulling === key ? "…" : "Pull"}
                    </button>
                    <button
                      disabled={busy === key}
                      onClick={() => onClear(r)}
                      className="rounded border px-3 py-1 text-xs hover:bg-black/5 disabled:opacity-40"
                      title={
                        isReview
                          ? "Nothing owed here — settle it and stop showing it"
                          : (r.order_count || 0) > 1
                            // HONEST about the one thing this button cannot do.
                            // The ledger is a single nullable timestamp, so one
                            // click settles EVERY outstanding order for this fan
                            // — there is no way to say "I sent one of two", and
                            // customs_watch will not re-mark what is stamped
                            // cleared. Say so where the click happens.
                            ? `Mark delivered — settles ALL ${r.order_count} orders for this fan, not just one`
                            : "Mark delivered; also clears the Custom tag on OnlyFans"
                      }
                    >
                      {busy === key ? "…" : isReview ? "Dismiss" : "Sent"}
                    </button>
                  </td>
                </tr>
              );
              // The brief's four visible states, named once. `undefined` =
              // never asked, `null` = in flight; deriving the phase here keeps
              // that encoding out of the JSX below.
              const briefPhase: "idle" | "loading" | "failed" | "not-found" | "ready" =
                brief === undefined
                  ? "idle"
                  : brief === null
                    ? "loading"
                    : !brief.ok
                      ? "failed"
                      : brief.found_request === false
                        ? "not-found"
                        : "ready";
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
                    {/* WHAT TO FILM. The button is the whole gate on cost —
                        nothing is generated until someone asks. */}
                    {ctx !== null && ctx.length > 0 && (
                      <div className="mb-3">
                        {/* ONE state, four renders. `undefined`/`null` as
                            "never asked"/"in flight" reads as two unrelated
                            nullables at the call site, so the phase is named
                            once here and switched on once below. */}
                        {briefPhase === "idle" && (
                          <button
                            onClick={() => void askBrief(r)}
                            className="rounded border px-2 py-1 text-xs hover:bg-black/5"
                            title="Read the thread and say what the voice note has to contain"
                          >
                            What to film?
                          </button>
                        )}
                        {briefPhase === "loading" && (
                          <p className="text-xs opacity-60">Reading the thread…</p>
                        )}
                        {briefPhase === "failed" && brief && (
                          <p className="text-xs opacity-60">
                            Couldn&rsquo;t summarise ({brief.reason || "unknown"}) &mdash;
                            the thread is below.
                          </p>
                        )}
                        {briefPhase === "not-found" && brief && (
                          <div className="rounded border border-amber-500/40 bg-amber-500/10 px-3 py-2">
                            <p className="text-xs font-semibold">
                              His ask isn&rsquo;t in this excerpt
                            </p>
                            <p className="mt-1 text-xs opacity-80">
                              {brief.summary ||
                                "He never says what he wants in the messages we have."}{" "}
                              Try &ldquo;Pull&rdquo; to fetch more of the thread from
                              OnlyFans, or read it below.
                            </p>
                          </div>
                        )}
                        {briefPhase === "ready" && brief && (
                          <div className="rounded border border-emerald-600/40 bg-emerald-600/5 px-3 py-2">
                            <p className="text-xs font-semibold">
                              Work order
                              {/* From the DB. The model is never asked for a
                                  figure — see the endpoint's docstring. */}
                              {brief.paid_cents ? (
                                <span className="ml-1 font-normal opacity-70">
                                  · {money(brief.paid_cents)}
                                  {(brief.order_count || 0) > 1
                                    ? ` · ${brief.order_count} tips`
                                    : ""}
                                </span>
                              ) : null}
                            </p>
                            {brief.summary && (
                              <p className="mt-1 text-xs">{brief.summary}</p>
                            )}
                            {(brief.deliverables || []).length > 0 && (
                              <ul className="mt-1 list-disc pl-4 text-xs">
                                {(brief.deliverables || []).map((d, i) => (
                                  <li key={i}>{d}</li>
                                ))}
                              </ul>
                            )}
                            {brief.say_by_name && (
                              <p className="mt-1 text-xs">
                                Say his name: <strong>{brief.say_by_name}</strong>
                              </p>
                            )}
                            {(brief.uncertain || []).length > 0 && (
                              <p className="mt-1 text-xs opacity-70">
                                Unclear &mdash; your call: {(brief.uncertain || []).join("; ")}
                              </p>
                            )}
                            <p className="mt-1 text-[11px] opacity-50">
                              Generated from his messages. Check them below.
                            </p>
                          </div>
                        )}
                      </div>
                    )}
                    {ctx !== null && workOrderQuote(ctx) && (
                      <blockquote
                        className="mb-3 border-l-2 border-emerald-600/50 pl-3 text-xs italic opacity-90"
                        title="His longest message here — verbatim, not summarised"
                      >
                        {workOrderQuote(ctx)}
                      </blockquote>
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

export default function CustomsTab() {
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
    <div>
      <header className="mb-6 flex flex-wrap items-baseline justify-between gap-3">
        <div>
          <h2 className="text-lg font-semibold">Customs owed</h2>
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

    </div>
  );
}
