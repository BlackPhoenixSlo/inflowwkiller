"use client";

/**
 * TopNav — thin nav strip. Pinned at the top of every page inside the
 * EmployeePickerGate. Shows the current employee + scope + a few links
 * to the surfaces that actually exist (home + setup). Adds more entries
 * as Phase B/C ship Inbox / Vault / etc.
 */

import Link from "next/link";
import dynamic from "next/dynamic";
import { usePathname } from "next/navigation";
import { useEffect, useRef, useState } from "react";

import { useEmployee } from "@/contexts/EmployeeContext";
import { useUser } from "@/contexts/UserContext";
import { useChatter } from "@/contexts/ChatterContext";
import { useTheme } from "@/hooks/useTheme";
import { cn } from "@/lib/utils";
import ScopeSwitcher from "@/components/ScopeSwitcher";
import { NotificationBell } from "@/components/NotificationBell";
import { ErrorBadge } from "@/components/ErrorBadge";

// PostComposer + MassMessageComposer mount in TopNav (so they're on every
// page), but the user only sees them when they pick "New post" / "Mass
// message" from the +New dropdown. Statically importing them pulled the
// vault picker + media tray cascade into the initial bundle on every
// route; dynamic import + render-gated keeps the chunk off boot.
const PostComposer = dynamic(
  () => import("@/components/compose/PostComposer").then((m) => m.PostComposer),
  { ssr: false },
);
const MassMessageComposer = dynamic(
  () => import("@/components/compose/MassMessageComposer").then((m) => m.MassMessageComposer),
  { ssr: false },
);
const PremadeComposer = dynamic(
  () => import("@/components/compose/PremadeComposer").then((m) => m.PremadeComposer),
  { ssr: false },
);
const NudgeComposer = dynamic(
  () => import("@/components/compose/NudgeComposer").then((m) => m.NudgeComposer),
  { ssr: false },
);

interface NavLink {
  href: string;
  label: string;
  // Surfaces hidden from a chatter-only session. Owner-only routes
  // (Setup) and management dashboards live here. The chatter still has
  // the URL — if they type it, the route's own data fetches will 403
  // — but the nav strip won't advertise them.
  chatterHidden?: boolean;
}

const LINKS: NavLink[] = [
  { href: "/",          label: "Home"     },
  { href: "/inbox",     label: "Inbox"    },
  { href: "/messages",  label: "Messages" },
  { href: "/stats",       label: "Stats"    },
  { href: "/automations", label: "Automations", chatterHidden: true },
  { href: "/vault",       label: "Vault",       chatterHidden: true },
  { href: "/setup",       label: "Setup",    chatterHidden: true },
  { href: "/settings",    label: "Settings" },
];

export default function TopNav() {
  const { current, clear } = useEmployee();
  const { user, logout } = useUser();
  const { chatter, logout: chatterLogout } = useChatter();
  const pathname = usePathname();
  const { theme, toggle } = useTheme();

  // Chatter-only session: hide owner-only nav surfaces (Setup,
  // employee picker chip). The User cookie always wins precedence, so
  // we only narrow the nav when no User is signed in. Matches the
  // server-side admin-gate which 403s /admin/* for chatter sessions.
  const isChatterOnly = !user && !!chatter;
  const visibleLinks = LINKS.filter((l) => !(isChatterOnly && l.chatterHidden));

  const [composeOpen, setComposeOpen] = useState(false);
  const [postOpen, setPostOpen] = useState(false);
  const [massOpen, setMassOpen] = useState(false);
  const [massOnlineOpen, setMassOnlineOpen] = useState(false);
  const [premadeMassOpen, setPremadeMassOpen] = useState(false);
  const [nudgeOpen, setNudgeOpen] = useState(false);
  const [mobileNavOpen, setMobileNavOpen] = useState(false);
  const composeRef = useRef<HTMLDivElement | null>(null);
  const mobileNavRef = useRef<HTMLDivElement | null>(null);

  // Close the dropdown when clicking outside or pressing Esc.
  useEffect(() => {
    if (!composeOpen) return;
    const onClick = (e: MouseEvent) => {
      if (!composeRef.current?.contains(e.target as Node)) setComposeOpen(false);
    };
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") setComposeOpen(false); };
    document.addEventListener("mousedown", onClick);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onClick);
      document.removeEventListener("keydown", onKey);
    };
  }, [composeOpen]);

  // Same outside-click / Esc behaviour for the mobile hamburger sheet.
  useEffect(() => {
    if (!mobileNavOpen) return;
    const onClick = (e: MouseEvent) => {
      if (!mobileNavRef.current?.contains(e.target as Node)) setMobileNavOpen(false);
    };
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") setMobileNavOpen(false); };
    document.addEventListener("mousedown", onClick);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onClick);
      document.removeEventListener("keydown", onKey);
    };
  }, [mobileNavOpen]);

  // Auto-close the mobile sheet on route change — without this, picking
  // a link leaves the sheet hovering over the new page.
  useEffect(() => { setMobileNavOpen(false); }, [pathname]);

  return (
    <header className="border-b border-border bg-panel/80 backdrop-blur sticky top-0 z-30">
      <div className="max-w-7xl mx-auto px-3 lg:px-6 h-14 flex items-center gap-2 lg:gap-6">
        {/* Hamburger — visible below lg (1024px). Phones in BOTH portrait
         *  and landscape fall below this breakpoint (iPhone Pro Max land
         *  ≈ 956px CSS), so we get a clean compact bar with no wrap, and
         *  the link list lives in the slide-down sheet. */}
        <button
          type="button"
          onClick={() => setMobileNavOpen((v) => !v)}
          className="lg:hidden w-9 h-9 -ml-1 grid place-items-center rounded-lg text-fg-dim hover:text-fg hover:bg-bg-elev-1 shrink-0"
          title="Menu"
          aria-label="Open menu"
          aria-expanded={mobileNavOpen}
        >
          <span aria-hidden className="text-lg leading-none">≡</span>
        </button>

        <Link href="/" className="font-semibold text-fg tracking-tight shrink-0">
          Fastt
        </Link>

        <nav className="hidden lg:flex items-center gap-1">
          {visibleLinks.map((l) => (
            <Link
              key={l.href}
              href={l.href}
              className={cn(
                "px-3 py-1.5 rounded-lg text-sm transition-colors whitespace-nowrap",
                pathname === l.href
                  ? "bg-bg-elev-1 text-fg"
                  : "text-fg-dim hover:text-fg hover:bg-bg-elev-1/50",
              )}
            >
              {l.label}
            </Link>
          ))}
        </nav>

        <div className="ml-auto flex items-center gap-1.5 lg:gap-3">
          {/* Compose dropdown — opens either New post or New mass message. */}
          <div className="relative shrink-0" ref={composeRef}>
            <button
              type="button"
              onClick={() => setComposeOpen((v) => !v)}
              className="px-2.5 lg:px-3 py-1.5 rounded-lg text-sm bg-accent text-white hover:bg-accent-hover font-medium whitespace-nowrap"
              title="Compose"
            >
              <span className="lg:hidden">+</span>
              <span className="hidden lg:inline">+ New ▾</span>
            </button>
            {composeOpen && (
              <div className="absolute right-0 mt-1 w-56 bg-panel border border-border rounded-lg shadow-xl py-1 z-40">
                <button
                  type="button"
                  onClick={() => { setComposeOpen(false); setPostOpen(true); }}
                  className="w-full text-left px-3 py-2 text-sm hover:bg-bg-elev-1 flex items-center gap-2"
                >
                  <span>📝</span>
                  <span>
                    <div className="font-medium">New post</div>
                    <div className="text-[10px] text-fg-dim">Public feed post</div>
                  </span>
                </button>
                <button
                  type="button"
                  onClick={() => { setComposeOpen(false); setMassOpen(true); }}
                  className="w-full text-left px-3 py-2 text-sm hover:bg-bg-elev-1 flex items-center gap-2"
                >
                  <span>📣</span>
                  <span>
                    <div className="font-medium">Mass message</div>
                    <div className="text-[10px] text-fg-dim">Broadcast to fans</div>
                  </span>
                </button>
                <button
                  type="button"
                  onClick={() => { setComposeOpen(false); setMassOnlineOpen(true); }}
                  className="w-full text-left px-3 py-2 text-sm hover:bg-bg-elev-1 flex items-center gap-2"
                >
                  <span>🟢</span>
                  <span>
                    <div className="font-medium">Mass Online</div>
                    <div className="text-[10px] text-fg-dim">Blast fans online now + filters</div>
                  </span>
                </button>
                <button
                  type="button"
                  onClick={() => { setComposeOpen(false); setPremadeMassOpen(true); }}
                  className="w-full text-left px-3 py-2 text-sm hover:bg-bg-elev-1 flex items-center gap-2"
                >
                  <span>♻️</span>
                  <span>
                    <div className="font-medium">Premade Mass</div>
                    <div className="text-[10px] text-fg-dim">Ready broadcasts — resend + auto-unsend</div>
                  </span>
                </button>
                <button
                  type="button"
                  onClick={() => { setComposeOpen(false); setNudgeOpen(true); }}
                  className="w-full text-left px-3 py-2 text-sm hover:bg-bg-elev-1 flex items-center gap-2"
                >
                  <span>🔔</span>
                  <span>
                    <div className="font-medium">Nudge Online</div>
                    <div className="text-[10px] text-fg-dim">DM a fan when they come online — set up + roll out</div>
                  </span>
                </button>
              </div>
            )}
          </div>
          <ScopeSwitcher />
          <NotificationBell />
          <ErrorBadge />
          <button
            type="button"
            onClick={toggle}
            className="hidden sm:grid w-8 h-8 place-items-center rounded-lg text-sm bg-bg-elev-1 hover:bg-bg-elev-2 border border-border shrink-0"
            title={`Switch to ${theme === "dark" ? "light" : "dark"} mode`}
            aria-label="Toggle theme"
          >
            {theme === "dark" ? "☀" : "☾"}
          </button>
          {/* Owner-side employee picker chip. Chatter sessions don't pick
           *  — the audit pipeline auto-resolves a per-owner mirror
           *  Employee from (chatter, account.owner). Hide to avoid a
           *  meaningless "—" chip in the bar. */}
          {!isChatterOnly && (
            <button
              type="button"
              onClick={clear}
              className="flex items-center gap-2 px-2 lg:px-3 py-1.5 rounded-lg text-sm bg-bg-elev-1 hover:bg-bg-elev-2 border border-border max-w-[8rem] lg:max-w-none shrink-0"
              title="Switch employee"
            >
              <span
                className="w-2 h-2 rounded-full shrink-0"
                style={{ background: current?.color || "#888" }}
              />
              <span className="truncate whitespace-nowrap">{current?.display_name || "—"}</span>
            </button>
          )}
          {user && (
            <button
              type="button"
              onClick={() => { void logout(); }}
              className="hidden sm:inline px-2 lg:px-3 py-1.5 rounded-lg text-xs text-fg-dim hover:text-fg hover:bg-bg-elev-1 border border-border shrink-0"
              title={`Signed in as @${user.username} — log out`}
            >
              @{user.username} · out
            </button>
          )}
          {/* Chatter-only session: parallel logout chip. User cookie wins
           *  precedence — when both are present we only render the User
           *  chip above. */}
          {!user && chatter && (
            <button
              type="button"
              onClick={() => { void chatterLogout(); }}
              className="hidden sm:inline px-2 lg:px-3 py-1.5 rounded-lg text-xs text-fg-dim hover:text-fg hover:bg-bg-elev-1 border border-border shrink-0"
              title={`Chatter @${chatter.username} — log out`}
            >
              @{chatter.username} · out
            </button>
          )}
        </div>
      </div>

      {/* Slide-down nav for everything below lg (1024px). Mirrors the
       *  desktop link row but stacks vertically so it stays readable on
       *  phones and small tablets. Also shows the theme toggle here
       *  since it's hidden in the bar at < sm. */}
      {mobileNavOpen && (
        <div ref={mobileNavRef} className="lg:hidden border-t border-border bg-panel">
          <nav className="flex flex-col px-3 py-2 gap-1">
            {visibleLinks.map((l) => (
              <Link
                key={l.href}
                href={l.href}
                onClick={() => setMobileNavOpen(false)}
                className={cn(
                  "px-3 py-2 rounded-lg text-sm transition-colors",
                  pathname === l.href
                    ? "bg-bg-elev-1 text-fg"
                    : "text-fg-dim hover:text-fg hover:bg-bg-elev-1/50",
                )}
              >
                {l.label}
              </Link>
            ))}
            <button
              type="button"
              onClick={toggle}
              className="sm:hidden mt-1 px-3 py-2 rounded-lg text-sm text-left text-fg-dim hover:text-fg hover:bg-bg-elev-1/50"
            >
              {theme === "dark" ? "☀ Switch to light mode" : "☾ Switch to dark mode"}
            </button>
          </nav>
        </div>
      )}

      {/* Render-gate the dynamic imports: until the user actually opens
       *  one of the composers, the chunk never even fetches. */}
      {postOpen && <PostComposer open={postOpen} onClose={() => setPostOpen(false)} />}
      {massOpen && <MassMessageComposer open={massOpen} onClose={() => setMassOpen(false)} />}
      {massOnlineOpen && (
        <MassMessageComposer open={massOnlineOpen} mode="online" onClose={() => setMassOnlineOpen(false)} />
      )}
      {premadeMassOpen && (
        <PremadeComposer open={premadeMassOpen} kind="mass_premade" onClose={() => setPremadeMassOpen(false)} />
      )}
      {nudgeOpen && (
        <NudgeComposer open={nudgeOpen} onClose={() => setNudgeOpen(false)} />
      )}
    </header>
  );
}
