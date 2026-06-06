"use client";

/**
 * FunnelEditor — create / edit one mass-message funnel (W8-P1), on top of the
 * funnels CRUD API (service/funnels_api.py). A funnel is GLOBAL: an opener
 * (`opening_message`, sent by send_mass_message) plus an ordered list of steps
 * reply_mass_funnel walks per replying fan. Two step shapes:
 *
 *   REPLY — poll the fan on `check_intervals_min`, then send one of a message
 *           VARIANT pool (first ≤2 used) or generate via `prompt`/`generate`.
 *   PPV   — sales copy + locked vault media at a price (`type:"paid_ppv"`).
 *
 * PPV media MUST be RAW vault ids in `media_files` (reply_mass_funnel does
 * int(m) per id). Client-side validation mirrors the server's _validate_steps.
 *
 * `MassFunnelsTab` is the list + editor toggle mounted under ReadyMadePanel's
 * "Mass funnels" tab; it passes the panel's accountId to the VaultPicker.
 */

import { useEffect, useMemo, useState } from "react";

import { Badge, Button, Card, Input } from "@/components/ui/primitives";
import { cn } from "@/lib/utils";
import { VaultPicker } from "@/components/chat/VaultPicker";
import { FunnelLaunchPanel } from "@/components/automations/FunnelLaunchPanel";
import type { VaultMedia } from "@/lib/relay";
import {
  useCreateFunnel,
  useDeleteFunnel,
  useFunnel,
  useFunnels,
  useUpdateFunnel,
  type FunnelStep,
  type PpvStep,
} from "@/hooks/useFunnels";

// ── Local draft model (easy to edit; converted to/from wire FunnelStep) ──

type StepKind = "reply" | "ppv";

interface DraftStep {
  kind: StepKind;
  /** REPLY: poll cadence as a comma list of minutes (blank → executor default). */
  intervals: string;
  /** Message VARIANT pool (used when `generate` is off). */
  messages: string[];
  /** Generate the reply/copy with the AI instead of static messages. */
  generate: boolean;
  prompt: string;
  // PPV only:
  price: string;
  mediaIds: number[];
  /** How many of the leading picked media are FREE previews (the teaser). */
  previewCount: number;
  lockedText: boolean;
}

function blankStep(kind: StepKind): DraftStep {
  return {
    kind,
    intervals: "",
    messages: [""],
    generate: false,
    prompt: "",
    price: kind === "ppv" ? "0" : "",
    mediaIds: [],
    previewCount: 0,
    lockedText: false,
  };
}

/** A wire step → editor draft (best-effort; unknown keys are dropped on save). */
function toDraft(step: FunnelStep): DraftStep {
  const isPpv = step.type === "paid_ppv";
  const msgs = Array.isArray(step.messages) ? step.messages.map(String) : [];
  const hasStatic = msgs.some((m) => m.trim());
  const generate = !hasStatic && Boolean(step.generate || (step.prompt && step.prompt.trim()));
  const ivRaw = (step as { check_intervals_min?: number[] }).check_intervals_min;
  const intervals = Array.isArray(ivRaw) ? ivRaw.join(", ") : "";
  const ppv = step as PpvStep;
  const mediaIds = isPpv && Array.isArray(ppv.media_files)
    ? ppv.media_files.map((m) => Number(m)).filter((n) => Number.isInteger(n) && n > 0)
    : [];
  const previewCount = isPpv && Array.isArray(ppv.previews)
    ? Math.min(ppv.previews.length, mediaIds.length)
    : 0;
  return {
    kind: isPpv ? "ppv" : "reply",
    intervals,
    messages: msgs.length ? msgs : [""],
    generate,
    prompt: step.prompt ?? "",
    price: isPpv && typeof ppv.price === "number" ? String(ppv.price) : isPpv ? "0" : "",
    mediaIds,
    previewCount,
    lockedText: isPpv ? Boolean(ppv.locked_text) : false,
  };
}

/** Parse a comma/space list of positive whole ints; throws on a bad token. */
function parseIntervals(raw: string): number[] {
  const parts = raw.split(/[\s,]+/).map((s) => s.trim()).filter(Boolean);
  const out: number[] = [];
  for (const p of parts) {
    const n = Number(p);
    if (!Number.isInteger(n) || n <= 0) {
      throw new Error(`interval "${p}" must be a positive whole number of minutes`);
    }
    out.push(n);
  }
  return out;
}

/** Draft → wire step (throws with a clear message on invalid input). */
function toWire(d: DraftStep, idx: number): FunnelStep {
  const label = `Step ${idx + 1}`;
  const msgs = d.messages.map((m) => m.trim()).filter(Boolean);
  const generated = d.generate;
  if (!generated && msgs.length === 0) {
    throw new Error(`${label} needs at least one message (or switch it to AI-generated).`);
  }

  if (d.kind === "ppv") {
    const out: PpvStep = { step: idx + 1, type: "paid_ppv" };
    const price = Number(d.price);
    if (d.price.trim() !== "") {
      if (!Number.isInteger(price) || price < 0) {
        throw new Error(`${label} price must be a whole number ≥ 0.`);
      }
      out.price = price;
    }
    if (d.mediaIds.length) out.media_files = d.mediaIds;
    if (d.mediaIds.length && d.previewCount > 0) {
      out.previews = d.mediaIds.slice(0, Math.min(d.previewCount, d.mediaIds.length));
    }
    if (generated) {
      out.generate = true;
      if (d.prompt.trim()) out.prompt = d.prompt.trim();
    } else {
      out.messages = msgs;
    }
    if (d.lockedText) out.locked_text = true;
    return out;
  }

  // reply step
  const out: ReplyWire = { step: idx + 1 };
  const intervals = parseIntervals(d.intervals);
  if (intervals.length) out.check_intervals_min = intervals;
  if (generated) {
    out.generate = true;
    if (d.prompt.trim()) out.prompt = d.prompt.trim();
  } else {
    out.messages = msgs;
  }
  return out;
}

type ReplyWire = {
  step: number;
  check_intervals_min?: number[];
  messages?: string[];
  prompt?: string;
  generate?: boolean;
};

// ── Single-funnel editor ────────────────────────────────────────────────

export function FunnelEditor({
  funnelId,
  accountId,
  onClose,
  onSaved,
}: {
  funnelId: number | null; // null = create
  accountId: string | null; // for the VaultPicker (PPV media)
  onClose: () => void;
  onSaved?: () => void;
}) {
  const isEdit = funnelId != null;
  const fullQ = useFunnel(funnelId);
  const createM = useCreateFunnel();
  const updateM = useUpdateFunnel();

  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [opening, setOpening] = useState("");
  const [steps, setSteps] = useState<DraftStep[]>([]);
  const [seeded, setSeeded] = useState(!isEdit);
  const [pickerFor, setPickerFor] = useState<number | null>(null);
  const [err, setErr] = useState<string | null>(null);

  // Seed once from the loaded funnel (edit mode). Create mode starts blank.
  useEffect(() => {
    if (seeded || !fullQ.data) return;
    setName(fullQ.data.name);
    setDescription(fullQ.data.description ?? "");
    setOpening(fullQ.data.opening_message);
    setSteps((fullQ.data.steps ?? []).map(toDraft));
    setSeeded(true);
  }, [fullQ.data, seeded]);

  const busy = createM.isPending || updateM.isPending;

  function patchStep(i: number, patch: Partial<DraftStep>) {
    setSteps((prev) => prev.map((s, idx) => (idx === i ? { ...s, ...patch } : s)));
  }
  function addStep(kind: StepKind) {
    setSteps((prev) => [...prev, blankStep(kind)]);
  }
  function removeStep(i: number) {
    setSteps((prev) => prev.filter((_, idx) => idx !== i));
  }
  function moveStep(i: number, dir: -1 | 1) {
    setSteps((prev) => {
      const j = i + dir;
      if (j < 0 || j >= prev.length) return prev;
      const next = prev.slice();
      [next[i], next[j]] = [next[j], next[i]];
      return next;
    });
  }
  // Variant-pool helpers (mirrors PremadeForm's add/remove text-variant UX).
  function patchMsg(i: number, mi: number, val: string) {
    setSteps((prev) => prev.map((s, idx) =>
      idx === i ? { ...s, messages: s.messages.map((m, k) => (k === mi ? val : m)) } : s));
  }
  function addMsg(i: number) {
    setSteps((prev) => prev.map((s, idx) =>
      idx === i ? { ...s, messages: [...s.messages, ""] } : s));
  }
  function removeMsg(i: number, mi: number) {
    setSteps((prev) => prev.map((s, idx) =>
      idx === i
        ? { ...s, messages: s.messages.length <= 1 ? s.messages : s.messages.filter((_, k) => k !== mi) }
        : s));
  }

  async function save() {
    setErr(null);
    if (!name.trim()) return setErr("Name is required.");
    if (!opening.trim()) return setErr("Opening message is required.");
    let wireSteps: FunnelStep[];
    try {
      wireSteps = steps.map((s, i) => toWire(s, i));
    } catch (e) {
      return setErr((e as Error).message);
    }
    try {
      if (isEdit && funnelId != null) {
        await updateM.mutateAsync({
          id: funnelId,
          name: name.trim(),
          description: description.trim() || null,
          opening_message: opening.trim(),
          steps: wireSteps,
        });
      } else {
        await createM.mutateAsync({
          name: name.trim(),
          description: description.trim() || null,
          opening_message: opening.trim(),
          steps: wireSteps,
        });
      }
      onSaved?.();
      onClose();
    } catch (e) {
      setErr((e as Error)?.message || "Save failed");
    }
  }

  if (isEdit && fullQ.isLoading && !seeded) {
    return <Card className="p-4 text-sm text-fg-dim">Loading funnel…</Card>;
  }
  if (isEdit && fullQ.isError) {
    return (
      <Card className="p-4 space-y-2">
        <div className="text-sm text-err">
          {(fullQ.error as Error)?.message || "Failed to load funnel"}
        </div>
        <Button size="sm" variant="ghost" onClick={onClose}>Close</Button>
      </Card>
    );
  }

  return (
    <Card className="p-4 space-y-3 border-accent/40">
      <div className="flex items-center justify-between gap-3">
        <h3 className="text-sm font-semibold text-fg">
          {isEdit ? `Edit funnel · ${name || funnelId}` : "New funnel"}
        </h3>
        <button
          type="button"
          onClick={onClose}
          className="text-fg-dim hover:text-fg text-sm"
          aria-label="Close editor"
        >
          ✕
        </button>
      </div>

      {/* Header: name + description + opener. */}
      <div className="grid gap-3 sm:grid-cols-2">
        <label className="block space-y-1">
          <span className="text-[11px] uppercase tracking-wide text-fg-dim">Name</span>
          <Input value={name} onChange={(e) => setName(e.target.value)} placeholder="e.g. strokes" />
        </label>
        <label className="block space-y-1">
          <span className="text-[11px] uppercase tracking-wide text-fg-dim">Description (optional)</span>
          <Input
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            placeholder="what this funnel is for"
          />
        </label>
      </div>
      <label className="block space-y-1">
        <span className="text-[11px] uppercase tracking-wide text-fg-dim">Opening message</span>
        <textarea
          value={opening}
          onChange={(e) => setOpening(e.target.value)}
          rows={3}
          placeholder="The first message sent to the audience (the funnel opener)…"
          className="w-full bg-bg border border-border rounded-md px-3 py-2 text-sm placeholder:text-muted focus:outline-none focus:border-accent resize-y"
        />
      </label>

      {/* Ordered step list. */}
      <div className="space-y-2">
        <div className="text-[11px] uppercase tracking-wide text-fg-dim">
          Steps <span className="opacity-60">({steps.length})</span>
        </div>
        {steps.length === 0 && (
          <div className="text-[11px] text-fg-dim italic">
            No follow-up steps yet — the opener sends and the funnel ends. Add a
            reply step to chase a response, or a PPV step to sell.
          </div>
        )}
        {steps.map((s, i) => (
          <StepCard
            key={i}
            index={i}
            total={steps.length}
            step={s}
            onPatch={(patch) => patchStep(i, patch)}
            onRemove={() => removeStep(i)}
            onMove={(dir) => moveStep(i, dir)}
            onMsgChange={(mi, v) => patchMsg(i, mi, v)}
            onMsgAdd={() => addMsg(i)}
            onMsgRemove={(mi) => removeMsg(i, mi)}
            onPickMedia={() => setPickerFor(i)}
          />
        ))}

        <div className="flex items-center gap-2">
          <Button size="sm" variant="secondary" onClick={() => addStep("reply")}>
            + Reply step
          </Button>
          <Button size="sm" variant="secondary" onClick={() => addStep("ppv")}>
            + PPV step
          </Button>
        </div>
      </div>

      {err && <div className="text-xs text-err whitespace-pre-wrap">{err}</div>}

      <div className="flex items-center gap-2">
        <Button size="sm" variant="primary" onClick={save} disabled={busy}>
          {busy ? "Saving…" : isEdit ? "Save changes" : "Create funnel"}
        </Button>
        <Button size="sm" variant="ghost" onClick={onClose} disabled={busy}>
          Cancel
        </Button>
      </div>

      <VaultPicker
        open={pickerFor !== null}
        onClose={() => setPickerFor(null)}
        accountId={accountId}
        fanId={null}
        initialSelectedIds={pickerFor !== null ? steps[pickerFor]?.mediaIds ?? [] : []}
        onConfirm={(picked: VaultMedia[]) => {
          if (pickerFor !== null) {
            const ids = picked
              .map((m) => m.id)
              .filter((id): id is number => typeof id === "number" && id > 0);
            // Clamp the free-preview count to the new media length.
            const prevCount = Math.min(steps[pickerFor]?.previewCount ?? 0, ids.length);
            patchStep(pickerFor, { mediaIds: ids, previewCount: prevCount });
          }
          setPickerFor(null);
        }}
      />
    </Card>
  );
}

/** One step card: reply / PPV toggle + the per-kind fields. */
function StepCard({
  index,
  total,
  step,
  onPatch,
  onRemove,
  onMove,
  onMsgChange,
  onMsgAdd,
  onMsgRemove,
  onPickMedia,
}: {
  index: number;
  total: number;
  step: DraftStep;
  onPatch: (patch: Partial<DraftStep>) => void;
  onRemove: () => void;
  onMove: (dir: -1 | 1) => void;
  onMsgChange: (mi: number, val: string) => void;
  onMsgAdd: () => void;
  onMsgRemove: (mi: number) => void;
  onPickMedia: () => void;
}) {
  const isPpv = step.kind === "ppv";
  return (
    <div className="border border-border rounded-md p-3 space-y-2.5">
      <div className="flex items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          <span className="text-[11px] uppercase tracking-wide text-fg-dim">Step {index + 1}</span>
          <Badge color={isPpv ? "warn" : "muted"}>{isPpv ? "paid PPV" : "reply"}</Badge>
        </div>
        <div className="flex items-center gap-1">
          <button
            type="button"
            onClick={() => onMove(-1)}
            disabled={index === 0}
            className="text-fg-dim hover:text-fg disabled:opacity-30 text-xs px-1"
            title="Move up"
          >↑</button>
          <button
            type="button"
            onClick={() => onMove(1)}
            disabled={index === total - 1}
            className="text-fg-dim hover:text-fg disabled:opacity-30 text-xs px-1"
            title="Move down"
          >↓</button>
          <button
            type="button"
            onClick={onRemove}
            className="text-[11px] text-err hover:underline ml-1"
          >remove</button>
        </div>
      </div>

      {/* Reply / PPV toggle. */}
      <div className="flex items-center gap-1.5">
        <KindChip active={!isPpv} onClick={() => onPatch({ kind: "reply" })} label="Reply" />
        <KindChip active={isPpv} onClick={() => onPatch({ kind: "ppv" })} label="Paid PPV" />
      </div>

      {/* REPLY: poll intervals. */}
      {!isPpv && (
        <label className="block space-y-1">
          <span className="text-[11px] text-fg-dim">
            Check for a reply every <span className="opacity-60">(minutes, comma-separated; blank = default 2, 4, 10)</span>
          </span>
          <Input
            value={step.intervals}
            onChange={(e) => onPatch({ intervals: e.target.value })}
            placeholder="2, 4, 10"
          />
        </label>
      )}

      {/* PPV: price + media + locked-text. */}
      {isPpv && (
        <div className="space-y-2">
          <div className="grid grid-cols-2 gap-2">
            <label className="block space-y-1">
              <span className="text-[11px] text-fg-dim">Price (USD, whole)</span>
              <Input
                type="number"
                min={0}
                value={step.price}
                onChange={(e) => onPatch({ price: e.target.value })}
                placeholder="0"
              />
            </label>
            <div className="flex flex-col gap-1">
              <span className="text-[11px] text-fg-dim">Media (vault)</span>
              <div className="flex items-center gap-2">
                <Button size="sm" variant="secondary" onClick={onPickMedia}>
                  {step.mediaIds.length ? `🖼 ${step.mediaIds.length} picked` : "🖼 Pick media"}
                </Button>
                {step.mediaIds.length > 0 && (
                  <button
                    type="button"
                    onClick={() => onPatch({ mediaIds: [], previewCount: 0 })}
                    className="text-[11px] text-fg-dim hover:text-err"
                  >clear</button>
                )}
              </div>
            </div>
          </div>
          {/* Free preview: the leading N picked media go out UNLOCKED as the
              teaser; the rest stay paywalled (OF `previews`). */}
          {step.mediaIds.length > 0 && (
            <label className="block space-y-1">
              <span className="text-[11px] text-fg-dim">
                Free preview — first
                {" "}
                <Input
                  type="number"
                  min={0}
                  max={step.mediaIds.length}
                  value={String(step.previewCount)}
                  onChange={(e) => {
                    const n = Math.max(0, Math.min(Number(e.target.value) || 0, step.mediaIds.length));
                    onPatch({ previewCount: n });
                  }}
                  className="inline-block w-16 mx-1 align-middle"
                />
                {" "}of {step.mediaIds.length} shown free as a teaser
              </span>
              <span className="block text-[11px] text-fg-dim">
                {step.previewCount > 0
                  ? `${step.previewCount} preview image${step.previewCount === 1 ? "" : "s"} sent unlocked; the rest are paywalled.`
                  : "0 = nothing shown free (fully locked PPV)."}
              </span>
            </label>
          )}
          <label className="flex items-center gap-2 text-xs cursor-pointer select-none">
            <input
              type="checkbox"
              checked={step.lockedText}
              onChange={(e) => onPatch({ lockedText: e.target.checked })}
            />
            <span>Lock the text too (fan must pay to read the copy)</span>
          </label>
        </div>
      )}

      {/* Copy: AI-generate toggle, then either a prompt or a variant pool. */}
      <label className="flex items-center gap-2 text-xs cursor-pointer select-none">
        <input
          type="checkbox"
          checked={step.generate}
          onChange={(e) => onPatch({ generate: e.target.checked })}
        />
        <span>Generate the {isPpv ? "sales copy" : "reply"} with AI</span>
      </label>

      {step.generate ? (
        <label className="block space-y-1">
          <span className="text-[11px] text-fg-dim">
            Prompt <span className="opacity-60">(optional — guides the AI; blank uses the default persona)</span>
          </span>
          <textarea
            value={step.prompt}
            onChange={(e) => onPatch({ prompt: e.target.value })}
            rows={2}
            placeholder="e.g. tease the PPV and create urgency…"
            className="w-full bg-bg border border-border rounded-md px-3 py-2 text-sm placeholder:text-muted focus:outline-none focus:border-accent resize-y"
          />
        </label>
      ) : (
        <div className="space-y-1.5">
          <span className="text-[11px] text-fg-dim">
            {isPpv ? "Sales copy" : "Reply"} variants <span className="opacity-60">(one is sent at random; first ≤2 used)</span>
          </span>
          {step.messages.map((m, mi) => (
            <div key={mi} className="flex items-start gap-1.5">
              <textarea
                value={m}
                onChange={(e) => onMsgChange(mi, e.target.value)}
                rows={2}
                placeholder={step.messages.length > 1 ? `Variant ${mi + 1}` : "Message…"}
                className="flex-1 bg-bg border border-border rounded-md px-3 py-2 text-sm placeholder:text-muted focus:outline-none focus:border-accent resize-y"
              />
              {step.messages.length > 1 && (
                <button
                  type="button"
                  onClick={() => onMsgRemove(mi)}
                  title="Remove this variant"
                  className="text-fg-dim hover:text-err text-sm leading-none mt-2"
                >×</button>
              )}
            </div>
          ))}
          <button
            type="button"
            onClick={onMsgAdd}
            className="text-[11px] text-accent hover:underline"
          >
            + add variant{step.messages.length > 1 ? ` (${step.messages.length})` : ""}
          </button>
        </div>
      )}
    </div>
  );
}

function KindChip({ active, onClick, label }: { active: boolean; onClick: () => void; label: string }) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={cn(
        "px-2.5 py-1 rounded-full text-[11px] border transition-colors",
        active
          ? "bg-accent text-white border-accent"
          : "bg-bg-elev-1 text-fg-dim border-border hover:text-fg hover:border-fg-dim",
      )}
    >
      {label}
    </button>
  );
}

// ── Mass-funnels tab: list + create/edit toggle (mounted in ReadyMadePanel) ──

export function MassFunnelsTab({ accountId }: { accountId: string | null }) {
  const listQ = useFunnels();
  const deleteM = useDeleteFunnel();
  const [editing, setEditing] = useState<number | null>(null);
  const [adding, setAdding] = useState(false);
  const [launching, setLaunching] = useState<number | null>(null);
  const [rowErr, setRowErr] = useState<{ id: number; msg: string } | null>(null);

  const funnels = listQ.data ?? [];

  function close() {
    setEditing(null);
    setAdding(false);
  }

  async function remove(id: number, name: string) {
    setRowErr(null);
    if (!window.confirm(`Delete funnel "${name}"?`)) return;
    try {
      await deleteM.mutateAsync(id);
      if (editing === id) close();
    } catch (e) {
      setRowErr({ id, msg: (e as Error)?.message || "Delete failed" });
    }
  }

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between gap-3">
        <p className="text-xs text-fg-dim">
          Funnels are global (shared across all your models). Reference one from a
          mass send to auto-walk the reply / PPV steps per replying fan.
        </p>
        <Button
          size="sm"
          variant="primary"
          onClick={() => { setEditing(null); setAdding(true); }}
        >
          + New funnel
        </Button>
      </div>

      {(adding || editing != null) && (
        <FunnelEditor
          key={editing ?? "new"}
          funnelId={editing}
          accountId={accountId}
          onClose={close}
        />
      )}

      {listQ.isLoading && <div className="text-sm text-fg-dim">Loading…</div>}
      {listQ.isError && (
        <div className="text-sm text-err">
          {(listQ.error as Error)?.message || "Failed to load funnels"}
        </div>
      )}

      {!listQ.isLoading && !listQ.isError && (
        funnels.length === 0 ? (
          <div className="text-sm text-fg-dim py-2">
            No funnels yet. Click <span className="text-fg">+ New funnel</span> to create one.
          </div>
        ) : (
          <ul className="divide-y divide-border">
            {funnels.map((f) => (
              <li key={f.id} className="py-2.5 space-y-1">
                <div className="flex flex-wrap items-center gap-3">
                  <div className="min-w-0 flex-1">
                    <div className="flex items-center gap-2 min-w-0">
                      <span className="text-sm font-medium text-fg truncate">{f.name}</span>
                      <Badge color="muted">{f.step_count} step{f.step_count === 1 ? "" : "s"}</Badge>
                    </div>
                    <div className="text-[11px] text-fg-dim truncate">
                      {f.description || f.opening_message}
                    </div>
                  </div>
                  <div className="flex items-center gap-1.5 shrink-0">
                    <Button
                      size="sm"
                      variant="primary"
                      onClick={() => { setEditing(null); setAdding(false); setLaunching(launching === f.id ? null : f.id); }}
                    >
                      ▶ Start
                    </Button>
                    <Button
                      size="sm"
                      variant="ghost"
                      onClick={() => { setAdding(false); setLaunching(null); setEditing(f.id); }}
                    >
                      Edit
                    </Button>
                    <Button
                      size="sm"
                      variant="danger"
                      onClick={() => remove(f.id, f.name)}
                      disabled={deleteM.isPending}
                    >
                      Delete
                    </Button>
                  </div>
                </div>
                {launching === f.id && (
                  <FunnelLaunchPanel
                    funnel={f}
                    accountId={accountId}
                    onClose={() => setLaunching(null)}
                  />
                )}
                {rowErr?.id === f.id && (
                  <div className="text-[11px] text-err">{rowErr.msg}</div>
                )}
              </li>
            ))}
          </ul>
        )
      )}
    </div>
  );
}
