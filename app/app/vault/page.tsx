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
 *
 * The one thing the shell DOES own is which model everything below is scoped to
 * (`VaultAccountProvider`), and it owns it because the answer has to be the same
 * for the grid, the review queue and the importer — see `VaultAccount`. The
 * picker sits in the header rather than inside the grid so that the model whose
 * vault you are about to upload into is named above the upload form, not below it.
 */

import { AccountChips } from "@/components/AccountChips";
import { VaultAccountProvider, useVaultAccount } from "@/components/vault/VaultAccount";
import VaultImportCard from "@/components/vault/VaultImportCard";
import VaultManagePanel from "@/components/vault/VaultManagePanel";
import VaultReviewTab from "@/components/vault/VaultReviewTab";

/** Whose vault this is. Named even for a single-account install — `AccountChips`
 *  renders nothing when there is nothing to pick, and "Vault" over a stranger's
 *  media is exactly the doubt this line exists to remove. */
function VaultHeader() {
  const { accountId, account, sessionLost, setAccountId } = useVaultAccount();
  return (
    <header>
      <div className="flex items-baseline gap-2 flex-wrap mb-1">
        <h1 className="text-2xl font-semibold">Vault</h1>
        {account && (
          <span className="text-sm text-fg-dim">
            {account.nickname || account.id}
          </span>
        )}
      </div>
      <AccountChips accountId={accountId} onChange={setAccountId} className="mb-2" />
      {sessionLost && (
        /* Said, not acted on. This model's OnlyFans session dropped, so she is
           no longer in the pickable list — but quietly re-adopting the first
           account that IS would move the operator to a different creator
           mid-action, remount the whole page under them, and lose the staged
           import files and the grid selection with no message at all. The
           selection stays where it was put; every request below will fail
           until the session is back, and this line is why. */
        <p className="mb-2 text-xs text-warn">
          {account?.nickname || account?.id || "This model"}&apos;s OnlyFans
          session has dropped — nothing on this page can load until it is
          re-captured. Pick another model above to keep working.
        </p>
      )}
      <p className="hidden sm:block text-sm text-fg-dim">
        Browse and organize the model&apos;s vault — filter by folder or type,
        search, and manage media. Searches are cached so repeat lookups feel
        instant. Everything on this page — browsing, review and import — follows
        the model picked here.
      </p>
    </header>
  );
}

/** The page body, inside the provider so it can key its children on the model. */
function VaultBody() {
  const { accountId, ready } = useVaultAccount();
  return (
    <div className="max-w-shell mx-auto p-3 sm:p-6 space-y-3 sm:space-y-5">
      <VaultHeader />
      {/* Nothing below mounts until the model is known, and that is a contract,
          not a loading nicety. It makes `accountId` a plain string for the whole
          subtree (`useVaultAccountId`), which is what removes the null branch
          from three components — and with it the "…" placeholder, the disabled
          Start button, and two hand-written copies of this same empty state.
          It also stops the first paint mounting the entire page under a
          placeholder key and throwing it away one render later.

          `key` = the model, so switching one REMOUNTS all three rather than
          re-rendering them with the previous model's state still in hand. The
          panel holds every selection, the open drawer, the folder filter, the
          search box and the drag-reorder buffer; the import card holds staged
          files and folder ids that go to OnlyFans verbatim. Picking a new chip
          used to leave 30 of the OLD model's media ids selected, and the next
          "add to folder" wrote those ids into the NEW model's folder. One
          mechanism for all three, because a remount cannot be forgotten the
          next time a piece of state is added — a reset effect can, and the
          review tab already carried one that could no longer fire.
          The cost is that `type` and `sort` go back to their defaults on a
          switch; they are preferences, and worth it. */}
      {accountId === null ? (
        <p className="text-sm text-fg-dim">
          {ready
            ? "No model accounts with a live session."
            : "Loading models…"}
        </p>
      ) : (
        <div key={accountId} className="space-y-3 sm:space-y-5">
          <VaultImportCard />
          <VaultManagePanel />
          <VaultReviewTab />
        </div>
      )}
    </div>
  );
}

export default function VaultPage() {
  return (
    <VaultAccountProvider>
      <VaultBody />
    </VaultAccountProvider>
  );
}
