/**
 * Phase 2.6d e2e smoke — the one Playwright test we have so far.
 *
 * What it covers:
 *   1. /settings/ → enter server URL + token → save (writes
 *      localStorage so ConnectionGate unlocks the rest of the app).
 *   2. /runs/   → list renders with the canned API row.
 *   3. /runs/detail/?id=run_X → opens, header + summary render.
 *   4. /assets/ → list renders one row, kind filter is wired.
 *   5. /assets/detail/?kind=&asset_id=&project_id= → loads, header
 *      shows asset metadata, versions table renders.
 *
 * API responses are mocked via ``page.route()`` so this test
 * doesn't depend on a live FastAPI.  Each route's URL shape is
 * pinned in the matching vitest case — if a refactor breaks one
 * but not the other, the vitest matrix tells us where to look.
 */

import { test, expect } from "@playwright/test";


const SERVER = "https://api.example.com";
const TOKEN  = "evk_e2e_test_token";


test.beforeEach(async ({ context }) => {
  // Mock every ``api.example.com`` call so the page never reaches
  // the network.  Anything outside this prefix passes through
  // (the static export's own JS / CSS).
  await context.route(/api\.example\.com\/v1\/.*/, async (route) => {
    const url = new URL(route.request().url());
    const path = url.pathname;

    // Health — the UI doesn't actually call this on every page,
    // but having it makes "did my mock catch this" easier to
    // debug.
    if (path === "/v1/health") {
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          status: "ok", version: "0.0.0-e2e",
          mode: "auth", db: "sqlite",
        }),
      });
    }

    if (path === "/v1/runs") {
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          runs: [{
            run_id: "run_e2e0000000001",
            project: "demo",
            status: "passed",
            gate_status: "passed",
            started_at:  "2026-05-14T07:00:00",
            finished_at: "2026-05-14T07:00:30",
            row_count: 5, row_pass_count: 5, row_fail_count: 0,
            cost_usd: 0.01,
            ingested_at: "2026-05-14T07:01:00",
            ingested_by: "key_e2e",
            source: "cli",
          }],
          next: null,
        }),
      });
    }

    if (path === "/v1/runs/run_e2e0000000001") {
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          schema_version: "1.0.0",
          run_id:  "run_e2e0000000001",
          project: "demo",
          status:  "passed",
          gate_status: "passed",
          row_count: 5, row_pass_count: 5, row_fail_count: 0,
          cost_usd: 0.01,
          trials: [{
            trial_id: "trial_e2e000001",
            provider_id: "mock:m", provider: "mock", model: "m",
            config: {}, metrics: {},
            row_count: 5, row_pass_count: 5, row_fail_count: 0,
            cost_usd: 0.01,
            status: "passed", gate_status: "passed",
            started_at: "2026-05-14T07:00:00",
            finished_at: "2026-05-14T07:00:30",
            gates: [], rows: [],
          }],
          server: {
            ingested_at: "2026-05-14T07:01:00",
            ingested_by: "key_e2e",
            project_id:  "proj_e2e",
          },
        }),
      });
    }

    if (path === "/v1/assets") {
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          assets: [{
            kind: "judge",
            asset_id: "q-e2e",
            project_id: "proj_e2e",
            project_name: "demo",
            version_count: 2,
            run_count: 3,
            last_seen: "2026-05-14T07:00:30",
            last_run_id: "run_e2e0000000001",
            last_version_id: "sha256-aaaaaaaa",
          }],
        }),
      });
    }

    if (path === "/v1/assets/judge/q-e2e/versions") {
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          kind: "judge",
          asset_id: "q-e2e",
          project_id: "proj_e2e",
          project_name: "demo",
          versions: [{
            version_id: "sha256-aaaaaaaa",
            run_id:     "run_e2e0000000001",
            project_name: "demo",
            ingested_at: "2026-05-14T07:01:00",
            source: "cli",
          }],
        }),
      });
    }

    // Fall through to a 404 so an un-mocked endpoint surfaces
    // clearly in the test output (rather than the test hanging
    // on a network call to a fake hostname).
    return route.fulfill({
      status: 404,
      contentType: "application/json",
      body: JSON.stringify({ detail: `e2e mock missing ${path}` }),
    });
  });
});


test("settings → runs → run detail → assets → asset detail", async ({ page }) => {
  // 1. Settings.
  await page.goto("/settings/");
  await page.getByLabel(/server url/i).fill(SERVER);
  await page.getByLabel(/api token/i).fill(TOKEN);
  await page.getByRole("button", { name: /save/i }).click();

  // 2. Runs list.  ``ConnectionGate`` should now unlock — give it
  // a moment to flush localStorage and re-render.
  await page.goto("/runs/");
  await expect(page.getByRole("heading", { name: /^runs$/i })).toBeVisible();
  // Address the row by its ``data-run-id`` attribute rather than a
  // text-match on the link — a future change that reorders columns
  // or shares a prefix between two runs wouldn't break this.
  const runRow = page.locator('[data-testid="run-row"][data-run-id="run_e2e0000000001"]');
  await expect(runRow).toBeVisible();

  // 3. Run detail — click the run id link inside the addressed row.
  await runRow.getByRole("link").first().click();
  // Detail page renders the run_id as a header.
  await expect(
    page.getByRole("heading", { name: "run_e2e0000000001" }),
  ).toBeVisible();
  await expect(page.getByText(/project/i).first()).toBeVisible();

  // 4. Assets list.
  await page.goto("/assets/");
  // The kind tabs default to "dataset"; switch to "judge" to find
  // our mocked asset.
  await page.getByRole("button", { name: /^judges$/i }).click();
  await expect(page.getByText("q-e2e")).toBeVisible();

  // 5. Asset detail — click the asset_id.
  await page.getByRole("link", { name: "q-e2e" }).click();
  await expect(
    page.getByRole("heading", { name: "q-e2e" }),
  ).toBeVisible();
  // Versions table renders one row with the mocked version_id /
  // run_id badge / source.  The row carries our ``data-row-id``
  // attr so we can locate it without depending on text content.
  await expect(
    page.locator('[data-testid="asset-version-row"][data-run-id="run_e2e0000000001"]'),
  ).toBeVisible();
});
