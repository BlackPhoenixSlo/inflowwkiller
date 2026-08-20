/**
 * useKeyDraft — "blank means leave unchanged" has to survive being TYPED blank.
 *
 * The bug this pins: `save()` used to submit the draft verbatim, and a field the
 * user typed into and then backspaced empty sits in that draft as "". The server
 * reads "" as "clear this key", so pasting a wrong key and undoing it DELETED the
 * stored one — while the card said "Saved" and its own copy promised that a blank
 * field keeps the stored value.
 *
 * On the agency-keys card that is not cosmetic: a deleted key fails every account
 * that owner runs closed (llm_client._tenant_api_key raises), and the only signal
 * is a server-side warning nobody is watching at the time.
 *
 * `clear(name)` stays the one path allowed to send "".
 */
import { describe, expect, it, vi } from "vitest";
import { act, renderHook } from "@testing-library/react";

import { useKeyDraft } from "@/hooks/useKeyDraft";

function setup() {
  const submit = vi.fn().mockResolvedValue({});
  const hook = renderHook(() => useKeyDraft(submit, "Saved"));
  return { submit, hook };
}

describe("useKeyDraft", () => {
  it("submits only fields that carry a value", async () => {
    const { submit, hook } = setup();
    act(() => hook.result.current.setField("deepseek", "sk-real"));
    act(() => hook.result.current.setField("deepinfra", ""));
    await act(async () => { await hook.result.current.save(); });
    expect(submit).toHaveBeenCalledWith({ deepseek: "sk-real" });
  });

  it("does not delete a key when a touched field is left blank", async () => {
    const { submit, hook } = setup();
    // typed, then backspaced back to empty
    act(() => hook.result.current.setField("deepseek", "sk-oops"));
    act(() => hook.result.current.setField("deepseek", ""));
    expect(hook.result.current.dirty).toBe(false);
    await act(async () => { await hook.result.current.save(); });
    expect(submit).not.toHaveBeenCalled();
  });

  it("treats a whitespace-only field as blank, like the server does", async () => {
    const { submit, hook } = setup();
    // Both servers decide with .strip(), so " " would be stored as "" — i.e. a
    // delete. It must never leave the browser.
    act(() => hook.result.current.setField("deepseek", " "));
    expect(hook.result.current.dirty).toBe(false);
    await act(async () => { await hook.result.current.save(); });
    expect(submit).not.toHaveBeenCalled();
  });

  it("keeps an edit typed while an earlier save was still in flight", async () => {
    let release: (v: unknown) => void = () => {};
    const submit = vi.fn().mockImplementation(
      () => new Promise((res) => { release = res; }),
    );
    const hook = renderHook(() => useKeyDraft(submit, "Saved"));
    act(() => hook.result.current.setField("deepseek", "sk-first"));
    let saving!: Promise<void>;
    act(() => { saving = hook.result.current.save(); });
    // ...operator moves to the next field while the PUT is still open
    act(() => hook.result.current.setField("deepinfra", "sk-second"));
    await act(async () => { release({}); await saving; });
    expect(submit).toHaveBeenCalledWith({ deepseek: "sk-first" });
    expect(hook.result.current.draft.deepinfra).toBe("sk-second");
    expect(hook.result.current.dirty).toBe(true);
  });

  it("still lets clear() send an explicit empty value", async () => {
    const { submit, hook } = setup();
    await act(async () => { await hook.result.current.clear("deepseek"); });
    expect(submit).toHaveBeenCalledWith({ deepseek: "" });
  });

  it("keeps the draft when a save fails, so nothing is silently lost", async () => {
    const submit = vi.fn().mockRejectedValue(new Error("boom"));
    const hook = renderHook(() => useKeyDraft(submit, "Saved"));
    act(() => hook.result.current.setField("deepseek", "sk-real"));
    await act(async () => { await hook.result.current.save(); });
    expect(hook.result.current.draft.deepseek).toBe("sk-real");
    expect(hook.result.current.note).toBe("boom");
  });
});
