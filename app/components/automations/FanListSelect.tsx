"use client";

/**
 * FanListSelect — the shared OF fan-folder picker (audience features).
 *
 * Reads useAllChatFolders (the FULL folder set) on purpose: useChatFolders
 * filters to isPinnedToChat and would silently hide most of an account's
 * folders from the picker. Only user-modifiable folders (custom /
 * close-friends) are offered — the include mirror crawls `/lists/{id}/users`,
 * which system buckets ("fans", "following", …) don't support — and folders
 * without canAddUsers are still pickable as a fence but can't host auto-add
 * (the caller gates that checkbox via `selected?.canAddUsers`).
 */

import { useAllChatFolders, type ChatFolder } from "@/hooks/useChatFolders";

export function pickableFanLists(folders: ChatFolder[] | undefined): ChatFolder[] {
  return (folders ?? []).filter(
    (f) => f.type === "custom" || f.type === "close_friends",
  );
}

export default function FanListSelect({
  accountId,
  value,
  onChange,
  className,
  disabled,
}: {
  accountId: string | null;
  /** The selected OF list id (audience_of_list_id), or null. */
  value: number | null;
  onChange: (ofListId: number | null, folder: ChatFolder | null) => void;
  className?: string;
  disabled?: boolean;
}) {
  const foldersQ = useAllChatFolders(accountId);
  const folders = pickableFanLists(foldersQ.data);
  return (
    <select
      value={value == null ? "" : String(value)}
      disabled={disabled || !accountId}
      onChange={(e) => {
        const raw = e.target.value;
        if (!raw) {
          onChange(null, null);
          return;
        }
        const folder = folders.find((f) => String(f.id) === raw) ?? null;
        onChange(Number(raw), folder);
      }}
      className={className}
    >
      <option value="">— pick a folder —</option>
      {folders.map((f) => (
        <option key={String(f.id)} value={String(f.id)}>
          {(f.name ?? `list ${f.id}`) + ` (${f.usersCount ?? 0})`}
          {f.canAddUsers ? "" : " — read-only"}
        </option>
      ))}
    </select>
  );
}
