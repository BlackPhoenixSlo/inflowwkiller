"use client";

/**
 * CsvExportButton — server-streamed CSV download for the messages list.
 *
 * Why an `<a download>` anchor click, NOT fetch().blob():
 *   • fetch().blob() materializes the whole body in JS memory before
 *     handing it to URL.createObjectURL. At the 50k row cap the file is
 *     50-200 MB; the tab will GC-stall or OOM on a tight machine.
 *   • An anchor `download` click pipes the response straight to disk via
 *     the browser's native downloader, which already handles streamed
 *     `Content-Disposition: attachment`. Zero JS allocation regardless
 *     of file size.
 *
 * Trade-off: we can't read `X-Row-Count` from a native download (the
 * response never enters JS). The backend always emits `X-Row-Count` =
 * the cap (50000), so the toast warning would be a false positive 99%
 * of the time. We rely on the inline `# TRUNCATED` sentinel row in the
 * file instead — Excel/Numbers shows it on the last line.
 */

import { useState } from "react";
import { Download } from "lucide-react";

import { Button } from "@/components/ui/primitives";
import { resolveShareToken } from "@/lib/relay";

import {
  buildPaidMessagesQuery,
  type UsePaidMessagesParams,
} from "@/hooks/usePaidMessages";

interface Props {
  params: UsePaidMessagesParams;
  /** Disabled while date range isn't populated. */
  disabled?: boolean;
}

export default function CsvExportButton({ params, disabled }: Props) {
  const [busy, setBusy] = useState(false);

  const onClick = () => {
    if (busy) return;
    if (!params.from || !params.to) return;
    setBusy(true);
    try {
      const qs = buildPaidMessagesQuery(params, null, { format: "csv" });
      // Inject share-token at the URL level — buildPaidMessagesQuery
      // doesn't know about it (relay.ts owns that), but the native
      // anchor click bypasses the relay wrapper.
      const tok = resolveShareToken();
      const url = tok
        ? `/admin/paid-messages?${qs}&t=${encodeURIComponent(tok)}`
        : `/admin/paid-messages?${qs}`;

      const a = document.createElement("a");
      a.href = url;
      a.rel = "noopener";
      // Empty string lets the server's Content-Disposition pick the name.
      a.download = "";
      document.body.appendChild(a);
      a.click();
      a.remove();
    } finally {
      // The click is synchronous; busy is just a momentary debounce so
      // a double-click doesn't fire two downloads.
      window.setTimeout(() => setBusy(false), 500);
    }
  };

  return (
    <Button
      type="button"
      variant="secondary"
      size="sm"
      onClick={onClick}
      disabled={disabled || busy}
      title="Download filtered messages as CSV (up to 50,000 rows)"
    >
      <Download size={14} className="inline-block mr-1" aria-hidden />
      {busy ? "Starting…" : "Export CSV"}
    </Button>
  );
}
