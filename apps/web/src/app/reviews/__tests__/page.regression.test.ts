import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

/**
 * Source-pin regressions for ``apps/web/src/app/reviews/page.tsx``.
 *
 * Round-6 review caught two issues that source-pin tests can lock
 * cheaply without spinning up a real React tree:
 *
 * 1. ``existing.error`` was previously swallowed — only ``queue.error``
 *    rendered. A 403 / 404 / timeout on ``listRunReviews`` left the
 *    reviewer staring at an empty section. The fix is the
 *    ``queue.error ?? existing.error`` cascade; pin it so a refactor
 *    that splits the two branches again doesn't quietly regress.
 *
 * 2. ``verdictTone`` had no default case — a new server-side verdict
 *    would silently return ``undefined`` for its badge tone. The fix
 *    is an exhaustiveness check via ``const _exhaustive: never = v;``
 *    which fails the TypeScript build instead of dropping the tone
 *    at runtime.
 */

const PAGE_PATH = resolve(__dirname, "..", "page.tsx");
const PAGE_SRC = readFileSync(PAGE_PATH, "utf-8");

describe("/reviews page — round-6 regressions", () => {
  it("renders both queue and existing-reviews errors (round-6 BLOCKER)", () => {
    // The cascade pattern that surfaces either error to the user.
    expect(PAGE_SRC).toMatch(/queue\.error\s*\?\?\s*existing\.error/);
  });

  it("verdictTone has an exhaustiveness check (round-6 MAJOR)", () => {
    // ``const _exhaustive: never = v`` is the TS idiom for
    // exhaustive switches; this assertion fails if either side of
    // the switch is removed.
    expect(PAGE_SRC).toMatch(/const _exhaustive:\s*never\s*=\s*v/);
  });
});
