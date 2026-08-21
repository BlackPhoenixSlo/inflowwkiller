"use strict";

// What kind of signature is this bundle actually carrying?
//
// This exists because of one specific trap: Electron 42 moved macOS
// notifications to Apple's UNNotification API, which only delivers for a
// code-signed app. Native notifications are the main reason this shell exists
// over a browser tab, and when they break they break *silently* — no error, no
// missing UI, just nothing ever arriving.
//
// The trap has a second half that makes it nearly untestable by hand: in dev
// (`npm start`) the host bundle is Electron's OWN properly-signed Electron.app,
// so notifications work perfectly. They die only in the packaged build. Anyone
// verifying the feature the obvious way gets a false pass.
//
// So we read the shipped bundle's real signature at boot and report it as a
// fact. Deciding what is a warning and what is merely informational belongs to
// the caller — this module only answers "what is true", never "what is bad".

const { execFile } = require("child_process");
const path = require("path");

const CODESIGN_TIMEOUT_MS = 4000;

/** The .app bundle we are running from: …/Fastt.app/Contents/MacOS/Fastt → …/Fastt.app */
function bundlePath(execPath) {
  return path.resolve(execPath, "..", "..", "..");
}

/**
 * Classify `codesign -dv --verbose=2` output.
 *
 * Kinds:
 *   "developer-id" — a real signing identity (a Team ID is present)
 *   "adhoc"        — the linker's ad-hoc signature. arm64 binaries always get
 *                    one, so "unsigned" builds are really this. It carries no
 *                    stable identity: `Identifier=Electron`, Info.plist unbound.
 *   "unsigned"     — no signature at all (possible on x64)
 *   "unknown"      — codesign unavailable/failed; we do not guess
 */
function classify(output) {
  const text = String(output || "");
  if (/code object is not signed at all/i.test(text)) return { kind: "unsigned", teamId: null };

  const team = text.match(/^TeamIdentifier=(.+)$/m);
  const teamId = team && team[1].trim() !== "not set" ? team[1].trim() : null;

  // Check ad-hoc BEFORE trusting a Team ID: the flags line is the authoritative
  // statement about what the signature IS, and an ad-hoc signature never has a
  // meaningful team.
  if (/\badhoc\b/i.test(text) || /^Signature=adhoc$/m.test(text)) {
    return { kind: "adhoc", teamId: null };
  }
  if (teamId) return { kind: "developer-id", teamId };
  if (/^Signature=/m.test(text) || /^Authority=/m.test(text)) {
    return { kind: "developer-id", teamId: null };
  }
  return { kind: "unknown", teamId: null };
}

/**
 * Describe the running bundle's signing state. Never throws, never blocks boot
 * for more than CODESIGN_TIMEOUT_MS, and resolves to kind "unknown" rather than
 * guessing when it cannot tell.
 *
 * `deps` is injectable so this is testable without a packaged app.
 */
function describeSigning(deps = {}) {
  const {
    platform = process.platform,
    isPackaged = true,
    electronVersion = process.versions.electron,
    execPath = process.execPath,
    run = execFile,
  } = deps;

  const electronMajor = parseInt(String(electronVersion), 10) || 0;
  const base = {
    platform,
    isPackaged,
    electronMajor,
    // The version where macOS notifications started requiring a signature.
    notificationsRequireSigning: platform === "darwin" && electronMajor >= 42,
    kind: "not-applicable",
    teamId: null,
  };

  // Only macOS has this constraint, and only a packaged bundle tells the truth:
  // in dev we would be inspecting Electron's own signed app and learn nothing
  // about what we ship.
  if (platform !== "darwin" || !isPackaged) return Promise.resolve(base);

  return new Promise((resolve) => {
    let settled = false;
    const done = (result) => {
      if (settled) return;
      settled = true;
      resolve({ ...base, ...result });
    };

    try {
      run(
        "codesign",
        ["-dv", "--verbose=2", bundlePath(execPath)],
        { timeout: CODESIGN_TIMEOUT_MS },
        (err, stdout, stderr) => {
          // codesign writes its report to STDERR and exits non-zero for an
          // unsigned bundle — so a non-zero exit is data, not a failure.
          const text = `${stderr || ""}\n${stdout || ""}`;
          if (!text.trim()) return done({ kind: err ? "unknown" : "unknown" });
          done(classify(text));
        }
      );
    } catch {
      done({ kind: "unknown" });
    }
  });
}

/** One-line, log-friendly summary. Facts only — no verdict. */
function summarize(d) {
  if (d.platform !== "darwin") return `signing: n/a (${d.platform})`;
  if (!d.isPackaged) return "signing: n/a (dev build runs inside Electron's own signed app)";
  const team = d.teamId ? ` team=${d.teamId}` : "";
  return (
    `signing: ${d.kind}${team} · electron=${d.electronMajor} · ` +
    `notifications require signing: ${d.notificationsRequireSigning ? "yes" : "no"}`
  );
}

module.exports = { describeSigning, classify, summarize, bundlePath };
