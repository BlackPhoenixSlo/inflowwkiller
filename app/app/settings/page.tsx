"use client";

/**
 * /settings — team management + per-account content (templates, mass,
 * scheduled, audit).
 *
 * Tabs visible depend on the active principal:
 *   • Owner  — every tab.
 *   • Chatter — Templates / Scheduled / Mass messages only. The
 *               owner-only management surfaces (Employees, Chatters,
 *               Transfer, Audit) are hidden because the relay 403s
 *               their backing endpoints under a chatter session.
 *
 * The tab's data fetch still runs server-side authorisation — this
 * filter is UI hygiene, not the gate.
 */

import { useCallback, useState } from "react";

import EmployeesTab from "@/components/settings/EmployeesTab";
import ChattersTab from "@/components/settings/ChattersTab";
import AuditTab from "@/components/settings/AuditTab";
import TemplatesTab from "@/components/settings/TemplatesTab";
import ScheduledTab from "@/components/settings/ScheduledTab";
import MassMessagesTab from "@/components/settings/MassMessagesTab";
import AutoStoriesTab from "@/components/settings/AutoStoriesTab";
import RestrictionsTab from "@/components/settings/RestrictionsTab";
import TransferTab from "@/components/settings/TransferTab";
import LanesTab from "@/components/settings/LanesTab";
import { useTabParam } from "@/hooks/useTabParam";
import { useUser } from "@/contexts/UserContext";
import { useChatter } from "@/contexts/ChatterContext";
import { cn } from "@/lib/utils";

type Tab = "employees" | "chatters" | "templates" | "scheduled" | "mass" | "stories" | "restrictions" | "audit" | "transfer" | "lanes";

interface TabDef {
  key: Tab;
  label: string;
  // Tabs hidden from chatter-only sessions. The backing endpoints
  // (/admin/employees, /admin/chatters, /admin/audit, transfer) all
  // 403 chatters anyway; this just trims the UI noise.
  chatterHidden?: boolean;
}

const TABS: TabDef[] = [
  { key: "employees", label: "Employees",     chatterHidden: true },
  { key: "chatters",  label: "Chatters",      chatterHidden: true },
  { key: "templates", label: "Templates" },
  { key: "scheduled", label: "Scheduled" },
  { key: "mass",      label: "Mass messages" },
  { key: "stories",   label: "Auto stories", chatterHidden: true },
  { key: "restrictions", label: "Restrictions" },
  { key: "transfer",  label: "Transfer model", chatterHidden: true },
  { key: "audit",     label: "Audit log",      chatterHidden: true },
  // Owner-only: it reports process-wide ceilings, which are not a chatter concern.
  { key: "lanes",     label: "Concurrency",   chatterHidden: true },
];

export default function SettingsPage() {
  const { user } = useUser();
  const { chatter } = useChatter();
  const isChatterOnly = !user && !!chatter;

  const visibleTabs = TABS.filter((t) => !(isChatterOnly && t.chatterHidden));
  // Default to the first visible tab so chatters don't land on a
  // hidden "Employees" tab they can't see.
  const [tab, setTab] = useState<Tab>(visibleTabs[0]?.key ?? "templates");
  // Deep link from the help assistant: /settings?tab=stories. Only a VISIBLE
  // tab is honoured, so a chatter following an owner-only link stays on their
  // own default instead of landing on a tab the nav does not show them.
  const isVisible = useCallback(
    (v: string): v is Tab => visibleTabs.some((t) => t.key === v),
    [visibleTabs],
  );
  useTabParam("tab", isVisible, setTab);

  return (
    <div className="max-w-6xl mx-auto p-3 sm:p-6 space-y-5">
      <header>
        <div className="flex items-center gap-3 mb-1">
          <h1 className="text-2xl font-semibold">Settings</h1>
          {/* Get the desktop app. A plain <a>, not next/link — /download is a
           *  static file in public/, outside the router, so client-side nav
           *  would 404. Lived in the top nav before; moved here because the
           *  nav strip fights for width. Hidden below sm — an installer is
           *  no use on a phone. */}
          <a
            href="/download"
            className="hidden sm:flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs text-fg-dim hover:text-fg hover:bg-bg-elev-1 border border-border shrink-0 whitespace-nowrap"
            title="Get the Fastt desktop app for Mac and Windows"
          >
            <span aria-hidden="true">&#8595;</span>
            Desktop app
          </a>
        </div>
        <p className="text-sm text-fg-dim">
          {isChatterOnly
            ? "Templates, scheduled sends, and mass-message tools."
            : "Team roster + audit history."}
        </p>
      </header>

      <nav className="flex items-center gap-1 border-b border-border overflow-x-auto">
        {visibleTabs.map((t) => (
          <TabBtn key={t.key} active={tab === t.key} onClick={() => setTab(t.key)}>
            {t.label}
          </TabBtn>
        ))}
      </nav>

      {tab === "employees" && !isChatterOnly && <EmployeesTab />}
      {tab === "chatters" && !isChatterOnly && <ChattersTab />}
      {tab === "templates" && <TemplatesTab />}
      {tab === "scheduled" && <ScheduledTab />}
      {tab === "mass" && <MassMessagesTab />}
      {tab === "stories" && !isChatterOnly && <AutoStoriesTab />}
      {tab === "restrictions" && <RestrictionsTab />}
      {tab === "transfer" && !isChatterOnly && <TransferTab />}
      {tab === "audit" && !isChatterOnly && <AuditTab />}
      {tab === "lanes" && !isChatterOnly && <LanesTab />}
    </div>
  );
}

function TabBtn({
  active, onClick, children,
}: { active: boolean; onClick: () => void; children: React.ReactNode }) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={cn(
        "px-4 py-2 text-sm border-b-2 -mb-px transition-colors whitespace-nowrap",
        active
          ? "border-accent text-fg"
          : "border-transparent text-fg-dim hover:text-fg",
      )}
    >
      {children}
    </button>
  );
}
