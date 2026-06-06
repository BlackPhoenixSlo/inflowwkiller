// MAIN-world interceptor — identical strategy to loginExtension/, kept as a
// standalone copy so this extension can be installed without the other one.
//
// Captures:
//   1. Header values on outgoing /api2/v2/ requests: user-id, x-bc,
//      x-of-rev, sign, time, user-agent
//   2. static_param, start, end via webpack module hooks (89668 = js-sha1,
//      802313 = sign builder)

(function () {
  "use strict";

  const REQUIRED_HDRS = ["user-id", "x-bc", "x-of-rev", "sign", "time"];
  const lastSeen = {};
  const lastRules = {};

  function publish() {
    for (const k of REQUIRED_HDRS) if (!lastSeen[k]) return;
    try {
      window.postMessage(
        {
          __chatterly_nt_capture: true,
          payload: {
            url: lastSeen.__url || "https://onlyfans.com/api2/v2/users/me",
            headers: {
              "user-id":    lastSeen["user-id"],
              "x-bc":       lastSeen["x-bc"],
              "x-of-rev":   lastSeen["x-of-rev"],
              "sign":       lastSeen["sign"],
              "time":       lastSeen["time"],
              "user-agent": lastSeen["user-agent"] || navigator.userAgent,
            },
            rules: {
              static_param: lastRules.static_param || null,
              start:        lastRules.start || null,
              end:          lastRules.end || null,
            },
            capturedAt: new Date().toISOString(),
          },
        },
        location.origin
      );
    } catch (_) {}
  }

  function recordHeader(name, value) {
    const lc = String(name || "").toLowerCase();
    if (!value) return;
    if (lc === "user-id")    lastSeen["user-id"]    = String(value);
    else if (lc === "x-bc")       lastSeen["x-bc"]       = String(value);
    else if (lc === "x-of-rev")   lastSeen["x-of-rev"]   = String(value);
    else if (lc === "sign")       lastSeen["sign"]       = String(value);
    else if (lc === "time")       lastSeen["time"]       = String(value);
    else if (lc === "user-agent") lastSeen["user-agent"] = String(value);
  }

  const _xhrOpen = XMLHttpRequest.prototype.open;
  const _xhrSetHeader = XMLHttpRequest.prototype.setRequestHeader;
  const _xhrSend = XMLHttpRequest.prototype.send;

  XMLHttpRequest.prototype.open = function (method, url, ...rest) {
    this.__cl_url = typeof url === "string" ? url : "";
    return _xhrOpen.call(this, method, url, ...rest);
  };
  XMLHttpRequest.prototype.setRequestHeader = function (name, value) {
    recordHeader(name, value);
    return _xhrSetHeader.call(this, name, value);
  };
  XMLHttpRequest.prototype.send = function (...args) {
    const url = this.__cl_url;
    if (url && url.includes("/api2/v2/")) {
      lastSeen.__url = url;
      publish();
    }
    return _xhrSend.apply(this, args);
  };

  const _fetch = window.fetch;
  window.fetch = function (input, init) {
    try {
      const url = typeof input === "string" ? input : (input && input.url) || "";
      const hdrs = (init && init.headers) || (input && input.headers);
      if (hdrs) {
        if (typeof hdrs.forEach === "function") {
          hdrs.forEach((v, k) => recordHeader(k, v));
        } else if (Array.isArray(hdrs)) {
          for (const [k, v] of hdrs) recordHeader(k, v);
        } else {
          for (const k of Object.keys(hdrs)) recordHeader(k, hdrs[k]);
        }
      }
      if (url && url.includes("/api2/v2/")) {
        lastSeen.__url = url;
        publish();
      }
    } catch (_) {}
    return _fetch.apply(this, arguments);
  };

  const wpChunks = (self.webpackChunkof_vue = self.webpackChunkof_vue || []);
  const origPush = wpChunks.push.bind(wpChunks);

  wpChunks.push = function (chunk) {
    const modules = chunk[1];
    if (!modules) return origPush(chunk);

    if (modules[89668]) {
      const origMod = modules[89668];
      modules[89668] = function (module, exports, require) {
        origMod(module, exports, require);
        const origSha1 = module.exports;
        if (typeof origSha1 === "function") {
          const wrapped = function (input) {
            const result = origSha1(input);
            try {
              if (typeof input === "string" && input.split("\n").length === 4) {
                const staticParam = input.split("\n")[0];
                if (staticParam && lastRules.static_param !== staticParam) {
                  lastRules.static_param = staticParam;
                  publish();
                }
              }
            } catch (_) {}
            return result;
          };
          for (const k of Object.keys(origSha1)) wrapped[k] = origSha1[k];
          if (origSha1.prototype) wrapped.prototype = origSha1.prototype;
          module.exports = wrapped;
        }
      };
    }

    if (modules[802313]) {
      const origMod = modules[802313];
      modules[802313] = function (module, exports, require) {
        origMod(module, exports, require);
        if (exports && typeof exports.A === "function") {
          const origFn = exports.A;
          exports.A = function (...args) {
            const result = origFn.apply(this, args);
            try {
              if (result && typeof result === "object") {
                for (const k of Object.keys(result)) {
                  const v = result[k];
                  if (typeof v === "string" && v.split(":").length === 4) {
                    const [start, , , end] = v.split(":");
                    let changed = false;
                    if (start && lastRules.start !== start) { lastRules.start = start; changed = true; }
                    if (end   && lastRules.end   !== end)   { lastRules.end   = end;   changed = true; }
                    if (changed) publish();
                    break;
                  }
                }
              }
            } catch (_) {}
            return result;
          };
        }
      };
    }

    return origPush(chunk);
  };
})();
