"use client";

/**
 * useKeyDraft — the edit state both Setup → Keys cards share.
 *
 * Identical in the house-keys card and the agency-keys card, and identical for
 * the same reason: a secret field can only be edited safely one way. The stored
 * value is never loaded into the input (it is a placeholder), a blank field
 * means "leave unchanged", and the save sends ONLY the fields that were typed
 * in — so saving one key can never overwrite another with its own mask.
 *
 * "Blank means unchanged" has to hold for a field the user typed into and then
 * emptied, not just one they never touched — otherwise pasting a wrong key and
 * backspacing it out DELETES the stored one. So `save` submits only fields that
 * are non-blank AFTER TRIMMING, and `dirty` counts only those too; `clear(name)`
 * is the single path that sends "". The trim is load-bearing, not tidiness: both
 * servers decide with `.strip()`, so a field left holding one space would sail
 * through a `!== ""` test and destroy the key exactly as an empty one did.
 *
 * On the agency card that matters more than it looks: a deleted key fails every
 * one of that owner's accounts closed, while the operator reads "Saved" and
 * believes nothing changed.
 *
 * The cards render different things (the house card has groups, textareas and
 * an env-vs-UI source badge; the agency card is a flat list of providers), so
 * only the behaviour is shared here. The markup stays where it belongs.
 */

import { useEffect, useRef, useState } from "react";

export interface KeyDraft {
  /** Every field the user has touched, INCLUDING ones they emptied again — the
   *  input needs those to render as empty. What gets submitted is the trimmed
   *  non-blank subset; see `save`. */
  draft: Record<string, string>;
  dirty: boolean;
  setField: (name: string, value: string) => void;
  /** Save the typed fields, then clear the draft. */
  save: () => Promise<void>;
  /** Clear one stored key (sends "" for that field alone). */
  clear: (name: string) => Promise<void>;
  /** Transient status line — success fades, failure sticks. */
  note: string | null;
}

export function useKeyDraft(
  submit: (values: Record<string, string>) => Promise<unknown>,
  savedNote: string,
): KeyDraft {
  const [draft, setDraft] = useState<Record<string, string>>({});
  const [note, setNote] = useState<string | null>(null);

  // The fields that carry an actual value — what "typed in" means, and the one
  // set both `save` and `dirty` are allowed to see. Trimmed, because that is how
  // the server decides too.
  const typed = Object.fromEntries(
    Object.entries(draft).filter(([, v]) => v.trim() !== ""),
  );

  // One pending "saved" timer, cancelled before it can wipe a later failure —
  // the note contract is that success fades and failure sticks.
  const noteTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  useEffect(() => () => {
    if (noteTimer.current) clearTimeout(noteTimer.current);
  }, []);

  function flashNote(msg: string) {
    if (noteTimer.current) clearTimeout(noteTimer.current);
    setNote(msg);
    noteTimer.current = setTimeout(() => setNote(null), 3000);
  }

  function stickNote(msg: string) {
    if (noteTimer.current) clearTimeout(noteTimer.current);
    setNote(msg);
  }

  function setField(name: string, value: string) {
    setDraft((d) => ({ ...d, [name]: value }));
  }

  function drop(name: string) {
    setDraft((d) => {
      const { [name]: _drop, ...rest } = d;
      return rest;
    });
  }

  async function save() {
    setNote(null);
    const sending = typed;
    const names = Object.keys(sending);
    if (!names.length) return;
    try {
      await submit(sending);
      // Drop ONLY what was sent. Anything typed while the request was in flight
      // is a real unsaved edit; clearing the whole draft would swallow it and
      // still report success.
      setDraft((d) => {
        const rest = { ...d };
        for (const n of names) if (rest[n] === sending[n]) delete rest[n];
        return rest;
      });
      flashNote(savedNote);
    } catch (e) {
      stickNote(e instanceof Error ? e.message : "Save failed");
    }
  }

  async function clear(name: string) {
    setNote(null);
    try {
      await submit({ [name]: "" });
      drop(name);
      // Removing a credential is destructive and otherwise silent — the row
      // just re-renders as "not set", which looks the same as a page that
      // never had one.
      flashNote("Cleared.");
    } catch (e) {
      stickNote(e instanceof Error ? e.message : "Clear failed");
    }
  }

  return { draft, dirty: Object.keys(typed).length > 0, setField, save, clear, note };
}
