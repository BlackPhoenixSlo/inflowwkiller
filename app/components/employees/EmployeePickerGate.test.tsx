/**
 * EmployeePickerGate — the two failures that reached prod from this screen.
 *
 *  1. Clicking a name in the grid threw "employee N not in roster": the grid
 *     rendered off a query of the gate's own while `pick` validated against
 *     an effect-synced copy in context, so the commit that PAINTED the grid
 *     wired every button to an empty roster.
 *  2. The popout flashed the picker over an already-signed-in session: the
 *     "wait quietly" guard tested `query.isLoading`, which a DISABLED query
 *     reports as false — and the query is disabled until /auth/me resolves.
 *
 * Both are wiring bugs between the gate, the context and the roster query,
 * so these stub the NETWORK (relay.get) rather than useRoster — the real
 * hook, the real cache key, one shared entry, exactly like production.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import EmployeePickerGate from "@/components/employees/EmployeePickerGate";
import { EmployeeProvider } from "@/contexts/EmployeeContext";
import { RelayError, type Employee } from "@/lib/relay";
import { makeTestQueryClient, renderWithProviders } from "@/test-utils";

let mockUser: { user: { user_id: string; username: string } | null; loading: boolean };
vi.mock("@/contexts/UserContext", () => ({
  useUser: () => mockUser,
}));
vi.mock("@/contexts/ChatterContext", () => ({
  useChatter: () => ({ chatter: null }),
}));

const relayGet = vi.fn();
vi.mock("@/lib/relay", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/relay")>();
  return { ...actual, relay: { ...actual.relay, get: (...args: unknown[]) => relayGet(...args) } };
});

const SANTAN: Employee = { id: 14, display_name: "Santan", color: "#8b5cf6", is_active: true };
const MIRROR: Employee = { id: 20, display_name: "kingsley1", color: null, is_active: true, chatter_id: "c1" };
const DISABLED: Employee = { id: 27, display_name: "jackob", color: null, is_active: false };
const ROSTER = [SANTAN, MIRROR, DISABLED];

/** Seeds the roster into the cache the way a popout gets it: restored from
 *  localStorage by persistQueryClient, present on the very first commit. */
function renderGate(cached?: Employee[]) {
  const client = makeTestQueryClient();
  if (cached) client.setQueryData(["employees", "all"], { employees: cached });
  return renderWithProviders(
    <EmployeeProvider>
      <EmployeePickerGate><div>APP</div></EmployeePickerGate>
    </EmployeeProvider>,
    client,
  );
}

beforeEach(() => {
  mockUser = { user: { user_id: "u1", username: "owner" }, loading: false };
  relayGet.mockReturnValue(new Promise(() => {}));
  window.localStorage.clear();
});
afterEach(() => { cleanup(); vi.clearAllMocks(); });

describe("EmployeePickerGate", () => {
  it("lets the first click on a painted name through to the app", async () => {
    renderGate(ROSTER);
    expect(screen.queryByText("APP")).not.toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: /Santan/ }));

    expect(await screen.findByText("APP")).toBeInTheDocument();
    expect(window.localStorage.getItem("chatterly:employee_id")).toBe("14");
  });

  it("offers only pickable rows — no chatter mirrors, no disabled", () => {
    renderGate(ROSTER);
    expect(screen.getByRole("button", { name: /Santan/ })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /kingsley1/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /jackob/ })).not.toBeInTheDocument();
  });

  it("waits quietly instead of flashing the picker while /auth/me resolves", () => {
    // No cache, no user yet: the query is DISABLED, so isLoading is false.
    // That is the state the old guard fell through on.
    window.localStorage.setItem("chatterly:employee_id", "14");
    mockUser = { user: null, loading: true };

    renderGate();

    expect(screen.queryByText(/Who's chatting/)).not.toBeInTheDocument();
    expect(screen.queryByText("APP")).not.toBeInTheDocument();
    expect(relayGet).not.toHaveBeenCalled();
  });

  it("renders the app straight through when the pick resolves from cache", () => {
    window.localStorage.setItem("chatterly:employee_id", "14");
    renderGate(ROSTER);
    expect(screen.getByText("APP")).toBeInTheDocument();
  });

  it("surfaces a roster failure with a retry", async () => {
    relayGet.mockRejectedValue(new RelayError(403, { detail: "nope" }));
    renderGate();
    expect(await screen.findByRole("button", { name: /Retry/ })).toBeInTheDocument();
  });
});
