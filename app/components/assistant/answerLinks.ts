/**
 * answerLinks — turn the help bot's click paths into real hrefs.
 *
 * The bot answers in the manual's own words: "Growth → ❤️ Auto-follow",
 * "Automations → Ready-made posts & broadcasts → 🗓️ Auto Posts". Those are
 * exact tab labels, so they can be resolved to a route instead of being read
 * and re-typed by hand.
 *
 * ONE TABLE describes the nav: `SECTIONS` keys off the word the manual writes
 * and carries everything that word implies — where it goes, whether it has a
 * tab strip (and which query key that strip reads), and whether it is worth
 * linking on its own. Keeping href, tabs and bare-linking as three structures
 * keyed by the same word is how they drift apart.
 *
 * THE TAB TABLES ARE COPIES, and that is deliberate. Importing the real `TABS`
 * arrays would drag four page components (and their data hooks) into the
 * widget's bundle for a string map. The cost of the copy is drift, so
 * `answerLinks.test.ts` asserts every key here still exists in the component
 * that owns it — a renamed tab fails the suite instead of silently linking to
 * a default tab.
 *
 * Resolution is BEST-EFFORT and always falls back to the section page: a path
 * whose last segment we cannot name still lands the reader on /growth, which
 * beats not linking at all. What it must never do is invent — an unrecognised
 * word yields no link, so a sentence about "the growth of an account" stays
 * plain text.
 */

/** Lowercase, drop emoji/punctuation, collapse runs — "🗓️ Auto Posts" → "auto posts". */
function norm(s: string): string {
  return s
    .toLowerCase()
    .replace(/[^a-z0-9&'\s-]/g, " ")
    .replace(/\s+/g, " ")
    .trim();
}

/** app/growth/page.tsx TABS. */
export const GROWTH_TABS: Record<string, string> = {
  "smart lists": "smart",
  "trial links": "trial",
  "tracking links": "tracking",
  promotion: "promotion",
  "auto-follow": "auto_follow",
  "auto-follow auto-like": "auto_follow",
  overview: "overview",
};

/** app/messages/page.tsx TABS — the page the nav calls "Stuff". */
export const MESSAGES_TABS: Record<string, string> = {
  ppv: "ppv",
  tips: "tips",
  all: "all",
  customs: "customs",
  // The manual and the tab's own heading both say "Customs owed".
  "customs owed": "customs",
  posts: "posts",
  "my feed": "myfeed",
  "top posts": "top",
};

/** app/settings/page.tsx TABS. */
export const SETTINGS_TABS: Record<string, string> = {
  employees: "employees",
  chatters: "chatters",
  templates: "templates",
  scheduled: "scheduled",
  "mass messages": "mass",
  "auto stories": "stories",
  restrictions: "restrictions",
  "transfer model": "transfer",
  "audit log": "audit",
  concurrency: "lanes",
};

/** components/automations/ReadyMadePanel.tsx TABS. */
export const READY_MADE_TABS: Record<string, string> = {
  "auto posts": "auto_posts",
  "mass premade": "mass_premade",
  "mass funnels": "mass_funnels",
  "nudge online": "nudge_online",
  "mass nudge": "mass_nudge",
  "blast online": "online_blast",
  "reply instant": "instant_reply",
  "auto convo": "autoreply",
  "ai chatter": "ai_seller",
  // The manual names the ENGINE "AI Seller" and the TAB "🤖 AI Chatter". Both
  // spellings appear in answers and both mean this tab.
  "ai seller": "ai_seller",
  "ai upseller": "ai_upseller",
  "tip reward": "tip_reward",
  "ppv library": "ppv_library",
  "vault ai": "vault_ai",
  "onboard old fans": "onboard",
};

/** A section's tab strip. One field, so `param` and `labels` cannot drift apart. */
interface TabStrip {
  /** Query key the strip reads on mount — see hooks/useTabParam.ts. */
  param: string;
  /** Normalised tab label → the strip's own tab key. */
  labels: Record<string, string>;
}

interface Section {
  href: string;
  strip?: TabStrip;
  /** Linkable on its own, with no "→ …" after it. "Messages", "Stats" and
   *  "Home" are ordinary words in a sentence about messages, stats or a home
   *  page, so they only ever link as the head of a path. */
  bare?: boolean;
}

/** Keyed by the nav word EXACTLY as the manual capitalises it — the match is
 *  case-sensitive, which is what keeps "the growth of an account" plain text. */
const SECTIONS: Record<string, Section> = {
  Home: { href: "/" },
  Inbox: { href: "/inbox" },
  // Stuff IS /messages — the label moved, the route didn't. "Messages" stays
  // as an alias because the manual is full of "Messages → Top posts" and an
  // operator who has been here a year still calls it that; both spellings are
  // ordinary words in a sentence, so neither is `bare`.
  Stuff: { href: "/messages", strip: { param: "tab", labels: MESSAGES_TABS } },
  Messages: { href: "/messages", strip: { param: "tab", labels: MESSAGES_TABS } },
  Stats: { href: "/stats" },
  // Now a tab of Stuff rather than its own page. Pointed straight at the tab:
  // /customs redirects there too, but a link that costs a round trip when the
  // real address is known is a link that renders slower for no reason.
  Customs: { href: "/messages?tab=customs", bare: true },
  Automations: {
    href: "/automations",
    // /automations is one page of stacked cards; the tab strip belongs to the
    // Ready-made panel inside it, so its param is named for that panel.
    strip: { param: "ready", labels: READY_MADE_TABS },
    bare: true,
  },
  Growth: {
    href: "/growth",
    strip: { param: "tab", labels: GROWTH_TABS },
    bare: true,
  },
  Vault: { href: "/vault", bare: true },
  Setup: { href: "/setup", bare: true },
  Settings: {
    href: "/settings",
    strip: { param: "tab", labels: SETTINGS_TABS },
    bare: true,
  },
};

const HEAD_RE = new RegExp(`\\b(${Object.keys(SECTIONS).join("|")})\\b`, "g");
const ARROW_RE = /^\s*(→|->)\s*/;
/** Ends a path segment. `.` only counts when it ends a sentence, so a decimal
 *  inside a label survives. Em dash and quotes end it because the manual uses
 *  them to append commentary after a path. `*` and a backtick end it so that
 *  calling this on RAW markdown — which AnswerText does not, but an independent
 *  caller might — cannot trail the bold marker into the link text. */
const SEG_END = /[\n,;:()[\]"“”*`]|\s—\s|\.(?=\s|$)|→|->/;

export interface PathHit {
  /** Index of the first character of the linked run. */
  start: number;
  /** Index one past the last character of the linked run. */
  end: number;
  href: string;
}

/**
 * The longest leading run of `raw` that names a tab, or null.
 *
 * Walks word by word rather than normalising the whole segment, because the
 * answer usually continues past the label — "🗓️ Auto Posts and press + New
 * auto post" must link only the first three words. Matching on the normalised
 * whole would fail outright and link the sentence.
 */
function leadingTab(
  raw: string,
  labels: Record<string, string>,
): { key: string; length: number } | null {
  let acc = "";
  let best: { key: string; length: number } | null = null;
  for (const piece of raw.split(/(\s+)/)) { // separators kept so lengths stay exact
    acc += piece;
    if (!piece.trim()) continue;
    const key = labels[norm(acc)];
    if (key) best = { key, length: acc.length };
  }
  return best;
}

/** Resolve one path segment against `section`'s tab strip, or null.
 *
 * Exists so the strip's optionality is narrowed ONCE, here, instead of forcing
 * a non-null assertion at the call site where `tab` being truthy says nothing
 * about `strip` to the compiler.
 */
function tabHref(
  section: Section,
  segment: string,
): { href: string; length: number } | null {
  const strip = section.strip;
  if (!strip) return null;
  const tab = leadingTab(segment, strip.labels);
  if (!tab) return null;
  return { href: `${section.href}?${strip.param}=${tab.key}`, length: tab.length };
}

/**
 * Find every click path in `text`.
 *
 * Hits never overlap and are returned in source order, so a caller can splice
 * the string in one pass.
 */
export function findPaths(text: string): PathHit[] {
  const hits: PathHit[] = [];
  HEAD_RE.lastIndex = 0;
  let m: RegExpExecArray | null;

  while ((m = HEAD_RE.exec(text))) {
    const section = SECTIONS[m[1]];
    const headEnd = m.index + m[1].length;
    let cursor = headEnd;
    let end = headEnd;
    let href = section.href;

    // Walk "→ segment" pairs for as long as they keep arriving.
    for (;;) {
      const arrow = ARROW_RE.exec(text.slice(cursor));
      if (!arrow) break;
      const segStart = cursor + arrow[0].length;
      const tail = text.slice(segStart);
      const stop = SEG_END.exec(tail);
      const seg = stop ? tail.slice(0, stop.index) : tail;
      if (!seg.trim()) break;

      const tab = tabHref(section, seg);
      if (tab) {
        href = tab.href;
        end = segStart + tab.length;
        // The label may be followed by prose ("Auto Posts and press …"); only
        // keep walking when the segment WAS the label and an arrow follows.
        if (tab.length < seg.trimEnd().length) break;
      } else {
        // An unrecognised segment is an intermediate node ("Ready-made posts &
        // broadcasts") only when another arrow follows it. Otherwise it is
        // prose and the link stops before it.
        if (!ARROW_RE.test(text.slice(segStart + seg.length))) break;
        end = segStart + seg.trimEnd().length;
      }
      cursor = segStart + seg.length;
    }

    // Nothing was consumed past the head: only some nav words stand alone.
    if (end === headEnd && !section.bare) continue;

    hits.push({ start: m.index, end, href });
    HEAD_RE.lastIndex = end; // never emit overlapping hits
  }

  return hits;
}
