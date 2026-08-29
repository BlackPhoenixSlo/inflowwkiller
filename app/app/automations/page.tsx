"use client";

/**
 * /automations — the automation control surface.
 *
 * Owner-only. Create/enable/disable/schedule/trigger the background automations
 * (`automation_rules`), and see each one's recent run history. All work lives in
 * <AutomationsPanel/>; this page is just the titled shell, matching /stats.
 */

import AutomationsPanel from "@/components/automations/AutomationsPanel";

export default function AutomationsPage() {
  return (
    <div className="max-w-shell mx-auto p-3 sm:p-6 space-y-5">
      <header>
        <h1 className="text-2xl font-semibold mb-1">Automations</h1>
        <p className="text-sm text-fg-dim">
          Turn automations on or off, set how often each runs, trigger one
          immediately, and watch recent runs. Enabled rules execute on their
          schedule via the background executor — the UI never blocks on them.
        </p>
      </header>

      <AutomationsPanel />
    </div>
  );
}
