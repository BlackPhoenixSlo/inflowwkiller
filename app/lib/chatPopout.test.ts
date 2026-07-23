import { afterEach, describe, expect, it, vi } from "vitest";
import type { MouseEvent } from "react";

import { chatTabName, openChatTab } from "./chatPopout";

describe("chatTabName", () => {
  it("strips the query so ?refresh=media and plain links share one tab", () => {
    expect(chatTabName("/chat/300/555?refresh=media")).toBe("fastt-/chat/300/555");
    expect(chatTabName("/chat/300/555")).toBe("fastt-/chat/300/555");
  });

  it("keys per (account, fan) — different fans get different tabs", () => {
    expect(chatTabName("/chat/300/555")).not.toBe(chatTabName("/chat/300/556"));
    expect(chatTabName("/chat/300/555")).not.toBe(chatTabName("/chat/301/555"));
  });
});

describe("openChatTab", () => {
  function ev(overrides: Partial<MouseEvent> = {}): MouseEvent {
    return {
      defaultPrevented: false,
      button: 0,
      metaKey: false,
      ctrlKey: false,
      shiftKey: false,
      altKey: false,
      preventDefault: vi.fn(),
      ...overrides,
    } as unknown as MouseEvent;
  }

  afterEach(() => vi.restoreAllMocks());

  it("plain left click → window.open on the named tab, focused, default prevented", () => {
    const focus = vi.fn();
    const open = vi
      .spyOn(window, "open")
      .mockReturnValue({ focus } as unknown as Window);
    const e = ev();

    openChatTab(e, "/chat/300/555?refresh=media");

    expect(open).toHaveBeenCalledWith("/chat/300/555?refresh=media", "fastt-/chat/300/555");
    expect(focus).toHaveBeenCalled();
    expect(e.preventDefault).toHaveBeenCalled();
  });

  it("modified clicks fall through to native anchor behavior", () => {
    const open = vi.spyOn(window, "open").mockReturnValue(null);
    for (const mod of [{ metaKey: true }, { ctrlKey: true }, { shiftKey: true }, { altKey: true }, { button: 1 }]) {
      const e = ev(mod);
      openChatTab(e, "/chat/300/555");
      expect(e.preventDefault).not.toHaveBeenCalled();
    }
    expect(open).not.toHaveBeenCalled();
  });

  it("survives a null WindowProxy (popup blocked) without throwing", () => {
    vi.spyOn(window, "open").mockReturnValue(null);
    expect(() => openChatTab(ev(), "/chat/300/555")).not.toThrow();
  });
});
