import { describe, expect, it } from "vitest";

import { itemLabel, runSummary, showsError } from "./VaultImportCard";

/**
 * The two pure formatters, kept separate from the polling component so the
 * wording can be tested without a query client. What matters here is that the
 * panel never overstates what happened: a deduped file is not an upload, an
 * interrupted run is not a finished one, and a failure shows the server's own
 * reason rather than a generic "error".
 */

const base = {
  name: "clip.mov",
  source: "local" as const,
  size: 1_000_000,
  status: "pending" as const,
  error: null,
  vault_id: null,
  deduped: null,
};

describe("itemLabel", () => {
  it("says a deduped file was already there, not that it uploaded", () => {
    expect(itemLabel({ ...base, status: "done", deduped: true, vault_id: 42 }))
      .toBe("already in vault");
  });

  it("shows the vault id for a real upload", () => {
    expect(itemLabel({ ...base, status: "done", deduped: false, vault_id: 42 }))
      .toBe("in vault · 42");
  });

  it("says a still-encoding upload is not yet playable, rather than plain done", () => {
    // The vault id is real and permanent — OnlyFans finishes the encode on its
    // own — but a chatter who sends it right now gets a broken attachment.
    expect(itemLabel({ ...base, status: "done", deduped: false, vault_id: 42,
                       still_transcoding: true }))
      .toBe("in vault · 42 · still processing");
  });

  it("surfaces the server's reason for a skip rather than a generic word", () => {
    expect(itemLabel({ ...base, status: "skipped", error: "465 MB is over the 200 MB limit" }))
      .toBe("465 MB is over the 200 MB limit");
  });

  it("falls back to a plain word when the server gave no reason", () => {
    expect(itemLabel({ ...base, status: "skipped" })).toBe("skipped");
    expect(itemLabel({ ...base, status: "failed" })).toBe("failed");
  });

  it("shows the error for a failure", () => {
    expect(itemLabel({ ...base, status: "failed", error: "claim failed: 504" }))
      .toBe("claim failed: 504");
  });
});

describe("runSummary", () => {
  it("says nothing has run when there is no run", () => {
    expect(runSummary({ running: false })).toBe("No import has run yet.");
  });

  it("names the phase while collecting, since no upload has started yet", () => {
    expect(runSummary({ running: true, run_id: "r", phase: "collecting", done: 0, total: 5 }))
      .toBe("fetching files — 0 of 5 done");
  });

  it("counts progress while uploading", () => {
    expect(runSummary({ running: true, run_id: "r", phase: "uploading", done: 2, total: 5 }))
      .toBe("uploading — 2 of 5 done");
  });

  it("does not report an interrupted run as finished", () => {
    expect(runSummary({ running: false, run_id: "r", status: "interrupted", done: 3, total: 5 }))
      .toContain("Interrupted");
  });

  it("reports failures and skips alongside successes", () => {
    expect(runSummary({ running: false, run_id: "r", status: "done", done: 3, failed: 1, skipped: 2 }))
      .toBe("3 in vault · 1 failed · 2 skipped");
  });

  it("stays quiet about zero failures", () => {
    expect(runSummary({ running: false, run_id: "r", status: "done", done: 3, failed: 0, skipped: 0 }))
      .toBe("3 in vault");
  });
});

describe("showsError", () => {
  const DAY = 24 * 60 * 60 * 1000;
  const now = Date.parse("2026-09-08T12:00:00Z");

  it("shows a fresh failure", () => {
    expect(showsError(
      { running: false, run_id: "r", status: "failed", error: "no disk",
        finished_at: "2026-09-08T11:00:00Z" }, now)).toBe(true);
  });

  it("stops showing a failure from last week on every page load", () => {
    expect(showsError(
      { running: false, run_id: "r", status: "failed", error: "no disk",
        finished_at: new Date(now - 7 * DAY).toISOString() }, now)).toBe(false);
  });

  it("never shows an error banner over a run that succeeded", () => {
    expect(showsError(
      { running: false, run_id: "r", status: "done", error: "stale text",
        finished_at: new Date(now - 60_000).toISOString() }, now)).toBe(false);
  });

  it("has nothing to show when there is no error", () => {
    expect(showsError({ running: false, run_id: "r", status: "failed" }, now)).toBe(false);
  });
});
