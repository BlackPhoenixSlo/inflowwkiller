"use client";

/**
 * MessageRow — one PPV in the paid-messages list.
 *
 * The right-rail status line is the one piece that needs care:
 *   • is_paid=true + purchased_at set → "PAID 18m later" (humanized
 *     delta against sent_at). Green.
 *   • is_paid=true + purchased_at=null → "just paid". Green. The WS
 *     pump occasionally writes the unlock flag before the timestamp;
 *     we render the truthful "we don't know exactly when" instead of
 *     substituting sent_at or now() (which would be a lie).
 *   • is_paid=false (or null) → "UNPAID · 3d old". Muted.
 */

import Link from "next/link";
import { Paperclip } from "lucide-react";

import { Badge } from "@/components/ui/primitives";
import FanAvatar from "@/components/messages/FanAvatar";
import { fmtCents, stripHtml } from "@/lib/messageFormat";
import { fmtRelTime } from "@/lib/utils";

import type { PaidMessageRow } from "@/hooks/usePaidMessages";

interface Props {
  row: PaidMessageRow;
}

/** "PAID 18m later" — purchased_at minus sent_at, no future/in prefix. */
function fmtUnlockDelta(sentIso: string | null, purchasedIso: string | null): string {
  if (!sentIso || !purchasedIso) return "later";
  const sent = new Date(sentIso).getTime();
  const purch = new Date(purchasedIso).getTime();
  if (Number.isNaN(sent) || Number.isNaN(purch)) return "later";
  const diffMs = Math.max(0, purch - sent);
  const sec = Math.floor(diffMs / 1000);
  if (sec < 60) return "<1m later";
  const min = Math.floor(sec / 60);
  if (min < 60) return `${min}m later`;
  const hr = Math.floor(min / 60);
  if (hr < 24) return `${hr}h later`;
  const day = Math.floor(hr / 24);
  return `${day}d later`;
}

export default function MessageRow({ row }: Props) {
  const previewBody = stripHtml(row.body);
  const isPaid = row.is_paid;
  const justPaid = isPaid && !row.purchased_at;

  return (
    <Link
      href={`/chat/${row.account_id}/${row.fan_id}`}
      className="block group"
    >
      <div className="flex gap-3 p-3 border border-border rounded-xl bg-panel hover:border-border-light transition-colors">
        <FanAvatar
          url={row.fan.avatar_url}
          accountId={row.account_id}
          alt={row.fan.username ?? "fan"}
        />

        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2 flex-wrap">
            <span className="font-medium text-fg text-sm">
              @{row.fan.username || row.fan_id}
            </span>
            {row.fan.display_name && (
              <span className="text-xs text-fg-dim truncate">
                ({row.fan.display_name})
              </span>
            )}
            <span className="ml-auto text-sm font-semibold text-fg">
              {fmtCents(row.price_cents)}
            </span>
            <span className="text-xs text-fg-dim">
              sent {fmtRelTime(row.sent_at)}
            </span>
          </div>

          <div className="mt-1 text-xs text-fg-dim line-clamp-2 break-words">
            {previewBody || <em className="text-muted">(no text)</em>}
          </div>

          <div className="mt-2 flex items-center gap-2 flex-wrap">
            {row.media_count > 0 && (
              <Badge color="muted">
                <Paperclip size={12} aria-hidden />
                <span>{row.media_count}</span>
              </Badge>
            )}

            {isPaid ? (
              justPaid ? (
                <Badge color="ok">just paid</Badge>
              ) : (
                <Badge color="ok">
                  PAID {fmtUnlockDelta(row.sent_at, row.purchased_at)}
                </Badge>
              )
            ) : (
              <Badge color="muted">
                <span>UNPAID</span>
                <span>·</span>
                <span>{fmtRelTime(row.sent_at)}</span>
              </Badge>
            )}

            {row.employee_name && (
              <span className="text-[11px] text-fg-dim">
                by {row.employee_name}
              </span>
            )}

            <span className="ml-auto text-xs text-fg-dim group-hover:text-accent">
              ↗ open chat
            </span>
          </div>
        </div>
      </div>
    </Link>
  );
}
