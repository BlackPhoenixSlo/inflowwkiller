"use client";

/**
 * ModelInfoButton — an ℹ︎ button in the chat header that opens a small
 * read-only popover with the account's chatter-facing "Persona" info.
 *
 * Ordered for the person actually running the chat, not for ops: it leads
 * with "Right now" (the model's current local time + what she's supposedly
 * doing, from `time_activities`), then her bio + location, an optional
 * "Today" schedule, and finally a muted footer with the LLM model / cost
 * cap (which a chatter rarely cares about but we keep for reference).
 *
 * The current-activity line uses the SAME 6-bucket time-of-day mapping the
 * backend uses to pick welcome/followup lines (send_welcome._time_activity /
 * _photo_index), so the popover shows exactly what the model would say.
 *
 * Data comes from the same owner-gated /admin/account-config route the
 * Brain editor uses (useAccountConfig). The query is lazy — it only
 * fires the first time the popover is opened, then stays cached
 * (keyed per account, so switching fans on the same model reuses it).
 *
 * Closes on outside-click + Esc, mirroring ChatActionsMenu.
 */

import { useEffect, useRef, useState } from "react";

import { useAccountConfig } from "@/hooks/useAccountConfig";

interface Props {
  accountId: string;
}

// The 6 time-of-day slots, ordered — mirrors send_welcome._SLOT_KEYS and
// account_config_api.TIME_SLOTS. Index maps to slotIndexForHour below.
const SLOT_KEYS = [
  "morning_1", "morning_2", "afternoon_1", "afternoon_2", "evening", "night",
] as const;

// Friendly labels for the "Today" schedule list, keyed by slot.
const SLOT_LABELS: Record<string, string> = {
  morning_1: "Early morning",
  morning_2: "Late morning",
  afternoon_1: "Early afternoon",
  afternoon_2: "Late afternoon",
  evening: "Evening",
  night: "Night",
};

function fmtOffset(h: number): string {
  if (!Number.isFinite(h)) return "—";
  const sign = h >= 0 ? "+" : "−";
  return `UTC${sign}${Math.abs(h)}`;
}

function fmtCap(cents: number): string {
  if (!Number.isFinite(cents) || cents <= 0) return "—";
  return `$${(cents / 100).toFixed(2)}/day`;
}

// Model's current local hour (0-23) = UTC hour + whole-hour offset, wrapped.
// Matches the backend, which truncates the offset to an int for slot picking.
function localHour(utcOffset: number): number {
  const off = Number.isFinite(utcOffset) ? Math.trunc(utcOffset) : 0;
  return (((new Date().getUTCHours() + off) % 24) + 24) % 24;
}

// Model's current local wall-clock, e.g. "6:32 PM". Supports :30 offsets for
// display (slot picking still uses the whole-hour localHour above).
function fmtLocalTime(utcOffset: number): string {
  const off = Number.isFinite(utcOffset) ? utcOffset : 0;
  const now = new Date();
  const total = now.getUTCHours() * 60 + now.getUTCMinutes() + Math.round(off * 60);
  const wrapped = ((total % 1440) + 1440) % 1440;
  const h = Math.floor(wrapped / 60);
  const m = wrapped % 60;
  const ampm = h >= 12 ? "PM" : "AM";
  const h12 = h % 12 === 0 ? 12 : h % 12;
  return `${h12}:${String(m).padStart(2, "0")} ${ampm}`;
}

// hour → slot index, same 6 buckets as send_welcome._photo_index.
function slotIndexForHour(hour: number): number {
  if (hour >= 5 && hour < 9) return 0;
  if (hour >= 9 && hour < 12) return 1;
  if (hour >= 12 && hour < 15) return 2;
  if (hour >= 15 && hour < 18) return 3;
  if (hour >= 18 && hour < 21) return 4;
  return 5;
}

// Coarse part-of-day word, mirrors send_welcome._time_activity labels.
function todLabel(hour: number): string {
  if (hour >= 5 && hour < 12) return "morning";
  if (hour >= 12 && hour < 18) return "afternoon";
  if (hour >= 18 && hour < 21) return "evening";
  return "night";
}

export function ModelInfoButton({ accountId }: Props) {
  const [open, setOpen] = useState(false);
  const [showDay, setShowDay] = useState(false);
  // Latch: once opened we keep the query enabled so the cached config
  // survives close/reopen instead of re-fetching each time.
  const [everOpened, setEverOpened] = useState(false);
  const ref = useRef<HTMLDivElement | null>(null);

  const q = useAccountConfig(everOpened ? accountId : null);
  const cfg = q.data?.config;

  useEffect(() => {
    if (!open) return;
    const onClick = (e: MouseEvent) => {
      if (!ref.current?.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    document.addEventListener("mousedown", onClick);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onClick);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  function toggle() {
    setEverOpened(true);
    setOpen((v) => !v);
  }

  // Derived "right now" context — only meaningful once cfg is loaded.
  const hour = cfg ? localHour(cfg.utc_offset) : 0;
  const nowSlot = slotIndexForHour(hour);
  const acts = cfg?.time_activities ?? {};
  const nowActivity = (acts[SLOT_KEYS[nowSlot]] || "").trim();
  const hasSchedule = SLOT_KEYS.some((k) => (acts[k] || "").trim());

  return (
    <div ref={ref} className="relative shrink-0">
      <button
        type="button"
        onClick={toggle}
        className={
          "grid place-items-center w-6 h-6 rounded-md text-[13px] leading-none " +
          (open ? "text-accent bg-bg-elev-1" : "text-fg-dim hover:text-fg hover:bg-bg-elev-1")
        }
        title="Persona"
        aria-label="Persona"
        aria-expanded={open}
      >
        ⓘ
      </button>

      {open && (
        <div
          className="absolute right-0 top-full mt-1 z-40 w-72 rounded-lg border border-border bg-panel shadow-xl text-xs"
          role="dialog"
          aria-label="Persona"
        >
          <div className="px-3 py-2 border-b border-border flex items-center justify-between">
            <span className="font-semibold text-fg">Persona</span>
            <span className="text-[10px] text-fg-dim">acct {accountId}</span>
          </div>

          <div className="px-3 py-2">
            {q.isLoading ? (
              <div className="text-fg-dim py-2">Loading…</div>
            ) : q.isError ? (
              <div className="text-red-400 py-2">Couldn&apos;t load persona.</div>
            ) : !cfg ? (
              <div className="text-fg-dim py-2">No brain configured for this account.</div>
            ) : (
              <div className="space-y-2">
                {/* Right now — local time + what she's supposedly doing. */}
                <div className="rounded bg-bg-elev-1 px-2 py-1.5">
                  <div className="flex items-baseline justify-between gap-2">
                    <span className="text-[10px] uppercase tracking-wide text-fg-dim">
                      Right now
                    </span>
                    <span className="text-fg">
                      {fmtLocalTime(cfg.utc_offset)}
                      <span className="text-fg-dim"> · {todLabel(hour)}</span>
                    </span>
                  </div>
                  {nowActivity && (
                    <p className="mt-1 text-fg leading-snug whitespace-pre-wrap">
                      {nowActivity}
                    </p>
                  )}
                </div>

                {/* Bio (the persona / system-prompt text) — the thing chatters
                    actually care about, so it sits right under "Right now". */}
                <div>
                  <div className="text-[10px] uppercase tracking-wide text-fg-dim mb-0.5">
                    Bio
                  </div>
                  <div className="max-h-40 overflow-y-auto whitespace-pre-wrap text-fg leading-snug rounded bg-bg-elev-1 px-2 py-1.5">
                    {cfg.persona?.trim() || "—"}
                  </div>
                </div>

                {/* Location + timezone — minor context, below the bio. */}
                <div className="flex items-baseline justify-between gap-3">
                  <span className="text-[10px] uppercase tracking-wide text-fg-dim shrink-0">
                    Location
                  </span>
                  <span className="text-fg text-right">
                    {cfg.location || "—"}
                    {Number.isFinite(cfg.utc_offset) && (
                      <span className="text-fg-dim"> · {fmtOffset(cfg.utc_offset)}</span>
                    )}
                  </span>
                </div>

                {/* Today — the full day's activities, current slot highlighted. */}
                {hasSchedule && (
                  <div>
                    <button
                      type="button"
                      onClick={() => setShowDay((v) => !v)}
                      className="text-[10px] uppercase tracking-wide text-fg-dim hover:text-fg"
                      aria-expanded={showDay}
                    >
                      {showDay ? "▾" : "▸"} Today
                    </button>
                    {showDay && (
                      <ul className="mt-1 space-y-1">
                        {SLOT_KEYS.map((k, i) => {
                          const act = (acts[k] || "").trim();
                          const isNow = i === nowSlot;
                          return (
                            <li
                              key={k}
                              className={"rounded px-2 py-1 " + (isNow ? "bg-bg-elev-1" : "")}
                            >
                              <span
                                className={
                                  "text-[10px] uppercase tracking-wide " +
                                  (isNow ? "text-accent" : "text-fg-dim")
                                }
                              >
                                {SLOT_LABELS[k]}
                              </span>
                              <span className="block text-fg leading-snug whitespace-pre-wrap">
                                {act || "—"}
                              </span>
                            </li>
                          );
                        })}
                      </ul>
                    )}
                  </div>
                )}

                {/* Ops footer — model + cost cap, deliberately muted. */}
                <div className="pt-1.5 mt-1 border-t border-border flex items-center justify-between gap-3 text-[10px] text-fg-dim">
                  <span className="font-mono truncate">{cfg.model || "—"}</span>
                  <span className="shrink-0">{fmtCap(cfg.daily_cost_cap_cents)}</span>
                </div>
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
