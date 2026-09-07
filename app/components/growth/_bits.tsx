"use client";

/** Small shared bits for the Growth tabs (Trial / Tracking / Promotion).
 *
 *  The last-run line every automation surface renders is NOT here any more — it
 *  is `@/lib/runStats`, because the Brain's welcome card renders it too and this
 *  module's leading underscore says "internal to this folder". Nothing forwards
 *  it any more — import `RunStats` / `runStatsChunks` / `isDryRun` from
 *  `@/lib/runStats` directly; the compatibility re-export that stood here while
 *  the importers were repointed is gone. */

import { useState } from "react";
import { Button, Input } from "@/components/ui/primitives";

export function fmtUsd(cents: number): string {
  return `$${(Math.max(0, cents) / 100).toFixed(2)}`;
}


/** Message off a thrown value, with a fallback — collapses the
 *  `(e as Error)?.message || "…"` cast every Growth create/save catch repeated. */
export function errMsg(e: unknown, fallback: string): string {
  return (e as Error)?.message || fallback;
}

export function Field({
  label, children,
}: { label: string; children: React.ReactNode }) {
  return (
    <label className="block space-y-1">
      <span className="text-[11px] uppercase tracking-wide text-fg-dim">{label}</span>
      {children}
    </label>
  );
}

export function NumInput({
  value, onChange, min = 0, className,
}: { value: number; onChange: (v: number) => void; min?: number; className?: string }) {
  // Hold the raw keystrokes. Binding the number straight to the input let a
  // field sitting at 0 render "040" once you typed 40 in front of it, and made
  // the box impossible to clear while typing. The draft keeps typing natural;
  // blur normalises it back to the real number.
  const [draft, setDraft] = useState<string | null>(null);
  return (
    <Input
      type="number" min={min} className={className}
      value={draft ?? String(value)}
      onChange={(e) => {
        const raw = e.target.value;
        setDraft(raw);
        const n = Number(raw);
        if (raw !== "" && Number.isFinite(n)) onChange(n);
      }}
      onBlur={() => setDraft(null)}
    />
  );
}

// ── Automation last-run stats live in `@/lib/runStats` ────────────────
//
// `isDryRun`, `runStatsChunks`, `welcomeStatsChunks`, `RunStats` and `StatChunk`
// were here. `RunStats` is not growth-only — the Brain's welcome card renders it
// too, and reaching across the folder boundary for an underscore-prefixed module
// is the seam a welcome bag came through into the auto_follow formatter. A
// compatibility re-export stood here while the importers were repointed; it is
// gone, so `_bits`'s underscore means what it says again: growth-local only.

/** Danger button that guards a delete behind a native confirm. Every Growth
 *  list row (trial / tracking / redirect / promo / segment) deleted through the
 *  same confirm→mutate→disabled-while-pending shape; this is that shape, once. */
export function ConfirmDeleteButton({
  confirm, onConfirm, pending, label = "Delete",
}: {
  confirm: string;
  onConfirm: () => void;
  pending?: boolean;
  label?: string;
}) {
  return (
    <Button
      size="sm" variant="danger" disabled={pending}
      onClick={() => { if (window.confirm(confirm)) onConfirm(); }}
    >
      {label}
    </Button>
  );
}

/** Copy-to-clipboard button that flashes "Copied". */
export function CopyButton({ text, label = "Copy" }: { text: string; label?: string }) {
  const [done, setDone] = useState(false);
  return (
    <Button
      size="sm" variant="ghost"
      onClick={async () => {
        try {
          await navigator.clipboard?.writeText(text);
          setDone(true);
          setTimeout(() => setDone(false), 1200);
        } catch { /* clipboard blocked */ }
      }}
      title={text}
    >
      {done ? "✓ Copied" : label}
    </Button>
  );
}
