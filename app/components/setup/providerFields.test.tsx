import { describe, it, expect } from "vitest";
import { wrongFieldWarning } from "./providerFields";

/**
 * Pins the one paste mistake this card can make silently: a DeepSeek key
 * (`sk-` + 32 hex) dropped into the DeepInfra or Grok box stores fine and only
 * shows up later as 401s from that provider.
 */
describe("wrongFieldWarning", () => {
  const DEEPSEEK = "sk-" + "a".repeat(32);

  it("flags a DeepSeek-shaped key in another provider's field", () => {
    expect(wrongFieldWarning("deepinfra", DEEPSEEK)).toMatch(/DeepSeek/);
    expect(wrongFieldWarning("grok", DEEPSEEK)).toMatch(/DeepSeek/);
  });

  it("never flags the DeepSeek field itself", () => {
    expect(wrongFieldWarning("deepseek", DEEPSEEK)).toBeNull();
  });

  it("stays quiet on an empty or whitespace field", () => {
    expect(wrongFieldWarning("deepinfra", "")).toBeNull();
    expect(wrongFieldWarning("deepinfra", "   ")).toBeNull();
  });

  it("does not flag a key that is merely sk-prefixed but a different shape", () => {
    expect(wrongFieldWarning("deepinfra", "sk-short")).toBeNull();
    expect(wrongFieldWarning("deepinfra", "di-" + "b".repeat(32))).toBeNull();
  });
});
