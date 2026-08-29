/**
 * AssistantWidget — the boundaries this component actually owns.
 *
 * The answers themselves are the server's job (service/tests/test_assistant_api.py
 * covers those). What can only go wrong HERE is the request shape — a `role`
 * field the server must never be handed, an unscoped account fired anyway — and
 * the 200-always contract: a used-up cap comes back as `{answer, error:"cap"}`
 * and has to READ as an answer, because a bot that looks broken exactly when
 * someone needs config help is the failure mode the endpoint exists to avoid.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

// Hoisted with the vi.mock factories below — anything they close over has to
// be, or it is still in its TDZ when the mocked module is first imported.
const { relayPost, scope, employee, roster } = vi.hoisted(() => ({
  relayPost: vi.fn(),
  scope: { accountId: "1001" as string | null },
  employee: { current: { id: 7 } as { id: number } | null },
  roster: { list: [] as Array<{ id: string; nickname?: string | null }> },
}));

vi.mock("@/lib/relay", () => {
  class RelayError extends Error {
    status: number;
    body: unknown;
    constructor(status: number, body: unknown, message?: string) {
      super(message || `relay ${status}`);
      this.status = status;
      this.body = body;
    }
  }
  return { RelayError, relay: { post: (...a: unknown[]) => relayPost(...a) } };
});

vi.mock("@/contexts/ScopeContext", () => ({ useScope: () => scope }));
vi.mock("@/contexts/EmployeeContext", () => ({ useEmployee: () => employee }));
vi.mock("@/hooks/useAccounts", () => ({ useActiveAccounts: () => roster.list }));

import { renderWithProviders } from "@/test-utils";
import { AssistantWidget } from "./AssistantWidget";

/** The widget starts collapsed; every case here works in the open panel. */
async function openPanel() {
  renderWithProviders(<AssistantWidget />);
  const bubble = await screen.findByRole("button", { name: "Help assistant" });
  await userEvent.click(bubble);
  return bubble;
}

async function ask(question: string) {
  await userEvent.type(screen.getByPlaceholderText("Ask about a feature…"), question);
  await userEvent.click(screen.getByRole("button", { name: "Ask" }));
}

beforeEach(() => {
  relayPost.mockReset();
  scope.accountId = "1001";
  employee.current = { id: 7 };
  roster.list = [
    { id: "1001", nickname: "blake" },
    { id: "1002", nickname: "blake" },
  ];
  window.localStorage.clear();
});
afterEach(cleanup);

describe("AssistantWidget", () => {
  it("starts collapsed and remembers being opened", async () => {
    const bubble = await openPanel();
    expect(screen.getByPlaceholderText("Ask about a feature…")).toBeTruthy();
    expect(window.localStorage.getItem("chatterly:assistant_open")).toBe("1");

    await userEvent.click(bubble);
    expect(screen.queryByPlaceholderText("Ask about a feature…")).toBeNull();
    expect(window.localStorage.getItem("chatterly:assistant_open")).toBe("0");
  });

  it("sends {question, account_id} and NO role, scoped to the picked account", async () => {
    relayPost.mockResolvedValue({ answer: "Automations → Auto posts.", error: null });
    await openPanel();
    await ask("how do I set up auto posts?");

    await waitFor(() => expect(relayPost).toHaveBeenCalledTimes(1));
    const [path, body, ctx] = relayPost.mock.calls[0];
    expect(path).toBe("/admin/assistant/ask");
    expect(body).toEqual({ question: "how do I set up auto posts?", account_id: "1001" });
    // The server derives who is asking from the session — a role in the body
    // would be client-supplied theater, so it must not be there at all.
    expect(Object.keys(body as object)).not.toContain("role");
    expect(ctx).toMatchObject({ accountId: "1001", employeeId: 7 });
    // Under a scoped model the switcher IS the picker — the widget must not
    // grow a second one that could disagree with it.
    expect(
      screen.queryByLabelText("Account the help bot answers about"),
    ).toBeNull();
    // The answer renders, and its click path is a real destination rather than
    // a route to read and re-type. AnswerText.test.tsx owns the rendering
    // rules; what matters here is that the widget hands the answer to it.
    const link = await screen.findByRole("link", { name: /Auto posts/ });
    expect(link.getAttribute("href")).toBe("/automations?ready=auto_posts");
  });

  it("renders a handled failure (cap/config/provider) as a normal answer", async () => {
    relayPost.mockResolvedValue({
      answer: "This account's AI budget is used up for today.",
      error: "cap",
    });
    await openPanel();
    await ask("anything");

    expect(
      await screen.findByText("This account's AI budget is used up for today."),
    ).toBeTruthy();
  });

  it("answers in the bubble when the request never reaches a verdict", async () => {
    relayPost.mockRejectedValue(new Error("relay 502"));
    await openPanel();
    await ask("anything");

    expect(await screen.findByText(/Couldn't reach the assistant/)).toBeTruthy();
  });

  it("keeps the composer live while an answer is in flight", async () => {
    // v2: an in-flight answer must not lock the textarea — a second question
    // can be typed and sent, and answers settle to their own turns by id.
    const settles: Array<(v: unknown) => void> = [];
    relayPost.mockImplementation(() => new Promise((res) => settles.push(res)));
    await openPanel();
    await ask("first question");

    const box = screen.getByPlaceholderText("Ask about a feature…");
    expect((box as HTMLTextAreaElement).disabled).toBe(false);
    await ask("second question");
    await waitFor(() => expect(relayPost).toHaveBeenCalledTimes(2));

    // Settle out of order — each answer lands on its own turn.
    settles[1]({ answer: "second answer", error: null });
    settles[0]({ answer: "first answer", error: null });
    expect(await screen.findByText("second answer")).toBeTruthy();
    expect(await screen.findByText("first answer")).toBeTruthy();
  });

  it("unlocks in all-models scope with a visible account picker", async () => {
    // The product's DEFAULT scope is "all models". The composer must work
    // there — the server still gets exactly one account_id, chosen in the
    // inline picker (defaulting to the first live account), never null.
    relayPost.mockResolvedValue({ answer: "ok", error: null });
    scope.accountId = null;
    const bubble = await openPanel();

    const picker = screen.getByLabelText(
      "Account the help bot answers about",
    ) as HTMLSelectElement;
    expect(picker.value).toBe("1001");
    await ask("is my welcome on?");
    await waitFor(() => expect(relayPost).toHaveBeenCalledTimes(1));
    expect(relayPost.mock.calls[0][1]).toMatchObject({ account_id: "1001" });
    expect(relayPost.mock.calls[0][2]).toMatchObject({ accountId: "1001" });

    // Re-picking re-scopes the next question…
    await userEvent.selectOptions(picker, "1002");
    await ask("is my welcome on?");
    await waitFor(() => expect(relayPost).toHaveBeenCalledTimes(2));
    expect(relayPost.mock.calls[1][1]).toMatchObject({ account_id: "1002" });

    // …and the pick survives collapsing the panel: it lives in the gate, so a
    // close/reopen must not silently re-bill to the first account in the list.
    await userEvent.click(bubble);
    await userEvent.click(bubble);
    await ask("is my welcome on?");
    await waitFor(() => expect(relayPost).toHaveBeenCalledTimes(3));
    expect(relayPost.mock.calls[2][1]).toMatchObject({ account_id: "1002" });
  });

  it("gates only when there is no account at all", async () => {
    scope.accountId = null;
    roster.list = [];
    await openPanel();

    expect(screen.queryByPlaceholderText("Ask about a feature…")).toBeNull();
    expect(screen.getByText(/No account to answer about yet/)).toBeTruthy();
    expect(relayPost).not.toHaveBeenCalled();
  });
});
