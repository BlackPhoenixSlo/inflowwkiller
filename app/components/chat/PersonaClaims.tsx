"use client";

/**
 * PersonaClaims — "what the creator told him", in the fan drawer.
 *
 * The chat engines record every self-claim the creator makes to a fan and paste the
 * merged result into the prompt as "you already told him this — never give him a
 * different version". A pre-send check then verifies the draft against it. All of
 * that ran with no operator surface at all: a wrong claim could not be seen, let
 * alone corrected.
 *
 * WHAT THIS SHOWS is the merged block itself — every topic claimed so far, in
 * the prompt's own order and wording. The server builds it (`_persona
 * .resolved_claims`, the same function the prompt block renders), so this panel
 * displays what the AI is actually told instead of a second opinion about it.
 * Rebuilding the override rule here is the same two-implementations-of-one-rule
 * hazard that once had the browser and the server disagreeing about which media
 * had a readable still.
 *
 * The first version of this panel led with three input boxes — age, location,
 * job. That was backwards: those three are the only topics with a PATCH-editable
 * mirror column, which is a storage detail, not a limit on what can be claimed.
 * Accounts do tell fans their nationality, living situation, pets —
 * all of which reach the prompt and none of which had a box. So the list leads,
 * and a row is editable when it happens to have a column behind it.
 *
 * Its own file because FanDrawer is already 1.7k lines.
 */

import { useEffect, useState } from "react";

import { useAccountConfig } from "@/hooks/useAccountConfig";
import type { FanRecord, PersonaClaim, ResolvedClaim } from "@/lib/relay";

type ClaimColumn = NonNullable<ResolvedClaim["column"]>;

/** One pinned canon fact, as it reaches every prompt on this account. */
export interface CanonFact { key: string; label: string; value: string }

/** The canon half: the account's pinned facts, filled ones only, in the field
 *  contract's own order (which is "how often fans actually ask").
 *
 *  Filled-only because a blank slot is not something the creator has been told to
 *  say — it is a gap the model improvises into, and improvised answers are what
 *  shows up in the per-fan ledger below. Listing 26 empty rows on every fan would
 *  bury the three that matter. */
export function canonFacts(
  facts: Record<string, string> | undefined,
  fields: { key: string; label: string }[] | undefined,
): CanonFact[] {
  const f = facts ?? {};
  return (fields ?? [])
    .filter((fld) => (f[fld.key] ?? "").trim() !== "")
    .map((fld) => ({ key: fld.key, label: fld.label, value: f[fld.key] }));
}

/** `at` is an ISO stamp from the server; show the date only — the hour of a
 *  months-old claim is noise, and a bad value must not render "Invalid Date". */
export function claimDate(at: string | null | undefined): string {
  if (!at) return "";
  const d = new Date(at);
  return Number.isNaN(d.getTime()) ? "" : d.toLocaleDateString();
}

/** Newest last is how it is stored and how it reads as a conversation, so the
 *  list is NOT reversed. Entries with no claim text are dropped — an empty row
 *  tells the operator nothing and only makes the real ones harder to scan. */
export function visibleClaims(claims: PersonaClaim[] | undefined): PersonaClaim[] {
  return (claims ?? []).filter((c) => (c?.claim ?? "").trim() !== "");
}

/** Rows worth rendering: the server already merged and ordered them; this only
 *  drops anything with no value, which the prompt would print as a bare label. */
export function visibleResolved(rows: ResolvedClaim[] | undefined): ResolvedClaim[] {
  return (rows ?? []).filter((r) => (r?.value ?? "").trim() !== "");
}

/** History worth showing UNDER the merged list: the claims that are no longer
 *  the current answer. A row whose exact text is already on screen would just be
 *  the same sentence twice. */
export function supersededClaims(
  claims: PersonaClaim[] | undefined, resolved: ResolvedClaim[] | undefined,
): PersonaClaim[] {
  const current = new Set(visibleResolved(resolved).map((r) => r.value.trim()));
  return visibleClaims(claims).filter((c) => !current.has((c.claim ?? "").trim()));
}

export function PersonaClaims({
  accountId, fan, onSave,
}: {
  accountId: string;
  fan: FanRecord;
  onSave: (patch: Partial<Record<ClaimColumn, string | null>>) => void;
}) {
  const rows = visibleResolved(fan.persona_claims_resolved);
  const older = supersededClaims(fan.persona_claims, fan.persona_claims_resolved);

  // Shares the ["account-config", id] cache with the header's ℹ︎ popover, so on a
  // model whose info has been opened once this costs nothing.
  const cfgQ = useAccountConfig(accountId);
  const canon = canonFacts(cfgQ.data?.config?.persona_facts,
                           cfgQ.data?.persona_fact_fields);

  return (
    <div className="space-y-3 border-t border-border pt-3">
      {/* STEP 1 — the canon. Identical for every fan on this account, so it is
       *  collapsed: it is context for reading the ledger, not per-fan news. */}
      <details className="group">
        <summary className="flex cursor-pointer items-baseline justify-between gap-2 list-none">
          <span className="text-[11px] font-medium uppercase tracking-wide text-fg-dim
                           group-hover:text-fg">
            Creator facts · in every prompt
          </span>
          <span className="text-[10px] text-fg-dim tabular-nums">
            {canon.length || (cfgQ.isLoading ? "…" : "none set")}
          </span>
        </summary>
        {canon.length === 0 ? (
          <p className="mt-1.5 text-[11px] text-fg-dim">
            No facts pinned for this model, so every answer about the creator is
            improvised — and each one lands in the list below instead. Fill them
            once under Automations → the brain (🪄 Enrich proposes them).
          </p>
        ) : (
          <ul className="mt-1.5 space-y-1">
            {canon.map((c) => (
              <li key={c.key} className="text-[11px] leading-snug">
                <span className="text-fg-dim">{c.label}: </span>
                <span className="text-fg">{c.value}</span>
              </li>
            ))}
          </ul>
        )}
      </details>

      {/* STEP 2 — what was improvised for THIS fan. */}
      <div className="space-y-2">
      <div className="flex items-baseline justify-between gap-2">
        <span className="text-[11px] font-medium uppercase tracking-wide text-fg-dim">
          What this fan was told
        </span>
        {rows.length > 0 && (
          <span className="text-[10px] text-fg-dim tabular-nums">{rows.length}</span>
        )}
      </div>

      {rows.length === 0 ? (
        <p className="text-[11px] text-fg-dim">
          This fan hasn&apos;t been told anything about the creator yet. Whatever gets
          said is pinned here, so he can&apos;t be given a different version later.
        </p>
      ) : (
        <ul className="space-y-1.5">
          {rows.map((r) => (
            <ClaimRow
              key={`${r.label}-${r.at ?? ""}`}
              row={r}
              onCommit={(next) => r.column && onSave({ [r.column]: next || null })}
            />
          ))}
        </ul>
      )}

      {older.length > 0 && (
        <details className="pt-0.5">
          <summary className="cursor-pointer text-[11px] text-fg-dim hover:text-fg">
            Earlier versions given ({older.length})
          </summary>
          <ul className="mt-1.5 space-y-1">
            {older.map((c, i) => (
              <li key={`${c.topic ?? "claim"}-${c.at ?? i}`}
                  className="text-[11px] leading-snug text-fg-dim line-through decoration-fg-dim/40">
                {c.topic ?? "about the creator"}: {c.claim}
                {claimDate(c.at) && <span className="ml-1 tabular-nums">· {claimDate(c.at)}</span>}
              </li>
            ))}
          </ul>
        </details>
      )}

      {rows.length > 0 && (
        <p className="hidden md:block text-[10px] text-fg-dim">
          This is exactly what the prompt is given. Rows with a pencil can be
          corrected — the rest are recorded as they were said.
        </p>
      )}
      </div>
    </div>
  );
}

/** One merged row. Read-only unless the topic has a mirror column behind it, in
 *  which case clicking the pencil turns it into an input that commits on blur —
 *  saving per keystroke would PATCH the fan once per character. */
function ClaimRow({
  row, onCommit,
}: {
  row: ResolvedClaim;
  onCommit: (next: string) => void;
}) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(row.value);

  // Re-sync when the row changes underneath us — switching fans reuses this
  // component, and a stale draft would show the previous fan's claim.
  useEffect(() => { setDraft(row.value); setEditing(false); }, [row.value]);

  if (editing) {
    return (
      <li>
        <span className="block text-[10px] uppercase tracking-wide text-fg-dim">{row.label}</span>
        <input
          autoFocus
          type="text"
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onBlur={() => {
            setEditing(false);
            const next = draft.trim();
            if (next !== row.value.trim()) onCommit(next);
          }}
          onKeyDown={(e) => {
            if (e.key === "Enter") e.currentTarget.blur();
            if (e.key === "Escape") { setDraft(row.value); setEditing(false); }
          }}
          className="w-full bg-bg border border-border rounded-md px-2 py-1 text-base md:text-sm focus:outline-none focus:border-accent"
        />
      </li>
    );
  }

  return (
    <li className="flex items-baseline gap-1.5 text-[11px] leading-snug">
      <span className="text-fg-dim shrink-0">{row.label}:</span>
      <span className="text-fg break-words min-w-0">{row.value}</span>
      {claimDate(row.at) && (
        <span className="text-fg-dim tabular-nums shrink-0">· {claimDate(row.at)}</span>
      )}
      {row.column && (
        <button
          type="button"
          onClick={() => setEditing(true)}
          title={`Correct what this fan was told — this is what the prompt reads`}
          aria-label={`Correct ${row.label}`}
          className="ml-auto shrink-0 text-fg-dim hover:text-accent focus:outline-none focus:text-accent"
        >
          ✎
        </button>
      )}
    </li>
  );
}
