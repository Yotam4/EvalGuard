import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";
import path from "node:path";

/**
 * Vitest config for the apps/web unit suite.
 *
 * - ``happy-dom`` for the DOM stub: ~10x faster startup than jsdom
 *   for the simple component tests we need (no canvas / requestSubmit /
 *   ResizeObserver edge cases).
 * - ``@vitejs/plugin-react`` so JSX in test files compiles the same
 *   way as in app source — no separate babel config drift.
 * - ``setupFiles`` injects ``@testing-library/jest-dom`` matchers
 *   (``toBeInTheDocument``, ``toHaveTextContent``, …) and resets the
 *   ``localStorage`` shim between tests so per-test state never
 *   leaks.
 */
export default defineConfig({
  plugins: [react()],
  test: {
    environment: "happy-dom",
    globals: true,
    setupFiles: ["./vitest.setup.ts"],
    css: false,
    include: ["src/**/__tests__/**/*.{ts,tsx}", "src/**/*.test.{ts,tsx}"],
  },
  resolve: {
    alias: {
      // Mirror the tsconfig path so ``@/`` works in tests too.
      "@": path.resolve(__dirname, "./src"),
    },
  },
});
