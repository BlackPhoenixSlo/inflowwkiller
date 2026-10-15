// interceptor.js — runs at document_start (MAIN world)
// 1. Intercepts XHR headers (user-id, x-bc, x-of-rev, sign, time)
// 2. Intercepts XHR responses for /messages endpoints
// 3. Hooks OF's webpack modules to extract signing rules at runtime:
//    - Hooks js-sha1 (module 89668) to capture static_param from hash input
//    - Hooks sign function (module 802313) to capture start, end, checksum_indexes, checksum_constant
(function () {
  "use strict";

  // ── XHR header interception ────────────────────────────────
  const _origOpen = XMLHttpRequest.prototype.open;
  const _origSend = XMLHttpRequest.prototype.send;
  const _origSetHeader = XMLHttpRequest.prototype.setRequestHeader;

  XMLHttpRequest.prototype.open = function (method, url, ...rest) {
    this.__ofe_url = (typeof url === "string") ? url : "";
    return _origOpen.call(this, method, url, ...rest);
  };

  XMLHttpRequest.prototype.setRequestHeader = function (name, value) {
    const lc = name.toLowerCase();
    if (lc === "user-id" && value && !window.__ofe_userId) window.__ofe_userId = value;
    if (lc === "x-of-rev" && value && !window.__ofe_xOfRev) window.__ofe_xOfRev = value;
    if (lc === "x-bc" && value && !window.__ofe_xBc) window.__ofe_xBc = value;
    if (lc === "sign" && value) window.__ofe_lastSign = value;
    if (lc === "time" && value) window.__ofe_lastTime = value;
    return _origSetHeader.call(this, name, value);
  };

  // ── XHR response interception (for scroll mode + messages) ──
  XMLHttpRequest.prototype.send = function (...args) {
    const self = this;
    if (self.__ofe_url && self.__ofe_url.includes("/chats/") && self.__ofe_url.includes("/messages")) {
      self.addEventListener("load", function () {
        try {
          if (self.status === 200) {
            const data = JSON.parse(self.responseText);
            if (data && data.list) {
              window.dispatchEvent(new CustomEvent("__ofe_messages", {
                detail: { list: data.list, hasMore: !!data.hasMore }
              }));
            }
          }
        } catch (_) {}
      });
    }
    return _origSend.apply(this, args);
  };

  // ── Webpack module hooks to extract signing rules ──────────
  //
  // How OF's signing works (chunk 2313.js, obfuscated):
  //   1. Calls sha1(static_param + "\n" + time + "\n" + path + "\n" + userId)
  //   2. Maps sha1 hex chars to charCodes: bytes = sha1.split("").map(c => c.charCodeAt(0))
  //   3. Sums bytes at specific indexes + a constant = checksum
  //   4. Returns: start + ":" + sha1 + ":" + abs(checksum).toString(16) + ":" + end
  //
  // We hook two modules:
  //   - Module 89668 (js-sha1): wrap the hash function to read the input
  //     → input.split("\n")[0] = static_param
  //   - Module 802313 (sign builder): wrap to read the full sign output
  //     → from the sign string we get start and end
  //     → from multiple (sha1, checksum) pairs we solve for checksum_indexes + constant

  window.__ofe_extractedRules = null;
  window.__ofe_signSamples = [];  // collected (sha1Hex, checksumValue) pairs

  const wpChunks = self.webpackChunkof_vue = self.webpackChunkof_vue || [];
  const origPush = wpChunks.push.bind(wpChunks);

  wpChunks.push = function (chunk) {
    const modules = chunk[1];
    if (!modules) return origPush(chunk);

    // ── Hook js-sha1 (module 89668) ──────────────────────────
    if (modules[89668]) {
      const origSha1Module = modules[89668];
      modules[89668] = function (module, exports, require) {
        origSha1Module(module, exports, require);

        // js-sha1 exports a function: sha1(input) → hex string
        // It also has sha1.create(), sha1.update(), etc.
        // We need to wrap the main callable export.
        const origSha1 = module.exports;
        if (typeof origSha1 === "function") {
          const wrappedSha1 = function (input) {
            const result = origSha1(input);

            // Check if this looks like a sign hash input (has 3 newlines)
            if (typeof input === "string" && input.split("\n").length === 4) {
              const parts = input.split("\n");
              const staticParam = parts[0];
              const time = parts[1];
              const path = parts[2];
              const userId = parts[3];

              if (!window.__ofe_extractedRules) {
                window.__ofe_extractedRules = {};
              }
              window.__ofe_extractedRules.static_param = staticParam;

              console.log("[OFE] Captured sign hash input:", {
                static_param: staticParam,
                time,
                path,
                userId,
                sha1_output: result,
              });
            }

            return result;
          };

          // Copy over all properties (sha1.create, sha1.hex, etc.)
          Object.keys(origSha1).forEach(k => { wrappedSha1[k] = origSha1[k]; });
          if (origSha1.prototype) wrappedSha1.prototype = origSha1.prototype;

          module.exports = wrappedSha1;
          console.log("[OFE] Hooked js-sha1 module (89668)");
        }
      };
    }

    // ── Hook sign builder (module 802313) ─────────────────────
    if (modules[802313]) {
      const origSignModule = modules[802313];
      modules[802313] = function (module, exports, require) {
        origSignModule(module, exports, require);

        // exports.A is the sign function: signFn(urlPath) → { sign, time, ... }
        if (exports && exports.A) {
          const origSignFn = exports.A;
          exports.A = function (urlPath) {
            // Call the original sign function (no charCodeAt patching —
            // that was too invasive and interfered with RC4 decryption).
            // Instead, we extract indexes from the 2313.js source code
            // via static deobfuscation in extension.js.
            const result = origSignFn(urlPath);

            if (result && typeof result === "object") {
              for (const key of Object.keys(result)) {
                const val = result[key];
                if (typeof val === "string" && val.split(":").length === 4) {
                  const [start, sha1Hex, checksumHex, end] = val.split(":");
                  const checksumValue = parseInt(checksumHex, 16);

                  if (!window.__ofe_extractedRules) {
                    window.__ofe_extractedRules = {};
                  }
                  window.__ofe_extractedRules.start = start;
                  window.__ofe_extractedRules.end = end;

                  // Collect sample
                  window.__ofe_signSamples.push({ sha1Hex, checksumValue });

                  console.log("[OFE] Captured sign output:", {
                    start, end, sha1Hex, checksumHex, checksumValue,
                    samplesCollected: window.__ofe_signSamples.length,
                  });
                  break;
                }
              }
            }

            return result;
          };
          console.log("[OFE] Hooked sign builder module (802313)");
        }
      };
    }

    return origPush(chunk);
  };

  // ── Also process any chunks that already loaded before our hook ──
  // (unlikely since we run at document_start, but just in case)
  if (wpChunks.length > 0) {
    console.log("[OFE] Processing", wpChunks.length, "pre-loaded webpack chunks");
  }

})();
