"use client";

/**
 * RemoteChallengePopup — the human-in-the-loop surface for a served remote
 * browser login/refresh job (backend: service/remote_login.py).
 *
 * It polls GET /admin/login/remote/{id} for state, opens an EventSource on
 * .../screen (JPEG screencast over SSE), paints each frame to a canvas sized
 * to the browser's CSS viewport, and replays the operator's mouse / wheel /
 * key input back to the real browser via POST .../input (mapped 1:1). When
 * OnlyFans asks for a 2FA code the popup shows a field that POSTs .../otp.
 *
 * We never solve or bypass the challenge — a person does, exactly as they
 * would in a normal browser. Cancel ends the job.
 *
 * Shared by RemoteLoginCard (fresh login) and AccountsTable (refresh), so it
 * takes only a jobId + callbacks and owns no business logic itself.
 */

import { useQuery } from "@tanstack/react-query";
import { useEffect, useRef, useState, useCallback } from "react";

import { relay } from "@/lib/relay";
import { resolveShareToken } from "@/lib/shareToken";
import { Button } from "@/components/ui/primitives";

export interface RemoteJobStatus {
  id: string;
  kind: "login" | "refresh";
  state: "starting" | "typing" | "challenge" | "otp" | "capturing" | "done" | "error" | "cancelled";
  message: string;
  error: string | null;
  account_id: string | null;
  session_file: string | null;
  frame_w: number;
  frame_h: number;
  has_frame: boolean;
  expires_at: number;
}

type InputEvent =
  | { type: "mousemove" | "mousedown" | "mouseup" | "click"; x: number; y: number; button?: string }
  | { type: "wheel"; deltaX: number; deltaY: number }
  | { type: "key"; key?: string; text?: string; ctrl?: boolean; meta?: boolean; alt?: boolean };

const TERMINAL = new Set(["done", "error", "cancelled"]);

export default function RemoteChallengePopup({
  jobId,
  onClose,
  onDone,
}: {
  jobId: string;
  onClose: () => void;
  onDone: (status: RemoteJobStatus) => void;
}) {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const pendingRef = useRef<InputEvent[]>([]);
  const lastMoveRef = useRef(0);
  const [otp, setOtp] = useState("");
  const doneFiredRef = useRef(false);

  const statusQ = useQuery<RemoteJobStatus>({
    queryKey: ["remote-login", jobId],
    queryFn: () => relay.get<RemoteJobStatus>(`/admin/login/remote/${encodeURIComponent(jobId)}`),
    refetchInterval: (q) => (q.state.data && TERMINAL.has(q.state.data.state) ? false : 1000),
  });
  const status = statusQ.data;

  // Fire onDone exactly once when the job reaches `done`.
  useEffect(() => {
    if (status && status.state === "done" && !doneFiredRef.current) {
      doneFiredRef.current = true;
      onDone(status);
    }
  }, [status, onDone]);

  // ── Screencast (SSE → canvas) ──
  useEffect(() => {
    const tok = resolveShareToken();
    const url =
      `/admin/login/remote/${encodeURIComponent(jobId)}/screen` +
      (tok ? `?t=${encodeURIComponent(tok)}` : "");
    const es = new EventSource(url, { withCredentials: true });
    es.onmessage = (ev) => {
      try {
        const { jpeg_b64, w, h } = JSON.parse(ev.data) as { jpeg_b64: string; w: number; h: number };
        if (!jpeg_b64) return;
        const canvas = canvasRef.current;
        if (!canvas) return;
        const img = new Image();
        img.onload = () => {
          if (canvas.width !== w) canvas.width = w;
          if (canvas.height !== h) canvas.height = h;
          const ctx = canvas.getContext("2d");
          if (ctx) ctx.drawImage(img, 0, 0, w, h);
        };
        img.src = `data:image/jpeg;base64,${jpeg_b64}`;
      } catch {
        /* ignore malformed frame */
      }
    };
    es.onerror = () => { /* EventSource auto-reconnects; nothing to do */ };
    return () => es.close();
  }, [jobId]);

  // ── Flush buffered input events to the backend every 60ms ──
  useEffect(() => {
    const flush = () => {
      if (pendingRef.current.length === 0) return;
      const events = pendingRef.current;
      pendingRef.current = [];
      relay
        .post(`/admin/login/remote/${encodeURIComponent(jobId)}/input`, { events })
        .catch(() => { /* a dropped input batch is not fatal */ });
    };
    const h = window.setInterval(flush, 60);
    return () => { window.clearInterval(h); flush(); };
  }, [jobId]);

  const toFrameCoords = useCallback((e: React.MouseEvent) => {
    const canvas = canvasRef.current;
    if (!canvas) return { x: 0, y: 0 };
    const rect = canvas.getBoundingClientRect();
    const sx = canvas.width / rect.width;
    const sy = canvas.height / rect.height;
    return { x: Math.round((e.clientX - rect.left) * sx), y: Math.round((e.clientY - rect.top) * sy) };
  }, []);

  const push = (ev: InputEvent) => pendingRef.current.push(ev);
  const btn = (e: React.MouseEvent) => (e.button === 2 ? "right" : e.button === 1 ? "middle" : "left");

  const interactive = status ? ["challenge", "typing", "otp", "capturing"].includes(status.state) : false;

  const cancel = () =>
    relay.post(`/admin/login/remote/${encodeURIComponent(jobId)}/cancel`).catch(() => {});

  const submitOtp = () => {
    if (!otp.trim()) return;
    relay.post(`/admin/login/remote/${encodeURIComponent(jobId)}/otp`, { code: otp.trim() }).catch(() => {});
    setOtp("");
  };

  const w = status?.frame_w ?? 1366;
  const h = status?.frame_h ?? 900;

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4"
      role="dialog"
      aria-modal="true"
    >
      <div className="bg-bg border border-border rounded-xl shadow-xl max-w-[95vw] max-h-[95vh] flex flex-col overflow-hidden">
        <div className="flex items-center justify-between gap-3 px-4 py-2.5 border-b border-border">
          <div className="flex items-center gap-2 text-sm">
            <span className="font-semibold">Browser login</span>
            <span className="text-fg-dim">· {status?.state ?? "…"}</span>
            {status?.message && <span className="text-fg-dim text-xs">— {status.message}</span>}
          </div>
          <div className="flex items-center gap-2">
            <Button size="sm" variant="ghost" onClick={cancel}>Cancel</Button>
            <Button size="sm" variant="ghost" onClick={onClose}>Close</Button>
          </div>
        </div>

        {status?.state === "otp" && (
          <div className="flex items-center gap-2 px-4 py-2 border-b border-border bg-bg-elev-1/40">
            <span className="text-xs text-fg-dim">Enter the 2FA code OnlyFans sent:</span>
            <input
              value={otp}
              onChange={(e) => setOtp(e.target.value.replace(/[^0-9]/g, ""))}
              onKeyDown={(e) => { if (e.key === "Enter") submitOtp(); }}
              inputMode="numeric"
              className="bg-bg border border-border rounded px-2 py-1 text-sm w-28 font-mono"
              placeholder="123456"
            />
            <Button size="sm" onClick={submitOtp}>Submit</Button>
          </div>
        )}

        {status && TERMINAL.has(status.state) ? (
          <div className="p-6 text-sm">
            {status.state === "done" && (
              <div className="text-ok">Session captured for account {status.account_id}.</div>
            )}
            {status.state === "error" && (
              <div className="text-err whitespace-pre-wrap">Login failed: {status.error}</div>
            )}
            {status.state === "cancelled" && <div className="text-fg-dim">Cancelled.</div>}
            <div className="pt-4"><Button size="sm" onClick={onClose}>Done</Button></div>
          </div>
        ) : (
          <div className="overflow-auto p-2">
            <canvas
              ref={canvasRef}
              width={w}
              height={h}
              tabIndex={0}
              className="block max-w-full border border-border bg-bg-elev-1 cursor-crosshair outline-none"
              style={{ width: Math.min(w, 1000), height: "auto", aspectRatio: `${w} / ${h}` }}
              onContextMenu={(e) => e.preventDefault()}
              onMouseMove={(e) => {
                if (!interactive) return;
                const now = Date.now();
                if (now - lastMoveRef.current < 40) return; // throttle moves
                lastMoveRef.current = now;
                const { x, y } = toFrameCoords(e);
                push({ type: "mousemove", x, y });
              }}
              onMouseDown={(e) => { if (!interactive) return; const { x, y } = toFrameCoords(e); push({ type: "mousedown", x, y, button: btn(e) }); }}
              onMouseUp={(e) => { if (!interactive) return; const { x, y } = toFrameCoords(e); push({ type: "mouseup", x, y, button: btn(e) }); }}
              onClick={(e) => { if (!interactive) return; const { x, y } = toFrameCoords(e); push({ type: "click", x, y, button: btn(e) }); canvasRef.current?.focus(); }}
              onWheel={(e) => { if (!interactive) return; push({ type: "wheel", deltaX: e.deltaX, deltaY: e.deltaY }); }}
              onKeyDown={(e) => {
                if (!interactive) return;
                e.preventDefault();
                const printable = e.key.length === 1 && !e.ctrlKey && !e.metaKey && !e.altKey;
                push(printable
                  ? { type: "key", text: e.key }
                  : { type: "key", key: e.key, ctrl: e.ctrlKey, meta: e.metaKey, alt: e.altKey });
              }}
            />
            <p className="text-[11px] text-fg-dim px-1 pt-1">
              Click the canvas, then solve the verification exactly as you would in a normal
              browser. Your mouse and keyboard are sent to the real session. We never solve it for you.
            </p>
          </div>
        )}
      </div>
    </div>
  );
}
