/**
 * useTabParam — the half of a help-assistant link that makes it land.
 *
 * The link itself is only half the feature: `/growth?tab=promotion` is a
 * broken promise if the page opens on Smart Lists anyway. These cases pin the
 * three properties the callers rely on — it seeds from the URL, it refuses a
 * value the page does not own (a chatter following an owner-only link must not
 * land on a tab their nav does not show), and it is MOUNT-ONLY, so the tab
 * strip stays the source of truth once the reader is on the page.
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render } from "@testing-library/react";

import { useTabParam } from "./useTabParam";

afterEach(cleanup);

type Tab = "smart" | "promotion";
const isTab = (v: string): v is Tab => v === "smart" || v === "promotion";

function setSearch(search: string) {
  window.history.replaceState({}, "", `/growth${search}`);
}

function Probe({ onFound }: { onFound: (t: Tab) => void }) {
  useTabParam("tab", isTab, onFound);
  return null;
}

describe("useTabParam", () => {
  it("seeds the tab named in the URL", () => {
    setSearch("?tab=promotion");
    const found = vi.fn();
    render(<Probe onFound={found} />);
    expect(found).toHaveBeenCalledWith("promotion");
  });

  it("ignores a tab the page does not own", () => {
    setSearch("?tab=employees");
    const found = vi.fn();
    render(<Probe onFound={found} />);
    expect(found).not.toHaveBeenCalled();
  });

  it("does nothing without the param", () => {
    setSearch("");
    const found = vi.fn();
    render(<Probe onFound={found} />);
    expect(found).not.toHaveBeenCalled();
  });

  it("fires once, so a re-render cannot yank the reader off their tab", () => {
    setSearch("?tab=promotion");
    const found = vi.fn();
    const { rerender } = render(<Probe onFound={found} />);
    rerender(<Probe onFound={found} />);
    rerender(<Probe onFound={found} />);
    expect(found).toHaveBeenCalledTimes(1);
  });
});
