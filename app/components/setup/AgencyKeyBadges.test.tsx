import { describe, it, expect } from "vitest";
import { needsKey } from "./AgencyKeyBadges";

/**
 * `needs a key` is the badge that sends the founder to paste a credential, so
 * it must fire on exactly the agencies that are costing something — a model
 * that can talk with nothing for it to talk on — and stay quiet otherwise.
 */
describe("needsKey", () => {
  const base = { accounts: 1, live_accounts: 0, blocked_accounts: 0, providers_set: [] };

  it("fires on a live model with no key", () => {
    expect(needsKey({ ...base, live_accounts: 1 })).toBe(true);
  });

  it("stays quiet when a key is already stored", () => {
    expect(needsKey({ ...base, live_accounts: 1, providers_set: ["deepseek"] })).toBe(false);
  });

  it("stays quiet when nothing can talk — a key would buy nothing yet", () => {
    expect(needsKey({ ...base, accounts: 5, live_accounts: 0 })).toBe(false);
  });

  it("stays quiet for an agency whose only live models are BLOCKED", () => {
    // Two owners claim them, so the relay refuses regardless of any key. The
    // blocked badge says so; `needs a key` must not also claim a key helps.
    expect(needsKey({ ...base, live_accounts: 0, blocked_accounts: 2 })).toBe(false);
  });
});
