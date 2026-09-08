"use client";

/**
 * Vault import — drop files in, or paste public Google Drive links, and the
 * relay pushes them into the OnlyFans vault.
 *
 * WHAT THE UI HAS TO BE HONEST ABOUT, because the underlying operation is slow
 * and strange:
 *
 *  - OnlyFans has no "save to vault" API. Each file is attached to a
 *    far-future scheduled post, its vault id read off that post, and the post
 *    deleted. Nothing is ever published and no fan sees anything — but the
 *    import IS creating and deleting real objects, so the panel says so rather
 *    than pretending it is a plain upload.
 *  - OnlyFans throttles post creation to ~1 per 10s, so throughput is ~6
 *    files/minute NO MATTER how small the files or how fast the connection.
 *    A progress bar that implied otherwise would read as broken.
 *  - Big video is re-encoded first (OnlyFans rejects large objects outright),
 *    which can take minutes per file before a single byte is uploaded.
 *
 * So the panel reports phase and per-item state from the server's own run
 * record, and polls only while a run is live. State lives on the relay, not
 * here: a reload, a second tab, or a closed laptop does not lose the run.
 */

import { useEffect, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { relay } from "@/lib/relay";

const POLL_MS = 2500;

type ItemStatus = "pending" | "uploading" | "done" | "failed" | "skipped";

interface RunItem {
  name: string;
  source: "local" | "gdrive";
  size: number | null;
  status: ItemStatus;
  error: string | null;
  vault_id: number | null;
  deduped: boolean | null;
  compressed_from?: number | null;
}

interface RunStatus {
  running: boolean;
  run_id?: string;
  status?: "running" | "done" | "failed" | "interrupted";
  phase?: string;
  error?: string | null;
  total?: number;
  done?: number;
  failed?: number;
  skipped?: number;
  carriers_live?: number;
  items?: RunItem[];
}

const MB = 1024 * 1024;

function fmtSize(bytes: number | null | undefined): string {
  if (!bytes) return "";
  return bytes >= MB ? `${(bytes / MB).toFixed(1)} MB` : `${Math.max(1, Math.round(bytes / 1024))} KB`;
}

/** Plain-language state for one row. Exported for tests. */
export function itemLabel(it: RunItem): string {
  if (it.status === "done" && it.deduped) return "already in vault";
  if (it.status === "done") return `in vault · ${it.vault_id ?? ""}`;
  if (it.status === "uploading") return "uploading…";
  if (it.status === "pending") return "waiting";
  if (it.status === "skipped") return it.error || "skipped";
  return it.error || "failed";
}

/** One line summarising a run, in the words an operator would use. */
export function runSummary(s: RunStatus): string {
  if (!s.run_id) return "No import has run yet.";
  const done = s.done ?? 0;
  const total = s.total ?? 0;
  if (s.running) {
    const phase = s.phase === "collecting" ? "fetching files" : "uploading";
    return `${phase} — ${done} of ${total} done`;
  }
  if (s.status === "interrupted") return "Interrupted — the relay restarted mid-run. Re-run to finish.";
  const bits = [`${done} in vault`];
  if (s.failed) bits.push(`${s.failed} failed`);
  if (s.skipped) bits.push(`${s.skipped} skipped`);
  return bits.join(" · ");
}

export default function VaultImportCard({ listId }: { listId?: number | null }) {
  const qc = useQueryClient();
  const fileRef = useRef<HTMLInputElement>(null);
  const [picked, setPicked] = useState<File[]>([]);
  const [links, setLinks] = useState("");
  const [startError, setStartError] = useState<string | null>(null);
  const settledRef = useRef(false);

  const status = useQuery<RunStatus>({
    queryKey: ["vault-import-status"],
    queryFn: () => relay.get<RunStatus>("/api/of/v2/vault/upload/status"),
    // Self-arming: poll only while a run is live, and override the app-wide
    // 3-day staleTime, which would otherwise serve a cached `running: false`
    // and stop the interval from ever arming.
    refetchInterval: (q) => (q.state.data?.running ? POLL_MS : false),
    staleTime: 0,
    refetchOnWindowFocus: true,
  });

  const s = status.data ?? { running: false };
  const running = !!s.running;

  // When a run finishes, the vault grid is stale — it has new media in it.
  useEffect(() => {
    if (running) {
      settledRef.current = true;
      return;
    }
    if (settledRef.current) {
      settledRef.current = false;
      void qc.invalidateQueries({ queryKey: ["vault"] });
      void qc.invalidateQueries({ queryKey: ["vault-mirror"] });
    }
  }, [running, qc]);

  const start = useMutation({
    mutationFn: async () => {
      const form = new FormData();
      for (const f of picked) form.append("files", f, f.name);
      if (links.trim()) form.append("drive_links", links.trim());
      if (listId != null) form.append("list_id", String(listId));
      return relay.uploadForm("/api/of/v2/vault/upload/batch", form);
    },
    onSuccess: () => {
      setPicked([]);
      setLinks("");
      if (fileRef.current) fileRef.current.value = "";
      setStartError(null);
      void status.refetch();
    },
    onError: (e: unknown) => setStartError(e instanceof Error ? e.message : "Could not start the import"),
  });

  const nothingToDo = picked.length === 0 && !links.trim();
  const items = s.items ?? [];

  return (
    <section className="rounded-lg border border-border bg-panel p-4 space-y-3">
      <header className="space-y-1">
        <h2 className="text-sm font-semibold">Import to vault</h2>
        <p className="text-xs text-fg-dim">
          Files from this computer, or public Google Drive links (a folder link
          works). Large video is re-encoded before upload — OnlyFans refuses
          very large files. Nothing is posted or sent to fans.
        </p>
      </header>

      <div className="space-y-2">
        <input
          ref={fileRef}
          type="file"
          multiple
          accept="image/*,video/*,audio/*"
          disabled={running}
          onChange={(e) => setPicked(Array.from(e.target.files ?? []))}
          className="block w-full text-xs file:mr-3 file:rounded file:border-0 file:bg-bg file:px-3 file:py-1.5 file:text-xs file:text-fg disabled:opacity-50"
        />
        {picked.length > 0 && (
          <p className="text-xs text-fg-dim">
            {picked.length} file{picked.length === 1 ? "" : "s"} ·{" "}
            {fmtSize(picked.reduce((n, f) => n + f.size, 0))}
          </p>
        )}

        <textarea
          value={links}
          disabled={running}
          onChange={(e) => setLinks(e.target.value)}
          rows={2}
          placeholder="https://drive.google.com/drive/folders/…  (one per line)"
          className="w-full rounded border border-border bg-bg px-2 py-1.5 text-xs disabled:opacity-50"
        />
      </div>

      <div className="flex items-center gap-3">
        <button
          type="button"
          disabled={running || nothingToDo || start.isPending}
          onClick={() => start.mutate()}
          className="rounded bg-accent px-3 py-1.5 text-xs font-semibold text-white disabled:opacity-40"
        >
          {running ? "Importing…" : start.isPending ? "Starting…" : "Start import"}
        </button>
        <span className="text-xs text-fg-dim">{runSummary(s)}</span>
      </div>

      {running && (
        <p className="text-xs text-fg-dim">
          OnlyFans allows about one upload every 10 seconds, so this runs at
          roughly 6 files a minute. You can leave this page — the import keeps
          going and this panel picks it back up.
        </p>
      )}

      {(startError || s.error) && (
        <p className="text-xs text-err">{startError || s.error}</p>
      )}

      {!!s.carriers_live && (
        <p className="text-xs text-err">
          {s.carriers_live} scheduled post{s.carriers_live === 1 ? "" : "s"} could not be
          removed. Check Scheduled posts on OnlyFans — they publish on their date.
        </p>
      )}

      {items.length > 0 && (
        <ul className="max-h-56 overflow-y-auto divide-y divide-border text-xs">
          {items.map((it, i) => (
            <li key={`${it.name}-${i}`} className="flex items-baseline justify-between gap-3 py-1">
              <span className="truncate" title={it.name}>{it.name}</span>
              <span
                className={
                  it.status === "failed"
                    ? "shrink-0 text-err"
                    : it.status === "done"
                      ? "shrink-0 text-ok"
                      : "shrink-0 text-fg-dim"
                }
              >
                {it.compressed_from
                  ? `${fmtSize(it.compressed_from)} → ${fmtSize(it.size)} · `
                  : ""}
                {itemLabel(it)}
              </span>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
