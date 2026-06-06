"use client";

/**
 * NudgeComposer — the +New entry point for the nudge_online automation.
 *
 * Reuses the full Settings → "Nudge online" tab (per-account config + the
 * "roll out to models" checklist) inside a modal, so the automation can be set
 * up / enabled straight from the top bar like the other +New actions.
 */

import NudgeOnlineTab from "@/components/settings/NudgeOnlineTab";

export function NudgeComposer({ open, onClose }: { open: boolean; onClose: () => void }) {
  if (!open) return null;
  return (
    <div
      className="fixed inset-0 z-50 bg-black/50 grid place-items-center p-4"
      onClick={onClose}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        className="w-full max-w-[720px] max-h-[90vh] flex flex-col bg-panel border border-border rounded-xl shadow-2xl"
      >
        <header className="px-4 py-3 border-b border-border flex items-center justify-between">
          <h2 className="text-sm font-semibold">Set up automation</h2>
          <button
            type="button"
            onClick={onClose}
            className="text-fg-dim hover:text-fg text-lg leading-none"
            title="Close"
          >×</button>
        </header>
        <div className="flex-1 overflow-y-auto p-4">
          <NudgeOnlineTab />
        </div>
      </div>
    </div>
  );
}
