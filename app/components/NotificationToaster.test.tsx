/**
 * NotificationToaster — inbound-message bubble (the NEW `api2_chat_message`
 * listener inside NotificationDeltaPoller).
 *
 * Confirms end-to-end that a fan's inbound DM, delivered over the SSE bus as
 * an `api2_chat_message` envelope, produces a top-right toast card — and that
 * `settings.bubble.message` controls the accent-ring "bubble" styling, while
 * `settings.toast.message` gates whether the toast appears at all. Outbound
 * echoes and out-of-scope accounts must NOT ping; history is always mirrored.
 *
 * Strategy: NotificationToaster carries MODULE-LEVEL state (currentToasts,
 * seenIdsByAccount, toastListeners). We isolate every case with
 * vi.resetModules() + a fresh vi.doMock of every dependency + a dynamic
 * import() of the component. The events module is mocked so eventBus.on
 * records the handler in a registry the test fires synchronously.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, render } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactElement } from "react";

// notifSettings is a REAL dependency (we assert on the history it writes and
// reuse its types/defaults); only its consumers are mocked.
import {
  DEFAULT_NOTIF_SETTINGS,
  readNotifHistory,
  type NotifSettings,
} from "@/lib/notifSettings";

// ── per-test mutable mock state ───────────────────────────────────────────
// Set by each test BEFORE the dynamic import; read by the doMock factories.
let mockSettings: NotifSettings;
let mockAccountId: string | null;
type Handler = (env: unknown, rawType?: string) => void;
// eventType -> handlers registered via eventBus.on in this isolated module graph.
let handlers: Map<string, Set<Handler>>;

function settingsWith(over: {
  toastMessage: boolean;
  bubbleMessage: boolean;
}): NotifSettings {
  return {
    ...DEFAULT_NOTIF_SETTINGS,
    enabled: true,
    osPing: false,
    toast: { ...DEFAULT_NOTIF_SETTINGS.toast, message: over.toastMessage },
    bubble: { ...DEFAULT_NOTIF_SETTINGS.bubble, message: over.bubbleMessage },
  };
}

/** Re-apply every mock against the freshly-reset module graph, then import
 *  the component. Must be called AFTER mockSettings/mockAccountId are set. */
async function loadComponent(): Promise<() => ReactElement> {
  vi.resetModules();
  handlers = new Map();

  vi.doMock("@/lib/events", () => ({
    eventBus: {
      on(type: string, fn: Handler) {
        let set = handlers.get(type);
        if (!set) {
          set = new Set();
          handlers.set(type, set);
        }
        set.add(fn);
        return () => set!.delete(fn);
      },
    },
  }));

  vi.doMock("@/hooks/useDeferredMount", () => ({
    useDeferredMount: () => true,
  }));

  vi.doMock("@/hooks/useNotificationSettings", () => ({
    useNotificationSettings: () => ({ settings: mockSettings }),
  }));

  vi.doMock("@/contexts/ScopeContext", () => ({
    useScope: () => ({ accountId: mockAccountId }),
  }));

  vi.doMock("@/hooks/useAccounts", () => ({
    useActiveAccounts: () => [],
  }));

  vi.doMock("@/lib/relay", () => ({
    proxyImage: (url: string | null | undefined) => url ?? null,
    relay: {},
  }));

  const mod = await import("@/components/NotificationToaster");
  return () => <mod.NotificationToaster />;
}

/** Fire a synthetic envelope to every registered handler of `type`. */
function fire(type: string, env: unknown) {
  const set = handlers.get(type);
  if (!set || set.size === 0) {
    throw new Error(`no handler registered for "${type}" — effect didn't subscribe`);
  }
  act(() => {
    for (const fn of set) fn(env);
  });
}

function renderToaster(Comp: () => ReactElement) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0, staleTime: 0 } },
  });
  return render(<QueryClientProvider client={qc}><Comp /></QueryClientProvider>);
}

/** The toast card div carries the message text + the ring class. */
function findToastByText(container: HTMLElement, text: string): HTMLElement | null {
  const cards = Array.from(
    container.querySelectorAll<HTMLElement>("div.bg-panel"),
  );
  return cards.find((c) => c.textContent?.includes(text)) ?? null;
}

beforeEach(() => {
  window.localStorage.clear();
  mockSettings = settingsWith({ toastMessage: true, bubbleMessage: false });
  mockAccountId = "100";
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  vi.resetModules();
  vi.useRealTimers();
});

// Each case uses a UNIQUE account id + message id so even if module state
// leaked, assertions stay independent. We additionally reset modules per case.
describe("NotificationToaster — inbound api2_chat_message bubble", () => {
  it("case 1: inbound + toast.message + bubble.message → toast with accent-ring bubble", async () => {
    mockAccountId = "100";
    mockSettings = settingsWith({ toastMessage: true, bubbleMessage: true });
    const Comp = await loadComponent();
    const { container } = renderToaster(Comp);

    fire("api2_chat_message", {
      __account_id: "100",
      api2_chat_message: { id: 1001, text: "hey from case1", fromUser: { id: 777, name: "Flinty" } },
    });

    const card = findToastByText(container, "hey from case1");
    expect(card).not.toBeNull();
    // The headline assertion: bubble styling is the accent ring.
    expect(card!.className).toContain("ring-accent");
    expect(card!.className).toContain("ring-2");
    expect(card!.className).not.toContain("ring-border");
    // Actor name renders.
    expect(card!.textContent).toContain("Flinty");
  });

  it("case 2: inbound + toast.message + NO bubble → plain toast (ring-border, not ring-accent)", async () => {
    mockAccountId = "101";
    mockSettings = settingsWith({ toastMessage: true, bubbleMessage: false });
    const Comp = await loadComponent();
    const { container } = renderToaster(Comp);

    fire("api2_chat_message", {
      __account_id: "101",
      api2_chat_message: { id: 1002, text: "plain case2", fromUser: { id: 778, name: "Plainy" } },
    });

    const card = findToastByText(container, "plain case2");
    expect(card).not.toBeNull();
    expect(card!.className).toContain("ring-border");
    expect(card!.className).toContain("ring-1");
    expect(card!.className).not.toContain("ring-accent");
  });

  it("case 3: outbound echo (fromUser.id === account, with __fan_id) → NO toast", async () => {
    mockAccountId = "102";
    mockSettings = settingsWith({ toastMessage: true, bubbleMessage: true });
    const Comp = await loadComponent();
    const { container } = renderToaster(Comp);

    fire("api2_chat_message", {
      __account_id: "102",
      __fan_id: 779,
      api2_chat_message: { id: 1003, text: "outbound case3", fromUser: { id: 102, name: "Me" } },
    });

    expect(findToastByText(container, "outbound case3")).toBeNull();
    // ToastStack renders nothing at all.
    expect(container.querySelector("div.bg-panel")).toBeNull();
  });

  it("case 4: toast.message:false → NO toast, but history still mirrored", async () => {
    mockAccountId = "103";
    mockSettings = settingsWith({ toastMessage: false, bubbleMessage: true });
    const Comp = await loadComponent();
    const { container } = renderToaster(Comp);

    fire("api2_chat_message", {
      __account_id: "103",
      api2_chat_message: { id: 1004, text: "history case4", fromUser: { id: 780, name: "Histy" } },
    });

    // No visible toast.
    expect(findToastByText(container, "history case4")).toBeNull();
    expect(container.querySelector("div.bg-panel")).toBeNull();

    // History IS mirrored regardless of the toggle.
    const hist = readNotifHistory();
    const entries = hist["103"] ?? [];
    const entry = entries.find((e) => e.id === "m1004");
    expect(entry).toBeTruthy();
    expect(entry!.text).toContain("history case4");
    expect(entry!.typeKey).toBe("message");
    // Sanity: the raw localStorage key the bell reads from is populated.
    expect(window.localStorage.getItem("chatterly:notif-history:v1")).toContain("m1004");
  });

  it("case 5: out-of-scope account (__account_id not in targetIds) → NO toast", async () => {
    // Scope pins account "104"; the envelope is for "999" → must be ignored.
    mockAccountId = "104";
    mockSettings = settingsWith({ toastMessage: true, bubbleMessage: true });
    const Comp = await loadComponent();
    const { container } = renderToaster(Comp);

    fire("api2_chat_message", {
      __account_id: "999",
      api2_chat_message: { id: 1005, text: "scope case5", fromUser: { id: 781, name: "Stranger" } },
    });

    expect(findToastByText(container, "scope case5")).toBeNull();
    expect(container.querySelector("div.bg-panel")).toBeNull();
    // And nothing leaked into history for the in-scope account.
    const hist = readNotifHistory();
    expect(hist["104"]).toBeUndefined();
  });

  it("case 6: empty text + mediaCount>0 → toast text contains 'Sent you media'", async () => {
    mockAccountId = "105";
    mockSettings = settingsWith({ toastMessage: true, bubbleMessage: false });
    const Comp = await loadComponent();
    const { container } = renderToaster(Comp);

    fire("api2_chat_message", {
      __account_id: "105",
      api2_chat_message: { id: 1006, text: "", mediaCount: 2, fromUser: { id: 782, name: "Media" } },
    });

    const card = findToastByText(container, "Sent you media");
    expect(card).not.toBeNull();
    expect(card!.textContent).toContain("Media");
  });
});

// The `toasts` SSE path (OF notification toasts). Tip/purchase toasts must
// deep-link with the one-shot ?refresh=media flag — the popout chat page
// reads it on mount and refetches the FanDrawer's manual-↻ cache set so the
// just-received transaction shows instead of the rehydrated stale copy.
// Non-money types must link plain.
describe("NotificationToaster — tip/purchase toast href carries ?refresh=media", () => {
  function fireOfToast(aid: string, toast: Record<string, unknown>) {
    fire("toasts", { __account_id: aid, toasts: [toast] });
  }

  function findChatAnchor(container: HTMLElement, fanId: number): HTMLAnchorElement | null {
    const anchors = Array.from(container.querySelectorAll<HTMLAnchorElement>("a"));
    return anchors.find((a) => a.getAttribute("href")?.includes(`/${fanId}`)) ?? null;
  }

  it("tip toast → href ends with ?refresh=media", async () => {
    mockAccountId = "300";
    const Comp = await loadComponent();
    const { container } = renderToaster(Comp);

    fireOfToast("300", {
      id: 9001,
      type: "tip",
      text: "tipped you $10",
      data: { relatedUser: { id: 555, name: "Tipper" } },
    });

    const a = findChatAnchor(container, 555);
    expect(a).not.toBeNull();
    expect(a!.getAttribute("href")).toBe("/chat/300/555?refresh=media");
  });

  it("purchase toast (paided_message) → href ends with ?refresh=media", async () => {
    mockAccountId = "301";
    const Comp = await loadComponent();
    const { container } = renderToaster(Comp);

    fireOfToast("301", {
      id: 9002,
      type: "paided_message",
      text: "unlocked your message",
      data: { relatedUser: { id: 556, name: "Buyer" } },
    });

    const a = findChatAnchor(container, 556);
    expect(a).not.toBeNull();
    expect(a!.getAttribute("href")).toBe("/chat/301/556?refresh=media");
  });

  it("subscribed toast → plain href, no refresh flag", async () => {
    mockAccountId = "302";
    const Comp = await loadComponent();
    const { container } = renderToaster(Comp);

    fireOfToast("302", {
      id: 9003,
      type: "subscribed",
      text: "subscribed to you",
      data: { relatedUser: { id: 557, name: "Newsub" } },
    });

    const a = findChatAnchor(container, 557);
    expect(a).not.toBeNull();
    expect(a!.getAttribute("href")).toBe("/chat/302/557");
  });
});
