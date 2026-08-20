"use client";

/**
 * /setup — the Setup screen. Phase A.8's flagship UI.
 *
 * Composition:
 *   • DriftBanner    — only renders when at least one model is stale.
 *   • PasteCurlCard  — primary bootstrap path; mirrors the legacy /ui/.
 *   • AccountsTable  — every model, with rename + default + delete.
 *   • ProxiesTable   — every proxy + assignment + test.
 *   • AgencyKeysCard — YOUR agency's LLM keys (what your models bill).
 *   • KeysCard       — the server's own house keys + tokens.
 *
 * Order is intentional: stale banner first (urgent), then paste cURL
 * (the fix), then the read-only tables for review.
 */

import DriftBanner from "@/components/setup/DriftBanner";
import AccountsTable from "@/components/setup/AccountsTable";
import ProxiesTable from "@/components/setup/ProxiesTable";
import PasteCurlCard from "@/components/setup/PasteCurlCard";
import KeysCard from "@/components/setup/KeysCard";
import AgencyKeysCard from "@/components/setup/AgencyKeysCard";

export default function SetupPage() {
  return (
    <div className="max-w-6xl mx-auto p-3 sm:p-6 space-y-6">
      <header>
        <h1 className="text-2xl font-semibold mb-1">Setup</h1>
        <p className="text-sm text-fg-dim">
          Capture sessions, manage proxies, keys, monitor drift. Everything that
          keeps the relay talking to OnlyFans.
        </p>
      </header>

      <DriftBanner />

      {/* Desk-only cards. One wrapper PER card — a single shared wrapper would
          collapse the parent's space-y-6 gap on desktop. */}
      <div className="hidden md:block"><PasteCurlCard /></div>

      <AccountsTable />

      <p className="md:hidden text-sm text-fg-dim">
        Session capture, proxies and keys are desktop-only.
      </p>

      <div className="hidden md:block"><ProxiesTable /></div>

      <div className="hidden md:block"><AgencyKeysCard /></div>

      <div className="hidden md:block"><KeysCard /></div>
    </div>
  );
}
