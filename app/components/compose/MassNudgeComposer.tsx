"use client";

/**
 * MassNudgeComposer — the +New entry point for the mass_nudge automation.
 * Reuses the Settings → Mass Nudge tab inside a modal.
 */

import MassNudgeTab from "@/components/settings/MassNudgeTab";

export function MassNudgeComposer({ open, onClose }: { open: boolean; onClose: () => void }) {
  if (!open) return null;
  return (
    <div className="fixed inset-0 z-50 bg-black/50 grid place-items-center p-4" onClick={onClose}>
      <div
        onClick={(e) => e.stopPropagation()}
        className="w-full max-w-[720px] max-h-[90vh] flex flex-col bg-panel border border-border rounded-xl shadow-2xl"
      >
        <header className="px-4 py-3 border-b border-border flex items-center justify-between">
          <h2 className="text-sm font-semibold">Set up automation</h2>
          <button type="button" onClick={onClose} className="text-fg-dim hover:text-fg text-lg leading-none w-11 h-11 -mr-2 -my-2 grid place-items-center shrink-0 md:w-auto md:h-auto md:mr-0 md:my-0 md:inline" title="Close">×</button>
        </header>
        <div className="flex-1 overflow-y-auto p-4">
          <MassNudgeTab />
        </div>
      </div>
    </div>
  );
}
