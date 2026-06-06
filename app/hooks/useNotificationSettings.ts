"use client";

/**
 * useNotificationSettings — read/write the toaster prefs with re-render
 * on change.
 *
 * Subscribes to the custom "chatterly:notif-settings" event we fire from
 * writeNotifSettings so other components (the bell, the toaster) refresh
 * the moment the user flips a toggle. Also listens to "storage" so popout
 * tabs stay in sync.
 */

import { useCallback, useEffect, useState } from "react";

import {
  readNotifSettings,
  writeNotifSettings,
  type NotifSettings,
  type NotifTypeKey,
} from "@/lib/notifSettings";

export function useNotificationSettings() {
  const [settings, setSettings] = useState<NotifSettings>(() => readNotifSettings());

  useEffect(() => {
    function refresh() { setSettings(readNotifSettings()); }
    function onStorage(e: StorageEvent) {
      if (e.key === "chatterly:notif-settings:v1") refresh();
    }
    window.addEventListener("chatterly:notif-settings", refresh);
    window.addEventListener("storage", onStorage);
    return () => {
      window.removeEventListener("chatterly:notif-settings", refresh);
      window.removeEventListener("storage", onStorage);
    };
  }, []);

  // Side effects (localStorage write + event dispatch) MUST live outside
  // setSettings' updater function — React calls updaters during render in
  // concurrent mode, and dispatching an event from a render triggered a
  // setState in the sibling NotificationDeltaPoller. Read `settings` from
  // closure instead and pass the next value directly to setSettings.
  const update = useCallback((patch: Partial<NotifSettings>) => {
    const next = { ...settings, ...patch };
    setSettings(next);
    writeNotifSettings(next);
  }, [settings]);

  const setToast = useCallback((key: NotifTypeKey, on: boolean) => {
    const next = { ...settings, toast: { ...settings.toast, [key]: on } };
    setSettings(next);
    writeNotifSettings(next);
  }, [settings]);

  const setBubble = useCallback((key: NotifTypeKey, on: boolean) => {
    const next = { ...settings, bubble: { ...settings.bubble, [key]: on } };
    setSettings(next);
    writeNotifSettings(next);
  }, [settings]);

  return { settings, update, setToast, setBubble };
}
