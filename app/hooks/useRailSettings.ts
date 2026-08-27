"use client";

/**
 * The money rail's global settings, hydration-gated.
 *
 * The settings are written from OUTSIDE whichever component is reading them —
 * the rail's own ⚙, the bell's "show it here" restore button, and any other
 * tab — so every reader has to subscribe rather than read once. Both readers
 * used to hand-roll that subscription, and they had drifted: the restore
 * button listened for the same-tab SETTINGS_EVENT but not the cross-tab
 * `storage` event, so hiding the rail from the main tab left a popout's bell
 * still offering to un-hide something that was already hidden.
 *
 * `settings` is null until localStorage has actually been read. The server
 * and the first client render must agree, and neither knows the saved pick —
 * so null means "don't decide yet", not "no settings".
 */

import { useCallback, useEffect, useState } from "react";

import {
  SETTINGS_EVENT,
  SETTINGS_KEY,
  readSettings,
  writeSettings,
  type RailSettings,
} from "@/lib/moneyRailStorage";

export interface RailSettingsHandle {
  /** null until hydrated — see the note above. */
  settings: RailSettings | null;
  /** Apply a pick locally AND persist it. The write queues a SETTINGS_EVENT,
   *  which every other reader (including this hook in another component)
   *  picks up. */
  commit: (next: RailSettings) => void;
}

export function useRailSettings(): RailSettingsHandle {
  const [settings, setSettings] = useState<RailSettings | null>(null);

  useEffect(() => {
    const reread = () => setSettings(readSettings());
    reread();
    const onStorage = (e: StorageEvent) => {
      if (e.key === SETTINGS_KEY) reread();
    };
    window.addEventListener(SETTINGS_EVENT, reread);
    window.addEventListener("storage", onStorage);
    return () => {
      window.removeEventListener(SETTINGS_EVENT, reread);
      window.removeEventListener("storage", onStorage);
    };
  }, []);

  const commit = useCallback((next: RailSettings) => {
    setSettings(next);
    writeSettings(next);
  }, []);

  return { settings, commit };
}
