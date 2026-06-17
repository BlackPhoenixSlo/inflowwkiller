/**
 * PremadeForm return-payload mode (W5b-2).
 *
 * When the RuleEditor embeds PremadeForm to build an `auto_posts` / `mass_premade`
 * rule payload, it passes `onComposePayload` + `forcedAccountId`. In that mode the
 * form must:
 *   • render "Use in rule" instead of the enqueue button,
 *   • hand the SAME `{posts|messages:[…]}` payload back via the callback, and
 *   • NEVER hit the /admin/automation/enqueue route (no real send).
 * These pin that contract — the additive return-mode path, not the default
 * enqueue path.
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, screen } from "@testing-library/react";

import { renderWithProviders } from "@/test-utils";
import { PremadeForm } from "@/components/compose/PremadeComposer";

// The enqueue route — assert it's never called in return-payload mode.
const postSpy = vi.fn(async (..._a: unknown[]) => ({ enqueued_job_id: 1 }));

vi.mock("@/lib/relay", () => ({
  relay: { post: (...a: unknown[]) => postSpy(...a), get: vi.fn(async () => []) },
}));
vi.mock("@/contexts/EmployeeContext", () => ({
  useEmployee: () => ({ current: { id: 1 } }),
}));
vi.mock("@/hooks/useAccounts", () => ({
  useActiveAccounts: () => [{ id: "acct1", nickname: "A", color: "#fff" }],
}));
vi.mock("@/hooks/useAllModelsInclude", () => ({
  useAllModelsInclude: () => ({ isIncluded: () => true, toggle: vi.fn() }),
}));
vi.mock("@/hooks/useVaultMedia", () => ({
  useVaultLists: () => ({ data: { list: [] }, isLoading: false }),
}));
vi.mock("@/components/chat/VaultPicker", () => ({ VaultPicker: () => null }));

afterEach(() => {
  cleanup();
  postSpy.mockClear();
});

describe("PremadeForm return-payload mode", () => {
  it("auto_posts: hands back {posts:[…]} and never enqueues", () => {
    const onComposePayload = vi.fn();
    renderWithProviders(
      <PremadeForm
        kind="auto_posts"
        forcedAccountId="acct1"
        onComposePayload={onComposePayload}
      />,
    );

    // No all-models switch + no enqueue button in return mode.
    expect(screen.queryByText(/from ALL models/i)).toBeNull();
    expect(screen.queryByRole("button", { name: /enqueue/i })).toBeNull();

    fireEvent.change(screen.getByPlaceholderText(/post caption/i), {
      target: { value: "hello world" },
    });
    fireEvent.click(screen.getByRole("button", { name: /use in rule/i }));

    expect(onComposePayload).toHaveBeenCalledTimes(1);
    expect(onComposePayload).toHaveBeenCalledWith({ posts: [{ text: "hello world" }] });
    expect(postSpy).not.toHaveBeenCalled();
  });

  it("auto_posts: a PPV price rides into the {posts:[…]} payload", () => {
    const onComposePayload = vi.fn();
    renderWithProviders(
      <PremadeForm
        kind="auto_posts"
        forcedAccountId="acct1"
        onComposePayload={onComposePayload}
      />,
    );

    fireEvent.change(screen.getByPlaceholderText(/post caption/i), {
      target: { value: "locked pic" },
    });
    // The auto_posts-only price field (dollars) → emitted only when >0.
    fireEvent.change(screen.getByPlaceholderText(/0 \(free\)/i), {
      target: { value: "19.99" },
    });
    fireEvent.click(screen.getByRole("button", { name: /use in rule/i }));

    expect(onComposePayload).toHaveBeenCalledWith({
      posts: [{ text: "locked pic", price: 19.99 }],
    });
    expect(postSpy).not.toHaveBeenCalled();
  });

  it("mass_premade: hands back {messages:[…]} and never enqueues", () => {
    const onComposePayload = vi.fn();
    renderWithProviders(
      <PremadeForm
        kind="mass_premade"
        forcedAccountId="acct1"
        onComposePayload={onComposePayload}
      />,
    );

    fireEvent.change(screen.getByPlaceholderText(/broadcast text/i), {
      target: { value: "premade promo" },
    });
    // Price parity with auto_posts: a PPV broadcast rides `price` too
    // (mass_premade → send_mass_message both read it).
    fireEvent.change(screen.getByPlaceholderText(/0 \(free\)/i), {
      target: { value: "9.99" },
    });
    fireEvent.click(screen.getByRole("button", { name: /use in rule/i }));

    expect(onComposePayload).toHaveBeenCalledTimes(1);
    const arg = onComposePayload.mock.calls[0][0] as {
      messages: Array<{ text: string; price?: number }>;
    };
    expect(arg.messages[0].text).toBe("premade promo");
    expect(arg.messages[0].price).toBe(9.99);
    expect(postSpy).not.toHaveBeenCalled();
  });

  it("does nothing (no callback, no enqueue) when there's no content", () => {
    const onComposePayload = vi.fn();
    renderWithProviders(
      <PremadeForm
        kind="auto_posts"
        forcedAccountId="acct1"
        onComposePayload={onComposePayload}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: /use in rule/i }));
    expect(onComposePayload).not.toHaveBeenCalled();
    expect(postSpy).not.toHaveBeenCalled();
  });
});
