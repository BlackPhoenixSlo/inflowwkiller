"use client";

/**
 * BrainPanel — the per-account "Brain" editor (account_ai_config) on
 * /automations. The voice + caps a model speaks with: persona,
 * the 6 time-of-day activities + their per-slot vault images, location/utc_offset,
 * the daily spend cap, and the LLM model (global + per-purpose override).
 *
 * gen_info / send_welcome / send_followup / of_ai_chat resolve this at run time,
 * so it's the screen that fills the brain a fresh account starts without. Reuses
 * the account picker pattern from AutomationsPanel and the VaultPicker image
 * slots from the Nudge tab.
 */

import { useEffect, useState } from "react";

import { Button, Card, Input } from "@/components/ui/primitives";
import { EditRawJsonButton } from "@/components/settings/JsonConfigModal";
import { cn } from "@/lib/utils";
import { useActiveAccounts } from "@/hooks/useAccounts";
import {
  useAccountConfig,
  useSaveAccountConfig,
  type BrainConfig,
} from "@/hooks/useAccountConfig";
import {
  useAutomationRules,
  useCreateRule,
  useUpdateRule,
  useAutomationPreview,
  useWelcomePin,
  type AutomationPreviewResult,
} from "@/hooks/useAutomations";
import { VaultPicker } from "@/components/chat/VaultPicker";
import { useVaultMediaByIds } from "@/hooks/useVaultMediaByIds";
import { proxyImage, type VaultMedia } from "@/lib/relay";
import { TIMEZONES, zoneLabel } from "@/components/settings/sellerShared";
import { useEnrichPersona } from "@/hooks/useAccountConfig";

// The welcome automation's default cadence: poll OF's new-subscriber feed every
// 5 min (matches the send_welcome spec's {every_seconds: 300}).
const WELCOME_DEFAULT_EVERY_S = 300;

// send_followup's default per-step silence thresholds (hours) — mirrors
// send_followup._STEP_THRESHOLDS_H {1:26, 2:64, 3:256}. Shown when the rule has
// no payload.step_hours override yet.
const FOLLOWUP_DEFAULT_STEP_HOURS = [26, 64, 256];
// Follow-up sweep cadence — how often the rule checks for due nudges (V1 ran the
// followup pass every 45 min). The per-fan silence thresholds above gate sends.
const FOLLOWUP_DEFAULT_EVERY_S = 45 * 60;

const SLOT_LABEL: Record<string, string> = {
  morning_1: "Morning (early)",
  morning_2: "Morning (late)",
  afternoon_1: "Afternoon (early)",
  afternoon_2: "Afternoon (late)",
  evening: "Evening",
  night: "Night",
};

// Human label for each per-purpose model override (the raw keys come from the
// API's PURPOSES). Unknown/future purposes fall back to their raw key.
const PURPOSE_LABEL: Record<string, string> = {
  gen_info: "Fan profiles",
  of_ai_chat: "Get to know fans (info-gather)",
  send_welcome: "Welcomes",
  send_followup: "Follow-ups",
  deep_convo: "Deep convo",
  reply_mass_funnel: "Mass-funnel replies",
};

// A brain is "blank" (never filled in) when it has no voice at all — so we seed
// the editor from the defaults instead of an empty form. time_images is ignored
// here: images are per-account, never part of the default.
function isBlankBrain(c: BrainConfig): boolean {
  return (
    !c.persona &&
    !c.location &&
    !c.model &&
    Object.keys(c.time_activities || {}).length === 0
  );
}

// The defaults, with this account's own images kept (defaults carry none).
// timezone AND her facts are account identity (who SHE is, where SHE lives), not
// brain voice — a reset must never move the creator to another city or wipe the
// birthplace her fans have already been told.
//
// `voice` is identity in exactly that sense and is preserved for exactly that
// reason. BRAIN_DEFAULTS has no voice key, so spreading it alone dropped the
// field entirely — and this function runs on the FIRST render of any blank brain,
// which is what a newly-created male account is. There is a Creator dropdown now,
// but this line is what stops "Reset to defaults" from quietly un-maling an
// account whose operator only meant to reset its brain text.
function defaultsWithImages(defaults: BrainConfig, current: BrainConfig): BrainConfig {
  return { ...defaults, time_images: current.time_images ?? {},
           timezone: current.timezone ?? null,
           voice: current.voice ?? "her",
           persona_facts: current.persona_facts ?? {} };
}

// "UTC-7" / "her clock: 1:20 AM" for the picked IANA zone — the operator's
// glance-check that the creator's clock is right (the Witcher incident was a
// bot claiming 10am at 12:30am creator-time). Computed by Intl, never stored:
// the saved utc_offset is only a legacy fallback for zone-less accounts.
function zoneOffsetLabel(tz: string): string | null {
  try {
    const part = new Intl.DateTimeFormat("en-US", { timeZone: tz, timeZoneName: "shortOffset" })
      .formatToParts(new Date())
      .find((p) => p.type === "timeZoneName")?.value;
    return part ? part.replace("GMT", "UTC") : null;
  } catch {
    return null;
  }
}

function localTimeIn(tz: string): string | null {
  try {
    return new Date().toLocaleTimeString("en-US", {
      timeZone: tz, hour: "numeric", minute: "2-digit",
    });
  } catch {
    return null;
  }
}

const textareaCls =
  "w-full rounded-lg bg-bg-elev-1 border border-border text-sm px-2 py-1.5 text-fg focus:outline-none focus:ring-2 focus:ring-accent/40";
const selectCls =
  "w-full rounded-lg bg-bg-elev-1 border border-border text-sm px-2 py-1.5 text-fg focus:outline-none focus:ring-2 focus:ring-accent/40";

export default function BrainPanel() {
  const accounts = useActiveAccounts();
  const [accountId, setAccountId] = useState<string | null>(null);

  useEffect(() => {
    if (!accountId && accounts.length > 0) setAccountId(accounts[0].id);
  }, [accounts, accountId]);

  const cfgQ = useAccountConfig(accountId);
  const saveM = useSaveAccountConfig(accountId);

  const [form, setForm] = useState<BrainConfig | null>(null);
  const [mediaCache, setMediaCache] = useState<Record<number, VaultMedia>>({});
  const [pickerSlot, setPickerSlot] = useState<string | null>(null);
  const [msg, setMsg] = useState<string | null>(null);

  // ── Welcome AUTOMATION (the send_welcome rule), surfaced here in the Brain
  // now that its settings live here too. Find-or-create one rule per account. ──
  const rulesQ = useAutomationRules(accountId);
  const welcomeRule =
    rulesQ.data?.find((r) => r.kind === "send_welcome") ?? null;
  // Separate mutation instances per section so each Save button's pending
  // state reflects only its own write — saving welcome must not show the
  // follow-up button as "Saving…" (and vice-versa).
  const createWelcomeRule = useCreateRule(accountId);
  const updateWelcomeRule = useUpdateRule(accountId);
  const createFollowupRule = useCreateRule(accountId);
  const updateFollowupRule = useUpdateRule(accountId);
  const previewM = useAutomationPreview();
  const enrichM = useEnrichPersona();
  const [enrichMsg, setEnrichMsg] = useState<string | null>(null);
  // Which keys the last 🪄 run filled — highlighted so the operator reviews the
  // PROPOSED values rather than the ones they already wrote themselves.
  const [enrichedKeys, setEnrichedKeys] = useState<string[]>([]);
  const welcomePinM = useWelcomePin();

  const [welcomeEnabled, setWelcomeEnabled] = useState(false);
  const [welcomeMinutes, setWelcomeMinutes] = useState(WELCOME_DEFAULT_EVERY_S / 60);
  const [welcomeMsg, setWelcomeMsg] = useState<string | null>(null);
  const [previewFan, setPreviewFan] = useState("");
  const [preview, setPreview] = useState<AutomationPreviewResult | null>(null);
  // "" = the creator's current local slot; otherwise one of the 6 slot keys.
  const [previewSlot, setPreviewSlot] = useState("");
  // Show the REAL AI-restyled line (what actually ships) vs the plain template.
  const [previewRestyle, setPreviewRestyle] = useState(true);

  // ── Follow-ups AUTOMATION (the send_followup rule) — the 3 step-delay hours
  // live on the rule's payload.step_hours; we find-or-create one rule per account. ──
  const followupRule =
    rulesQ.data?.find((r) => r.kind === "send_followup") ?? null;
  const followupPreviewM = useAutomationPreview();
  const [stepHours, setStepHours] = useState<number[]>(FOLLOWUP_DEFAULT_STEP_HOURS);
  const [followupEnabled, setFollowupEnabled] = useState(false);
  const [followupMinutes, setFollowupMinutes] = useState(FOLLOWUP_DEFAULT_EVERY_S / 60);
  const [followupWithImage, setFollowupWithImage] = useState(true);
  const [followupMsg, setFollowupMsg] = useState<string | null>(null);
  const [followupPreviewFan, setFollowupPreviewFan] = useState("");
  const [followupPreview, setFollowupPreview] = useState<AutomationPreviewResult | null>(null);

  // Reset on account switch, then seed once the config arrives (don't clobber
  // in-flight edits: only seed while the form is empty).
  useEffect(() => {
    setForm(null);
    setMsg(null);
    setWelcomeMsg(null);
    setPreview(null);
    setPreviewFan("");
    setPreviewSlot("");
    setPreviewRestyle(true);
    setFollowupMsg(null);
    setFollowupPreview(null);
    setFollowupPreviewFan("");
  }, [accountId]);
  // Seed the welcome toggle/cadence from the existing rule (or the defaults when
  // no rule exists yet). Keyed on the rule's identity so a later poll doesn't
  // stomp an in-flight edit between saves.
  useEffect(() => {
    if (welcomeRule) {
      setWelcomeEnabled(welcomeRule.is_enabled);
      setWelcomeMinutes(
        Math.max(1, Math.round((welcomeRule.every_seconds ?? WELCOME_DEFAULT_EVERY_S) / 60)),
      );
    } else {
      setWelcomeEnabled(false);
      setWelcomeMinutes(WELCOME_DEFAULT_EVERY_S / 60);
    }
  }, [welcomeRule?.id, welcomeRule?.is_enabled, welcomeRule?.every_seconds]);
  // Seed the step-delay hours + enable/cadence/with_image from the rule (defaults
  // when no rule exists yet). Keyed on rule identity so a poll won't stomp edits.
  useEffect(() => {
    const sh = followupRule?.payload?.step_hours;
    if (Array.isArray(sh) && sh.length === 3 && sh.every((n) => typeof n === "number")) {
      setStepHours(sh as number[]);
    } else {
      setStepHours(FOLLOWUP_DEFAULT_STEP_HOURS);
    }
    if (followupRule) {
      setFollowupEnabled(followupRule.is_enabled);
      setFollowupMinutes(
        Math.max(1, Math.round((followupRule.every_seconds ?? FOLLOWUP_DEFAULT_EVERY_S) / 60)),
      );
      setFollowupWithImage(followupRule.payload?.with_image !== false);
    } else {
      setFollowupEnabled(false);
      setFollowupMinutes(FOLLOWUP_DEFAULT_EVERY_S / 60);
      setFollowupWithImage(true);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [followupRule?.id, followupRule?.is_enabled, followupRule?.every_seconds, JSON.stringify(followupRule?.payload)]);
  // THE SELECTED LANE. The dropdown is form state until Save, so the stored
  // column still holds the old lane while the operator is looking at the new one.
  // Before the form exists there is no selection yet and the stored column IS the
  // answer, which is what the seed below wants.
  //
  // Read STRICTLY out of the per-lane maps — there is deliberately no fallback to
  // the singular `defaults` / `persona_fact_fields`. Those describe the STORED
  // lane, so under a moved dropdown they are the female starter brain on a male
  // account, i.e. the bug the maps were added to end, reachable through their own
  // fallback. The version-skew they would have covered does not exist: relay and
  // app rsync from one repo and rebuild together in one `docker compose up`
  // (scripts/deploy-fastt.sh), both stopped first, so no browser ever sees a
  // payload older than this file.
  const lane = form?.voice ?? cfgQ.data?.config?.voice ?? "her";
  // The canon field contract — keys, labels, placeholders and which are
  // operator-only — comes wholly from the API. Nothing about it lives here.
  // These labels are what the operator types against ("Why he started OF"), so a
  // female label on a male account is an instruction to write female canon.
  const factFields = cfgQ.data?.persona_fact_fields_by_voice?.[lane] ?? [];
  // Reset writes prose INTO the account, so there is no half-answer here: either
  // we hold the selected lane's brain or the button must not be clickable.
  const laneDefaults = cfgQ.data?.defaults_by_voice?.[lane];

  // Seed once the config arrives. A blank account (never saved a brain) gets the
  // one account-derived defaults so it has a worked example to show, not an empty form;
  // an account with its own brain keeps it. Images are never seeded from defaults.
  useEffect(() => {
    if (form === null && cfgQ.data && laneDefaults) {
      const { config } = cfgQ.data;
      setForm(isBlankBrain(config) ? defaultsWithImages(laneDefaults, config) : config);
    }
  }, [cfgQ.data, form, laneDefaults]);

  const slots = cfgQ.data?.slots ?? [];
  const modelOptions = cfgQ.data?.model_options ?? [];
  const purposes = cfgQ.data?.purposes ?? [];
  const languages = cfgQ.data?.languages ?? [{ code: "en", label: "English" }];
  // Resolve the SAVED slot image ids (and the two preview image ids) back to
  // VaultMedia so the slots/preview render real thumbnails on load — mediaCache
  // only ever holds images the operator just picked this session. The lookup
  // prefers the just-picked cache (instant, no fetch) and falls back to the
  // by-id resolution for everything that came from the stored config.
  const idsToResolve = [
    ...(form ? Object.values(form.time_images) : []),
    ...(preview?.image ? [preview.image] : []),
    ...(followupPreview?.image ? [followupPreview.image] : []),
  ].filter((n): n is number => typeof n === "number" && n > 0);
  const resolvedById = useVaultMediaByIds(accountId, idsToResolve);
  const lookupMedia = (id: number): VaultMedia | undefined =>
    mediaCache[id] ?? resolvedById[id];

  function set<K extends keyof BrainConfig>(key: K, val: BrainConfig[K]) {
    setForm((f) => (f ? { ...f, [key]: val } : f));
    setMsg(null);
  }
  function setFact(key: string, text: string) {
    setForm((f) =>
      f ? { ...f, persona_facts: { ...(f.persona_facts ?? {}), [key]: text } } : f,
    );
    setMsg(null);
  }
  function setActivity(slot: string, text: string) {
    setForm((f) => (f ? { ...f, time_activities: { ...f.time_activities, [slot]: text } } : f));
    setMsg(null);
  }
  function setImage(slot: string, mediaId: number | undefined) {
    setForm((f) => {
      if (!f) return f;
      const next = { ...f.time_images };
      if (mediaId === undefined) delete next[slot];
      else next[slot] = mediaId;
      return { ...f, time_images: next };
    });
    setMsg(null);
  }
  function setPurposeModel(purpose: string, model: string) {
    setForm((f) => {
      if (!f) return f;
      const next = { ...f.model_by_purpose };
      if (!model) delete next[purpose];
      else next[purpose] = model;
      return { ...f, model_by_purpose: next };
    });
    setMsg(null);
  }

  // Refill every brain field from the defaults, KEEPING this account's images
  // (defaults carry none). Doesn't persist — the operator reviews then Saves.
  function resetToDefaults() {
    if (!laneDefaults || !form) return;
    setForm(defaultsWithImages(laneDefaults, form));
    setMsg("Reset to defaults — review and Save brain to apply.");
  }

  async function save() {
    if (!form) return;
    setMsg(null);
    try {
      const res = await saveM.mutateAsync(form);
      setForm(res.config); // reflect server-side normalisation
      setMsg("Saved.");
    } catch (e) {
      setMsg((e as Error)?.message || "Save failed");
    }
  }

  // Find-or-create the account's send_welcome rule with the chosen enable +
  // cadence. The per-run knobs (limit, image, etc.) keep their defaults — the
  // Brain only owns the on/off + cadence here; richer knobs stay in the rules UI.
  async function saveWelcome() {
    if (!accountId) return;
    setWelcomeMsg(null);
    const every_seconds = Math.max(60, Math.round(welcomeMinutes * 60));
    try {
      if (welcomeRule) {
        await updateWelcomeRule.mutateAsync({
          id: welcomeRule.id,
          every_seconds,
          is_enabled: welcomeEnabled,
        });
      } else {
        await createWelcomeRule.mutateAsync({
          account_id: accountId,
          kind: "send_welcome",
          name: "Welcome new subscribers",
          every_seconds,
          is_enabled: welcomeEnabled,
        });
      }
      setWelcomeMsg("Saved.");
    } catch (e) {
      setWelcomeMsg((e as Error)?.message || "Save failed");
    }
  }

  // 🪄 Enrich — propose values for the EMPTY canon slots, and resolve her
  // timezone from the resulting city. Nothing is saved here: the proposal lands
  // in the form for the operator to read and edit, then the normal Save writes
  // it. The timezone comes back resolved in CODE (never model-guessed) — six
  // live accounts were 1-4h wrong and the prompt clock then instructs the model
  // to defend the wrong hour, so this is the one field a model may not author.
  async function runEnrich() {
    if (!accountId || !form) return;
    setEnrichMsg(null);
    try {
      const res = await enrichM.mutateAsync({ account_id: accountId });
      const filled = Object.keys(res.proposed ?? {});
      setForm((f) => (f ? { ...f, persona_facts: res.facts ?? {} } : f));
      setEnrichedKeys(filled);
      if (res.timezone && res.timezone !== form.timezone) {
        set("timezone", res.timezone);
      }
      const bits: string[] = [];
      bits.push(filled.length ? `filled ${filled.length} field${filled.length === 1 ? "" : "s"}` : "nothing left to fill");
      if (res.timezone && res.timezone_changed) bits.push(`timezone → ${res.timezone}`);
      else if (!res.timezone) bits.push("timezone unresolved — pick it below");
      setEnrichMsg(`${bits.join(" · ")}. Review, then Save.`);
      setMsg(null);
    } catch (e) {
      setEnrichMsg(e instanceof Error ? e.message : "enrich failed");
    }
  }

  async function runWelcomePreview(ignorePin = false, slotOverride?: string) {
    if (!accountId) return;
    setWelcomeMsg(null);
    // Preview against the UNSAVED on-screen edits (draft), not just the saved brain,
    // so tweaking an activity line + Regenerate is WYSIWYG. Only the fields the
    // welcome compose reads are sent; keys mirror the backend's _load_ai_config.
    const draftCfg = form
      ? {
          persona: form.persona,
          persona_facts: form.persona_facts,
          location: form.location,
          utc_offset: form.utc_offset,
          time_activities: form.time_activities,
          time_images: form.time_images,
          model: form.model,
        }
      : null;
    try {
      const res = await previewM.mutateAsync({
        account_id: accountId,
        kind: "send_welcome",
        fan_id: previewFan.trim() ? Number(previewFan.trim()) : null,
        // slotOverride pins the re-preview to a specific slot (after Keep/Unpin) so
        // it can't drift to whatever the dropdown currently shows.
        slot: slotOverride ?? (previewSlot || null),
        restyle: previewRestyle,
        config: draftCfg,
        ignore_pin: ignorePin, // Regenerate bypasses the pin to sample a fresh one
      });
      setPreview(res);
    } catch (e) {
      setWelcomeMsg(`Preview failed: ${(e as Error)?.message || "unknown"}`);
    }
  }

  // Pin the currently-previewed line for its slot → send_welcome sends exactly this
  // (weekday auto-refreshed) instead of re-rolling. Re-previews to reflect the pin.
  async function keepWelcomeLine() {
    if (!accountId || !preview?.slot || !preview.bubbles?.[1]) return;
    const slot = preview.slot; // the concrete slot the shown line belongs to
    setWelcomeMsg(null);
    try {
      await welcomePinM.mutateAsync({ account_id: accountId, slot, line: preview.bubbles[1] });
      setPreviewSlot(slot);              // keep the dropdown in sync with what we pinned
      await runWelcomePreview(false, slot); // re-preview THAT slot (now shows the pin)
      setWelcomeMsg("Pinned — this line will send for that slot.");
    } catch (e) {
      setWelcomeMsg(`Pin failed: ${(e as Error)?.message || "unknown"}`);
    }
  }

  async function unpinWelcomeLine() {
    if (!accountId || !preview?.slot) return;
    const slot = preview.slot;
    setWelcomeMsg(null);
    try {
      await welcomePinM.mutateAsync({ account_id: accountId, slot, line: null });
      setPreviewSlot(slot);
      await runWelcomePreview(false, slot);
      setWelcomeMsg("Unpinned — back to auto AI lines.");
    } catch (e) {
      setWelcomeMsg(`Unpin failed: ${(e as Error)?.message || "unknown"}`);
    }
  }

  const welcomeSaving = createWelcomeRule.isPending || updateWelcomeRule.isPending;

  // Find-or-create the account's send_followup rule, writing the 3 step-delay
  // hours onto its payload.step_hours (merged over any existing payload knobs).
  async function saveFollowup() {
    if (!accountId) return;
    setFollowupMsg(null);
    const step_hours = stepHours.map((h) => Math.max(1, Math.round(h)));
    const every_seconds = Math.max(60, Math.round(followupMinutes * 60));
    try {
      if (followupRule) {
        await updateFollowupRule.mutateAsync({
          id: followupRule.id,
          every_seconds,
          is_enabled: followupEnabled,
          payload: { ...followupRule.payload, step_hours, with_image: followupWithImage },
        });
      } else {
        await createFollowupRule.mutateAsync({
          account_id: accountId,
          kind: "send_followup",
          name: "Follow up quiet fans",
          every_seconds,
          payload: { step_hours, with_image: followupWithImage },
          is_enabled: followupEnabled,
        });
      }
      setFollowupMsg("Saved.");
    } catch (e) {
      setFollowupMsg((e as Error)?.message || "Save failed");
    }
  }

  async function runFollowupPreview() {
    if (!accountId) return;
    setFollowupMsg(null);
    try {
      const res = await followupPreviewM.mutateAsync({
        account_id: accountId,
        kind: "send_followup",
        fan_id: followupPreviewFan.trim() ? Number(followupPreviewFan.trim()) : null,
      });
      setFollowupPreview(res);
    } catch (e) {
      setFollowupMsg(`Preview failed: ${(e as Error)?.message || "unknown"}`);
    }
  }

  const followupSaving = createFollowupRule.isPending || updateFollowupRule.isPending;
  const STEP_LABELS = ["1st nudge after", "2nd nudge after", "3rd nudge after"];

  return (
    <Card className="p-4 space-y-3">
      <header className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <h2 className="text-sm font-medium text-fg">Brain</h2>
          <p className="text-xs text-fg-dim">
            The voice + caps this model speaks with — persona, time-of-day lines &amp;
            images, location, spend cap, and LLM model. Used by gen_info, welcome,
            follow-up and AI chat.
          </p>
        </div>
        <div className="flex items-center gap-2">
          <EditRawJsonButton surface="account-config" accountId={accountId} />
          <Button
            size="sm"
            variant="ghost"
            onClick={resetToDefaults}
            disabled={!form || !laneDefaults || saveM.isPending}
            title="Refill every field from the default brain (keeps this account's images)"
          >
            Reset to defaults
          </Button>
          <Button
            size="sm"
            variant="primary"
            onClick={save}
            disabled={!form || saveM.isPending}
          >
            {saveM.isPending ? "Saving…" : "Save brain"}
          </Button>
        </div>
      </header>

      {/* Account picker */}
      {accounts.length > 1 && (
        <div className="flex items-center gap-1.5 flex-wrap">
          <span className="text-[10px] uppercase tracking-wide text-fg-dim mr-1">Account</span>
          {accounts.map((a) => {
            const active = accountId === a.id;
            return (
              <button
                key={a.id}
                type="button"
                onClick={() => setAccountId(a.id)}
                className={cn(
                  "px-2.5 py-1 rounded-full text-xs border transition-colors",
                  active
                    ? "bg-accent text-white border-accent"
                    : "bg-bg-elev-1 text-fg-dim border-border hover:text-fg hover:border-fg-dim",
                )}
              >
                {a.nickname || a.id}
              </button>
            );
          })}
        </div>
      )}

      {/* Plain wrapper (no space-y of its own) so the phone-only sticky Save bar
       *  below is NOT a `space-y-3` sibling of the Card — appending a hidden
       *  last child would otherwise hand the block above it a 12px bottom
       *  margin at every width, including desktop. */}
      <div>
      {cfgQ.isLoading || !form ? (
        <div className="text-sm text-fg-dim py-2">Loading…</div>
      ) : (
        <div className="space-y-4 pb-16 md:pb-0">
          {/* Persona */}
          <label className="block space-y-1">
            <span className="text-[11px] uppercase tracking-wide text-fg-dim">Persona</span>
            <textarea
              rows={4}
              value={form.persona ?? ""}
              onChange={(e) => set("persona", e.target.value)}
              placeholder="Who this model is — name, vibe, how they talk…"
              className={textareaCls}
            />
          </label>

          {/* Location / voice / language / offset / cap */}
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
            <label className="block space-y-1">
              <span className="text-[11px] uppercase tracking-wide text-fg-dim">Location</span>
              <Input
                value={form.location ?? ""}
                onChange={(e) => set("location", e.target.value)}
                placeholder="e.g. Vancouver Island"
              />
            </label>
            {/* The creator's own gender. A dropdown and not a checkbox on purpose:
                it is a lane the account is IN, not a feature that is on — and the
                API speaks the same two values ("her" / "him"), so what the operator
                picks is exactly what gets stored. Saving "Female" writes NULL, which
                is what every account that has ever run here already has. */}
            <label className="block space-y-1">
              <span className="text-[11px] uppercase tracking-wide text-fg-dim">Creator</span>
              <select
                value={form.voice ?? "her"}
                onChange={(e) => set("voice", e.target.value)}
                className={selectCls}
                title="Whose voice this account writes in. Female is the default every account runs on. Male rewrites the texting frame, the FaceTime refusal, the persona canon labels ('Why he started OF'), the off-platform deflections and the script pack — nothing else on the account changes, and no other account is affected."
              >
                <option value="her">Female (default)</option>
                <option value="him">Male</option>
              </select>
            </label>
            <label className="block space-y-1">
              <span className="text-[11px] uppercase tracking-wide text-fg-dim">Language</span>
              <select
                value={form.language ?? "en"}
                onChange={(e) => set("language", e.target.value)}
                className={selectCls}
                title="The language this account writes in AND which safety-word list runs. Changing it re-generates each fan's saved lines on their next profile pass."
              >
                {languages.map((l) => (
                  <option key={l.code} value={l.code}>{l.label}</option>
                ))}
              </select>
            </label>
            <label className="block space-y-1">
              <span className="text-[11px] uppercase tracking-wide text-fg-dim">Timezone</span>
              <select
                value={form.timezone ?? ""}
                onChange={(e) => set("timezone", e.target.value || null)}
                className={selectCls}
                title="The creator's IANA timezone — powers the clock in chat prompts and the sleep window. The offset is computed from it (DST-aware), so there's nothing else to set."
              >
                <option value="">— not set —</option>
                {(form.timezone && !TIMEZONES.includes(form.timezone)
                  ? [form.timezone, ...TIMEZONES]
                  : TIMEZONES
                ).map((z) => (
                  <option key={z} value={z}>{zoneLabel(z)}</option>
                ))}
              </select>
              {form.timezone ? (
                <span className="block text-[11px] text-fg-dim">
                  {zoneOffsetLabel(form.timezone)} · local time: {localTimeIn(form.timezone)}
                </span>
              ) : form.utc_offset !== 0 ? (
                <span className="block text-[11px] text-amber-400"
                  title="A fixed offset drifts an hour every DST change — pick the real timezone above.">
                  legacy UTC{form.utc_offset > 0 ? "+" : ""}{form.utc_offset} offset in use
                </span>
              ) : (
                <span className="block text-[11px] text-fg-dim">
                  unset — no clock in chat
                </span>
              )}
            </label>
            <label className="block space-y-1">
              <span className="text-[11px] uppercase tracking-wide text-fg-dim">Daily cap (¢)</span>
              <Input
                type="number"
                min={0}
                value={String(form.daily_cost_cap_cents)}
                onChange={(e) => set("daily_cost_cap_cents", Math.max(0, Number(e.target.value) || 0))}
              />
            </label>
          </div>

          {/* ── The canon the chat AI may never contradict ── */}
          <div className="space-y-2 border-t border-border pt-3">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <span className="text-[11px] uppercase tracking-wide text-fg-dim">
                Creator facts
              </span>
              <button
                type="button"
                onClick={runEnrich}
                // Also gated on the slate having rendered. Enrich replaces
                // `persona_facts` wholesale and says "review, then Save" — so
                // with no boxes on screen it would be asking for a review of
                // something invisible, and Save would persist it unread. Same
                // rule as Reset one section up: a control that cannot be
                // answered for is not clickable.
                disabled={enrichM.isPending || !form.persona || !factFields.length}
                className="rounded border border-border px-2 py-1 text-[11px] hover:bg-bg-hover disabled:opacity-40"
                title={
                  form.persona
                    ? "Fill the empty fields from the persona, and work out the timezone from the city. Nothing is saved until you press Save."
                    : "Write the persona first — enrich fills the gaps in it."
                }
              >
                {enrichM.isPending ? "Enriching…" : "🪄 Enrich"}
              </button>
            </div>
            <p className="text-[11px] text-fg-dim">
              Pinned into every chat prompt as facts the creator can never contradict — and as
              material to relate to a fan with. A blank field is one the model will
              improvise, differently each time it scrolls out of the conversation.
              Ordered by how often fans actually ask. 🔒 fields are yours alone; Enrich
              never guesses those.
            </p>
            {enrichMsg ? (
              <p className="text-[11px] text-amber-400">{enrichMsg}</p>
            ) : null}
            <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
              {factFields.map((f) => {
                const filled = enrichedKeys.includes(f.key);
                return (
                  <label key={f.key} className="block space-y-1">
                    <span className="text-[11px] uppercase tracking-wide text-fg-dim">
                      {f.label}
                      {filled ? (
                        <span className="ml-1 text-amber-400" title="Just proposed by Enrich — check it before saving">
                          •
                        </span>
                      ) : null}
                      {f.operator_only ? (
                        <span
                          className="ml-1 text-fg-dim"
                          title="Enrich never fills this. What the creator will and won't do on camera is your call, not the model's — a wrong guess here either costs a sale or promises something the creator doesn't offer."
                        >
                          🔒
                        </span>
                      ) : null}
                    </span>
                    <Input
                      value={form.persona_facts?.[f.key] ?? ""}
                      onChange={(e) => setFact(f.key, e.target.value)}
                      placeholder={f.placeholder}
                      className={filled ? "border-amber-500/60" : undefined}
                    />
                  </label>
                );
              })}
            </div>
          </div>

          {/* Model + per-purpose overrides */}
          <div className="space-y-2 border-t border-border pt-3">
            <span className="text-[11px] uppercase tracking-wide text-fg-dim">Model</span>
            <label className="block space-y-1 max-w-xs">
              <span className="text-[11px] text-fg-dim">Default (all purposes)</span>
              <select
                value={form.model ?? ""}
                onChange={(e) => set("model", e.target.value || null)}
                className={selectCls}
              >
                <option value="">— system default —</option>
                {modelOptions.map((m) => (
                  <option key={m} value={m}>{m}</option>
                ))}
              </select>
            </label>

            {/* Per-purpose overrides — optional; each job uses a different model
                only if set, else it inherits the default above. */}
            {purposes.length > 0 && (
              <div className="space-y-1.5 pt-1">
                <span className="text-[11px] text-fg-dim">
                  Per-purpose overrides{" "}
                  <span className="text-fg-dim/70">— optional, each job inherits the default unless set</span>
                </span>
                <div className="grid gap-3 sm:grid-cols-3">
                  {purposes.map((p) => (
                    <label key={p} className="block space-y-1">
                      <span className="text-[11px] text-fg-dim">{PURPOSE_LABEL[p] ?? p}</span>
                      <select
                        value={form.model_by_purpose[p] ?? ""}
                        onChange={(e) => setPurposeModel(p, e.target.value)}
                        className={selectCls}
                      >
                        <option value="">inherit default</option>
                        {modelOptions.map((m) => (
                          <option key={m} value={m}>{m}</option>
                        ))}
                      </select>
                    </label>
                  ))}
                </div>
              </div>
            )}
          </div>

          {/* Time-of-day activities + images */}
          <div className="space-y-2 border-t border-border pt-3">
            <span className="text-[11px] uppercase tracking-wide text-fg-dim">
              Time-of-day lines &amp; images
            </span>
            <p className="text-[11px] text-fg-dim">
              The “it’s {`{weekday} {tod}`} here, I’m {`{activity}`}” line + the image
              welcome/follow-up attach for each slot.
            </p>
            <div className="space-y-2">
              {slots.map((slot) => {
                const imgId = form.time_images[slot];
                return (
                  <div key={slot} className="flex items-start gap-2">
                    <div className="w-28 shrink-0 pt-1.5 text-[11px] text-fg-dim">
                      {SLOT_LABEL[slot] ?? slot}
                    </div>
                    <Input
                      value={form.time_activities[slot] ?? ""}
                      onChange={(e) => setActivity(slot, e.target.value)}
                      placeholder="what they're doing…"
                      className="flex-1"
                    />
                    <SlotImage
                      id={imgId}
                      media={imgId ? lookupMedia(imgId) : undefined}
                      accountId={accountId}
                      onPick={() => setPickerSlot(slot)}
                      onClear={() => setImage(slot, undefined)}
                    />
                  </div>
                );
              })}
            </div>
          </div>

          {/* Welcome automation — enable + cadence + live preview */}
          <div className="space-y-2 border-t border-border pt-3">
            <div className="flex items-center justify-between gap-2">
              <span className="text-[11px] uppercase tracking-wide text-fg-dim">Welcome</span>
              <Button
                size="sm"
                variant="primary"
                onClick={saveWelcome}
                disabled={welcomeSaving || !accountId}
              >
                {welcomeSaving ? "Saving…" : welcomeRule ? "Save welcome" : "Enable welcome"}
              </Button>
            </div>
            <p className="text-[11px] text-fg-dim">
              Auto-message every new subscriber, using the persona, welcome rules &amp;
              time-of-day lines/images above.
            </p>
            <div className="flex flex-wrap items-end gap-3">
              <label className="flex items-center gap-2 text-sm text-fg">
                <input
                  type="checkbox"
                  checked={welcomeEnabled}
                  onChange={(e) => {
                    setWelcomeEnabled(e.target.checked);
                    setWelcomeMsg(null);
                  }}
                  className="accent-accent"
                />
                Enabled
              </label>
              <label className="flex items-center gap-1.5">
                <span className="text-[11px] text-fg-dim">Check every</span>
                <Input
                  type="number"
                  min={1}
                  value={String(welcomeMinutes)}
                  onChange={(e) => {
                    setWelcomeMinutes(Math.max(1, Number(e.target.value) || 1));
                    setWelcomeMsg(null);
                  }}
                  className="w-20"
                />
                <span className="text-[11px] text-fg-dim">min</span>
              </label>
            </div>

            {/* Live preview — composes the welcome text + image WITHOUT sending.
                With "AI restyle" on it runs the REAL restyle so you see the exact
                line that ships; Regenerate rerolls a fresh sample. */}
            <div className="flex flex-wrap items-end gap-2 pt-1">
              <label className="flex flex-col gap-0.5">
                <span className="text-[11px] text-fg-dim">Time of day</span>
                <select
                  value={previewSlot}
                  onChange={(e) => setPreviewSlot(e.target.value)}
                  className={selectCls}
                >
                  <option value="">Current time</option>
                  {slots.map((s) => (
                    <option key={s} value={s}>{SLOT_LABEL[s] ?? s}</option>
                  ))}
                </select>
              </label>
              <label className="flex items-center gap-1.5">
                <span className="text-[11px] text-fg-dim">Preview for fan id</span>
                <Input
                  value={previewFan}
                  onChange={(e) => setPreviewFan(e.target.value)}
                  placeholder="optional"
                  className="w-32"
                />
              </label>
              <label
                className="flex items-center gap-1.5 pb-1.5 text-sm text-fg"
                title="Run the real AI restyle so the preview matches what actually sends. Each preview/regenerate is a small AI call that counts toward your daily cap."
              >
                <input
                  type="checkbox"
                  checked={previewRestyle}
                  onChange={(e) => setPreviewRestyle(e.target.checked)}
                  className="accent-accent"
                />
                AI restyle
              </label>
              <Button
                size="sm"
                variant="ghost"
                onClick={() => runWelcomePreview(false)}
                disabled={previewM.isPending || !accountId}
              >
                {previewM.isPending ? "Composing…" : "Preview"}
              </Button>
              <Button
                size="sm"
                variant="ghost"
                onClick={() => runWelcomePreview(true)}
                disabled={previewM.isPending || !accountId || !preview}
                title="Reroll a fresh AI-restyled sample (ignores any pinned line)"
              >
                {previewM.isPending ? "…" : "↻ Regenerate"}
              </Button>
              {preview && preview.bubbles && preview.bubbles.length > 1 && !preview.pinned && (
                <Button
                  size="sm"
                  variant="primary"
                  onClick={keepWelcomeLine}
                  disabled={welcomePinM.isPending || !accountId}
                  title="Send THIS exact line for this slot from now on (weekday auto-updates)"
                >
                  {welcomePinM.isPending ? "Pinning…" : "✓ Keep this line"}
                </Button>
              )}
              {preview?.pinned && (
                <Button
                  size="sm"
                  variant="ghost"
                  onClick={unpinWelcomeLine}
                  disabled={welcomePinM.isPending || !accountId}
                  title="Stop sending the fixed line — go back to auto AI restyle"
                >
                  {welcomePinM.isPending ? "…" : "Unpin"}
                </Button>
              )}
            </div>
            {previewRestyle && (
              <p className="text-[10px] text-fg-dim">
                Uses a real AI call (counts toward the daily cap) so the preview
                matches what ships. Turn off to preview the plain template for free.
              </p>
            )}
            {preview && (
              <div className="flex items-start gap-2 rounded-lg border border-border bg-bg-elev-1 p-2">
                {preview.image ? (
                  <PreviewThumb
                    id={preview.image}
                    media={lookupMedia(preview.image)}
                    accountId={accountId}
                  />
                ) : null}
                <div className="min-w-0 flex-1 space-y-1">
                  <div className="text-[10px] uppercase tracking-wide text-fg-dim">
                    Greeting “{preview.name}” · {preview.slot}
                    {preview.image ? "" : " · no image"}
                  </div>
                  {preview.cap_hit && (
                    <div className="text-[10px] text-warn">
                      Daily AI cap reached — showing the plain template line.
                    </div>
                  )}
                  {preview.pinned && (
                    <div className="text-[10px] text-accent">
                      📌 Pinned — this exact line sends for “{SLOT_LABEL[preview.slot] ?? preview.slot}”
                      (weekday auto-updates). Hit ↻ Regenerate to try a new one.
                    </div>
                  )}
                  {preview.bubbles?.length ? (
                    // Send shape: one block per chat bubble (image rides on the
                    // first; the 2nd is the AI-restyled activity line).
                    <div className="space-y-1">
                      {preview.bubbles.map((b, i) => (
                        <p
                          key={i}
                          className="whitespace-pre-wrap rounded-md bg-bg-elev-2 px-2 py-1 text-sm text-fg"
                        >
                          {i === 1 && (preview.pinned || preview.restyled) && (
                            <span className="mr-1 rounded bg-accent/20 px-1 text-[9px] uppercase tracking-wide text-accent align-middle">
                              {preview.pinned ? "📌 pinned" : "AI"}
                            </span>
                          )}
                          {b}
                        </p>
                      ))}
                      {preview.bubbles.length > 1 && (
                        <div className="text-[10px] text-fg-dim">
                          {preview.pinned
                            ? "2 bubbles · typing pauses between · 2nd line is your pinned line (sends as-is)"
                            : preview.restyled
                            ? "2 bubbles · typing pauses between · 2nd line is the AI-restyled line that ships (↻ Regenerate for another, or Keep this line to lock it in)"
                            : "2 bubbles · typing pauses between · 2nd line is the plain template (turn on AI restyle to preview the shipped line)"}
                        </div>
                      )}
                    </div>
                  ) : (
                    <p className="whitespace-pre-wrap text-sm text-fg">{preview.text}</p>
                  )}
                </div>
              </div>
            )}
            {welcomeMsg && (
              <div className={cn("text-xs", welcomeMsg === "Saved." ? "text-ok" : "text-err")}>
                {welcomeMsg}
              </div>
            )}
          </div>

          {/* Follow-ups automation — the 3 step-delay hours + live preview */}
          <div className="space-y-2 border-t border-border pt-3">
            <div className="flex items-center justify-between gap-2">
              <span className="text-[11px] uppercase tracking-wide text-fg-dim">Follow-ups</span>
              <Button
                size="sm"
                variant="primary"
                onClick={saveFollowup}
                disabled={followupSaving || !accountId}
              >
                {followupSaving ? "Saving…" : "Save follow-ups"}
              </Button>
            </div>
            <p className="text-[11px] text-fg-dim">
              How long a fan stays quiet before each re-engagement nudge. Up to three,
              each escalating in warmth. Enable + cadence are owned here — turn it on
              and Save to start it.
            </p>

            {/* Enable + cadence + image — fully start follow-ups from here. */}
            <div className="flex flex-wrap items-center gap-4">
              <label className="flex items-center gap-1.5 text-sm">
                <input
                  type="checkbox"
                  checked={followupEnabled}
                  onChange={(e) => { setFollowupEnabled(e.target.checked); setFollowupMsg(null); }}
                  className="accent-accent"
                />
                Enabled
              </label>
              <label className="flex items-center gap-1.5">
                <span className="text-[11px] text-fg-dim">Check every</span>
                <Input
                  type="number"
                  min={1}
                  value={String(followupMinutes)}
                  onChange={(e) => { setFollowupMinutes(Math.max(1, Number(e.target.value) || 1)); setFollowupMsg(null); }}
                  className="w-20"
                />
                <span className="text-[11px] text-fg-dim">min</span>
              </label>
              <label className="flex items-center gap-1.5 text-sm">
                <input
                  type="checkbox"
                  checked={followupWithImage}
                  onChange={(e) => { setFollowupWithImage(e.target.checked); setFollowupMsg(null); }}
                  className="accent-accent"
                />
                Attach image
              </label>
            </div>
            {followupWithImage && (
              <p className="text-[10px] text-fg-dim">
                Uses the <span className="text-fg">time-of-day images set above</span> (the
                per-slot pictures, shared with welcome). Set a picture for each slot up top —
                the nudge attaches the one matching the fan&apos;s local time. Slots left
                blank send text only.
              </p>
            )}

            <div className="flex flex-wrap gap-3">
              {stepHours.map((h, i) => (
                <label key={i} className="flex items-center gap-1.5">
                  <span className="text-[11px] text-fg-dim">{STEP_LABELS[i]}</span>
                  <Input
                    type="number"
                    min={1}
                    value={String(h)}
                    onChange={(e) => {
                      const v = Math.max(1, Number(e.target.value) || 1);
                      setStepHours((prev) => prev.map((x, j) => (j === i ? v : x)));
                      setFollowupMsg(null);
                    }}
                    className="w-20"
                  />
                  <span className="text-[11px] text-fg-dim">h</span>
                </label>
              ))}
            </div>

            {/* Live preview — composes the nudge text + image WITHOUT sending */}
            <div className="flex flex-wrap items-end gap-2 pt-1">
              <label className="flex items-center gap-1.5">
                <span className="text-[11px] text-fg-dim">Preview for fan id</span>
                <Input
                  value={followupPreviewFan}
                  onChange={(e) => setFollowupPreviewFan(e.target.value)}
                  placeholder="optional"
                  className="w-32"
                />
              </label>
              <Button
                size="sm"
                variant="ghost"
                onClick={runFollowupPreview}
                disabled={followupPreviewM.isPending || !accountId}
              >
                {followupPreviewM.isPending ? "Composing…" : "Preview"}
              </Button>
            </div>
            {followupPreview && (
              <div className="flex items-start gap-2 rounded-lg border border-border bg-bg-elev-1 p-2">
                {followupPreview.image ? (
                  <PreviewThumb
                    id={followupPreview.image}
                    media={lookupMedia(followupPreview.image)}
                    accountId={accountId}
                  />
                ) : null}
                <div className="min-w-0 flex-1 space-y-1">
                  <div className="text-[10px] uppercase tracking-wide text-fg-dim">
                    Nudge to “{followupPreview.name}” · step {followupPreview.step ?? 1} · {followupPreview.slot}
                    {followupPreview.image ? "" : " · no image"}
                  </div>
                  <p className="whitespace-pre-wrap text-sm text-fg">{followupPreview.text}</p>
                </div>
              </div>
            )}
            {followupMsg && (
              <div className={cn("text-xs", followupMsg === "Saved." ? "text-ok" : "text-err")}>
                {followupMsg}
              </div>
            )}
          </div>

          {cfgQ.isError && (
            <div className="text-xs text-err">
              {(cfgQ.error as Error)?.message || "Failed to load brain"}
            </div>
          )}
          {msg && (
            <div className={cn("text-xs", msg === "Saved." ? "text-ok" : "text-err")}>{msg}</div>
          )}
        </div>
      )}

      {/* Phone-only Save bar. The real Save lives in this Card's header, which
       *  is ~2500px above the last control at 390px. `md:hidden` removes the
       *  whole node at desktop, so nothing here can compute above 768px. */}
      <div className="md:hidden sticky bottom-0 z-20 -mx-4 mt-3 flex items-center gap-2 border-t border-border bg-panel/95 px-4 py-3 pb-[calc(env(safe-area-inset-bottom)+0.75rem)] backdrop-blur">
        <Button
          size="sm"
          variant="primary"
          onClick={save}
          disabled={!form || saveM.isPending}
          className="flex-1 min-h-11"
        >
          {saveM.isPending ? "Saving…" : "Save brain"}
        </Button>
      </div>
      </div>

      {pickerSlot && accountId && (
        <VaultPicker
          open
          onClose={() => setPickerSlot(null)}
          accountId={accountId}
          fanId={null}
          initialSelectedIds={form?.time_images[pickerSlot] ? [form.time_images[pickerSlot]] : []}
          onConfirm={(picked) => {
            const m = picked[0];
            if (m) {
              setMediaCache((prev) => ({ ...prev, [m.id]: m }));
              setImage(pickerSlot, m.id);
            }
            setPickerSlot(null);
          }}
        />
      )}
    </Card>
  );
}

function SlotImage({
  id,
  media,
  accountId,
  onPick,
  onClear,
}: {
  id: number | undefined;
  media: VaultMedia | undefined;
  accountId: string | null;
  onPick: () => void;
  onClear: () => void;
}) {
  const thumb = media?.files?.thumb?.url || media?.files?.squarePreview?.url || media?.files?.preview?.url || "";
  const url = thumb ? proxyImage(thumb, accountId) : "";
  return (
    <div className="flex items-center gap-1 shrink-0">
      {id ? (
        <>
          <button
            type="button"
            onClick={onPick}
            title="Change image"
            className="w-9 h-9 rounded-md border border-border overflow-hidden bg-bg-elev-1 flex items-center justify-center text-[9px] text-fg-dim"
          >
            {url ? (
              // eslint-disable-next-line @next/next/no-img-element
              <img src={url} alt="" className="w-full h-full object-cover" />
            ) : (
              `#${id}`
            )}
          </button>
          <button
            type="button"
            onClick={onClear}
            title="Remove image"
            className="text-fg-dim hover:text-err text-xs"
          >
            ✕
          </button>
        </>
      ) : (
        <Button size="sm" variant="ghost" onClick={onPick}>
          + Image
        </Button>
      )}
    </div>
  );
}

// Read-only thumbnail for the welcome preview — shows the chosen image, falling
// back to its id badge when we don't have the VaultMedia cached (the preview
// returns only the id; the slot editor populates mediaCache on pick).
function PreviewThumb({
  id,
  media,
  accountId,
}: {
  id: number;
  media: VaultMedia | undefined;
  accountId: string | null;
}) {
  const thumb =
    media?.files?.thumb?.url || media?.files?.squarePreview?.url || media?.files?.preview?.url || "";
  const url = thumb ? proxyImage(thumb, accountId) : "";
  return (
    <div className="w-12 h-12 shrink-0 rounded-md border border-border overflow-hidden bg-bg-elev-2 flex items-center justify-center text-[9px] text-fg-dim">
      {url ? (
        // eslint-disable-next-line @next/next/no-img-element
        <img src={url} alt="" className="w-full h-full object-cover" />
      ) : (
        `#${id}`
      )}
    </div>
  );
}
