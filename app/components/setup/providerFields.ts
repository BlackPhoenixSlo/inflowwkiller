/**
 * providerFields — how an LLM provider reads in the UI, and the one paste
 * mistake worth warning about.
 *
 * Shared because there are two places to type a provider key and they must say
 * the same things: the owner's own card (Setup → Your AI keys) and the founder's
 * form for another agency (Setup → Other agencies' AI keys, and Admin → Manage).
 * They did not: the founder's form — the one actually used to key an agency —
 * showed raw ids like "deepinfra" and carried no wrong-field warning at all,
 * which is precisely where a DeepSeek key got pasted into the DeepInfra box.
 */

/** Provider id → how it reads in the UI. An id with no entry renders as itself,
 *  so a provider added to the server registry shows up with no frontend deploy. */
export const LABELS: Record<string, string> = {
  deepseek: "DeepSeek API key",
  deepinfra: "DeepInfra API key",
  grok: "Grok (x.ai) API key",
};

/** A DeepSeek key is `sk-` + 32 hex and nothing else on this list looks like
 *  that, so a value in that shape sitting in another provider's field is a
 *  paste into the wrong box — the single most likely way to configure this
 *  card wrong, and one that fails SILENTLY: the key stores fine, and the first
 *  sign of trouble is that provider's calls 401ing later.
 *
 *  A warning, never a block. Key formats are the vendor's to change, and being
 *  wrong about a shape must not stop someone saving a key that works. */
const DEEPSEEK_SHAPE = /^sk-[0-9a-f]{32}$/i;

export function wrongFieldWarning(provider: string, value: string): string | null {
  const v = value.trim();
  if (!v || provider === "deepseek") return null;
  if (DEEPSEEK_SHAPE.test(v)) {
    return "That looks like a DeepSeek key — it belongs in the DeepSeek field below.";
  }
  return null;
}

export const HELP: Record<string, string> = {
  deepseek: "Powers chat replies and most automations. platform.deepseek.com.",
  deepinfra: "Vision — vault image describes and inbound-photo replies. deepinfra.com.",
  grok: "Optional second provider (x.ai). Only needed if a model of yours uses it.",
};
