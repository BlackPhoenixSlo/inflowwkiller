/**
 * THE RULE, ENFORCED OVER THE WHOLE AUTOMATIONS RENDER GRAPH.
 *
 * `sellerShared.tsx` states it with a ⚠️: name the ROLE, never the creator's gender.
 * In the male lane the creator AND the fan are "he", so a creator pronoun collides
 * with the fan's — "he runs his thread" states the opposite of what it means.
 *
 * THE SCOPE IS COMPUTED, NOT TYPED, AND IT TOOK FOUR GOES TO MEAN IT. v1 listed
 * three filenames and was green while `ReengageBuyersTab` — mounted INSIDE the AI
 * Chatter tab — shipped "It skips anyone she messaged recently". v2 walked the import
 * graph but seeded at the two seller tabs, which is a hand-typed scope one level up:
 * `AutoreplyTab`, a SIBLING tab, carried five gendered strings while this was green,
 * one of them edited by the same commit. v3 seeds at the composition ROOT — the panel
 * that actually mounts the tabs — so every tab is in scope by construction and a new
 * one joins on the day its import line lands. v4 stopped typing anything: the seeds
 * are every `app/**\/page.tsx`, because a route is the one thing no one hand-picks.
 *
 * Each version failed the way the python guard's own docstring predicts ("WHICH
 * functions are laned is DERIVED, not listed"): a scope you type is a scope you
 * forget.
 *
 * BOTH import spellings are followed — `@/components/…` and relative `./Foo` — and
 * anything under components/ that cannot be resolved FAILS the run instead of
 * dropping out of scope. Following only the alias was the same defect one directory
 * over from the python guard's `_voice.HER` matching: a scope computed from a
 * spelling. `@/hooks`, `@/lib` and `@/contexts` are deliberately not followed (they
 * render no prose), which keeps the closure small with no exclusion list. It still
 * reaches `chat/VaultPicker`, which renders inside a seller tab and which no
 * `components/settings/` glob could find.
 *
 * COMMENTS ARE EXCLUDED: the modules explain why the rule exists, and that
 * explanation must quote the banned words to be worth reading.
 *
 * The FAN is not in scope. He is male in both lanes, so "he"/"him"/"his" is correct.
 */
import fs from "node:fs";
import path from "node:path";
import { describe, expect, it } from "vitest";

const ROOT = path.resolve(__dirname, "../..");

/** THE ROUTES. Not a filename — a glob of every `app/**\/page.tsx`.
 *
 *  Four versions of this guard typed their own scope and all four forgot something:
 *  three filenames missed `ReengageBuyersTab`; the two seller tabs missed the sibling
 *  `AutoreplyTab`; `ReadyMadePanel` missed `BrainPanel` — the panel holding the
 *  Female/Male dropdown itself, sitting ABOVE the tabs on the same page, shipping
 *  "Her facts" over field labels this series had just taught to say "Why he started
 *  OF". Each time the fix was "seed one level up", and one level up was still typed.
 *
 *  Pages are the only things nobody chooses: Next mounts them, the glob finds them,
 *  and a new screen is in scope the day its route exists. */
function routeSeeds(): string[] {
  const out: string[] = [];
  const walk = (dir: string) => {
    for (const e of fs.readdirSync(path.join(ROOT, dir), { withFileTypes: true })) {
      const rel = path.join(dir, e.name);
      if (e.isDirectory()) walk(rel);
      // EVERY .tsx under app/, not `page.tsx`. `page` is one of Next's route-file
      // names, not the route contract: `layout.tsx` composes too, and the ROOT
      // layout mounts TopNav/MoneyRail/NotificationToaster on every screen. Picking
      // one filename out of that set was v5 of the same typed-scope mistake.
      // Over-seeding is free (the walker de-dupes and these files are the app);
      // under-seeding is the failure this guard exists to stop.
      else if (e.name.endsWith(".tsx")) out.push(rel);
    }
  };
  walk("app");
  return out.sort();
}
const SEEDS = routeSeeds();

/** Words that assert the creator is a woman. `girl` included: "Girl style" was a
 *  checkbox label until it was renamed, and it is the same mistake in one word. */
const BANNED = /\b(she|her|hers|herself|girl|girls|woman|women)\b/i;

/** Source minus comments, so the rule's own rationale can quote what it forbids.
 *
 *  Block comments are stripped only where `/*` OPENS A LINE. A global `/\*[\s\S]*?\*\/`
 *  is fail-open here: one `/*` inside a string or JSX text swallows everything up to
 *  the next JSDoc terminator, deleting real copy from the scan. Every block comment in
 *  these files starts its own line, so the narrow rule loses nothing.
 *
 *  Trailing `//` counts too — a guard that fires on a comment is one people switch
 *  off. `(?<![:\w])` keeps `https://` intact. */
export function codeOnly(src: string): string {
  // Comments are BLANKED, not deleted — a multi-line block replaced by "" collapses
  // every line after it, so the reported file:line pointed at the wrong place (the
  // guard's first real catch cited :651 for a string on :674). Keep the newlines and
  // the numbering is the file's own.
  const keepLines = (m: string) => m.replace(/[^\n]/g, "");
  return src
    // `[^*]*\*+([^/*][^*]*\*+)*` is the classic non-backtracking block-comment
    // body: it cannot run past the FIRST `*/`. The lazy `[\s\S]*?` did — it
    // backtracked across comment boundaries hunting a `*/` that happened to end
    // a line, blanking real code in between.
    .replace(/^[ \t]*\{?\/\*[^*]*\*+(?:[^/*][^*]*\*+)*\/\}?[ \t]*$/gm, keepLines)
    .replace(/(?<![:\w])\/\/.*$/gm, "")                            // // to EOL, not a URL
    // A string literal whose ENTIRE content is a lane id is wire DATA, never copy
    // (`form?.voice ?? "her"`). Whole-literal only — never a substring, never JSX
    // text — so no judgement call and no exclusion list.
    .replace(/"(her|him)"/g, '"·"');
}

/** Every import specifier in `src`, static or dynamic, aliased or relative. */
function specifiers(src: string): string[] {
  const out: string[] = [];
  for (const re of [/from\s+"([^"]+)"/g, /import\(\s*"([^"]+)"\s*\)/g,
                    /require\(\s*"([^"]+)"\s*\)/g]) {
    for (const m of src.matchAll(re)) out.push(m[1]);
  }
  return out;
}

/** A specifier → a repo-relative path, or null when it is not one of ours.
 *
 *  BOTH SPELLINGS. The first version followed only `@/components/…`, so a sibling
 *  imported `./Foo` — which is house style three doors down (AutoreplyTab,
 *  PPVLibraryTab, WebhookDispatchTab all write `./JsonConfigModal`) — silently
 *  dropped out of scope. "Computed from the import graph" was computed from a
 *  spelling, which is the same defect as the python guard's `_voice.HER` matching,
 *  one directory over. `index` and `.jsx` are handled for the same reason: every
 *  unhandled form is a silent hole, not an error. */
function resolveSpec(spec: string, fromRel: string): string | null {
  let base: string | null = null;
  if (spec.startsWith("@/")) base = spec.slice(2);
  else if (spec.startsWith("./") || spec.startsWith("../")) {
    base = path.normalize(path.join(path.dirname(fromRel), spec));
  }
  if (base === null) return null;                       // a package, not ours
  for (const cand of [base + ".tsx", base + ".ts", base + ".jsx", base + ".js",
                      path.join(base, "index.tsx"), path.join(base, "index.ts")]) {
    if (fs.existsSync(path.join(ROOT, cand))) return cand;
  }
  return base;   // ours by shape but unresolvable — reported, never skipped
}

/** Modules reachable from `seeds`, following every specifier that names a file of
 *  ours. Returns the closure plus anything under components/ it could NOT resolve —
 *  FAIL CLOSED: a resolver that silently drops what it cannot follow is how the
 *  typed list failed, one level down. */
function renderGraph(seeds: string[]): { modules: string[]; unresolved: string[] } {
  const seen = new Set<string>();
  const unresolved = new Set<string>();
  const queue = [...seeds];
  while (queue.length) {
    const rel = queue.shift()!;
    if (seen.has(rel)) continue;
    const abs = path.join(ROOT, rel);
    if (!fs.existsSync(abs)) { unresolved.add(rel); continue; }
    seen.add(rel);
    for (const spec of specifiers(fs.readFileSync(abs, "utf8"))) {
      const target = resolveSpec(spec, rel);
      if (target === null) continue;                    // a package
      if (!fs.existsSync(path.join(ROOT, target))) {
        // Only OUR directories matter; hooks/lib/contexts render no prose.
        if (target.startsWith("components/")) unresolved.add(`${rel} -> ${spec}`);
        continue;
      }
      if (target.startsWith("components/")) queue.push(target);
    }
  }
  return { modules: [...seen].sort(), unresolved: [...unresolved].sort() };
}

const { modules: MODULES, unresolved: UNRESOLVED } = renderGraph(SEEDS);


describe("seller-tab operator copy names the role, not the gender", () => {
  it("finds the whole render graph, and follows everything it should", () => {
    // FAIL CLOSED. A walker that silently drops what it cannot follow collapses to
    // the two seeds and passes forever — the same fail-open as the typed list, one
    // level down. Anything under components/ it could not resolve is reported here
    // rather than quietly leaving the scope.
    expect(UNRESOLVED).toEqual([]);
    expect(SEEDS.length).toBeGreaterThanOrEqual(20);   // the app/ tree still resolves
    expect(MODULES.length).toBeGreaterThanOrEqual(35);
    for (const must of ["components/automations/BrainPanel.tsx",     // above the tabs
                        "components/settings/ScriptsTab.tsx",        // a mounted tab
                        "components/settings/UpsellerTab.tsx",
                        "components/settings/AutoreplyTab.tsx",       // a SIBLING tab
                        "components/settings/ReengageBuyersTab.tsx",  // nested in one
                        "components/settings/sellerShared.tsx",       // shared by two
                        "components/chat/VaultPicker.tsx"]) {         // cross-directory
      expect(MODULES, `${must} is mounted under Automations`).toContain(must);
    }
  });

  for (const rel of MODULES) {
    it(rel, () => {
      const src = fs.readFileSync(path.join(ROOT, rel), "utf8");
      const offenders = codeOnly(src)
        .split("\n")
        .map((line, i) => ({ line, n: i + 1 }))
        .filter(({ line }) => BANNED.test(line))
        .map((o) => `${rel}:${o.n} ${o.line.trim()}`);
      expect(offenders).toEqual([]);
    });
  }

  it("the matcher and the stripper still do their jobs", () => {
    expect(BANNED.test("  <span>Never contradict herself</span>")).toBe(true);
    expect(BANNED.test('  question_hook: "she opens a scene"')).toBe(true);
    // The fan's pronouns are correct in both lanes and must NOT trip it.
    expect(BANNED.test('  "after he buys, the ask goes up"')).toBe(false);
    // Nor a substring inside an ordinary word.
    expect(BANNED.test("  where the other thread gathers")).toBe(false);
    // A trailing comment is a comment; a URL is not.
    expect(codeOnly("const a = 1; // she used to do this").trim()).toBe("const a = 1;");
    // Blanking, not deleting: a 3-line comment must still occupy 3 lines, or every
    // offender it reports afterwards carries the wrong coordinates.
    expect(codeOnly("/* a\n b\n */\nreal").split("\n")).toHaveLength(4);
    expect(codeOnly('const u = "https://x/y"; // note')).toContain("https://x/y");
    // …and an inline `/*` in a STRING must not eat the copy after it.
    const tricky = 'const s = "50% /* off";\n  <span>she is here</span>\n';
    expect(codeOnly(tricky)).toContain("she is here");
  });
});
