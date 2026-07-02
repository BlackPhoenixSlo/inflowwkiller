import { describe, it, expect } from "vitest";

import { bucketOf } from "./CompetitorReports";

/**
 * Regression guard for the "Posts = $0.00" bug: paid FEED-post sales are
 * ingested by transaction_ingest.py as `ppv_post` (OF ledger prefixes
 * "Post from " / "Post purchase by "), and post-tips as `tip_post`. Both must
 * land in the "posts" channel, NOT be swallowed by the generic ppv→messages
 * catch-all. Likewise `ppv_stream` belongs in "streams", not "messages".
 */
describe("bucketOf", () => {
  it("routes paid feed-post revenue to posts", () => {
    expect(bucketOf("ppv_post")).toBe("posts");
    expect(bucketOf("tip_post")).toBe("posts");
  });

  it("routes stream revenue to streams", () => {
    expect(bucketOf("ppv_stream")).toBe("streams");
    expect(bucketOf("tip_stream")).toBe("streams");
  });

  it("keeps chat PPV in messages", () => {
    expect(bucketOf("ppv")).toBe("messages");          // WS-pump kind
    expect(bucketOf("ppv_message")).toBe("messages");  // ledger kind
  });

  it("maps subscriptions, rebills and plain tips", () => {
    expect(bucketOf("subscription")).toBe("subscriptions");
    expect(bucketOf("rebill")).toBe("subscriptions");
    expect(bucketOf("tip")).toBe("tips");
  });

  it("falls back to other for null / unknown kinds", () => {
    expect(bucketOf(null)).toBe("other");
    expect(bucketOf("chargeback")).toBe("other");
  });
});
