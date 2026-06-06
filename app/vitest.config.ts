/**
 * Vitest config for the Next.js app (React 19).
 *
 * Inert until the dev-deps are installed by T-TESTKIT:
 *   pnpm add -D vitest @vitejs/plugin-react jsdom \
 *     @testing-library/react @testing-library/jest-dom @testing-library/user-event
 *
 * Run:  pnpm test        (watch)
 *       pnpm test:run    (one-shot, CI)
 */
import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";
import { resolve } from "node:path";

export default defineConfig({
  plugins: [react()],
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./vitest.setup.ts"],
    include: ["**/*.test.{ts,tsx}"],
    exclude: ["node_modules/**", ".next/**"],
    // "0 tests = ok" until the first frontend tests land (T-TESTKIT). Without
    // this, `vitest run` exits 1 on an empty suite and reddens run-tests.sh.
    passWithNoTests: true,
  },
  resolve: {
    // Mirror tsconfig paths: "@/*" -> "./*"
    alias: { "@": resolve(__dirname, ".") },
  },
});
