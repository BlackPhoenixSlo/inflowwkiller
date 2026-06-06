"use client";

/**
 * MessageRowGeneric — direction-aware row for the All-Messages tab.
 *
 * Unlike MessageRow (PPV-only, price + paid/unpaid badge front-and-
 * center), this row handles all four shapes:
 *   • outbound paid     — price + PAID/UNPAID badge, employee name
 *   • outbound free     — body only, employee name
 *   • inbound  paid     — fan-purchased (rare but exists), no PAID badge
 *   • inbound  free     — body only
 *
 * Body isn't clamped to 200 chars server-side here (the All-Messages
 * endpoint uses a 2000 cap), so we line-clamp visually instead — full
 * text is in the chat link the row navigates to.
 */

import Link from "next/link";
import { ArrowDownLeft, ArrowUpRight, Paperclip } from "lucide-react";

import { Badge } from "@/components/ui/primitives";
import { proxyImage } from "@/lib/relay";
import { cn, fmtRelTime } from "@/lib/utils";

import type { PaidMessageRow } from "@/hooks/usePaidMessages";

interface Props {
  row: PaidMessageRow;
}

function fmtPrice(cents: number): string {
  return `$${(cents / 100).toFixed(2)}`;
}

function stripHtml(s: string): string {
  return s.replace(/<\/?p[^>]*>/gi, "").replace(/<br\s*\/?>/gi, " ").trim();
}

export default function MessageRowGeneric({ row }: Props) {
  const previewBody = stripHtml(row.body);
  const direction = row.direction ?? "out";
  const isInbound = direction === "in";
  const isSystem = direction === "system";
  const isPriced = row.price_cents > 0;
  const isPaid = row.is_paid;

  return (
    <Link
      href={`/chat/${row.account_id}/${row.fan_id}`}
      className="block group"
    >
      <div
        className={cn(
          "flex gap-3 p-3 border border-border rounded-xl bg-panel hover:border-border-light transition-colors",
          // Subtle left border to make scanning direction quick at a glance.
          isInbound && "border-l-2 border-l-accent/40",
        )}
      >
        <Avatar
          url={row.fan.avatar_url}
          accountId={row.account_id}
          alt={row.fan.username ?? "fan"}
        />

        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2 flex-wrap">
            <DirectionChip direction={direction} />
            <span className="font-medium text-fg text-sm">
              @{row.fan.username || row.fan_id}
            </span>
            {row.fan.display_name && (
              <span className="text-xs text-fg-dim truncate">
                ({row.fan.display_name})
              </span>
            )}
            {isPriced && (
              <span className="ml-auto text-sm font-semibold text-fg">
                {fmtPrice(row.price_cents)}
              </span>
            )}
            <span className={cn("text-xs text-fg-dim", !isPriced && "ml-auto")}>
              {fmtRelTime(row.sent_at)}
            </span>
          </div>

          <div className="mt-1 text-xs text-fg-dim line-clamp-3 break-words">
            {previewBody || <em className="text-muted">(no text)</em>}
          </div>

          <div className="mt-2 flex items-center gap-2 flex-wrap">
            {row.media_count > 0 && (
              <Badge color="muted">
                <Paperclip size={12} aria-hidden />
                <span>{row.media_count}</span>
              </Badge>
            )}

            {row.is_tip && (
              <Badge color="ok">TIP</Badge>
            )}

            {isPriced && !isSystem && (
              isPaid ? (
                <Badge color="ok">PAID</Badge>
              ) : (
                <Badge color="muted">UNPAID</Badge>
              )
            )}

            {!isInbound && row.employee_name && (
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

function DirectionChip({ direction }: { direction: "in" | "out" | "system" }) {
  if (direction === "in") {
    return (
      <Badge color="muted" className="!px-1.5">
        <ArrowDownLeft size={11} aria-hidden />
        <span>from fan</span>
      </Badge>
    );
  }
  if (direction === "out") {
    return (
      <Badge color="muted" className="!px-1.5">
        <ArrowUpRight size={11} aria-hidden />
        <span>from us</span>
      </Badge>
    );
  }
  return (
    <Badge color="warn" className="!px-1.5">
      <span>system</span>
    </Badge>
  );
}

function Avatar({
  url,
  accountId,
  alt,
}: {
  url: string | null;
  accountId: string;
  alt: string;
}) {
  const src = url ? proxyImage(url, accountId) : "";
  return (
    <div
      className={cn(
        "w-10 h-10 rounded-full bg-bg-elev-1 border border-border overflow-hidden flex-shrink-0",
      )}
    >
      {src ? (
        // eslint-disable-next-line @next/next/no-img-element
        <img src={src} alt={alt} className="w-full h-full object-cover" />
      ) : (
        <div className="w-full h-full grid place-items-center text-fg-dim text-xs">
          {alt[0]?.toUpperCase() ?? "?"}
        </div>
      )}
    </div>
  );
}
