import type { Metadata, Viewport } from "next";
import { Sora } from "next/font/google";

import Providers from "@/components/providers";
import AuthGate from "@/components/auth/AuthGate";
import EmployeePickerGate from "@/components/employees/EmployeePickerGate";
import TopNav from "@/components/TopNav";
import { ImpersonationBanner } from "@/components/ImpersonationBanner";
import { NotificationToaster } from "@/components/NotificationToaster";

import "./globals.css";

const sora = Sora({
  subsets: ["latin"],
  weight: ["300", "400", "500", "600", "700"],
  display: "swap",
  variable: "--font-sora",
});

export const metadata: Metadata = {
  title: {
    default: "Fastt",
    template: "%s · Fastt",
  },
  description: "OnlyFans agency dashboard — unified inbox + automations.",
  // Prevent the desktop OS app store from grabbing the title bar.
  icons: { icon: "/favicon.ico" },
};

// Without `width=device-width` mobile browsers render the page at the
// default ~980px viewport and zoom out — fixed widths + dense desktop
// layouts then become unreadable. `viewportFit: cover` lets the chat
// composer slot under iOS Safari's bottom inset; `safe-area-*` padding
// utilities in components handle the offset back.
//
// Zoom is intentionally locked (`maximum=minimum=1`, `userScalable=false`).
// This is an internal agency tool — chatters spend 8h/day in it on
// company-issued phones, and the dense desktop-mirror layout doesn't
// survive a fractional zoom factor (pinch leaves the message list and
// composer at different effective widths). iOS still honors OS-level
// "Display & Text Size" / Android "Display size" even with this lock,
// so accessibility-zoom users aren't shut out — just ad-hoc pinch is.
export const viewport: Viewport = {
  width: "device-width",
  initialScale: 1,
  maximumScale: 1,
  minimumScale: 1,
  userScalable: false,
  viewportFit: "cover",
};

// Inline script that runs BEFORE React hydrates so the persisted theme
// is on <html> by the time the first paint happens. Without this you'd
// see a flash of dark mode before the client-side useTheme hook caught
// up. Tiny + safe; the worst-case (localStorage unavailable) falls back
// to the default dark theme defined in globals.css.
const THEME_BOOT_SCRIPT = `
try {
  var t = localStorage.getItem('chatterly:theme');
  if (t === 'light') document.documentElement.setAttribute('data-theme', 'light');
} catch (_e) {}
`;

// Visual-viewport tracker — maintains `--vvh` (px) on <html> so the chat
// surface and composer follow the *visible* area instead of the layout
// viewport. Two reasons to do this in an inline script and not a React
// effect:
//   1. The chat page mounts a 100dvh-tall container; if `--vvh` isn't
//      written before first paint there's a one-frame flash where the
//      composer is below the iOS keyboard.
//   2. iOS Safari fires `visualViewport.resize` *during* keyboard
//      animation. Listening from the layout-level keeps the var in
//      sync across route changes (which would otherwise unmount and
//      re-arm an effect every time).
// Cheap: one DOM property write per event, no rAF needed.
const VVH_BOOT_SCRIPT = `
(function(){
  var vv = window.visualViewport;
  if (!vv) return;
  function update(){
    document.documentElement.style.setProperty('--vvh', vv.height + 'px');
  }
  update();
  vv.addEventListener('resize', update);
  vv.addEventListener('scroll', update);
})();
`;

// Pinch-zoom kill switch. The viewport meta already declares
// `maximum-scale=1, user-scalable=no`, which works on Android Chrome —
// but iOS Safari ignores both in some versions / when accessibility
// "page-zoom" is on, leaving pinch-IN still usable. Cancelling the
// `gesturestart` family of events stops the gesture before WebKit
// applies a scale. Same trick used by Slack / Notion / Linear on
// mobile. `passive: false` is required because gesture* events default
// to passive on iOS 14+, which silently no-ops preventDefault().
//
// Also blocks `dblclick` zoom on iOS (double-tap to zoom) via the body
// CSS \`touch-action: manipulation\` that's set in globals.css.
const PINCH_BLOCK_BOOT_SCRIPT = `
(function(){
  function block(e){ e.preventDefault(); }
  document.addEventListener('gesturestart',  block, { passive: false });
  document.addEventListener('gesturechange', block, { passive: false });
  document.addEventListener('gestureend',    block, { passive: false });
})();
`;

// Stale-chunk recovery. Must be an inline script (not a React component)
// because the chunk that most often goes stale after a Turbopack restart
// is the layout chunk itself — if the React tree can't boot, a component-
// based handler never registers its listener and the tab just sits blank
// until the user manually reloads. This script runs while HTML parses,
// before any chunk is requested, so it survives a missing layout chunk.
//
// Same guard semantics as before: ≤3 reloads per session, 60s cooldown,
// bail if sessionStorage is blocked (better a blank tab than a 1000 req/s
// reload loop).
const CHUNK_ERROR_BOOT_SCRIPT = `
(function(){
  var GUARD='chatterly:chunk-reload-attempted',COUNT='chatterly:chunk-reload-count',MAX=3,COOL=60000;
  function stale(e){if(!e)return false;if(e.name==='ChunkLoadError')return true;var m=e.message||'';
    return m.indexOf('Loading chunk')>=0||m.indexOf('Failed to fetch dynamically imported module')>=0||m.indexOf('error loading dynamically imported module')>=0;}
  function reload(){try{var s=sessionStorage.getItem(GUARD);if(s&&Date.now()-Number(s)<COOL)return;
    var c=Number(sessionStorage.getItem(COUNT)||'0');if(c>=MAX){console.warn('[chunk-reloader] reload cap reached');return;}
    sessionStorage.setItem(GUARD,String(Date.now()));sessionStorage.setItem(COUNT,String(c+1));}catch(_){return;}
    location.reload();}
  addEventListener('error',function(ev){if(stale(ev.error)||stale({message:ev.message}))reload();});
  addEventListener('unhandledrejection',function(ev){if(stale(ev.reason))reload();});
})();
`;

export default function RootLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  // The font variable lets us reference --font-sora from Tailwind's
  // `font-sans` token (configured in globals.css @theme).
  return (
    <html lang="en" className={sora.variable} suppressHydrationWarning>
      <body className="min-h-screen bg-bg text-fg antialiased" suppressHydrationWarning>
        {/* This inline script runs synchronously before React hydrates, so
         *  the persisted theme is on <html data-theme=…> by the first paint.
         *  Lives in <body> rather than <head> because Next App Router's
         *  <head> rendering is opinionated and inline scripts there can
         *  end up unreachable in dev mode. */}
        <script dangerouslySetInnerHTML={{ __html: THEME_BOOT_SCRIPT }} />
        <script dangerouslySetInnerHTML={{ __html: VVH_BOOT_SCRIPT }} />
        <script dangerouslySetInnerHTML={{ __html: PINCH_BLOCK_BOOT_SCRIPT }} />
        <script dangerouslySetInnerHTML={{ __html: CHUNK_ERROR_BOOT_SCRIPT }} />
        <Providers>
          <AuthGate>
            <EmployeePickerGate>
              <ImpersonationBanner />
              <TopNav />
              {children}
              <NotificationToaster />
            </EmployeePickerGate>
          </AuthGate>
        </Providers>
      </body>
    </html>
  );
}
