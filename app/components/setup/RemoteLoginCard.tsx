"use client";

/**
 * RemoteLoginCard — start a served remote-browser login (Part C).
 *
 * The operator enters the creator's OnlyFans email + password and picks (or
 * pastes) the proxy to route through. We POST /admin/login/remote/start; a
 * real headless browser on the server types the credentials and, if OnlyFans
 * shows a Cloudflare / Turnstile / reCAPTCHA check or asks for 2FA, the
 * RemoteChallengePopup streams the page to the operator to solve by hand.
 *
 * The password is sent once and never stored or echoed back — the status the
 * card polls carries no secrets. On success the captured session flows through
 * the SAME pipeline as paste-curl, so we invalidate the same query keys.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { relay, type ProxyMeta, RelayError } from "@/lib/relay";
import { Button, Card, Input } from "@/components/ui/primitives";
import RemoteChallengePopup, { type RemoteJobStatus } from "@/components/setup/RemoteChallengePopup";

interface ProxiesResp {
  proxies: ProxyMeta[];
}

const PASTE_NEW = "__paste_new__";

export default function RemoteLoginCard() {
  const qc = useQueryClient();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [proxyLabel, setProxyLabel] = useState("");
  const [proxyLine, setProxyLine] = useState("");
  const [jobId, setJobId] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);

  const proxiesQ = useQuery<ProxiesResp>({
    queryKey: ["proxies", "list"],
    queryFn: () => relay.get<ProxiesResp>("/admin/proxies"),
    staleTime: 60_000,
  });

  const startM = useMutation<RemoteJobStatus, RelayError, void>({
    mutationFn: () => {
      const body: Record<string, unknown> = { email: email.trim(), password };
      if (proxyLabel === PASTE_NEW) body.proxy = proxyLine.trim();
      else if (proxyLabel) body.proxy_label = proxyLabel;
      return relay.post<RemoteJobStatus>("/admin/login/remote/start", body);
    },
    onSuccess: (job) => {
      setErr(null);
      setPassword(""); // drop the password from React state the moment it's sent
      setJobId(job.id);
    },
    onError: (e) => setErr(e.message),
  });

  function onDone() {
    qc.invalidateQueries({ queryKey: ["accounts"] });
    qc.invalidateQueries({ queryKey: ["proxies"] });
    qc.invalidateQueries({ queryKey: ["drift"] });
    qc.invalidateQueries({ queryKey: ["health-all"] });
  }

  const canStart =
    email.trim() !== "" &&
    password !== "" &&
    (proxyLabel === PASTE_NEW ? proxyLine.trim() !== "" : proxyLabel !== "") &&
    !startM.isPending;

  return (
    <Card className="space-y-4">
      <div>
        <h2 className="text-base font-semibold mb-1">Log in with a browser</h2>
        <p className="text-sm text-fg-dim leading-relaxed">
          A real browser on the server logs the creator in through the chosen proxy, so the
          session is minted from the same IP the relay will sign from. If OnlyFans shows a
          human-verification challenge or asks for a 2FA code, you solve it in a popup — we never
          solve or bypass it.
        </p>
      </div>

      <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
        <div>
          <label className="block text-xs text-fg-dim mb-1">OnlyFans email</label>
          <Input value={email} onChange={(e) => setEmail(e.target.value)} autoComplete="off" placeholder="creator@example.com" />
        </div>
        <div>
          <label className="block text-xs text-fg-dim mb-1">Password (sent once, never stored)</label>
          <Input type="password" value={password} onChange={(e) => setPassword(e.target.value)} autoComplete="new-password" />
        </div>
      </div>

      <div>
        <label className="block text-xs text-fg-dim mb-1">Proxy (egress)</label>
        <select
          value={proxyLabel}
          onChange={(e) => setProxyLabel(e.target.value)}
          className="w-full bg-bg border border-border rounded-lg px-3 py-2 text-sm focus:outline-none focus:border-accent"
        >
          <option value="">— choose a proxy —</option>
          {(proxiesQ.data?.proxies || []).map((p) => (
            <option key={p.label} value={p.label}>{p.label} · {p.host}:{p.port}</option>
          ))}
          <option value={PASTE_NEW}>+ Paste a new proxy…</option>
        </select>
        {proxyLabel === PASTE_NEW && (
          <Input
            className="mt-2 font-mono"
            placeholder="scheme://user:pass@host:port  (or host:port:user:pass)"
            value={proxyLine}
            onChange={(e) => setProxyLine(e.target.value)}
          />
        )}
      </div>

      {err && <div className="text-err text-sm whitespace-pre-wrap">{err}</div>}

      <Button type="button" disabled={!canStart} onClick={() => startM.mutate()}>
        {startM.isPending ? "Starting browser…" : "Start browser login"}
      </Button>

      {jobId && (
        <RemoteChallengePopup
          jobId={jobId}
          onClose={() => setJobId(null)}
          onDone={() => onDone()}
        />
      )}
    </Card>
  );
}
