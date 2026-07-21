"use client";

/**
 * DateRangePicker — fully controlled. Parent owns `from`/`to` (as
 * YYYY-MM-DD strings or null = backend default) and is told whenever
 * either side changes. Preset buttons (7d / 30d / custom) just call
 * back with the right pair; the date inputs flip the mode to "custom"
 * the moment the user types into either.
 *
 * The backend treats missing dates as "last 7d" for coverage, "last
 * 30d" for per-model. We don't try to mirror that here — when a preset
 * is selected we send explicit dates so the URL state stays
 * deterministic regardless of which endpoint reads it.
 */

import { useMemo } from "react";

import { Button } from "@/components/ui/primitives";
import { cn } from "@/lib/utils";

export type RangePreset = "7d" | "30d" | "custom";

interface Props {
  from: string | null;
  to: string | null;
  preset: RangePreset;
  onChange: (from: string | null, to: string | null, preset: RangePreset) => void;
}

function todayISO(): string {
  // Local date wall-clock; backend parses ISO and treats date-only as UTC
  // midnight which is fine for "last N days" semantics here.
  const d = new Date();
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, "0");
  const dd = String(d.getDate()).padStart(2, "0");
  return `${y}-${m}-${dd}`;
}

function daysAgoISO(n: number): string {
  const d = new Date();
  d.setDate(d.getDate() - n);
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, "0");
  const dd = String(d.getDate()).padStart(2, "0");
  return `${y}-${m}-${dd}`;
}

export default function DateRangePicker({ from, to, preset, onChange }: Props) {
  const todayStr = useMemo(() => todayISO(), []);

  const setPreset = (p: "7d" | "30d") => {
    const days = p === "7d" ? 7 : 30;
    onChange(daysAgoISO(days), todayStr, p);
  };

  const handleFrom = (v: string) => {
    onChange(v || null, to, "custom");
  };
  const handleTo = (v: string) => {
    onChange(from, v || null, "custom");
  };

  return (
    <div className="flex flex-wrap items-center gap-2">
      <PresetBtn active={preset === "7d"} onClick={() => setPreset("7d")}>7d</PresetBtn>
      <PresetBtn active={preset === "30d"} onClick={() => setPreset("30d")}>30d</PresetBtn>
      {/* Custom span + the two native date inputs are desk-only: they are
          ~340px of unshrinkable chrome. `hidden sm:flex` with the same
          `items-center gap-2` reproduces the parent's own gap at ≥640px, so
          the row is unchanged from sm up. */}
      <div className="hidden sm:flex items-center gap-2">
        <span
          className={cn(
            "px-2 py-1 text-[11px] uppercase tracking-wide rounded-md border",
            preset === "custom"
              ? "border-accent/40 text-accent bg-accent/10"
              : "border-border text-fg-dim bg-bg-elev-1",
          )}
        >
          Custom
        </span>
        <input
          type="date"
          value={from ?? ""}
          onChange={(e) => handleFrom(e.target.value)}
          className="bg-bg border border-border rounded-lg px-2 py-1 text-xs text-fg focus:outline-none focus:border-accent"
          aria-label="From date"
        />
        <span className="text-fg-dim text-xs">→</span>
        <input
          type="date"
          value={to ?? ""}
          onChange={(e) => handleTo(e.target.value)}
          className="bg-bg border border-border rounded-lg px-2 py-1 text-xs text-fg focus:outline-none focus:border-accent"
          aria-label="To date"
        />
      </div>
      {/* Read-only echo so a desktop-set custom range stays visible on phone. */}
      {preset === "custom" && (
        <span className="sm:hidden px-2 py-1 text-[11px] uppercase tracking-wide rounded-md border border-accent/40 text-accent bg-accent/10">
          Custom · {from ?? "…"} → {to ?? "…"}
        </span>
      )}
    </div>
  );
}

function PresetBtn({
  active,
  onClick,
  children,
}: {
  active: boolean;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <Button
      type="button"
      variant={active ? "primary" : "secondary"}
      size="sm"
      onClick={onClick}
    >
      {children}
    </Button>
  );
}
