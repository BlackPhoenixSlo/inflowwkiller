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
import { ArrowDownLeft, ArrowUpRight, Eye, Megaphone, Paperclip, Users } from "lucide-react";

import { Badge } from "@/components/ui/primitives";
import { proxyImage } from "@/lib/relay";
import { cn, fmtRelTime } from "@/lib/utils";

import type { PaidMessageRow } from "@/hooks/usePaidMessages";

interface Props {
  row: PaidMessageRow;
}

/** Friendly labels for the automation_kind tokens (= grok_calls.purpose). */
const AUTOMATION_LABELS: Record<string, string> = {
  of_ai_chat: "AI Chat",
  welcome: "Welcome",
  deep_convo: "Deep Convo",
  followup: "Follow-up",
  autoreply: "Auto-reply",
  reply_mass_funnel: "Funnel reply",
  send_mass_message: "Mass message",
  mass_nudge: "Mass nudge",
  online_blast: "Online blast",
  nudge_online: "Online nudge",
  gen_info: "Profiler",
};

export function automationLabel(kind?: string | null): string | null {
  if (!kind) return null;
  return AUTOMATION_LABELS[kind] ?? kind;
}

function fmtPrice(cents: number): string {
  return `$${(cents / 100).toFixed(2)}`;
}

function stripHtml(s: string): string {
  return s.replace(/<\/?p[^>]*>/gi, "").replace(/<br\s*\/?>/gi, " ").trim();
}

export default function MessageRowGeneric({ row }: Props) {
  if (row.is_mass_summary) return <MassSummaryRow row={row} />;

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
              <Badge color="info">TIP</Badge>
            )}

            {isPriced && !isSystem && (
              isPaid ? (
                <Badge color="ok">PAID</Badge>
              ) : (
                <Badge color="muted">UNPAID</Badge>
              )
            )}

            {!isInbound && (automationLabel(row.automation_kind) ? (
              // Specific automation that sent it — beats the flat "Automation"
              // sentinel that employee_name resolves to for every bot send.
              <Badge color="muted" className="!px-1.5 !text-[10px]">
                <Megaphone size={10} aria-hidden />
                <span>{automationLabel(row.automation_kind)}</span>
              </Badge>
            ) : row.employee_name ? (
              <span className="text-[11px] text-fg-dim">
                by {row.employee_name}
              </span>
            ) : null)}

            <span className="ml-auto text-xs text-fg-dim group-hover:text-accent">
              ↗ open chat
            </span>
          </div>
        </div>
      </div>
    </Link>
  );
}

/**
 * MassSummaryRow — one collapsed row standing in for a whole broadcast
 * (mass_run), instead of N identical per-fan rows flooding the list. Shows
 * recipient count, the automation + funnel that ran it, and the broadcast body.
 * Not a chat link (there's no single fan); it's an at-a-glance audit row.
 */
function MassSummaryRow({ row }: Props) {
  const previewBody = stripHtml(row.body);
  const isPriced = row.price_cents > 0;
  const label = automationLabel(row.automation_kind) ?? "Mass message";

  return (
    <div className="flex gap-3 p-3 border border-border rounded-xl bg-panel border-l-2 border-l-warn/50">
      <div className="w-10 h-10 rounded-full bg-warn/10 border border-warn/30 grid place-items-center flex-shrink-0 text-warn">
        <Megaphone size={16} aria-hidden />
      </div>

      <div className="flex-1 min-w-0">
        <div className="flex items-center gap-2 flex-wrap">
          <Badge color="warn" className="!px-1.5">
            <span>MASS</span>
          </Badge>
          <span className="font-medium text-fg text-sm">{label}</span>
          {row.funnel_name && (
            <span className="text-xs text-fg-dim truncate">· {row.funnel_name}</span>
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
          <Badge color="muted">
            <Users size={12} aria-hidden />
            <span>{row.recipient_count ?? 0} sent</span>
          </Badge>
          {(row.viewed_count ?? 0) > 0 && (
            <Badge color="muted">
              <Eye size={12} aria-hidden />
              <span>{row.viewed_count} viewed</span>
            </Badge>
          )}
          {row.media_count > 0 && (
            <Badge color="muted">
              <Paperclip size={12} aria-hidden />
              <span>{row.media_count}</span>
            </Badge>
          )}
        </div>
      </div>
    </div>
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
