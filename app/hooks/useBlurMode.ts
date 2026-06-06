"use client";

/**
 * useBlurMode — persisted "should images be blurred?" toggle.
 *
 * Three states:
 *   • "off"      — no blur, render images as-is (default)
 *   • "hover"    — blurred until the user hovers the tile
 *   • "constant" — blurred always (NSFW / over-the-shoulder mode)
 *
 * Applied to both chat-message media tiles and vault picker thumbnails.
 * The setting lives in the ChatList settings gear so the user can flip
 * it from a single place without leaving the inbox.
 */

import { useEffect, useState } from "react";

export type BlurMode = "off" | "hover" | "constant";

const STORAGE_KEY = "chatterly:blur-mode";
const EVENT = "chatterly-blur-mode-change";

function isMode(v: unknown): v is BlurMode {
  return v === "off" || v === "hover" || v === "constant";
}

export function readBlurMode(): BlurMode {
  if (typeof window === "undefined") return "off";
  const raw = window.localStorage.getItem(STORAGE_KEY);
  return isMode(raw) ? raw : "off";
}

export function useBlurMode(): [BlurMode, (next: BlurMode) => void] {
  const [val, setVal] = useState<BlurMode>(() => readBlurMode());

  useEffect(() => {
    const onChange = () => setVal(readBlurMode());
    window.addEventListener(EVENT, onChange);
    window.addEventListener("storage", onChange);
    return () => {
      window.removeEventListener(EVENT, onChange);
      window.removeEventListener("storage", onChange);
    };
  }, []);

  function update(next: BlurMode) {
    setVal(next);
    try {
      if (next === "off") window.localStorage.removeItem(STORAGE_KEY);
      else window.localStorage.setItem(STORAGE_KEY, next);
      window.dispatchEvent(new Event(EVENT));
    } catch { /* quota — silent */ }
  }

  return [val, update];
}

/** Tailwind classes for the currently selected mode. Empty string when
 *  blur is off so the original image styling isn't disturbed. */
export function blurImageClass(mode: BlurMode): string {
  if (mode === "hover") return "blur-md hover:blur-none transition-[filter] duration-200";
  if (mode === "constant") return "blur-md";
  return "";
}
