"use client";

/**
 * WebhookDispatchTab — Settings → "⚡ Instant reply" (W7 webhook-priority dispatch).
 *
 * Per-account toggle for real-time dispatch: when ON, an inbound fan DM makes the
 * bot react in seconds instead of waiting for the 30s poll tick. The response
 * delay (+ random jitter) makes it feel human and gives a live human chatter a
 * head start — if the human replies during the wait, the bot stands down.
 *
 * Persists account_ai_config.webhook_config_json via the owner-gated
 * /admin/webhook-config routes (useWebhookConfig). Default OFF. ⚠️ Enabling on an
 * account whose inbound is stranger/promo spam would auto-reply to strangers —
 * only turn on for accounts with real fans.
 */

import { useEffect, useMemo, useState } from "react";
import { Zap } from "lucide-react";

import { Button, Card } from "@/components/ui/primitives";
import { EditRawJsonButton } from "./JsonConfigModal";
import {
  useWebhookConfig,
  useSaveWebhookConfig,
  type WebhookConfig,
} from "@/hooks/useWebhookConfig";

const INPUT_CLS =
  "w-24 bg-bg border border-border rounded-lg px-3 py-2 text-base md:text-sm focus:outline-none focus:border-accent";

export default function WebhookDispatchTab({ accountId }: { accountId: string | null }) {
  const cfgQ = useWebhookConfig(accountId);
  const saveM = useSaveWebhookConfig(accountId);

  const eff: WebhookConfig = useMemo(() => {
    const d = cfgQ.data?.defaults ?? {};
    const c = cfgQ.data?.config ?? {};
    return { ...d, ...c };
  }, [cfgQ.data]);

  const [form, setForm] = useState<WebhookConfig>({});
  // Re-seed the form whenever the loaded config changes (account switch / refetch).
  useEffect(() => {
    setForm(eff);
  }, [eff]);

  if (!accountId) {
    return <div className="text-sm text-fg-dim">Pick an account above.</div>;
  }
  if (cfgQ.isLoading) {
    return <div className="text-sm text-fg-dim">Loading…</div>;
  }

  const enabled = !!form.enabled;
  const delay = form.delay_seconds ?? 0;
  const jitter = form.jitter_seconds ?? 0;
  const lo = delay;
  const hi = delay + jitter;
  const manualYield = form.manual_yield_minutes ?? 1;
  const typingWpm = form.typing_wpm ?? 38;
  const typingIndicator = form.typing_indicator ?? true;  // ON by default

  return (
    <Card className="p-4 space-y-4 max-w-xl">
      <header className="flex items-center gap-2">
        <Zap size={16} className="text-accent" />
        <h3 className="text-sm font-medium">Instant reply (real-time dispatch)</h3>
      </header>

      <div className="text-xs text-fg-dim leading-relaxed space-y-2">
        <p>
          This does not change <span className="text-fg">what</span> the AI says —
          only <span className="text-fg">how fast</span> it replies. Normally the
          bot checks each chat on a 30-second timer. When ON, an incoming fan
          message wakes the same AI reply <span className="text-fg">instantly</span>,
          so it answers in seconds instead of waiting for the next check.
        </p>
        <p>
          The 30-second check keeps running underneath as a safety net, and the
          two can never double-reply a fan: whichever fires first sends, the other
          sees the fan was just answered and stands down.
        </p>
        <p>
          A human always wins: if you or a chatter are handling a chat, the bot
          backs off (see “Stand down”, below).
        </p>
      </div>

      {/* Enabled toggle */}
      <label className="flex items-center gap-3 cursor-pointer">
        <input
          type="checkbox"
          className="h-4 w-4 accent-[var(--accent)]"
          checked={enabled}
          onChange={(e) => setForm((f) => ({ ...f, enabled: e.target.checked }))}
        />
        <span className="text-sm">
          {enabled ? "Enabled — replies in real time" : "Disabled (polls every 30s)"}
        </span>
      </label>

      {/* Delay + jitter */}
      <div className="flex items-end gap-4">
        <label className="space-y-1">
          <div className="text-xs text-fg-dim">Wait before replying (seconds)</div>
          <input
            type="number"
            min={0}
            max={600}
            className={INPUT_CLS}
            value={delay}
            disabled={!enabled}
            onChange={(e) =>
              setForm((f) => ({ ...f, delay_seconds: Math.max(0, Number(e.target.value) || 0) }))
            }
          />
        </label>
        <label className="space-y-1">
          <div className="text-xs text-fg-dim">Random extra (jitter, seconds)</div>
          <input
            type="number"
            min={0}
            max={600}
            className={INPUT_CLS}
            value={jitter}
            disabled={!enabled}
            onChange={(e) =>
              setForm((f) => ({ ...f, jitter_seconds: Math.max(0, Number(e.target.value) || 0) }))
            }
          />
        </label>
      </div>
      {enabled && (
        <p className="text-xs text-fg-dim">
          Replies land{" "}
          <span className="text-fg font-medium">
            {hi > lo ? `${lo}–${hi}s` : `${lo}s`}
          </span>{" "}
          after a fan's message. A short, slightly random wait keeps it from
          looking robotic and gives a human chatter a moment to step in first.
          Leave both at 0 for an immediate reply.
        </p>
      )}

      {/* Manual-yield: how long the bot stands down after a human takes over. */}
      <div className="pt-1 border-t border-border space-y-1.5">
        <label className="space-y-1 block">
          <div className="text-xs text-fg-dim">
            Stand down after a human sends in the chat (minutes)
          </div>
          <input
            type="number"
            min={0}
            max={1440}
            step="0.5"
            className={INPUT_CLS}
            value={manualYield}
            onChange={(e) =>
              setForm((f) => ({ ...f, manual_yield_minutes: Math.max(0, Number(e.target.value) || 0) }))
            }
          />
        </label>
        <p className="text-xs text-fg-dim">
          {manualYield > 0
            ? `When you (or a chatter) message a fan, the bot won't auto-reply to them for ${manualYield} min.`
            : "0 = the bot is never paused by a human message (not recommended)."}
        </p>
      </div>

      {/* Typing speed: each reply bubble is held for the time it'd take to type. */}
      <div className="pt-1 border-t border-border space-y-1.5">
        <label className="space-y-1 block">
          <div className="text-xs text-fg-dim">Typing speed (words per minute)</div>
          <input
            type="number"
            min={0}
            max={400}
            step="5"
            className={INPUT_CLS}
            value={typingWpm}
            onChange={(e) =>
              setForm((f) => ({ ...f, typing_wpm: Math.max(0, Number(e.target.value) || 0) }))
            }
          />
        </label>
        <p className="text-xs text-fg-dim">
          {typingWpm > 0
            ? `Each reply bubble lands after the time it'd take to type it at ${typingWpm} wpm (a 10-word line ≈ ${Math.round((10 / typingWpm) * 60)}s). Applies to AI chat, Auto Convo and deep convo.`
            : "0 = bubbles send instantly (no typing delay)."}
        </p>
      </div>

      {/* Live "...is typing" indicator during the typing-speed hold. */}
      <div className="pt-1 border-t border-border space-y-1.5">
        <label className="flex items-center gap-3 cursor-pointer">
          <input
            type="checkbox"
            className="h-4 w-4 accent-[var(--accent)]"
            checked={typingIndicator}
            onChange={(e) =>
              setForm((f) => ({ ...f, typing_indicator: e.target.checked }))
            }
          />
          <span className="text-sm">Show the fan a live “…is typing” bubble</span>
        </label>
        <p className="text-xs text-fg-dim">
          {typingIndicator
            ? typingWpm > 0
              ? "While each bubble is “being typed”, the fan sees OnlyFans’ real typing indicator (re-sent every ~2.5s). Applies to AI chat, Auto Convo, deep convo and funnels."
              : "Needs a typing speed above 0 — with no typing delay there’s no window to show the indicator."
            : "Off — replies arrive after the typing delay with no visible typing bubble."}
        </p>
      </div>

      <div className="flex items-center gap-3 pt-1">
        <Button
          onClick={() => saveM.mutate(form)}
          disabled={saveM.isPending}
        >
          {saveM.isPending ? "Saving…" : "Save"}
        </Button>
        {saveM.isSuccess && !saveM.isPending && (
          <span className="text-xs text-emerald-500">Saved ✓</span>
        )}
        {saveM.isError && (
          <span className="text-xs text-red-500">Save failed</span>
        )}
        <div className="ml-auto">
          <EditRawJsonButton surface="webhook-config" accountId={accountId} />
        </div>
      </div>
    </Card>
  );
}
