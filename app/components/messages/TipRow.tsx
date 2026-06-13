"use client";

/**
 * TipRow — one tip in the /messages > Tips tab list.
 *
 * Two attribution states:
 *   • attribution_source === "tip_message" — the tip was attached to an
 *     outbound message and that message has a sent_by_employee_id. We
 *     render the op's name in subdued text under the tip note.
 *   • attribution_source === null — standalone tip (or chat-linked but
 *     the message had no employee). Rendered as "Unattributed
 *     (standalone tip)" in dim grey so the row still shows but the lack
 *     of credit is obvious.
 *
 * The tip note (messages.body, truncated to 200 chars) is italicized;
 * empty/missing notes fall back to "(no message)" so the row never has
 * a visually empty middle line.
 */

import Link from "next/link";

import { Badge } from "@/components/ui/primitives";
import FanAvatar from "@/components/messages/FanAvatar";
import { fmtCents, stripHtml } from "@/lib/messageFormat";
import { fmtRelTime } from "@/lib/utils";

import type { TipsListRow } from "@/hooks/useTipsList";

interface Props {
  row: TipsListRow;
}

export default function TipRow({ row }: Props) {
  const note = row.tip_note ? stripHtml(row.tip_note) : "";
  const credited = row.attribution_source === "tip_message";

  // Chat link only makes sense when we have a fan_id — standalone tips on
  // unknown fans (no fans-table row) still have a fan_id from the tx, so
  // this is almost always present, but we guard for safety.
  const chatHref =
    row.fan_id != null ? `/chat/${row.account_id}/${row.fan_id}` : null;

  const inner = (
    <div className="flex gap-3 p-3 border border-border rounded-xl bg-panel hover:border-border-light transition-colors">
      <FanAvatar
        url={row.fan.avatar_url}
        accountId={row.account_id}
        alt={row.fan.username ?? "fan"}
      />

      <div className="flex-1 min-w-0">
        <div className="flex items-center gap-2 flex-wrap">
          <span className="font-medium text-fg text-sm">
            @{row.fan.username || row.fan_id || "unknown"}
          </span>
          {row.fan.display_name && (
            <span className="text-xs text-fg-dim truncate">
              ({row.fan.display_name})
            </span>
          )}
          <Badge color="ok">
            <span>💰</span>
            <span>TIP</span>
          </Badge>
          <span className="ml-auto text-sm font-semibold text-fg">
            {fmtCents(row.amount_cents)}
          </span>
          <span className="text-xs text-fg-dim">
            {fmtRelTime(row.occurred_at)}
          </span>
        </div>

        <div className="mt-1 text-xs italic text-fg-dim line-clamp-2 break-words">
          {note ? `"${note}"` : <span className="not-italic">(no message)</span>}
        </div>

        <div className="mt-2 flex items-center gap-2 flex-wrap">
          {credited ? (
            <span className="text-[11px] text-fg-dim">
              credited: {row.attributed_employee_name ?? `#${row.attributed_employee_id}`} via tip msg
            </span>
          ) : (
            <span className="text-[11px] text-muted">
              Unattributed (standalone tip)
            </span>
          )}

          {chatHref && (
            <span className="ml-auto text-xs text-fg-dim group-hover:text-accent">
              ↗ open chat
            </span>
          )}
        </div>
      </div>
    </div>
  );

  if (chatHref) {
    return (
      <Link href={chatHref} className="block group">
        {inner}
      </Link>
    );
  }
  return <div className="block">{inner}</div>;
}
