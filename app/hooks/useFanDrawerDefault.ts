"use client";

/**
 * useFanDrawerDefault — persisted flag that auto-opens the FanDrawer on
 * every chat-select when enabled. Toggled from inside the drawer itself
 * via a small "Keep open by default" checkbox, so the user can opt in
 * once and never have to click the header again.
 */

import { useEffect, useState } from "react";

const STORAGE_KEY = "chatterly:fan-drawer-default-open";
const EVENT = "chatterly-fan-drawer-default-change";

export function readFanDrawerDefault(): boolean {
  if (typeof window === "undefined") return true;
  return window.localStorage.getItem(STORAGE_KEY) !== "0";
}

export function useFanDrawerDefault(): [boolean, (next: boolean) => void] {
  const [val, setVal] = useState<boolean>(() => readFanDrawerDefault());

  useEffect(() => {
    const onChange = () => setVal(readFanDrawerDefault());
    window.addEventListener(EVENT, onChange);
    window.addEventListener("storage", onChange);
    return () => {
      window.removeEventListener(EVENT, onChange);
      window.removeEventListener("storage", onChange);
    };
  }, []);

  function update(next: boolean) {
    setVal(next);
    try {
      if (next) window.localStorage.removeItem(STORAGE_KEY);
      else window.localStorage.setItem(STORAGE_KEY, "0");
      window.dispatchEvent(new Event(EVENT));
    } catch { /* quota — silent */ }
  }

  return [val, update];
}
