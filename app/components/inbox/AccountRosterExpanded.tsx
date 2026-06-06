"use client";

/**
 * AccountRosterExpanded — wider panel when the roster is expanded.
 *
 * Per account: avatar + nickname, clickable to switch scope. No fan
 * peek list — the user found the cold-cache "fan 12345" fallback rows
 * noisy; the bar of fan rows under each model name is hidden by design.
 */

import { useAccountAvatar } from "@/hooks/useAccountAvatar";
import { decodeHtmlEntities } from "@/lib/utils";
import { cn } from "@/lib/utils";
import type { AccountMeta } from "@/lib/relay";

import { AccountAvatarSlot, AllModelsAvatarSlot } from "./AccountRosterIcon";

interface AccountRosterExpandedProps {
  accounts: AccountMeta[];
  activeAccountId: string | null;
  isIncluded: (id: string) => boolean;
  onPick: (accountId: string) => void;
  onPickAll: () => void;
  allActive: boolean;
}

export function AccountRosterExpanded({
  accounts, activeAccountId, isIncluded, onPick, onPickAll, allActive,
}: AccountRosterExpandedProps) {
  return (
    <div className="flex-1 min-h-0 overflow-y-auto">
      {allActive && <AllModelsBlock active onPick={onPickAll} />}
      {accounts.map((a) => (
        <AccountBlock
          key={a.id}
          account={a}
          active={a.id === activeAccountId}
          included={isIncluded(a.id)}
          onPick={() => onPick(a.id)}
        />
      ))}
      {!allActive && <AllModelsBlock active={false} onPick={onPickAll} />}
    </div>
  );
}

function AllModelsBlock({
  active, onPick,
}: { active: boolean; onPick: () => void }) {
  return (
    <button
      type="button"
      onClick={onPick}
      className={cn(
        "w-full px-3 py-2 flex items-center gap-2 text-left hover:bg-bg-elev-1",
        "border-b border-border/40",
        active && "bg-bg-elev-1/60",
      )}
    >
      <AllModelsAvatarSlot active={active} />
      <div className="min-w-0 flex-1">
        <div className="text-xs font-medium truncate">All models</div>
      </div>
    </button>
  );
}

function AccountBlock({
  account, active, included, onPick,
}: {
  account: AccountMeta;
  active: boolean;
  included: boolean;
  onPick: () => void;
}) {
  const avatarQ = useAccountAvatar(account.id);
  const profileName = avatarQ.data?.name || avatarQ.data?.username || null;
  const label = decodeHtmlEntities(account.nickname || profileName || account.id);

  return (
    <button
      type="button"
      onClick={onPick}
      className={cn(
        "w-full px-3 py-2 flex items-center gap-2 text-left hover:bg-bg-elev-1",
        "border-b border-border/40",
        active && "bg-bg-elev-1/60",
      )}
    >
      <AccountAvatarSlot account={account} active={active} />
      <div className="min-w-0 flex-1">
        <div className="text-xs font-medium truncate">{label}</div>
        {!included && (
          <div className="text-[10px] text-fg-dim">excluded from all-models</div>
        )}
      </div>
    </button>
  );
}
