"use client";

/**
 * /vault — the Vault manager.
 *
 * Owner-only. Browse the model's whole vault, filter by folder / type,
 * search, and (later steps) edit tags/descriptions, make/rename folders,
 * move media, and set per-folder manual ordering. Step 1 = read + search
 * over the existing OF vault endpoints; the local mirror + AI describe
 * layer land on top of this same surface. All work lives in
 * <VaultManagePanel/>; this page is just the titled shell, matching /automations.
 */

import VaultManagePanel from "@/components/vault/VaultManagePanel";
import VaultReviewTab from "@/components/vault/VaultReviewTab";

export default function VaultPage() {
  return (
    <div className="max-w-7xl mx-auto p-3 sm:p-6 space-y-5">
      <header>
        <h1 className="text-2xl font-semibold mb-1">Vault</h1>
        <p className="text-sm text-fg-dim">
          Browse and organize the model&apos;s vault — filter by folder or type,
          search, and manage media. Searches are cached so repeat lookups feel
          instant. Editing, folders, ordering, and AI descriptions arrive next.
        </p>
      </header>

      <VaultManagePanel />
      <VaultReviewTab />
    </div>
  );
}
