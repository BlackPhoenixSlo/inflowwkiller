import type { Metadata } from "next";

// The nav says Stuff; the browser tab should agree. The ROUTE stays
// /messages — see the page's docstring for why the address didn't move.
export const metadata: Metadata = { title: "Stuff" };

export default function Layout({ children }: { children: React.ReactNode }) {
  return children;
}
