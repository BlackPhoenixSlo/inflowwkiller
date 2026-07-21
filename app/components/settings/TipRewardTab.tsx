"use client";

/**
 * TipRewardTab — Automations → "🎁 Tip Reward".
 *
 * Per-account config for the `tip_reward` automation: when a fan tips, send them
 * vault images they haven't received yet. Writes account_ai_config.
 * tip_reward_config_json via /admin/tip-reward-config (mirrors AutoreplyTab).
 *
 * The rule, in plain terms:
 *   • COUNT  = tip ÷ "$ per image", clamped between min and max.
 *   • FOLDER = picked by a TIER — the highest tier whose threshold the fan has
 *     reached, where the threshold is checked against max(this tip, the fan's
 *     tip total over the last N hours). So a fan who has tipped past a tier in
 *     the window keeps that tier even on a small follow-up tip.
 *   • Images are FREE and only ever ones this fan has never been sent.
 *
 * Layout: two independent feature SECTIONS (ASK / REWARD), each with its own
 * on/off switch in its header. The body of each section only renders when its
 * switch is on, so the tab reads as two cards instead of one long column.
 */

import { useEffect, useMemo, useState } from "react";
import { Gift, FolderOpen, MessageCircle, Image as ImageIcon, Flame, HandCoins } from "lucide-react";

import { Button, Card } from "@/components/ui/primitives";
import { VaultFolderPicker } from "@/components/settings/VaultFolderPicker";
import { EditRawJsonButton } from "@/components/settings/JsonConfigModal";
import {
  useTipRewardConfig,
  useSaveTipRewardConfig,
  type TipRewardConfig,
} from "@/hooks/useTipRewardConfig";

const INPUT =
  "bg-bg border border-border rounded-lg px-3 py-2 text-base md:text-sm focus:outline-none focus:border-accent";

/** A tier as the form holds it. Folders are the vault folder NAMES this tier
 *  draws from — picked visually, the same string[] shape tip_reward stores. */
interface TierForm {
  name: string;
  minDollars: number;
  folders: string[];
}

function tiersToForm(tiers: TipRewardConfig["tiers"]): TierForm[] {
  return (tiers ?? []).map((t) => ({
    name: t.name ?? "",
    minDollars: Math.round((t.min_basis_cents ?? 0) / 100),
    folders: (t.folders ?? []).filter((f) => f.trim()),
  }));
}

export default function TipRewardTab({ accountId }: { accountId: string | null }) {
  const cfgQ = useTipRewardConfig(accountId);
  const saveM = useSaveTipRewardConfig(accountId);

  const eff: TipRewardConfig = useMemo(() => {
    const d = cfgQ.data?.defaults ?? {};
    const c = cfgQ.data?.config ?? {};
    return { ...d, ...c };
  }, [cfgQ.data]);

  const [enabled, setEnabled] = useState(false);
  const [alwaysReward, setAlwaysReward] = useState(false);
  const [dollarsPerImage, setDollarsPerImage] = useState(10);
  const [minImages, setMinImages] = useState(1);
  const [maxImages, setMaxImages] = useState(5);
  const [windowHours, setWindowHours] = useState(72);
  const [caption, setCaption] = useState("");
  const [tiers, setTiers] = useState<TierForm[]>([]);
  const [pickerTier, setPickerTier] = useState<number | null>(null);
  // ASK side (independent of the delivery `enabled` above): default ON, no set price.
  const [askEnabled, setAskEnabled] = useState(true);
  const [askAmount, setAskAmount] = useState<string>(""); // "" → ask with no fixed price
  const [askTemplate, setAskTemplate] = useState("");
  // Inbound-image buying-signal handler — two independent flags (default OFF).
  const [imageReplyEnabled, setImageReplyEnabled] = useState(false);
  const [imageCloserEnabled, setImageCloserEnabled] = useState(false);
  const [imageReplyCooldown, setImageReplyCooldown] = useState(6);
  const [imageReplyCaption, setImageReplyCaption] = useState("");

  // Hot-thread teaser (sent by the AI Seller when a thread goes hot).
  const [htEnabled, setHtEnabled] = useState(false);
  const [htCount, setHtCount] = useState(3);
  const [htCooldown, setHtCooldown] = useState(6);
  const [htFreeFolder, setHtFreeFolder] = useState("");
  const [htFreeMax, setHtFreeMax] = useState(3);
  const [htPaidFolder, setHtPaidFolder] = useState("");
  const [htPrice, setHtPrice] = useState(15); // dollars in the form; cents on the wire
  const [htPicker, setHtPicker] = useState<null | "free" | "paid">(null);

  // Conversational teaser ladder — climbs free → $10 → $50 during ordinary chat.
  const [cvEnabled, setCvEnabled] = useState(false);
  const [cvAfter, setCvAfter] = useState(20);
  const [cvCount, setCvCount] = useState(1);
  const [cvRungs, setCvRungs] = useState<{ folder: string; price: number }[]>([]);
  const [cvPicker, setCvPicker] = useState<number | null>(null); // which rung's folder

  // Item 42 — tip-request follow-up (nested tip_request config).
  const [trEnabled, setTrEnabled] = useState(false);
  const [trMediaId, setTrMediaId] = useState<string>(""); // "" = unset
  const [trCaption, setTrCaption] = useState("");
  const [trMinWait, setTrMinWait] = useState(2);
  const [trMaxAge, setTrMaxAge] = useState(48);
  const [trCooldown, setTrCooldown] = useState(168);

  useEffect(() => {
    setEnabled(!!eff.enabled);
    setAlwaysReward(!!eff.always_reward);
    setDollarsPerImage(eff.dollars_per_image ?? 10);
    setMinImages(eff.min_images ?? 1);
    setMaxImages(eff.max_images ?? 5);
    setWindowHours(eff.window_hours ?? 72);
    setCaption(eff.caption ?? "");
    setTiers(tiersToForm(eff.tiers));
    setAskEnabled(eff.ask_enabled !== false); // default ON when unset
    setAskAmount(eff.ask_amount_dollars == null ? "" : String(eff.ask_amount_dollars));
    setAskTemplate(eff.ask_template ?? "");
    setImageReplyEnabled(!!eff.image_reply_enabled);
    setImageCloserEnabled(!!eff.image_closer_enabled);
    setImageReplyCooldown(eff.image_reply_cooldown_hours ?? 6);
    setImageReplyCaption(eff.image_reply_caption ?? "");
    setHtEnabled(!!eff.hot_teaser_enabled);
    setHtCount(eff.hot_teaser_count ?? 3);
    setHtCooldown(eff.hot_teaser_cooldown_hours ?? 6);
    setHtFreeFolder(eff.hot_teaser_free_folder ?? "");
    setHtFreeMax(eff.hot_teaser_free_max ?? 3);
    setHtPaidFolder(eff.hot_teaser_paid_folder ?? "");
    setHtPrice(Math.max(0, Math.round((eff.hot_teaser_price_cents ?? 1500) / 100)));
    setCvEnabled(!!eff.teaser_convo_enabled);
    setCvAfter(eff.teaser_convo_after_fan_msgs ?? 20);
    setCvCount(eff.teaser_convo_count ?? 1);
    setCvRungs(
      (eff.teaser_convo_rungs ?? []).map((r) => ({
        folder: r.folder ?? "",
        price: Math.max(0, Math.round((r.price_cents ?? 0) / 100)),
      })),
    );
    const tr = eff.tip_request ?? {};
    setTrEnabled(!!tr.enabled);
    setTrMediaId(tr.media_id == null ? "" : String(tr.media_id));
    setTrCaption(tr.caption ?? "");
    setTrMinWait(tr.min_wait_hours ?? 2);
    setTrMaxAge(tr.max_age_hours ?? 48);
    setTrCooldown(tr.cooldown_hours ?? 168);
  }, [eff]);

  if (!accountId) return <div className="text-sm text-fg-dim">Pick an account above.</div>;
  if (cfgQ.isLoading) return <div className="text-sm text-fg-dim">Loading…</div>;

  // Any user edit makes the "Saved ✓"/error feedback stale — clear it so the
  // indicator only ever reflects the currently-persisted state.
  const markDirty = () => {
    if (saveM.isSuccess || saveM.isError) saveM.reset();
  };

  const setTier = (i: number, patch: Partial<TierForm>) => {
    markDirty();
    setTiers((ts) => ts.map((t, j) => (j === i ? { ...t, ...patch } : t)));
  };
  const addTier = () => {
    markDirty();
    setTiers((ts) => [...ts, { name: "", minDollars: 0, folders: [] }]);
  };
  const removeTier = (i: number) => {
    markDirty();
    setTiers((ts) => ts.filter((_, j) => j !== i));
  };

  const setCvRung = (i: number, patch: Partial<{ folder: string; price: number }>) => {
    markDirty();
    setCvRungs((rs) => rs.map((r, j) => (j === i ? { ...r, ...patch } : r)));
  };
  const addCvRung = () => {
    markDirty();
    setCvRungs((rs) => [...rs, { folder: "", price: 0 }]);
  };
  const removeCvRung = (i: number) => {
    markDirty();
    setCvRungs((rs) => rs.filter((_, j) => j !== i));
  };

  function buildConfig(): TipRewardConfig {
    const askAmtTrim = askAmount.trim();
    return {
      enabled,
      always_reward: alwaysReward,
      dollars_per_image: dollarsPerImage,
      min_images: minImages,
      max_images: maxImages,
      window_hours: windowHours,
      caption: caption.trim(),
      // ASK side — null amount means "ask for a tip without naming a price".
      ask_enabled: askEnabled,
      ask_amount_dollars: askAmtTrim === "" ? null : Math.max(1, Math.round(Number(askAmtTrim) || 0)),
      ask_template: askTemplate.trim(),
      // Inbound-image buying-signal handler — both flags draw on the tiers below.
      image_reply_enabled: imageReplyEnabled,
      image_closer_enabled: imageCloserEnabled,
      image_reply_cooldown_hours: imageReplyCooldown,
      image_reply_caption: imageReplyCaption.trim(),
      // Hot-thread teaser — form holds the price in DOLLARS; the wire is cents.
      hot_teaser_enabled: htEnabled,
      hot_teaser_count: htCount,
      hot_teaser_cooldown_hours: htCooldown,
      hot_teaser_free_folder: htFreeFolder.trim(),
      hot_teaser_free_max: htFreeMax,
      hot_teaser_paid_folder: htPaidFolder.trim(),
      hot_teaser_price_cents: Math.max(0, Math.round(htPrice * 100)),
      // Conversational teaser ladder — rungs climb free → $10 → $50 as the chat goes.
      teaser_convo_enabled: cvEnabled,
      teaser_convo_after_fan_msgs: cvAfter,
      teaser_convo_count: cvCount,
      teaser_convo_rungs: cvRungs.map((r) => ({
        folder: r.folder.trim(),
        price_cents: Math.max(0, Math.round(r.price * 100)),
      })),
      // Item 42 — tip-request follow-up. media_id "" → null (stays disabled).
      tip_request: {
        enabled: trEnabled,
        media_id: trMediaId.trim() === "" ? null : Math.max(1, Math.round(Number(trMediaId) || 0)),
        caption: trCaption.trim(),
        min_wait_hours: trMinWait,
        max_age_hours: trMaxAge,
        cooldown_hours: trCooldown,
      },
      tiers: tiers
        // a tier with no folders does nothing — drop it on save
        .map((t) => ({
          name: t.name.trim(),
          min_basis_cents: Math.max(0, Math.round(t.minDollars * 100)),
          folders: t.folders.map((f) => f.trim()).filter(Boolean),
        }))
        .filter((t) => t.folders.length > 0),
    };
  }

  const noFolders = tiers.every((t) => t.folders.length === 0);

  return (
    <Card className="p-5 space-y-4 max-w-2xl">
      <header className="flex items-center gap-2">
        <Gift size={16} className="text-accent" />
        <h3 className="text-sm font-medium">Tip Reward</h3>
        <div className="ml-auto">
          <EditRawJsonButton surface="tip-reward-config" accountId={accountId} />
        </div>
      </header>

      <p className="text-xs text-fg-dim leading-relaxed">
        Independent switches around the tip loop: get the AI to <b>ask</b> fans for a
        tip when they beg to see content, <b>reward</b> fans who tip with free vault
        media, and react when a fan <b>sends you a photo</b> (a buying signal) — send
        one back and/or hand them to the AI closer. Each works on its own.
      </p>

      {/* ── ASK ───────────────────────────────────────────────────────────
          The AI can ask for a tip even when reward delivery is off. */}
      <Section
        icon={<MessageCircle size={15} />}
        title="Ask fans to tip for content"
        subtitle="When a fan asks to see something in chat (“can i see…”, “send me something”), the AI teases “tip me and I’ll send you something” instead of a normal reply."
        toggle={
          <Toggle
            checked={askEnabled}
            onChange={(v) => {
              markDirty();
              setAskEnabled(v);
            }}
          />
        }
      >
        {askEnabled && (
          <label className="block space-y-1">
            <div className="text-xs font-medium text-fg">Suggested amount (optional)</div>
            <div className="flex items-center gap-2">
              <span className="text-xs text-fg-dim">$</span>
              <input
                type="number"
                min={1}
                max={10000}
                placeholder="no set amount"
                className={`${INPUT} w-40`}
                value={askAmount}
                onChange={(e) => {
                  markDirty();
                  setAskAmount(e.target.value);
                }}
              />
            </div>
            <div className="text-[11px] text-fg-dim/70">
              Leave blank (recommended) so she asks for a tip without naming a price.
              Set a number only if you want her to suggest a specific amount.
            </div>
          </label>
        )}
      </Section>

      {/* ── REWARD ─────────────────────────────────────────────────────────
          Delivery side: send free vault media to fans who tip. */}
      <Section
        icon={<Gift size={15} />}
        title="Reward tippers with vault media"
        subtitle={`Send free photos & videos the fan hasn’t seen yet — about one item per $${dollarsPerImage} tipped (capped). Bigger tippers can be routed to nicer folders.`}
        toggle={
          <Toggle
            checked={enabled}
            onChange={(v) => {
              markDirty();
              setEnabled(v);
            }}
          />
        }
      >
        {enabled && (
          <div className="space-y-5">
            {/* How many images */}
            <div className="space-y-2">
              <div className="text-xs font-medium text-fg">How many to send</div>
              <div className="flex flex-wrap gap-4">
                <NumField
                  label="$ per image" hint="1 image for every this many dollars"
                  value={dollarsPerImage} min={1} max={10000} suffix="$"
                  onChange={(n) => { markDirty(); setDollarsPerImage(n); }}
                />
                <NumField
                  label="Minimum" hint="floor for any tip (even under $/image)"
                  value={minImages} min={0} max={50}
                  onChange={(n) => { markDirty(); setMinImages(n); }}
                />
                <NumField
                  label="Maximum (cap)" hint="a whale tip never sends more than this"
                  value={maxImages} min={1} max={50}
                  onChange={(n) => { markDirty(); setMaxImages(n); }}
                />
              </div>
            </div>

            {/* Caption */}
            <label className="block space-y-1">
              <div className="text-xs font-medium text-fg">Caption (optional)</div>
              <input
                type="text"
                className={`${INPUT} w-full`}
                placeholder="omg thank you babe 🥰 here's something just for you…"
                value={caption}
                onChange={(e) => {
                  markDirty();
                  setCaption(e.target.value);
                }}
              />
              <div className="text-[11px] text-fg-dim/70">
                Sent as the message text with the images. Leave blank for images only.
              </div>
            </label>

            {/* Always reward — override the open-offer standdown */}
            <label className="flex items-start gap-3 cursor-pointer">
              <input
                type="checkbox"
                className="h-4 w-4 mt-0.5 accent-[var(--accent)]"
                checked={alwaysReward}
                onChange={(e) => {
                  markDirty();
                  setAlwaysReward(e.target.checked);
                }}
              />
              <span className="space-y-0.5">
                <span className="block text-sm">Always reward, even mid-sale</span>
                <span className="block text-[11px] text-fg-dim/80 leading-relaxed">
                  By default, when the AI has an open paid (PPV) offer with a fan, a tip
                  is treated as payment toward that offer and the reward stands down so
                  the fan doesn&apos;t get free media on top of what they paid for. Turn
                  this on to <b>always</b> send the reward anyway — the paid offer is
                  still credited, the fan just also gets the thank-you media.
                </span>
              </span>
            </label>

            {/* Tiers */}
            <div className="space-y-2">
              <div className="text-xs font-medium text-fg">Folder tiers</div>
              <p className="text-[11px] text-fg-dim/80 leading-relaxed">
                The fan&apos;s tier is the highest one whose threshold they&apos;ve reached,
                measured by the bigger of <i>this tip</i> and their tip total over the last{" "}
                <b>{windowHours}h</b>. Pick the vault folder(s) each tier draws from — both
                images and videos inside are eligible. A tier with no folders is ignored.
              </p>

              <div className="space-y-2">
                {tiers.map((t, i) => (
                  <div
                    key={i}
                    className="rounded-lg border border-border bg-bg-elev-1 p-3 space-y-2"
                  >
                    <div className="flex items-center gap-2 flex-wrap">
                      <input
                        type="text"
                        className={`${INPUT} w-32`}
                        placeholder="tier name"
                        value={t.name}
                        onChange={(e) => setTier(i, { name: e.target.value })}
                      />
                      <span className="text-xs text-fg-dim">applies at</span>
                      <div className="flex items-center gap-1">
                        <span className="text-xs text-fg-dim">$</span>
                        <input
                          type="number"
                          min={0}
                          className={`${INPUT} w-24`}
                          value={t.minDollars}
                          onChange={(e) =>
                            setTier(i, { minDollars: Math.max(0, Number(e.target.value) || 0) })
                          }
                        />
                        <span className="text-xs text-fg-dim">+</span>
                      </div>
                      <button
                        type="button"
                        onClick={() => removeTier(i)}
                        className="ml-auto text-fg-dim hover:text-err text-sm px-1"
                        title="Remove tier"
                      >
                        ✕
                      </button>
                    </div>
                    <div className="flex flex-wrap items-center gap-1.5">
                      {t.folders.length === 0 && (
                        <span className="text-[11px] text-fg-dim italic">No folders yet.</span>
                      )}
                      {t.folders.map((nm) => (
                        <span
                          key={nm}
                          className="inline-flex items-center gap-1 text-xs bg-bg-elev-1 border border-border rounded-full px-2 py-0.5"
                        >
                          {nm}
                          <button
                            type="button"
                            onClick={() =>
                              setTier(i, { folders: t.folders.filter((f) => f !== nm) })
                            }
                            className="text-fg-dim hover:text-err leading-none"
                            title="Remove folder"
                          >
                            ×
                          </button>
                        </span>
                      ))}
                      <Button
                        size="sm"
                        variant="secondary"
                        onClick={() => setPickerTier(i)}
                      >
                        <FolderOpen size={13} /> Pick from vault
                      </Button>
                    </div>
                  </div>
                ))}
              </div>

              <Button size="sm" variant="secondary" onClick={addTier}>
                + Add tier
              </Button>
            </div>
          </div>
        )}
      </Section>

      {/* ── IMAGE REPLY (Flag 1) ───────────────────────────────────────────
          A fan sending US a photo is a buying signal — send one free pic back. */}
      <Section
        icon={<ImageIcon size={15} />}
        title="Send a free pic back when a fan sends one"
        subtitle="When a fan sends you a photo (a buying signal), reply with one free vault item they haven’t seen — drawn from the basic “under $10” tier folders below. Throttled per fan so a photo-spammer can’t drain a folder."
        toggle={
          <Toggle
            checked={imageReplyEnabled}
            onChange={(v) => {
              markDirty();
              setImageReplyEnabled(v);
            }}
          />
        }
      >
        {imageReplyEnabled && (
          <div className="space-y-4">
            <NumField
              label="Cooldown (hours)"
              hint="min hours between free pics to the same fan (0 = every photo)"
              value={imageReplyCooldown} min={0} max={8760}
              onChange={(n) => { markDirty(); setImageReplyCooldown(n); }}
            />
            <label className="block space-y-1">
              <div className="text-xs font-medium text-fg">Caption (optional)</div>
              <input
                type="text"
                className={`${INPUT} w-full`}
                placeholder="mmm you first 🙈 here's one back…"
                value={imageReplyCaption}
                onChange={(e) => {
                  markDirty();
                  setImageReplyCaption(e.target.value);
                }}
              />
              <div className="text-[11px] text-fg-dim/70">
                Draws from the <b>basic</b> (lowest) tier in “Reward tippers” above —
                set folders there for this to have anything to send.
              </div>
            </label>
          </div>
        )}
      </Section>

      {/* ── IMAGE CLOSER (Flag 2) ───────────────────────────────────────────
          Hand a fan who just sent a photo to the AI closer (ai_chatter). */}
      <Section
        icon={<Flame size={15} />}
        title="Let the AI closer take over when a fan sends a pic"
        subtitle="A fan sending a photo is a hot moment — kick the AI Seller (closer) for that fan right away to convert it into a sale, even in intent-only mode."
        toggle={
          <Toggle
            checked={imageCloserEnabled}
            onChange={(v) => {
              markDirty();
              setImageCloserEnabled(v);
            }}
          />
        }
      >
        {imageCloserEnabled && (
          <p className="text-[11px] text-fg-dim/80 leading-relaxed">
            Requires <b>AI Seller</b> (ai_chatter) to be enabled for this account —
            the closer <i>is</i> the AI Seller. With it off, this switch does nothing.
          </p>
        )}
      </Section>

      {/* ── HOT-THREAD TEASER ──────────────────────────────────────────────
          The AI Seller attaches vault media to her reply when a thread goes hot —
          free warm-up for a $0 fan, a priced tease PPV for a proven buyer. */}
      <Section
        icon={<Flame size={15} />}
        title="Send pics when a chat gets hot"
        subtitle="When a conversation turns sexual and nothing’s being sold, the AI Seller attaches a few unseen vault pics to her next reply — free to warm up a fan who’s never paid (capped), or a priced tease PPV for a proven buyer. The pics ARE the lead-up."
        toggle={
          <Toggle
            checked={htEnabled}
            onChange={(v) => {
              markDirty();
              setHtEnabled(v);
            }}
          />
        }
      >
        {htEnabled && (
          <div className="space-y-5">
            <div className="flex flex-wrap gap-4">
              <NumField
                label="Pics per send" hint="unseen vault items each time"
                value={htCount} min={1} max={50}
                onChange={(n) => { markDirty(); setHtCount(n); }}
              />
              <NumField
                label="Cooldown (h)" hint="min hours between teasers to one fan"
                value={htCooldown} min={0} max={8760}
                onChange={(n) => { markDirty(); setHtCooldown(n); }}
              />
            </div>

            {/* FREE branch — $0 fans */}
            <div className="space-y-2">
              <div className="text-xs font-medium text-fg">Free warm-up — fans who’ve never paid</div>
              <p className="text-[11px] text-fg-dim/80 leading-relaxed">
                A fan with $0 lifetime spend gets these <b>free</b> — the images build
                the heat that makes the sale land. Hard-capped so a freeloader can’t
                drain the folder.
              </p>
              <FolderRow
                label="Free folder"
                folder={htFreeFolder}
                onPick={() => setHtPicker("free")}
                onClear={() => { markDirty(); setHtFreeFolder(""); }}
              />
              <NumField
                label="Free cap per fan" hint="most free teasers one $0 fan ever gets"
                value={htFreeMax} min={0} max={1000}
                onChange={(n) => { markDirty(); setHtFreeMax(n); }}
              />
            </div>

            {/* PAID branch — proven buyers */}
            <div className="space-y-2">
              <div className="text-xs font-medium text-fg">Paid tease — proven buyers</div>
              <p className="text-[11px] text-fg-dim/80 leading-relaxed">
                A fan who has paid before gets a locked tease PPV in the hot moment
                instead of a freebie.
              </p>
              <FolderRow
                label="Paid folder"
                folder={htPaidFolder}
                onPick={() => setHtPicker("paid")}
                onClear={() => { markDirty(); setHtPaidFolder(""); }}
              />
              <NumField
                label="PPV price" hint="price of the locked tease" suffix="$"
                value={htPrice} min={0} max={1000}
                onChange={(n) => { markDirty(); setHtPrice(n); }}
              />
            </div>

            <p className="text-[11px] text-fg-dim/70 leading-relaxed">
              Requires <b>AI Seller</b> (ai_chatter) enabled — the teaser rides her
              reply, so it never sends an extra message and never fires on a fan who
              said he’s broke.
            </p>
          </div>
        )}
      </Section>

      {/* ── CONVERSATIONAL TEASER LADDER ───────────────────────────────────
          Not hot-gated — climbs free → $10 → $50 during ordinary chat. */}
      <Section
        icon={<Flame size={15} />}
        title="Escalating teases during normal chat"
        subtitle="Even when it isn’t sexual yet: after every N of his messages, drop the next rung — a free tease first, then the $10 one, then the $50 one. The price climbs as the conversation goes. Rides her reply, so it’s never an extra message."
        toggle={
          <Toggle
            checked={cvEnabled}
            onChange={(v) => {
              markDirty();
              setCvEnabled(v);
            }}
          />
        }
      >
        {cvEnabled && (
          <div className="space-y-5">
            <div className="flex flex-wrap gap-4">
              <NumField
                label="Every N of his messages" hint="messages between rungs"
                value={cvAfter} min={1} max={1000}
                onChange={(n) => { markDirty(); setCvAfter(n); }}
              />
              <NumField
                label="Pics per tease" hint="unseen vault items each rung"
                value={cvCount} min={1} max={50}
                onChange={(n) => { markDirty(); setCvCount(n); }}
              />
            </div>

            <div className="space-y-2">
              <div className="text-xs font-medium text-fg">Rungs (climb in order)</div>
              <p className="text-[11px] text-fg-dim/80 leading-relaxed">
                Rung 1 fires first, then rung 2 the next time, and so on — holding at the
                last one. Price 0 = a free tease. A rung with no folder is skipped.
              </p>
              <div className="space-y-2">
                {cvRungs.map((r, i) => (
                  <div
                    key={i}
                    className="rounded-lg border border-border bg-bg-elev-1 p-3 space-y-2"
                  >
                    <div className="flex items-center gap-2 flex-wrap">
                      <span className="text-xs text-fg-dim">Rung {i + 1}</span>
                      <span className="text-xs text-fg-dim ml-1">$</span>
                      <input
                        type="number" min={0} max={1000}
                        className={`${INPUT} w-24`}
                        value={r.price}
                        onChange={(e) =>
                          setCvRung(i, { price: Math.max(0, Number(e.target.value) || 0) })
                        }
                      />
                      <span className="text-[11px] text-fg-dim/70">
                        {r.price === 0 ? "free tease" : "priced PPV"}
                      </span>
                      <button
                        type="button"
                        onClick={() => removeCvRung(i)}
                        className="ml-auto text-fg-dim hover:text-err text-sm px-1"
                        title="Remove rung"
                      >
                        ✕
                      </button>
                    </div>
                    <FolderRow
                      label="Folder"
                      folder={r.folder}
                      onPick={() => setCvPicker(i)}
                      onClear={() => setCvRung(i, { folder: "" })}
                    />
                  </div>
                ))}
              </div>
              <Button size="sm" variant="secondary" onClick={addCvRung}>
                + Add rung
              </Button>
            </div>

            <p className="text-[11px] text-fg-dim/70 leading-relaxed">
              Requires <b>AI Seller</b> (ai_chatter) enabled. A free rung keeps an
              ordinary chat warm; a priced rung is still an offer — it won’t go to a fan
              who said he’s broke or turned one down.
            </p>
          </div>
        )}
      </Section>

      {/* ── TIP REQUEST (item 42) ──────────────────────────────────────────
          A fan buys a MASS PPV and goes quiet → send one free teaser + ask for
          a tip. Its own automation (tip_request); needs a schedule rule to fire. */}
      <Section
        icon={<HandCoins size={15} />}
        title="Ask a quiet mass-buyer for a tip"
        subtitle="When a fan buys one of your mass PPVs and then doesn't reply, send them one free teaser image with a caption asking for a tip — to warm the chat back up. One global image for everyone."
        toggle={
          <Toggle
            checked={trEnabled}
            onChange={(v) => {
              markDirty();
              setTrEnabled(v);
            }}
          />
        }
      >
        {trEnabled && (
          <div className="space-y-4">
            <label className="block space-y-1">
              <div className="text-xs font-medium text-fg">Teaser image — vault media id</div>
              <input
                type="number"
                min={1}
                className={`${INPUT} w-56`}
                placeholder="e.g. 4123456789"
                value={trMediaId}
                onChange={(e) => {
                  markDirty();
                  setTrMediaId(e.target.value);
                }}
              />
              <div className="text-[11px] text-fg-dim/70">
                The one free image sent to every quiet mass-buyer. Blank = disabled
                (nothing sends). Grab the id from a vault item.
              </div>
            </label>
            <label className="block space-y-1">
              <div className="text-xs font-medium text-fg">Caption</div>
              <input
                type="text"
                className={`${INPUT} w-full`}
                placeholder="hope you loved that 🥺 wanna send me a lil tip so i keep going?"
                value={trCaption}
                onChange={(e) => {
                  markDirty();
                  setTrCaption(e.target.value);
                }}
              />
            </label>
            <div className="flex flex-wrap gap-4">
              <NumField
                label="Wait after buy (h)" hint="give them time to reply first"
                value={trMinWait} min={0} max={8760}
                onChange={(n) => { markDirty(); setTrMinWait(n); }}
              />
              <NumField
                label="Max age (h)" hint="don't chase a purchase older than this"
                value={trMaxAge} min={1} max={8760}
                onChange={(n) => { markDirty(); setTrMaxAge(n); }}
              />
              <NumField
                label="Cooldown (h)" hint="at most one tip-request per fan per this many hours"
                value={trCooldown} min={0} max={8760}
                onChange={(n) => { markDirty(); setTrCooldown(n); }}
              />
            </div>
            <p className="text-[11px] text-fg-dim/70 leading-relaxed">
              Runs as a scheduled sweep — add a <b>tip_request</b> automation rule
              (with a cadence trigger) for it to fire. Skips anyone recently
              contacted (contact guard) and won't re-nudge within the cooldown.
            </p>
          </div>
        )}
      </Section>

      {/* Save + feedback */}
      <div className="flex items-center gap-3 flex-wrap sticky bottom-0 z-20 -mx-5 px-5 py-3 pb-[calc(env(safe-area-inset-bottom)+0.75rem)] bg-panel/95 backdrop-blur border-t border-border rounded-b-2xl md:static md:z-auto md:mx-0 md:px-0 md:py-0 md:pt-1 md:pb-0 md:bg-transparent md:backdrop-blur-none md:border-t-0 md:rounded-b-none md:flex-nowrap">
        <Button onClick={() => saveM.mutate(buildConfig())} disabled={saveM.isPending}>
          {saveM.isPending ? "Saving…" : "Save"}
        </Button>
        {saveM.isSuccess && !saveM.isPending && (
          <span className="text-xs text-emerald-500">Saved ✓</span>
        )}
        {saveM.isError && (
          <span className="text-xs text-red-500">{saveM.error?.message || "Save failed"}</span>
        )}
        {enabled && noFolders && (
          <span className="text-xs text-warn">
            No folders set — nothing will send until you pick a vault folder.
          </span>
        )}
      </div>

      {/* Vault folder picker for the active tier. */}
      {pickerTier !== null && (
        <VaultFolderPicker
          open
          accountId={accountId}
          initialSelected={tiers[pickerTier]?.folders ?? []}
          onClose={() => setPickerTier(null)}
          onConfirm={(folders) => setTier(pickerTier, { folders })}
        />
      )}

      {/* Single-folder picker for the hot-teaser free / paid branch. */}
      {htPicker !== null && (
        <VaultFolderPicker
          open
          accountId={accountId}
          initialSelected={
            htPicker === "free"
              ? htFreeFolder ? [htFreeFolder] : []
              : htPaidFolder ? [htPaidFolder] : []
          }
          onClose={() => setHtPicker(null)}
          onConfirm={(folders) => {
            markDirty();
            const f = folders[0] ?? ""; // one folder per branch — take the first
            if (htPicker === "free") setHtFreeFolder(f);
            else setHtPaidFolder(f);
          }}
        />
      )}

      {/* Single-folder picker for a convo-ladder rung. */}
      {cvPicker !== null && (
        <VaultFolderPicker
          open
          accountId={accountId}
          initialSelected={cvRungs[cvPicker]?.folder ? [cvRungs[cvPicker].folder] : []}
          onClose={() => setCvPicker(null)}
          onConfirm={(folders) => setCvRung(cvPicker, { folder: folders[0] ?? "" })}
        />
      )}
    </Card>
  );
}

/** One vault folder as a removable chip + a "Pick from vault" button. The
 *  hot-teaser branches each hold a SINGLE folder, unlike the multi-folder tiers. */
function FolderRow({
  label, folder, onPick, onClear,
}: {
  label: string;
  folder: string;
  onPick: () => void;
  onClear: () => void;
}) {
  return (
    <div className="flex flex-wrap items-center gap-1.5">
      <span className="text-xs text-fg-dim w-20 shrink-0">{label}</span>
      {folder ? (
        <span className="inline-flex items-center gap-1 text-xs bg-bg-elev-1 border border-border rounded-full px-2 py-0.5">
          {folder}
          <button
            type="button"
            onClick={onClear}
            className="text-fg-dim hover:text-err leading-none"
            title="Remove folder"
          >
            ×
          </button>
        </span>
      ) : (
        <span className="text-[11px] text-fg-dim italic">No folder yet.</span>
      )}
      <Button size="sm" variant="secondary" onClick={onPick}>
        <FolderOpen size={13} /> Pick from vault
      </Button>
    </div>
  );
}

/** A bordered feature section with a title, optional subtitle, an on/off switch
 *  in the header, and a body that the caller only renders when the switch is on. */
function Section({
  icon, title, subtitle, toggle, children,
}: {
  icon: React.ReactNode;
  title: string;
  subtitle?: string;
  toggle: React.ReactNode;
  children?: React.ReactNode;
}) {
  return (
    <section className="rounded-xl border border-border bg-bg-elev-1/40 overflow-hidden">
      <header className="flex items-start gap-3 p-3.5">
        <div className="text-accent mt-0.5 shrink-0">{icon}</div>
        <div className="min-w-0 flex-1">
          <div className="text-sm font-medium">{title}</div>
          {subtitle && (
            <div className="text-[11px] text-fg-dim/80 leading-relaxed mt-0.5">{subtitle}</div>
          )}
        </div>
        <div className="shrink-0 mt-0.5">{toggle}</div>
      </header>
      {children && (
        <div className="border-t border-border p-3.5 space-y-4">{children}</div>
      )}
    </section>
  );
}

/** A pill on/off switch. Lighter-touch than a checkbox for a section header. */
function Toggle({
  checked, onChange,
}: {
  checked: boolean;
  onChange: (v: boolean) => void;
}) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      onClick={() => onChange(!checked)}
      className={`relative inline-flex h-5 w-9 shrink-0 items-center rounded-full transition-colors ${
        checked ? "bg-accent" : "bg-border"
      }`}
      title={checked ? "On" : "Off"}
    >
      <span
        className={`inline-block h-4 w-4 transform rounded-full bg-white shadow transition-transform ${
          checked ? "translate-x-4" : "translate-x-0.5"
        }`}
      />
    </button>
  );
}

function NumField({
  label, hint, value, onChange, min = 0, max = 100000, step = 1, suffix,
}: {
  label: string;
  hint?: string;
  value: number;
  min?: number;
  max?: number;
  step?: number;
  suffix?: string;
  onChange: (n: number) => void;
}) {
  return (
    <label className="space-y-1 block">
      <div className="text-xs text-fg-dim">{label}</div>
      <div className="flex items-center gap-2">
        {suffix === "$" && <span className="text-xs text-fg-dim">$</span>}
        <input
          type="number"
          min={min}
          max={max}
          step={step}
          className={`${INPUT} w-24`}
          value={value}
          onChange={(e) =>
            onChange(Math.max(min, Math.min(Number(e.target.value) || 0, max)))
          }
        />
        {suffix && suffix !== "$" && <span className="text-xs text-fg-dim">{suffix}</span>}
      </div>
      {hint && <div className="text-[11px] text-fg-dim/70">{hint}</div>}
    </label>
  );
}
