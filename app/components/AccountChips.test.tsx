/**
 * AccountChips — the one account picker, held to the contract its four call
 * sites (/automations, BrainPanel, TemplatesTab, /vault) each used to own a
 * private copy of.
 *
 * Two of those cases are load-bearing and invisible in the rendered page:
 * the single-account guard, because on a one-model agency EVERY call site
 * renders nothing and a regression here would put a dead one-chip row on four
 * surfaces at once; and the id passed to `onChange`, because the chip is
 * LABELLED by nickname but the callback must carry the account id — handing
 * back the label would scope the panel to an account that does not exist.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";

/** Mutable so each case can vary the roster; hoisted because the mock factory
 *  runs while the module graph is still resolving. */
const roster = vi.hoisted(() => ({ accounts: [] as Array<{ id: string; nickname?: string }> }));
vi.mock("@/hooks/useAccounts", () => ({ useActiveAccounts: () => roster.accounts }));

import { AccountChips } from "@/components/AccountChips";

const TWO = [
  { id: "ACCOUNT_ID", nickname: "ava" },
  { id: "ACCOUNT_ID_2", nickname: "blake" },
];

beforeEach(() => {
  roster.accounts = TWO;
});
afterEach(cleanup);

describe("AccountChips", () => {
  it("renders nothing when there is only one account to pick", () => {
    roster.accounts = [{ id: "ACCOUNT_ID", nickname: "ava" }];
    const { container } = render(<AccountChips accountId="ACCOUNT_ID" onChange={() => {}} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("still renders when the ONE pickable account is not the selected one", () => {
    // The way out of a dead end, and it used to be hidden. `useActiveAccounts`
    // filters on `has_session`, and /vault deliberately keeps pointing at a
    // model whose session drops rather than switching creators in silence — so
    // with two models and the selected one's session dead the roster is length
    // 1, the old `<= 1` shortcut rendered nothing, and the banner told the
    // operator to "pick another model above" over empty space. The remembered
    // id brought the same dead end back on every reload.
    roster.accounts = [{ id: "ACCOUNT_ID_2", nickname: "blake" }];
    render(<AccountChips accountId="ACCOUNT_ID" onChange={() => {}} />);
    expect(screen.getByRole("button", { name: "blake" })).toBeTruthy();
  });

  it("renders nothing when the roster has not loaded", () => {
    roster.accounts = [];
    const { container } = render(<AccountChips accountId={null} onChange={() => {}} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("gives every account a chip, labelled by nickname and falling back to the id", () => {
    roster.accounts = [{ id: "ACCOUNT_ID", nickname: "ava" }, { id: "ACCOUNT_ID_2" }];
    render(<AccountChips accountId="ACCOUNT_ID" onChange={() => {}} />);
    expect(screen.getByRole("button", { name: "ava" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "ACCOUNT_ID_2" })).toBeTruthy();
  });

  it("hands back the account id, not the label it was rendered with", () => {
    const onChange = vi.fn();
    render(<AccountChips accountId="ACCOUNT_ID" onChange={onChange} />);
    screen.getByRole("button", { name: "blake" }).click();
    expect(onChange.mock.calls).toEqual([["ACCOUNT_ID_2"]]);
  });

  it("puts a caller's spacing on the row, never on the chips", () => {
    const { container } = render(
      <AccountChips accountId="ACCOUNT_ID" onChange={() => {}} className="mb-3" />,
    );
    expect(container.firstElementChild?.className).toContain("mb-3");
    for (const b of screen.getAllByRole("button")) expect(b.className).not.toContain("mb-3");
  });
});
