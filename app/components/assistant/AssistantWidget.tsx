"use client";

/**
 * AssistantWidget — the in-product help bot's floating "?" bubble.
 *
 * Staff ask "how do I make autopost?" and get back WHERE in the UI it lives,
 * WHAT to set, and an honest "not supported" when that is the truth. The
 * thinking is entirely server-side (service/assistant_api.py, closed-book over
 * assistant_manual.md); this file is a composer, a list and one awaited POST.
 * Design record: plans/help-chatbot/README.md.
 *
 * TWO components, mirroring the MoneyRail dock precedent. `AssistantWidget` is
 * the gate: it owns the corner, the persisted open/closed bit and — unlike the
 * rail's gate — the conversation, because collapsing UNMOUNTS the panel and a
 * session that forgot the last three answers the moment you got them out of
 * the way is worse than no memory at all. `AssistantPanel` is presentation
 * plus the draft textarea and nothing else.
 *
 * BOTTOM-LEFT, not right: MoneyRail's default dock is `bottom-3 right-3` and
 * it is draggable, so its height is not something this widget can plan around.
 *
 * ERRORS ARE ANSWERS. The endpoint is a 200-always contract — a used-up cap, a
 * missing agency key and a sulking provider all come back as `{answer, error}`
 * with a human sentence in `answer` — so every one of them renders as a normal
 * assistant bubble (muted when `error` is set). The bot looking broken exactly
 * when someone needs config help is the failure mode the whole endpoint exists
 * to avoid, and a red toast would reintroduce it in the last 40 lines.
 *
 * v1 is deliberately thin: no streaming, no persistence across refresh, no
 * feedback buttons, and each question is sent SINGLE-SHOT — the server gets
 * this question and the role it derives from the session, never the transcript.
 *
 * ALL-MODELS SCOPE: the product's default scope is "all models", which used to
 * replace the composer with "pick an account first" — a help bot dead in the
 * app's default state. The server still requires exactly one account_id (it
 * bills the answer to that account's cap and scopes every live-state read to
 * it), so the fix lives HERE: in all-models scope the composer unlocks and
 * carries an inline "Answering about" picker. A human picks the account —
 * visibly — and the request goes out exactly as it does under a scoped model.
 * The server is never handed a null account, so billing scope and answer scope
 * stay the same thing.
 */

import { useCallback, useEffect, useRef, useState } from "react";

import { Button, Textarea } from "@/components/ui/primitives";
import { useEmployee } from "@/contexts/EmployeeContext";
import { useScope } from "@/contexts/ScopeContext";
import { useActiveAccounts } from "@/hooks/useAccounts";
import { RelayError, relay, type AccountMeta } from "@/lib/relay";
import { cn } from "@/lib/utils";
import { AnswerText } from "./AnswerText";

/** Open/closed survives a reload; the conversation does not. */
const OPEN_KEY = "chatterly:assistant_open";

/** Mirrors `MAX_QUESTION_CHARS` in service/assistant_api.py — the server's
 *  pydantic `max_length` would 422 past it, which is the one failure the
 *  200-always contract does NOT cover. Stop it in the composer instead. */
const MAX_QUESTION_CHARS = 1000;

/** The endpoint's response contract. `error` is a machine tag for the log
 *  (cap | config | provider | empty_answer | empty_question); `answer` is
 *  always a human sentence, including on every one of those. */
interface AskResponse {
  answer: string;
  error: string | null;
}

export interface Turn {
  id: number;
  question: string;
  /** null while the POST is in flight — that's the "thinking…" row. */
  answer: string | null;
  error: string | null;
}

// ── The gate ───────────────────────────────────────────────────────

export function AssistantWidget() {
  // The server doesn't know the persisted bit, so the first client render
  // must not either — read it in an effect and paint nothing until then.
  const [hydrated, setHydrated] = useState(false);
  const [open, setOpen] = useState(false);
  const [turns, setTurns] = useState<Turn[]>([]);
  const nextId = useRef(0);

  const { accountId: scopedId } = useScope();
  const accounts = useActiveAccounts();
  const { current } = useEmployee();
  const employeeId = current?.id ?? null;

  // The widget's own account pick, used only while the switcher says "all
  // models". It lives in the gate for the same reason `turns` does: collapsing
  // unmounts the panel, and a pick that reset every time the bubble closed
  // would quietly re-bill the next question to the first account in the list.
  const [picked, setPicked] = useState<string | null>(null);

  // A scoped model always wins — under one, the switcher IS the picker and
  // this widget behaves exactly as before. In all-models scope, fall back to
  // the user's pick, then to the first live account. That default is fine
  // ONLY because the picker row renders it: the account being billed and
  // answered about is on screen, never inferred silently server-side.
  const pickedValid =
    picked !== null && accounts.some((a) => a.id === picked) ? picked : null;
  const accountId = scopedId ?? pickedValid ?? accounts[0]?.id ?? null;

  useEffect(() => {
    try {
      setOpen(window.localStorage.getItem(OPEN_KEY) === "1");
    } catch {
      /* ignore quota / safari private */
    } finally {
      setHydrated(true);
    }
  }, []);

  const toggle = useCallback(() => {
    setOpen((v) => {
      const next = !v;
      try { window.localStorage.setItem(OPEN_KEY, next ? "1" : "0"); } catch {}
      return next;
    });
  }, []);

  const settle = useCallback((id: number, answer: string, error: string | null) => {
    setTurns((list) => list.map((t) => (t.id === id ? { ...t, answer, error } : t)));
  }, []);

  const ask = useCallback(async (question: string) => {
    if (!accountId) return;
    const id = nextId.current++;
    setTurns((list) => [...list, { id, question, answer: null, error: null }]);
    try {
      const r = await relay.post<AskResponse>(
        "/admin/assistant/ask",
        // No `role` — the server derives who is asking from the session, and a
        // role read out of this body would be client-supplied theater.
        { question, account_id: accountId },
        { accountId, employeeId },
      );
      const answer = (r?.answer || "").trim();
      settle(
        id,
        answer || "The assistant came back empty — try asking again.",
        answer ? (r?.error ?? null) : "empty_answer",
      );
    } catch (e) {
      // Getting HERE means the request never reached a verdict — the relay is
      // down, the share token is stale, or `assert_account_owned` said no.
      // Every failure the endpoint itself knows about arrives as a 200.
      const detail = e instanceof RelayError ? e.message : "";
      settle(
        id,
        `Couldn't reach the assistant${detail ? ` (${detail})` : ""} — try again in a moment.`,
        "transport",
      );
    }
  }, [accountId, employeeId, settle]);

  if (!hydrated) return null;

  return (
    <>
      {open && (
        <AssistantPanel
          turns={turns}
          accountId={accountId}
          accounts={accounts}
          scoped={scopedId !== null}
          onPick={setPicked}
          onAsk={ask}
          onClose={toggle}
        />
      )}
      <button
        type="button"
        onClick={toggle}
        // Desktop-only, same as the MoneyRail dock: at phone widths a fixed
        // bottom corner sits on top of the chat composer.
        className={cn(
          "fixed bottom-3 left-3 z-40 hidden md:grid place-items-center",
          "h-9 w-9 rounded-full border shadow-lg transition-colors",
          open
            ? "bg-bg-elev-1 border-border-light text-fg"
            : "bg-panel border-border text-fg-dim hover:text-fg hover:bg-bg-elev-1",
        )}
        title={open ? "Close help" : "Ask for help with this product"}
        aria-expanded={open}
        aria-label="Help assistant"
      >
        <span className="text-[13px] font-semibold" aria-hidden>?</span>
      </button>
    </>
  );
}

// ── The panel ──────────────────────────────────────────────────────

function AssistantPanel({
  turns,
  accountId,
  accounts,
  scoped,
  onPick,
  onAsk,
  onClose,
}: {
  turns: Turn[];
  accountId: string | null;
  accounts: AccountMeta[];
  /** True when the model switcher has a single model scoped — the picker row
   *  only renders in all-models scope, where the switcher names no account. */
  scoped: boolean;
  onPick: (id: string) => void;
  onAsk: (question: string) => void;
  onClose: () => void;
}) {
  const [draft, setDraft] = useState("");
  const listRef = useRef<HTMLDivElement | null>(null);
  // An in-flight answer does NOT lock the composer: concurrent turns are fine —
  // `settle()` matches answers to turns by id, never by order.
  const canSend = !!accountId && draft.trim().length > 0;

  // Follow the newest row — both when a question lands and when its answer
  // replaces the "thinking…" line (which is usually the taller of the two).
  // Scrolls the list BOX, not a sentinel: scrollIntoView walks up to the
  // nearest scrollable ancestor, which on a short page is the document.
  useEffect(() => {
    const el = listRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [turns]);

  const send = () => {
    if (!canSend) return;
    onAsk(draft.trim().slice(0, MAX_QUESTION_CHARS));
    setDraft("");
  };

  return (
    <div
      className={cn(
        "fixed bottom-14 left-3 z-40 hidden md:flex w-[340px] flex-col",
        "bg-panel border border-border rounded-lg shadow-xl overflow-hidden",
      )}
    >
      <div className="flex items-center gap-2 px-3 py-2 bg-bg-elev-1/50 border-b border-border">
        <span className="text-[12px] font-medium text-fg flex items-center gap-1.5">
          <span aria-hidden>💬</span> Help
        </span>
        <span className="text-[10px] text-fg-dim truncate">
          asks about this product, not about a fan
        </span>
        <button
          type="button"
          onClick={onClose}
          className="ml-auto shrink-0 px-1.5 py-1 rounded text-[11px] text-fg-dim hover:text-fg hover:bg-bg-elev-1"
          title="Close"
        >
          <span aria-hidden>✕</span>
        </button>
      </div>

      <div
        ref={listRef}
        className="max-h-[50vh] min-h-[120px] overflow-y-auto overscroll-contain px-3 py-2.5 flex flex-col gap-2.5"
      >
        {turns.length === 0 && (
          <p className="text-[11px] text-fg-dim leading-relaxed">
            Ask how something works — &ldquo;how do I set up auto posts?&rdquo;,
            &ldquo;what does the ghost cycle do?&rdquo;. Answers come from the
            product manual, so an honest &ldquo;not supported&rdquo; is a real
            answer.
          </p>
        )}
        {turns.map((t) => (
          <div key={t.id} className="flex flex-col gap-1.5">
            <div className="self-end max-w-[85%] rounded-lg rounded-br-sm bg-accent/15 border border-accent/25 px-2.5 py-1.5 text-[12px] text-fg whitespace-pre-wrap break-words">
              {t.question}
            </div>
            {t.answer === null ? (
              <div className="self-start text-[11px] text-fg-dim animate-pulse">
                thinking…
              </div>
            ) : (
              <div
                className={cn(
                  "self-start max-w-[92%] rounded-lg rounded-bl-sm border px-2.5 py-1.5",
                  "text-[12px] leading-relaxed break-words bg-bg-elev-1 border-border",
                  // A cap / missing key / provider wobble is still an answer —
                  // muted, never a red error chip. See the module note.
                  t.error ? "text-fg-dim" : "text-fg",
                )}
              >
                {/* The answer is markdown (the manual is), and the tab names in
                    it are real destinations — AnswerText renders both. No
                    `whitespace-pre-wrap` here: it owns its own block layout. */}
                <AnswerText text={t.answer} muted={!!t.error} />
              </div>
            )}
          </div>
        ))}
      </div>

      {accountId ? (
        <div className="border-t border-border p-2 flex flex-col gap-1.5">
          {/* In all-models scope nothing on screen names an account, but the
              server bills each answer to one and scopes its live-state reads
              to it — so the account in play must be visible, not inferred. */}
          {!scoped && (
            <label className="flex items-center gap-1.5">
              <span className="text-[10px] text-fg-dim shrink-0">
                Answering about
              </span>
              <select
                value={accountId}
                onChange={(e) => onPick(e.target.value)}
                className="flex-1 min-w-0 rounded border border-border bg-bg-elev-1 px-1.5 py-0.5 text-[11px] text-fg"
                aria-label="Account the help bot answers about"
              >
                {accounts.map((a) => (
                  <option key={a.id} value={a.id}>
                    {a.nickname || a.id}
                  </option>
                ))}
              </select>
            </label>
          )}
          <Textarea
            value={draft}
            onChange={(e) => setDraft(e.target.value.slice(0, MAX_QUESTION_CHARS))}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                send();
              }
            }}
            placeholder="Ask about a feature…"
            rows={2}
            className="font-sans text-[12px] min-h-0 h-14 resize-none"
          />
          <div className="flex items-center gap-2">
            <span className="text-[10px] text-fg-dim">
              Enter to send · Shift+Enter for a new line
            </span>
            {/* Always "Ask" — the per-turn "thinking…" row shows progress, and
                the composer stays live while an answer is in flight. */}
            <Button
              size="sm"
              onClick={send}
              disabled={!canSend}
              className="ml-auto"
            >
              Ask
            </Button>
          </div>
        </div>
      ) : (
        // Only reachable with NO live account at all (none captured yet, or
        // the roster hasn't loaded) — all-models scope now falls back to the
        // picker above. The server still needs one account_id per question,
        // so say so instead of firing a request that can only 422.
        <div className="border-t border-border px-3 py-2.5 text-[11px] text-fg-dim leading-relaxed">
          No account to answer about yet — the help bot scopes each answer to
          one model. Connect an account and the composer unlocks.
        </div>
      )}
    </div>
  );
}
