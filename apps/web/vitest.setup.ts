/**
 * Vitest setup — runs once before each test file.
 *
 * - Adds the ``@testing-library/jest-dom`` matchers so component
 *   tests can use ``toBeInTheDocument`` / ``toHaveTextContent`` /
 *   etc. without per-file imports.
 * - Resets ``localStorage`` between tests so the auth helpers (which
 *   read/write to ``localStorage``) don't leak state across the
 *   suite.
 */

import "@testing-library/jest-dom/vitest";
import { afterEach } from "vitest";

afterEach(() => {
  if (typeof window !== "undefined") {
    window.localStorage.clear();
  }
});
