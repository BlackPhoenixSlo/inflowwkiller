/**
 * useSmartListAudience — the mass composer's Smart-List include/exclude cluster,
 * lifted out of MassMessageComposer. These pin the behaviours a future edit
 * could silently break, and that decide WHO a mass send targets:
 *   • the query is disabled (null account) in all-models / compose-into-rule
 *     modes — segments are per-account and must not leak across a broadcast,
 *   • include and exclude are mutually exclusive (a list is one or the other),
 *   • resolve() de-dupes across lists and short-circuits the empty set, and
 *   • picks reset when the account / all-models scope changes.
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, renderHook } from "@testing-library/react";

import type { SmartList } from "@/hooks/useSmartLists";

// Hoisted so the vi.mock factory can reference them (vitest lifts mocks above
// imports). The hook composes useSmartLists (query) + resolveSmartList (expand).
const { useSmartListsMock, resolveSmartListMock } = vi.hoisted(() => ({
  useSmartListsMock: vi.fn(),
  resolveSmartListMock: vi.fn(),
}));

vi.mock("@/hooks/useSmartLists", () => ({
  useSmartLists: (accountId: string | null) => useSmartListsMock(accountId),
  resolveSmartList: (id: number) => resolveSmartListMock(id),
}));

import { useSmartListAudience } from "@/hooks/useSmartListAudience";

const seg = (id: number, name: string): SmartList => ({
  id, account_id: "acct1", name,
  rules: { match: "all", rules: [] }, created_at: null, updated_at: null,
});
const SAMPLE = [seg(1, "Whales"), seg(2, "Lapsed")];
const RESOLVE_MAP: Record<number, number[]> = { 1: [10, 11, 12], 2: [12, 13] };

afterEach(() => {
  cleanup();
  useSmartListsMock.mockReset();
  resolveSmartListMock.mockReset();
});

function render(props: { accountId: string | null; allModels: boolean; composeMode: boolean }) {
  useSmartListsMock.mockReturnValue({ data: SAMPLE });
  resolveSmartListMock.mockImplementation(async (id: number) => RESOLVE_MAP[id] ?? []);
  return renderHook(
    (p: typeof props) => useSmartListAudience(p.accountId, { allModels: p.allModels, composeMode: p.composeMode }),
    { initialProps: props },
  );
}

describe("useSmartListAudience", () => {
  it("disables the query (null account) in all-models and compose-into-rule modes", () => {
    render({ accountId: "acct1", allModels: false, composeMode: false });
    expect(useSmartListsMock).toHaveBeenLastCalledWith("acct1");

    render({ accountId: "acct1", allModels: true, composeMode: false });
    expect(useSmartListsMock).toHaveBeenLastCalledWith(null);

    render({ accountId: "acct1", allModels: false, composeMode: true });
    expect(useSmartListsMock).toHaveBeenLastCalledWith(null);
  });

  it("keeps include and exclude mutually exclusive", () => {
    const { result } = render({ accountId: "acct1", allModels: false, composeMode: false });

    act(() => result.current.toggleInclude(1));
    expect([...result.current.include]).toEqual([1]);
    expect([...result.current.exclude]).toEqual([]);

    // Excluding a list already included moves it across, not into both.
    act(() => result.current.toggleExclude(1));
    expect([...result.current.include]).toEqual([]);
    expect([...result.current.exclude]).toEqual([1]);

    // And back the other way.
    act(() => result.current.toggleInclude(1));
    expect([...result.current.include]).toEqual([1]);
    expect([...result.current.exclude]).toEqual([]);

    // A second toggle removes it entirely.
    act(() => result.current.toggleInclude(1));
    expect([...result.current.include]).toEqual([]);
  });

  it("resolve() de-dupes across lists and short-circuits the empty set", async () => {
    const { result } = render({ accountId: "acct1", allModels: false, composeMode: false });

    let ids: number[] = [];
    await act(async () => { ids = await result.current.resolve(new Set([1, 2])); });
    expect(ids).toEqual([10, 11, 12, 13]);
    expect(resolveSmartListMock).toHaveBeenCalledTimes(2);

    resolveSmartListMock.mockClear();
    let empty: number[] = [-1];
    await act(async () => { empty = await result.current.resolve(new Set()); });
    expect(empty).toEqual([]);
    expect(resolveSmartListMock).not.toHaveBeenCalled();
  });

  it("resets picks when the account or all-models scope changes", () => {
    const { result, rerender } = render({ accountId: "acct1", allModels: false, composeMode: false });

    act(() => result.current.toggleInclude(1));
    expect([...result.current.include]).toEqual([1]);

    rerender({ accountId: "acct2", allModels: false, composeMode: false });
    expect([...result.current.include]).toEqual([]);
    expect([...result.current.exclude]).toEqual([]);

    act(() => result.current.toggleExclude(2));
    expect([...result.current.exclude]).toEqual([2]);

    rerender({ accountId: "acct2", allModels: true, composeMode: false });
    expect([...result.current.exclude]).toEqual([]);
  });
});
