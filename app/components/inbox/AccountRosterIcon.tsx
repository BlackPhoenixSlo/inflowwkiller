"use client";

/**
 * AccountRosterIcon — single avatar circle in the inbox roster strip.
 *
 * Pulls the OF profile avatar via `useAccountAvatar(accountId)` so the
 * icon strip matches what each model looks like to fans. On miss
 * (no avatar yet, OF 404, broken session) we degrade to a colored
 * circle keyed off `AccountMeta.color` with the nickname's first
 * letter on top — never user-blocking, the icon is always *something*.
 *
 * Ask #1 lives here: this is the per-account-avatar surface the
 * DA review picked over a dynamic favicon. When the inbox is in
 * single-fan view, we can later overlay the fan's tier color via
 * the `tierColor` prop without changing the avatar pipeline.
 *
 * The avatar visual is split into `<AccountAvatarSlot>` so the same
 * picture can be used inside a parent <button> (e.g. expanded row)
 * without nesting <button>s — that's a hydration error in React DOM.
 */

import { useState } from "react";
import { Users } from "lucide-react";

import { useAccountAvatar, pickAvatarUrl } from "@/hooks/useAccountAvatar";
import { proxyImage, type AccountMeta } from "@/lib/relay";
import { cn } from "@/lib/utils";

/** Color used everywhere the UI represents the "all models" aggregate.
 *  Mirrors the ScopeSwitcher dot. */
export const ALL_MODELS_COLOR = "#a78bfa";

export interface AccountRosterIconProps {
  account: AccountMeta;
  active: boolean;
  onClick: () => void;
  /** Optional tier-color overlay ring (ask #1 — single-fan view). */
  tierColor?: string | null;
}

/**
 * AccountAvatarSlot — non-interactive avatar visual. Renders as a div so
 * it can be embedded inside a parent <button> without nesting.
 */
export function AccountAvatarSlot({
  account, active, tierColor,
}: { account: AccountMeta; active: boolean; tierColor?: string | null }) {
  const q = useAccountAvatar(account.id);
  const rawUrl = pickAvatarUrl(q.data);
  const url = proxyImage(rawUrl, account.id);
  const [broken, setBroken] = useState(false);

  const initial = (account.nickname || account.id || "?").trim().slice(0, 1).toUpperCase();
  const ringColor = tierColor || account.color || "#666";

  return (
    <div
      className={cn(
        "relative w-8 h-8 rounded-full grid place-items-center overflow-hidden shrink-0",
        active ? "" : "ring-1 ring-border",
      )}
      style={active ? { boxShadow: `0 0 0 2px ${ringColor}` } : undefined}
    >
      {url && !broken ? (
        <img
          src={url}
          alt=""
          loading="lazy"
          decoding="async"
          fetchPriority="low"
          onError={() => setBroken(true)}
          className="w-full h-full object-cover"
        />
      ) : (
        <span
          className="w-full h-full grid place-items-center text-[11px] font-medium text-white"
          style={{ background: account.color || "#666" }}
        >
          {initial}
        </span>
      )}
    </div>
  );
}

export function AccountRosterIcon({
  account, active, onClick, tierColor,
}: AccountRosterIconProps) {
  return (
    <button
      type="button"
      onClick={onClick}
      title={account.nickname || account.id}
      aria-pressed={active}
      className={cn(
        "rounded-full shrink-0 transition-transform hover:scale-105 focus:outline-none",
      )}
    >
      <AccountAvatarSlot account={account} active={active} tierColor={tierColor} />
    </button>
  );
}

/**
 * AllModelsAvatarSlot — non-interactive visual for the "all models"
 * aggregate. Matches the size/ring treatment of AccountAvatarSlot so the
 * two read as siblings in the roster strip.
 */
export function AllModelsAvatarSlot({ active }: { active: boolean }) {
  return (
    <div
      className={cn(
        "relative w-8 h-8 rounded-full grid place-items-center overflow-hidden shrink-0",
        active ? "" : "ring-1 ring-border",
      )}
      style={{
        background: ALL_MODELS_COLOR,
        ...(active ? { boxShadow: `0 0 0 2px ${ALL_MODELS_COLOR}` } : null),
      }}
    >
      <Users className="w-4 h-4 text-white" strokeWidth={2.25} />
    </div>
  );
}

/**
 * AllModelsRosterIcon — clickable "all models" circle in the roster strip.
 * Click swaps scope back to the unified all-models view.
 */
export function AllModelsRosterIcon({
  active, onClick,
}: { active: boolean; onClick: () => void }) {
  return (
    <button
      type="button"
      onClick={onClick}
      title="All models"
      aria-pressed={active}
      className="rounded-full shrink-0 transition-transform hover:scale-105 focus:outline-none"
    >
      <AllModelsAvatarSlot active={active} />
    </button>
  );
}
