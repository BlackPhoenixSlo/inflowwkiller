"use client";

/**
 * Minimal UI primitives — Button, Input, Textarea, Card.
 *
 * Intentionally NOT a full design system. We add a primitive when a second
 * caller needs it. Radix wrappers (Dialog, Tabs, etc.) live next to this
 * file under components/ui/ as separate files when a screen actually
 * needs them.
 */

import { forwardRef } from "react";
import { cn } from "@/lib/utils";

// ── Button ─────────────────────────────────────────────────────────

type ButtonVariant = "primary" | "secondary" | "ghost" | "danger";

export const Button = forwardRef<
  HTMLButtonElement,
  React.ButtonHTMLAttributes<HTMLButtonElement> & {
    variant?: ButtonVariant;
    size?: "sm" | "md";
  }
>(({ className, variant = "primary", size = "md", ...props }, ref) => {
  const variants: Record<ButtonVariant, string> = {
    primary:
      "bg-accent hover:bg-accent-hover text-white border-transparent",
    secondary:
      "bg-bg-elev-1 hover:bg-bg-elev-2 text-fg border-border hover:border-border-light",
    ghost:
      "bg-transparent hover:bg-bg-elev-1 text-fg border-transparent",
    danger:
      "bg-err/10 hover:bg-err/20 text-err border-err/30",
  };
  const sizes = {
    // Phone-only touch sizing (`max-md:` = below 768px). The unprefixed
    // desktop values are left exactly as they were, so >=768px is untouched
    // and any caller className override still wins at desktop width.
    sm: "px-3 py-1.5 text-xs max-md:py-2.5 max-md:min-h-11",
    md: "px-4 py-2 text-sm max-md:py-2.5 max-md:min-h-11",
  } as const;
  return (
    <button
      ref={ref}
      className={cn(
        "inline-flex items-center justify-center gap-2 rounded-lg font-medium",
        "border transition-colors disabled:opacity-50 disabled:cursor-not-allowed",
        "focus:outline-none focus:ring-2 focus:ring-accent/40",
        variants[variant],
        sizes[size],
        className,
      )}
      {...props}
    />
  );
});
Button.displayName = "Button";

// ── Input / Textarea ──────────────────────────────────────────────

export const Input = forwardRef<HTMLInputElement, React.InputHTMLAttributes<HTMLInputElement>>(
  ({ className, ...props }, ref) => (
    <input
      ref={ref}
      className={cn(
        "w-full bg-bg border border-border rounded-lg px-3 py-2 text-sm",
        // Phone-only: 16px text + 44px target. `max-md:` leaves the desktop
        // rules (px-3 py-2 text-sm) byte-identical and keeps call sites that
        // override py-/text- (TemplatesTab, MessagesFilters) authoritative.
        "max-md:py-2.5 max-md:text-base max-md:min-h-11",
        "placeholder:text-muted",
        "focus:outline-none focus:border-accent",
        className,
      )}
      {...props}
    />
  ),
);
Input.displayName = "Input";

export const Textarea = forwardRef<
  HTMLTextAreaElement,
  React.TextareaHTMLAttributes<HTMLTextAreaElement>
>(({ className, ...props }, ref) => (
  <textarea
    ref={ref}
    className={cn(
      "w-full bg-bg border border-border rounded-lg px-3 py-2",
      // `max-md:text-base` = 16px on phone only; the 11px mono desktop value
      // and every caller's own text-* override are unchanged at >=768px.
      "font-mono text-[11px] leading-relaxed max-md:text-base",
      "placeholder:text-muted",
      "focus:outline-none focus:border-accent",
      "resize-y min-h-24",
      className,
    )}
    {...props}
  />
));
Textarea.displayName = "Textarea";

// ── Card ──────────────────────────────────────────────────────────

export function Card({
  className,
  children,
  ...rest
}: React.HTMLAttributes<HTMLDivElement>) {
  return (
    <div
      className={cn(
        "bg-panel border border-border rounded-2xl p-5",
        className,
      )}
      {...rest}
    >
      {children}
    </div>
  );
}

// ── Tag / Badge ───────────────────────────────────────────────────

export function Badge({
  className, color, children, ...rest
}: React.HTMLAttributes<HTMLSpanElement> & { color?: "ok" | "warn" | "err" | "muted" | "info" }) {
  const colors = {
    ok:     "bg-ok/15 text-ok border-ok/30",
    warn:   "bg-warn/15 text-warn border-warn/30",
    err:    "bg-err/15 text-err border-err/30",
    muted:  "bg-bg-elev-1 text-fg-dim border-border",
    info:   "bg-info/15 text-info border-info/30",
  } as const;
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-[11px] font-medium border",
        colors[color || "muted"],
        className,
      )}
      {...rest}
    >
      {children}
    </span>
  );
}
