"use client";

/**
 * AuthGate — wraps the authed app shell. If no principal is signed in,
 * render the landing page (or the chatter-login page when the URL is
 * /chatter-login).
 *
 * Two principals participate:
 *   • User cookie (owner / founder) — owner perspective wins everywhere.
 *   • Chatter cookie — second-class principal that drives the picker via
 *     ChatterContext when the User cookie is absent.
 *
 * /login renders <Landing> always (the form self-redirects on submit).
 * /chatter-login renders its own page always (the form there self-
 * redirects to "/"). All other routes: gate behind one of the two
 * principals; otherwise render <Landing> in place so the URL stays
 * stable and the post-login redirect target is whatever the user asked.
 */

import { usePathname } from "next/navigation";

import { useUser } from "@/contexts/UserContext";
import { useChatter } from "@/contexts/ChatterContext";
import Landing from "@/components/auth/Landing";

export default function AuthGate({ children }: { children: React.ReactNode }) {
  const { user, loading: userLoading } = useUser();
  const { chatter, loading: chatterLoading } = useChatter();
  const pathname = usePathname();

  // Self-rendering auth pages: render their own children regardless.
  if (pathname === "/login" || pathname === "/chatter-login") return <>{children}</>;
  // Wait for BOTH probes to settle so we don't flash <Landing> before
  // chatter detection completes (or vice versa).
  if (userLoading || chatterLoading) return null;
  if (user || chatter) return <>{children}</>;
  return <Landing />;
}
