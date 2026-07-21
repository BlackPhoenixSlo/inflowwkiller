"use client";

/**
 * LinesPicker — composer button that surfaces a fan's saved questions + teases
 * (gen_info's Q1-3 / Tease1-3). Clicking a line INSERTS it into the composer (the
 * chatter can tweak before sending); the composer consumes (deletes) that line from
 * the DB once the message is actually sent, so it's never offered twice.
 *
 * Opens UPWARD (the composer sits at the bottom of the chat). Closes on outside
 * click. Fetches only while open.
 */

import { useEffect, useRef, useState } from "react";

import { useFanLines, type FanLine } from "@/hooks/useFanLines";

export interface LinesPickerProps {
  accountId: string | null;
  fanId: number | null;
  /** Insert this line's text into the composer + arm its slot for consume-on-send. */
  onPick: (line: FanLine) => void;
}

/** A stable fingerprint of the current batch — lets the poller tell when a freshly
 *  generated batch has actually landed (text differs from the pre-generate snapshot). */
function batchSig(d?: { questions: FanLine[]; teases: FanLine[] }): string {
  if (!d) return "";
  return [...d.questions, ...d.teases].map((l) => `${l.slot}:${l.text}`).join("|");
}

export function LinesPicker({ accountId, fanId, onPick }: LinesPickerProps) {
  const [open, setOpen] = useState(false);
  const [generating, setGenerating] = useState(false);
  const rootRef = useRef<HTMLDivElement | null>(null);
  // The batch fingerprint captured the moment "Generate" was clicked, so the
  // poller knows what "new lines arrived" looks like.
  const preGenSigRef = useRef<string>("");
  const q = useFanLines(accountId, fanId, open);

  useEffect(() => {
    if (!open) return;
    const onClick = (e: MouseEvent) => {
      if (rootRef.current?.contains(e.target as Node)) return;
      setOpen(false);
    };
    document.addEventListener("pointerdown", onClick);
    return () => document.removeEventListener("pointerdown", onClick);
  }, [open]);

  // While a batch is generating, refetch /lines every 4s. The job runs on the
  // executor's ~30s tick + an LLM call, so give the spinner a generous ~120s cap
  // before giving up (the new batch shows up on the next manual open regardless).
  useEffect(() => {
    if (!generating) return;
    let elapsed = 0;
    const id = setInterval(() => {
      elapsed += 4000;
      void q.refetch();
      if (elapsed >= 120_000) setGenerating(false);
    }, 4000);
    return () => clearInterval(id);
  }, [generating, q]);

  // Stop the spinner once the batch fingerprint changes from the pre-generate
  // snapshot (force_ids replaces the lines, so a non-trivial regen always differs).
  useEffect(() => {
    if (!generating) return;
    const cur = batchSig(q.data);
    if (cur && cur !== preGenSigRef.current) setGenerating(false);
  }, [generating, q.data]);

  async function onGenerate() {
    preGenSigRef.current = batchSig(q.data);
    setGenerating(true);
    try {
      await q.generate();
    } catch {
      setGenerating(false); // enqueue failed — drop the spinner so it's retryable
    }
  }

  const questions = q.data?.questions ?? [];
  const teases = q.data?.teases ?? [];
  const empty = !q.isLoading && questions.length === 0 && teases.length === 0;

  const item = (l: FanLine) => (
    <button
      key={l.slot}
      type="button"
      onClick={() => {
        onPick(l);
        setOpen(false);
      }}
      className="block w-full text-left px-2 py-1.5 rounded hover:bg-bg-elev-1 whitespace-pre-wrap leading-snug"
    >
      {l.text}
    </button>
  );

  return (
    <div ref={rootRef} className="relative">
      <button
        type="button"
        disabled={!fanId}
        onClick={() => setOpen((o) => !o)}
        title="Saved questions + teases for this fan"
        className="px-2 py-1 rounded-md text-xs border border-border text-fg-dim hover:text-fg hover:border-accent disabled:opacity-40 disabled:cursor-not-allowed"
      >
        Lines
      </button>
      {open && (
        <div className="absolute bottom-full mb-2 left-0 w-80 max-h-80 overflow-y-auto bg-panel border border-border rounded-lg shadow-2xl p-2 z-50 text-xs">
          {q.isLoading && <div className="text-fg-dim p-2">Loading…</div>}
          {empty && (
            <div className="text-fg-dim p-2">
              No saved lines yet — hit “Generate new batch” below.
            </div>
          )}
          {questions.length > 0 && (
            <div className="text-fg-dim uppercase tracking-wide text-[10px] px-1 pt-1 pb-0.5">
              Questions
            </div>
          )}
          {questions.map(item)}
          {teases.length > 0 && (
            <div className="text-fg-dim uppercase tracking-wide text-[10px] px-1 pt-2 pb-0.5">
              Teases
            </div>
          )}
          {teases.map(item)}
          {/* Generate a fresh batch on demand (force_ids gen_info). Lines land
              async on the executor tick, so we poll + show a generating state. */}
          {!q.isLoading && (
            <div className="border-t border-border mt-1 pt-1">
              <button
                type="button"
                disabled={generating}
                onClick={onGenerate}
                className="block w-full text-left px-2 py-1.5 rounded text-accent hover:bg-bg-elev-1 disabled:opacity-60 disabled:cursor-default"
              >
                {generating ? "Generating new batch…" : "↻ Generate new batch"}
              </button>
              {generating && (
                <div className="text-fg-dim px-2 pb-1 text-[10px] leading-snug">
                  Mining the latest messages — new lines appear here shortly.
                </div>
              )}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
