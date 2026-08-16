/**
 * EmployeeContext — the roster a consumer renders must be the roster it
 * picks from, on the very first render.
 *
 * The roster used to be provider state that EmployeePickerGate copied in
 * via useEffect while rendering its buttons off a query of its own. That
 * left one commit — the one that PAINTS the picker, from the persisted
 * React Query cache — in which every visible button was wired to a `pick`
 * closed over an empty roster. A click landing there threw "employee N
 * not in roster" past the ErrorBoundary (React re-throws event-handler
 * errors to window), so the click silently did nothing. Seen in prod on a
 * chat popout; employee 14 was active the whole time.
 *
 * Deriving `roster` from useRoster() during render removes that commit —
 * pinned here by picking with the FIRST render's context value.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup } from "@testing-library/react";

import { EmployeeProvider, useEmployee } from "@/contexts/EmployeeContext";
import type { Employee } from "@/lib/relay";
import { makeTestQueryClient, renderWithProviders } from "@/test-utils";

vi.mock("@/contexts/UserContext", () => ({
  useUser: () => ({ user: { user_id: "u1", username: "owner" }, loading: false }),
}));

const relayGet = vi.fn(() => new Promise(() => {}));
vi.mock("@/lib/relay", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/relay")>();
  return { ...actual, relay: { ...actual.relay, get: () => relayGet() } };
});

const SANTAN: Employee = { id: 14, display_name: "Santan", color: "#8b5cf6", is_active: true };
const DISABLED: Employee = { id: 27, display_name: "jackob", color: "#f59e0b", is_active: false };

type Ctx = ReturnType<typeof useEmployee>;

/** Renders the provider over a pre-seeded cache (a popout's restored
 *  localStorage snapshot) and hands back the context value captured on the
 *  FIRST render — the same closure a button painted in that commit owns. */
function renderProvider(cached?: Employee[]) {
  const client = makeTestQueryClient();
  if (cached) client.setQueryData(["employees", "all"], { employees: cached });

  const renders: Ctx[] = [];
  function Probe() {
    renders.push(useEmployee());
    return null;
  }
  renderWithProviders(<EmployeeProvider><Probe /></EmployeeProvider>, client);
  return { first: renders[0], latest: () => renders[renders.length - 1] };
}

beforeEach(() => { window.localStorage.clear(); });
afterEach(() => { cleanup(); vi.clearAllMocks(); });

describe("EmployeeContext", () => {
  it("exposes the cached roster on the first render", () => {
    const { first } = renderProvider([SANTAN, DISABLED]);
    expect(first.roster).toEqual([SANTAN, DISABLED]);
  });

  it("resolves a pick made with the first render's closure", () => {
    const { first, latest } = renderProvider([SANTAN, DISABLED]);
    // This is the click that used to throw: the handler wired up in the
    // commit that painted the list, before any effect had run.
    act(() => first.pick(SANTAN));
    expect(latest().current).toEqual(SANTAN);
    expect(window.localStorage.getItem("chatterly:employee_id")).toBe("14");
  });

  it("keeps disabled rows and mirrors in the roster but never as `current`", () => {
    window.localStorage.setItem("chatterly:employee_id", "27");
    const { latest } = renderProvider([SANTAN, DISABLED]);
    expect(latest().roster).toContain(DISABLED);
    expect(latest().pickedId).toBe(27);
    expect(latest().current).toBeNull();
  });

  it("clear() drops the pick and the stored id", () => {
    const { first, latest } = renderProvider([SANTAN, DISABLED]);
    act(() => first.pick(SANTAN));
    act(() => latest().clear());
    expect(latest().current).toBeNull();
    expect(window.localStorage.getItem("chatterly:employee_id")).toBeNull();
  });

  it("exposes an empty roster before the query resolves", () => {
    const { first } = renderProvider();
    expect(first.roster).toEqual([]);
    expect(first.current).toBeNull();
  });
});
