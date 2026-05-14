import { defineConfig, devices } from "@playwright/test";

/**
 * Playwright config for the EvalGuard UI.
 *
 * What this exercises: the static export under ``out/`` (post
 * ``next build``).  API responses are mocked via ``page.route()``
 * inside each test, so we don't depend on a live FastAPI here —
 * this is the UI's "does the bundle render and route?" surface.
 * Real end-to-end with the server lives in the Python integration
 * tests.
 *
 * Chromium-only to keep the install footprint small.  Operators
 * who want broader coverage can flip ``projects`` to include
 * webkit + firefox.
 */
export default defineConfig({
  testDir: "./e2e",
  // Slow flakes are usually network-bound; bump the timeout but
  // keep a hard ceiling so a deadlocked test fails the build.
  timeout: 30_000,
  expect:  { timeout: 5_000 },
  fullyParallel: true,
  // ``forbidOnly`` so a stray ``.only`` in a PR doesn't silently
  // skip the rest of the suite.
  forbidOnly: !!process.env.CI,
  retries:    process.env.CI ? 1 : 0,
  workers:    process.env.CI ? 2 : undefined,
  reporter:   process.env.CI
    ? [["list"], ["github"]]
    : "list",

  use: {
    baseURL: "http://127.0.0.1:4173",
    // Capture artifacts only on first failure so a green CI run
    // doesn't upload gigabytes of unused traces.
    trace:        "on-first-retry",
    screenshot:   "only-on-failure",
    video:        "retain-on-failure",
  },

  // ``out/`` is the static export from ``next build``.  ``serve``
  // works in CI; locally devs can use ``npm run dev`` instead and
  // override BASE_URL.  We prefer the static export so we're
  // testing what actually ships rather than the dev-mode bundle.
  webServer: {
    command:               "npx serve -l 4173 out",
    url:                   "http://127.0.0.1:4173",
    reuseExistingServer:   !process.env.CI,
    timeout:               60_000,
  },

  projects: [
    {
      name: "chromium",
      use:  { ...devices["Desktop Chrome"] },
    },
  ],
});
