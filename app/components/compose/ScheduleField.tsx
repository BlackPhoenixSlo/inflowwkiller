"use client";

/**
 * ScheduleField — datetime-local input with a smart-suggestion chip.
 *
 * Behavior mirrors the desktop app's chip: once the scope (account id)
 * has ≥1 prior pick in localStorage, we show a chip that fills the
 * field with the predicted next slot ("Tue, May 25 · 9:00 PM · +1d").
 * Click to apply, ✕ next to a populated field clears it.
 *
 * Empty value === "send immediately" — the parent decides what to do
 * with that. Callers should also call `recordSchedule(scope, value)`
 * after a successful submit so the cadence keeps learning.
 */

import { useMemo, useState, useEffect } from "react";

import {
  describePrediction,
  loadHistory,
  predictNext,
  type HistoryEntry,
} from "@/lib/scheduleHistory";

export function ScheduleField({
  scope, value, onChange, label = "Schedule (optional)",
}: {
  scope: string | null;
  value: string;
  onChange: (next: string) => void;
  label?: string;
}) {
  // Local copy so chip predictions refresh when the user clicks Submit
  // and we call recordSchedule from the parent (storage write fires our
  // own re-read via the "storage" event in the same tab is not guaranteed,
  // so we re-read on mount + when the scope changes).
  const [history, setHistory] = useState<HistoryEntry[]>(() => loadHistory(scope));
  useEffect(() => { setHistory(loadHistory(scope)); }, [scope]);

  const prediction = useMemo(() => predictNext(history), [history]);

  return (
    <div className="space-y-1.5">
      <label className="text-xs text-fg-dim block">{label}</label>
      <div className="flex items-center gap-2">
        <input
          type="datetime-local"
          value={value}
          onChange={(e) => onChange(e.target.value)}
          className="flex-1 bg-bg border border-border rounded-md px-2 py-1.5 text-xs focus:outline-none focus:border-accent"
        />
        {value && (
          <button
            type="button"
            onClick={() => onChange("")}
            className="text-xs text-fg-dim hover:text-fg px-2"
            title="Clear schedule (send immediately)"
          >
            ✕
          </button>
        )}
      </div>
      {prediction && (
        <button
          type="button"
          onClick={() => onChange(prediction.value)}
          className="inline-flex items-center gap-1.5 text-[11px] px-2 py-1 rounded-full border border-border bg-bg-elev-1/40 hover:bg-bg-elev-1 text-fg-dim hover:text-fg"
          title={
            prediction.fromDelta
              ? `Based on a ${prediction.intervalLabel} cadence learned from your recent picks`
              : "Suggested as next day at the same time"
          }
        >
          <span>⏱</span>
          <span>{describePrediction(prediction.value)}</span>
          {prediction.fromDelta && (
            <span className="opacity-70">+{prediction.intervalLabel}</span>
          )}
        </button>
      )}
    </div>
  );
}
