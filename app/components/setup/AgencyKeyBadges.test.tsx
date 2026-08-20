import { describe, it, expect } from "vitest";
import { needsKey } from "./AgencyKeyBadges";

/**
 * `needs a key` is the badge that sends the founder to paste a credential, so
 * it must fire on exactly the agencies that are costing something — a model
 * that can talk with nothing for it to talk on — and stay quiet otherwise.
 */
describe("needsKey", () => {
  const base = {
    accounts: 1, live_accounts: 0, blocked_accounts: 0,
    providers_set: [] as string[], missing_providers: [] as string[],
  };

  it("fires on a live model with no key", () => {
    expect(needsKey({ ...base, live_accounts: 1, missing_providers: ["deepseek"] })).toBe(true);
  });

  it("stays quiet when the required key is stored", () => {
    expect(needsKey({ ...base, live_accounts: 1, providers_set: ["deepseek"] })).toBe(false);
  });

  it("FIRES on the wrong provider's key — credentials resolve per provider", () => {
    // The agency has a key, so a `providers_set.length === 0` test would call
    // this configured while every chat reply fails closed.
    expect(needsKey({
      ...base, live_accounts: 1,
      providers_set: ["deepinfra"], missing_providers: ["deepseek"],
    })).toBe(true);
  });

  it("falls back to has-no-key when the backend predates missing_providers", () => {
    const legacy = { accounts: 1, live_accounts: 1, blocked_accounts: 0, providers_set: [] };
    expect(needsKey(legacy)).toBe(true);
    expect(needsKey({ ...legacy, providers_set: ["deepseek"] })).toBe(false);
  });

  it("stays quiet when nothing can talk — a key would buy nothing yet", () => {
    expect(needsKey({ ...base, accounts: 5, live_accounts: 0, missing_providers: ["deepseek"] })).toBe(false);
  });

  it("stays quiet for an agency whose only live models are BLOCKED", () => {
    // Two owners claim them, so the relay refuses regardless of any key. The
    // blocked badge says so; `needs a key` must not also claim a key helps.
    expect(needsKey({ ...base, live_accounts: 0, blocked_accounts: 2, missing_providers: ["deepseek"] })).toBe(false);
  });
});
