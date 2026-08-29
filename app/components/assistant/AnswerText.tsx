"use client";

/**
 * AnswerText — renders one help answer.
 *
 * The bot writes markdown because the manual is markdown, so an answer arrives
 * full of `**bold**`, `- ` bullets and `1.` steps. Rendered as plain text those
 * asterisks are just noise in front of the words that matter — the tab names.
 *
 * A DELIBERATELY SMALL SUBSET: bold, italic, inline code, bullet and numbered
 * lists, headings, paragraphs. No links-from-markdown, no tables, no images, no
 * HTML — the input is model output, and the only safe renderer for that is one
 * that builds React nodes and never touches `dangerouslySetInnerHTML`. Anything
 * it does not understand falls through as text, which is why an unclosed `**`
 * degrades to a visible asterisk instead of eating the rest of the answer.
 *
 * The one thing it ADDS is click paths: see answerLinks.ts.
 */

import type { ReactNode } from "react";
import Link from "next/link";

import { cn } from "@/lib/utils";
import { findPaths } from "./answerLinks";

/** `**bold**` · `*italic*` · `` `code` `` — first match wins, left to right. */
const INLINE_RE = /`([^`\n]+)`|\*\*([^\n]+?)\*\*|\*([^*\n]+?)\*/g;

/** Wrap every click path in `text` in a <Link>; everything else is a string.
 *
 * `run` disambiguates keys only: several calls land in ONE sibling array, so
 * without it the second run's first link would collide with the first run's.
 */
function linkPaths(text: string, run: number): ReactNode[] {
  const hits = findPaths(text);
  if (hits.length === 0) return [text];

  const out: ReactNode[] = [];
  let last = 0;
  hits.forEach((h, i) => {
    if (h.start > last) out.push(text.slice(last, h.start));
    out.push(
      <Link
        key={`l${run}-${i}`}
        href={h.href}
        className="text-accent underline decoration-accent/40 underline-offset-2 hover:decoration-accent"
      >
        {text.slice(h.start, h.end)}
      </Link>,
    );
    last = h.end;
  });
  if (last < text.length) out.push(text.slice(last));
  return out;
}

/** Inline markdown → nodes. Code spans are opaque: no links inside them.
 *
 * The result is always ONE element's children, so keys only have to be unique
 * within this call — no caller-supplied prefix needed.
 */
function inline(text: string): ReactNode[] {
  const out: ReactNode[] = [];
  let last = 0;
  let i = 0;
  let m: RegExpExecArray | null;
  INLINE_RE.lastIndex = 0;

  while ((m = INLINE_RE.exec(text))) {
    const [raw, code, bold, italic] = m;
    if (m.index > last) out.push(...linkPaths(text.slice(last, m.index), i));

    if (code !== undefined) {
      out.push(
        <code
          key={`c${i}`}
          className="px-1 py-0.5 rounded bg-bg-elev-1 border border-border text-[11px] font-mono break-words"
        >
          {code}
        </code>,
      );
    } else if (bold !== undefined) {
      out.push(
        <strong key={`b${i}`} className="font-semibold text-fg">
          {linkPaths(bold, i)}
        </strong>,
      );
    } else if (italic !== undefined) {
      out.push(<em key={`i${i}`}>{linkPaths(italic, i)}</em>);
    } else {
      // Unreachable: every alternative in INLINE_RE captures. Emitting the raw
      // match keeps this total rather than silently dropping text.
      out.push(raw);
    }

    last = INLINE_RE.lastIndex;
    i++;
  }
  if (last < text.length) out.push(...linkPaths(text.slice(last), i));
  return out;
}

/**
 * One rendered block. Paragraphs, list items and headings are all "a run of
 * strings", so they share ONE shape — three payload names (`lines` / `items` /
 * `text`) is what forces a parser to cast its way out of its own union.
 */
type BlockKind = "p" | "ul" | "ol" | "h";
interface Block {
  kind: BlockKind;
  items: string[];
}

/** Line prefixes that open a non-paragraph block, in match order. */
const LINE_KINDS: Array<[Exclude<BlockKind, "p">, RegExp]> = [
  ["h", /^\s*#{1,6}\s+(.*)$/],
  ["ul", /^\s*[-*•]\s+(.*)$/],
  ["ol", /^\s*\d+[.)]\s+(.*)$/],
];

/** A wrapped continuation of the item above, not a new one. */
const INDENT_RE = /^\s{2,}\S/;

function classify(line: string): { kind: BlockKind; text: string } {
  for (const [kind, re] of LINE_KINDS) {
    const m = re.exec(line);
    if (m) return { kind, text: m[1] };
  }
  return { kind: "p", text: line.trim() };
}

function parse(text: string): Block[] {
  const blocks: Block[] = [];
  let open: Block | null = null;

  for (const line of text.split("\n")) {
    if (!line.trim()) {
      open = null; // a blank line closes whatever was open
      continue;
    }

    // An indented line continues the list item above it.
    if (open && (open.kind === "ul" || open.kind === "ol") && INDENT_RE.test(line)) {
      open.items[open.items.length - 1] += ` ${line.trim()}`;
      continue;
    }

    const { kind, text: content } = classify(line);
    // A same-kind line joins the open block; a heading is always its own.
    if (!open || open.kind !== kind || kind === "h") {
      open = { kind, items: [] };
      blocks.push(open); // pushed on open, then filled — no flush pass needed
    }
    open.items.push(content);
  }
  return blocks;
}

export function AnswerText({ text, muted }: { text: string; muted?: boolean }) {
  const blocks = parse(text);

  return (
    <div className={cn("flex flex-col gap-1.5", muted && "text-fg-dim")}>
      {blocks.map((b, i) => {
        if (b.kind === "h") {
          return (
            <p key={i} className="font-semibold text-fg">
              {inline(b.items[0])}
            </p>
          );
        }
        if (b.kind === "p") {
          // Hard-wrapped prose rejoins into one paragraph; the panel is narrow
          // and the model's own line breaks are an artefact of the manual.
          return (
            <p key={i} className="break-words">
              {inline(b.items.join(" "))}
            </p>
          );
        }
        const List = b.kind; // "ul" | "ol"
        return (
          <List
            key={i}
            className={cn(
              "flex flex-col gap-1 pl-4 break-words",
              b.kind === "ul" ? "list-disc" : "list-decimal",
            )}
          >
            {b.items.map((it, j) => (
              <li key={j} className="marker:text-fg-dim">
                {inline(it)}
              </li>
            ))}
          </List>
        );
      })}
    </div>
  );
}
