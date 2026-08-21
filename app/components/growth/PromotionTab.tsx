"use client";

/**
 * PromotionTab — Growth → Promotion.
 *
 * Run subscription-promo campaigns for the creator's profile (discounted price
 * for N claims / N days) and read their performance. OF is the source of truth;
 * a local mirror keeps the operator's name + last-seen subscriber count.
 */

import { useState } from "react";

import { Badge, Button, Card, Input } from "@/components/ui/primitives";
import { ConfirmDeleteButton, Field, NumInput, errMsg } from "@/components/growth/_bits";
import {
  usePromotions, useCreatePromotion, useDeletePromotion, useSetPromoAutoReactivate,
} from "@/hooks/useGrowth";
import PromoReactivateCard from "@/components/growth/PromoReactivateCard";

export default function PromotionTab({ accountId }: { accountId: string | null }) {
  const promosQ = usePromotions(accountId);
  const createM = useCreatePromotion(accountId);
  const deleteM = useDeletePromotion(accountId);
  const reactM = useSetPromoAutoReactivate(accountId);

  const [name, setName] = useState("");
  const [discount, setDiscount] = useState(50);
  const [counts, setCounts] = useState(10);
  const [days, setDays] = useState(30);
  const [message, setMessage] = useState("");
  const [err, setErr] = useState<string | null>(null);

  const campaigns = promosQ.data?.campaigns ?? [];
  const ofAvailable = promosQ.data?.of_available ?? false;
  // 100% off IS OnlyFans' free trial: it sets price to 0 and, uniquely, honours
  // the duration. Below 100% the duration is discarded.
  const isFreeTrial = discount >= 100;

  async function create() {
    setErr(null);
    if (!name.trim()) { setErr("Name is required."); return; }
    if (discount < 1 || discount > 100) { setErr("Discount must be 1–100%."); return; }
    // OF ignores the duration for a percent discount (it stores 0 and sets no
    // end date) — only a 100%-off promo honours it. Don't promise an expiry
    // that will not happen, and don't promise a cap when 0 means unlimited.
    const offer = isFreeTrial
      ? `FREE for ${days} day${days === 1 ? "" : "s"}`
      : `${discount}% off with no time limit`;
    const cap = counts > 0
      ? `ending after ${counts} claim${counts === 1 ? "" : "s"}`
      : "with UNLIMITED claims — it runs until you end it";
    if (!window.confirm(`Create a real OnlyFans promo campaign — ${offer}, ${cap}?`)) return;
    try {
      await createM.mutateAsync({
        name: name.trim(), discount, subscribe_counts: counts,
        subscribe_days: days, message: message.trim(),
      });
      setName(""); setMessage("");
    } catch (e) { setErr(errMsg(e, "Create failed")); }
  }

  return (
    <div className="space-y-4 max-w-3xl">
      <header>
        <h2 className="text-lg font-semibold">Profile Promotion</h2>
        <p className="text-sm text-fg-dim">
          Discounted subscription campaigns for your profile — set a discount, a
          claim cap and a duration, then track sign-ups.
          {!ofAvailable && (
            <span className="text-warn"> · OnlyFans not reachable — showing saved campaigns only.</span>
          )}
        </p>
      </header>

      <Card className="p-4 space-y-3">
        <div className="grid gap-3 sm:grid-cols-4">
          <div className="sm:col-span-2">
            <Field label="Name">
              <Input value={name} onChange={(e) => setName(e.target.value)} placeholder="e.g. Summer 50% off" />
            </Field>
          </div>
          <Field label={isFreeTrial ? "Discount — 100% = free" : "Discount (% off)"}>
            <NumInput value={discount} min={1} onChange={setDiscount} />
          </Field>
          <Field label="Max claims (0 = unlimited)">
            <NumInput value={counts} min={0} onChange={setCounts} />
          </Field>
          {/* OF only honours a duration at 100% off. For a discount it discards
            *  the value and the offer runs until the claim cap fills, so
            *  offering the field as if it worked would be a lie. */}
          <Field label={isFreeTrial ? "Free days" : "Days (ignored by OF)"}>
            <NumInput value={days} min={1} onChange={setDays} />
          </Field>
          <div className="sm:col-span-3">
            <Field label="Message (optional)">
              <Input value={message} onChange={(e) => setMessage(e.target.value)} placeholder="Shown with the promo" />
            </Field>
          </div>
        </div>
        <div className="flex items-center gap-2">
          {err && <span className="text-err text-xs mr-auto">{err}</span>}
          <Button size="sm" onClick={create} disabled={createM.isPending || !accountId}
            className={err ? "" : "ml-auto"}>
            {createM.isPending ? "Creating…" : "Create promotion"}
          </Button>
        </div>
      </Card>

      {promosQ.isLoading && <div className="text-sm text-fg-dim">Loading…</div>}
      {!promosQ.isLoading && campaigns.length === 0 && (
        <div className="text-sm text-fg-dim">No promotions yet.</div>
      )}

      <ul className="divide-y divide-border">
        {campaigns.map((c, i) => (
          <li key={c.id ?? `of-${c.of_promo_id ?? i}`} className="py-3 flex flex-wrap items-center gap-3">
            <div className="min-w-0 flex-1">
              <div className="flex items-center gap-2">
                <span className="text-sm font-medium text-fg truncate">{c.name}</span>
                <Badge color={c.status === "active" ? "ok" : "muted"}>{c.status}</Badge>
                {c.source === "onlyfans" && <Badge color="muted">OnlyFans</Badge>}
              </div>
              <div className="text-[11px] text-fg-dim">
                {c.discount_percent === 100 ? "FREE trial"
                  : c.discount_percent != null ? `${c.discount_percent}% off` : "promo"} ·{" "}
                {/* OF reports 0 days for a percent discount — it has no end
                  *  date, it ends when the claim cap fills. Saying "0d" or
                  *  echoing the typed number would both mislead. */}
                {c.subscribe_days > 0 ? `${c.subscribe_days}d · ` : "no time limit · "}
                {/* OF echoes an unlimited cap back as null, not 0. */}
                {c.subscribe_counts ? `up to ${c.subscribe_counts} claims` : "unlimited claims"} ·{" "}
                <b className="text-fg">{c.subscriber_count}</b> claimed
              </div>
              {/* Opt this campaign into the re-arm automation. OnlyFans-only rows
               *  aren't mirrored locally, so they can't be re-created. */}
              {c.id != null && (
                <label className="mt-1 inline-flex items-center gap-1.5 text-[11px] text-fg-dim cursor-pointer">
                  <input
                    type="checkbox"
                    checked={c.auto_reactivate}
                    disabled={reactM.isPending}
                    onChange={(e) => reactM.mutate({ id: c.id!, auto_reactivate: e.target.checked })}
                    className="w-3.5 h-3.5 rounded accent-accent cursor-pointer"
                  />
                  Keep running <span className="opacity-70">— re-create it when it ends</span>
                  {c.reactivate_count > 0 && (
                    <Badge color="info">re-armed {c.reactivate_count}×</Badge>
                  )}
                </label>
              )}
            </div>
            {c.id != null && (
              <ConfirmDeleteButton
                confirm={`Delete promotion "${c.name}"?`}
                onConfirm={() => deleteM.mutate(c.id!)}
                pending={deleteM.isPending}
              />
            )}
          </li>
        ))}
      </ul>

      <PromoReactivateCard accountId={accountId} />
    </div>
  );
}
