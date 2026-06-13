"use client";

/**
 * MessagesFilters — controls for the PPV list.
 *
 * The page owns the date range (DateRangePicker is rendered in the
 * header). Everything else lives here: account picker, employee picker,
 * paid/unpaid/all toggle, and the fan-search input.
 *
 * Fan search debounces to 250ms on the parent's side via the
 * `onFanQueryCommit` callback — we also gate at <2 chars locally so the
 * inline "≥2 chars" hint matches the backend's 400 response.
 */

import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";

import { Button, Input } from "@/components/ui/primitives";
import { useActiveAccounts } from "@/hooks/useAccounts";
import { relay, type Employee } from "@/lib/relay";
import { cn } from "@/lib/utils";

import type { MessageDirection, MessageType, PaidStatus } from "@/hooks/usePaidMessages";

interface EmployeesResp {
  employees: Employee[];
}

interface Props {
  accountId: string | null;
  onAccountChange: (id: string | null) => void;
  employeeId: number | null;
  onEmployeeChange: (id: number | null) => void;
  status: PaidStatus;
  onStatusChange: (s: PaidStatus) => void;
  /** Defaults to true so the Status toggle keeps rendering for PPV tabs;
   *  pass false (e.g. from TipsTab) to hide it where it does nothing. */
  showStatus?: boolean;
  fanQuery: string;
  onFanQueryChange: (q: string) => void;
  /** When omitted, the Type segmented control isn't rendered (PPV tab
   *  doesn't need it). Pass it from AllMessagesTab. */
  type?: MessageType;
  onTypeChange?: (t: MessageType) => void;
  /** Same — Direction only renders when the parent owns the state. */
  direction?: MessageDirection;
  onDirectionChange?: (d: MessageDirection) => void;
}

export default function MessagesFilters({
  accountId,
  onAccountChange,
  employeeId,
  onEmployeeChange,
  status,
  onStatusChange,
  showStatus = true,
  fanQuery,
  onFanQueryChange,
  type,
  onTypeChange,
  direction,
  onDirectionChange,
}: Props) {
  // Backend rule (service/messages.py): Status only meaningful when
  // type !== "free", Employee only meaningful when direction === "out".
  // We mirror that in the UI so the user doesn't see filters that get
  // silently ignored. Side-effect resets (e.g. clear employeeId when
  // direction flips to "in") happen in the parent — this component
  // stays a pure controlled input.
  // `showStatus` (prop, default true) lets a surface with no status
  // concept (tips) drop the toggle entirely.
  const renderStatus = showStatus && type !== "free";
  const employeeDisabled = direction === "in";
  const accounts = useActiveAccounts();
  const empQ = useQuery<EmployeesResp>({
    queryKey: ["employees", "active"],
    queryFn: () => relay.get<EmployeesResp>("/admin/employees?include_disabled=false"),
    staleTime: 5 * 60_000,
    refetchOnWindowFocus: false,
  });

  // Local mirror so typing feels instant; we only push upstream after the
  // string has been stable for 250ms. Reset when the parent string clears.
  const [localFan, setLocalFan] = useState(fanQuery);
  useEffect(() => {
    setLocalFan(fanQuery);
  }, [fanQuery]);
  useEffect(() => {
    const handle = window.setTimeout(() => {
      if (localFan !== fanQuery) onFanQueryChange(localFan);
    }, 250);
    return () => window.clearTimeout(handle);
    // onFanQueryChange is stable from the parent; we intentionally key
    // off the local string only.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [localFan]);

  const showShortHint = localFan.length > 0 && localFan.length < 2;

  return (
    <div className="flex flex-wrap items-center gap-3 p-3 bg-panel border border-border rounded-xl">
      <Field label="Model">
        <select
          value={accountId ?? ""}
          onChange={(e) => onAccountChange(e.target.value || null)}
          className="bg-bg border border-border rounded-lg px-2 py-1.5 text-xs text-fg focus:outline-none focus:border-accent"
        >
          <option value="">All models</option>
          {accounts.map((a) => (
            <option key={a.id} value={a.id}>
              {a.nickname || a.id}
            </option>
          ))}
        </select>
      </Field>

      <Field label="Employee">
        <select
          value={employeeId ?? ""}
          onChange={(e) => onEmployeeChange(e.target.value ? Number(e.target.value) : null)}
          disabled={employeeDisabled}
          title={employeeDisabled ? "Inbound messages have no sending employee" : undefined}
          className="bg-bg border border-border rounded-lg px-2 py-1.5 text-xs text-fg focus:outline-none focus:border-accent disabled:opacity-50 disabled:cursor-not-allowed"
        >
          <option value="">All employees</option>
          {(empQ.data?.employees ?? []).map((e) => (
            <option key={e.id} value={e.id}>
              {e.display_name}
            </option>
          ))}
        </select>
      </Field>

      {type !== undefined && onTypeChange && (
        <Field label="Type">
          <div className="flex items-center gap-1">
            <StatusBtn active={type === "all"} onClick={() => onTypeChange("all")}>All</StatusBtn>
            <StatusBtn active={type === "paid"} onClick={() => onTypeChange("paid")}>Paid</StatusBtn>
            <StatusBtn active={type === "free"} onClick={() => onTypeChange("free")}>Free</StatusBtn>
          </div>
        </Field>
      )}

      {direction !== undefined && onDirectionChange && (
        <Field label="Direction">
          <div className="flex items-center gap-1">
            <StatusBtn active={direction === "any"} onClick={() => onDirectionChange("any")}>All</StatusBtn>
            <StatusBtn active={direction === "out"} onClick={() => onDirectionChange("out")}>Outbound</StatusBtn>
            <StatusBtn active={direction === "in"} onClick={() => onDirectionChange("in")}>Inbound</StatusBtn>
          </div>
        </Field>
      )}

      {renderStatus && (
        <Field label="Status">
          <div className="flex items-center gap-1">
            <StatusBtn active={status === "all"} onClick={() => onStatusChange("all")}>All</StatusBtn>
            <StatusBtn active={status === "paid"} onClick={() => onStatusChange("paid")}>Paid</StatusBtn>
            <StatusBtn active={status === "unpaid"} onClick={() => onStatusChange("unpaid")}>Unpaid</StatusBtn>
          </div>
        </Field>
      )}

      <Field label="Fan">
        <div className="flex flex-col gap-0.5">
          <Input
            value={localFan}
            onChange={(e) => setLocalFan(e.target.value)}
            placeholder="username / display name"
            className="text-xs py-1.5 w-52"
            aria-label="Search fan"
          />
          {showShortHint && (
            <span className="text-[10px] text-warn">≥2 chars</span>
          )}
        </div>
      </Field>
    </div>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className="flex flex-col gap-1">
      <span className="text-[10px] uppercase tracking-wide text-fg-dim">{label}</span>
      {children}
    </label>
  );
}

function StatusBtn({
  active,
  onClick,
  children,
}: {
  active: boolean;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <Button
      type="button"
      variant={active ? "primary" : "secondary"}
      size="sm"
      onClick={onClick}
      className={cn(!active && "border-border")}
    >
      {children}
    </Button>
  );
}
