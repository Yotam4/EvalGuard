# EvalGuard Web

Next.js 16 + React 19 + Tailwind 4 SPA that consumes the same JSON
contract `evalguard view --json` produces.  Apache-2.0 (server-tier
code, same family as `apps/api/`); the MIT-licensed CLI / evaluators
/ schemas are unaffected.

## TL;DR — how it's built

| Layer | Choice | Why |
|---|---|---|
| Framework | Next.js 16, App Router | Modern React conventions; the plan called for it |
| Runtime | **Static export** (`output: 'export'`) | No Node server.  Bundle ships from S3 / nginx / the API's static mount |
| Language | TypeScript, `strict` | Catches API drift at the type level |
| Styling | Tailwind 4 + in-CSS `@theme` tokens | Default for Next 16; tokens stay grep-able |
| Server-state | `@tanstack/react-query` | Cache + retry + invalidation; cache keys include URL params |
| View-state | URL query string (`?id=run_xxx` etc.) | Shareable links + back-button works |
| Auth | Bearer token in `localStorage` | Matches the CLI's `EVALGUARD_API_TOKEN`; no cookies, no OAuth dance |
| Runtime config | Settings page (no `NEXT_PUBLIC_*`) | One static bundle deploys against any environment |
| Unit tests | Vitest 3 + Testing Library + happy-dom | Fast component / hook tests, no jsdom overhead |
| E2E | Playwright | Real Chromium, API mocked via `page.route()` |
| Air-gap | Zero outbound calls from the bundle except the configured server | No CDN fonts, no analytics, no version-check ping |

## Architecture in one diagram

```
                            ┌───────────────────────────┐
                            │   localStorage            │
                            │   ├── evalguard.serverUrl │
                            │   └── evalguard.token     │
                            └─────────────┬─────────────┘
                                          │ read by
                                          ▼
   browser ──── click ────► useRouter / useSearchParams (URL is the source of truth)
       │                              │
       ▼                              ▼
   <ConnectionGate>          useQuery({ queryKey: [..., urlParams] })
       │                              │
       ▼                              ▼
   gate child OR             ─── lib/api.ts ───► fetch(server + path)
   "open Settings"                   │
                                     ▼
                            <Card> / <Badge> / pure
                            presentational components
                            (DriftBody / ReviewItem /
                             AssetVersionsTable / …)
```

## Page tour

| Route | What it shows | Key endpoints |
|---|---|---|
| `/settings/` | Server URL + token entry, health probe, mixed-content guard | `GET /v1/health` |
| `/runs/` | List, project filter, `?source=cli\|otlp` tab strip, 10 s polling | `GET /v1/runs` |
| `/runs/detail/?id=run_xxx` | Run header, gates, trials, assets, drift card | `GET /v1/runs/{id}`, `GET /v1/runs/{id}/drift?vs=…` |
| `/assets/` | Cross-run aggregate, kind tabs, project filter | `GET /v1/assets` |
| `/assets/detail/?kind=&asset_id=&project_id=` | Every `(version, run, ingested)` for one asset | `GET /v1/assets/{kind}/{asset_id}/versions` |
| `/reviews?run_id=run_xxx` | Human review queue for failing rows + verdict submit | `GET /v1/reviews/queue`, `POST /v1/reviews`, `GET /v1/runs/{id}/reviews` |
| `/calls/?project=…` | Per-project call stream with cursor pagination, Recent / Failures tabs | `GET /v1/projects/{slug}/calls` |
| `/golden/?project=…` | Promoted golden-dataset candidates | `GET /v1/projects/{slug}/golden/candidates` |
| `/orgs/` | Org list + create (admin) | `GET /v1/orgs`, `POST /v1/orgs` |
| `/projects/` | Project list + create | `GET /v1/projects`, `POST /v1/projects` |
| `/keys/` | API-key CRUD; new keys flash plaintext once with Copy | `GET /v1/orgs/{id}/api_keys`, `POST /v1/orgs/{id}/api_keys`, `DELETE /v1/api_keys/{id}` |

`next build` finalises every route as static content; the exact
count moves as features land — verify with `npm run build`.

## File layout

```
src/
  app/
    layout.tsx                       # Nav + react-query provider
    page.tsx                         # → /runs
    globals.css                      # Tailwind + @theme tokens
    runs/
      page.tsx                       # list, ?source= tabs, polling
      detail/page.tsx                # run header + drift card
    reviews/page.tsx                 # queue + verdict form
    assets/
      page.tsx                       # aggregate
      detail/page.tsx                # one-asset drill-down
    settings/page.tsx                # URL / token / probe
    orgs/page.tsx
    projects/page.tsx
    keys/page.tsx
  components/
    Nav.tsx                          # top tab strip
    Card.tsx / Badge.tsx             # primitives + statusTone()
    ConnectionGate.tsx               # render Settings prompt if no creds
    ConfirmButton.tsx                # 4-s armed destructive button
    DriftBody.tsx                    # presentational drift report
    ReviewItem.tsx                   # one queue card + verdict form
    AssetVersionsTable.tsx           # version × run table
  lib/
    api.ts                           # typed fetch wrapper + every endpoint
    auth.ts                          # localStorage helpers
    query.tsx                        # QueryClient provider (5-min stale)
  types/
    testing.d.ts                     # jest-dom matcher refs for tsc
e2e/
  smoke.spec.ts                      # one Playwright flow, API mocked
playwright.config.ts                 # webServer: npx serve out
vitest.config.ts                     # happy-dom + alias @/ → src
```

## Conventions

### Container vs presentational

Each page file owns the data fetching (useQuery / useMutation) and
the URL state.  Sub-components that *just render props* live in
`src/components/` so they're directly unit-testable without router
/ React-Query scaffolding.  Example: `DriftBody` takes a
`DriftReport`, `ReviewItem` takes a `ReviewQueueItem` + `onSubmit`,
`AssetVersionsTable` takes `AssetVersionRecord[]`.

### URL as source of truth

Anything a user might want to share or bookmark lives in the URL
query string, not React state.  React Query's `queryKey` includes
the URL params so navigating updates the cache key automatically.
`router.replace` (not `push`) is preferred when the user is iterating
on a filter — keeps history clean.

### Stable test handles

UI components emit `data-testid` and content-specific `data-*` attrs
(`data-run-id`, `data-row-id`, `data-source`, `data-version-id`) so
e2e tests address rows by attribute, not text-matching.  Text
matching breaks the moment two ids share a prefix.

### One vitest file per surface

Component tests live in a sibling `__tests__/` folder.  The api.ts
fetch wrapper has its own suite under `src/lib/__tests__/api.test.ts`
that pins the URL composition for every endpoint — server contract
drift surfaces as a failing assertion, not a 404 at runtime.

## State management

| State | Lives in | Lifecycle |
|---|---|---|
| Server data (runs, drift, reviews, assets, …) | React Query cache | Stale-while-revalidate; 10 s poll on `/runs/`; manual invalidation on mutations |
| Filter / detail-view state (`?project=`, `?id=`, `?source=`, `?baseline=`) | URL query string | Survives reload; back-button works; shared by link |
| Bearer + server URL | `localStorage` | Browser-only; never sent to a 3rd party; cleared via Settings → Forget |
| Form drafts | Local `useState` inside a form component | Lost on unmount — intentional |

## Auth flow

1. User opens any page.  `ConnectionGate` checks `localStorage` for
   both `evalguard.serverUrl` AND `evalguard.token`.
2. Missing either → render a Settings prompt linking to `/settings/`.
3. On `/settings/`, the user pastes URL + token.  `Test connection`
   hits `GET /v1/health` (the only un-bearered endpoint).
4. `Save` writes both keys to `localStorage`.  The next route load
   passes the gate.
5. The api.ts fetch wrapper injects `Authorization: Bearer <token>`
   on every request except `/v1/health`.  A `PUBLIC_PATHS` set is
   the canonical allowlist so a future endpoint coincidentally
   ending in `/health` doesn't silently get un-authed.

## Air-gap notes

The bundle makes zero outbound calls except to the operator-
configured Server URL — no CDN fonts, no analytics, no version
checks.  `next build` produces a fully self-contained `out/` you
can serve from an internal nginx, S3 bucket, GitHub Pages, or the
API server's static mount.  See the top-level [README.md](../../README.md)
for the broader air-gap audit (server-side has no outbound HTTP
either; the only network call from the whole platform is the user-
directed `evalguard push` URL).

## Quickstart — local dev

```bash
cd apps/web
npm install
npm run dev
# → http://localhost:3000
```

In another shell, boot the API server with permissive CORS:

```bash
cd ..   # repo root
EVALGUARD_API_KEY=$(openssl rand -hex 32) \
EVALGUARD_CORS_ORIGINS="http://localhost:3000" \
  uvicorn evalguard_api.main:app --port 8787
```

**Naming gotcha** — there are two similarly-named env vars on
different sides of the wire:

- `EVALGUARD_API_KEY` (server-side) — bootstrap admin token the
  lifespan materialises into the `api_keys` table on first start.
  This is what the quickstart sets above so you have *some* valid
  bearer to paste into the UI.
- `EVALGUARD_API_TOKEN` (CLI / external clients) — the bearer your
  HTTP client sends. The CLI reads it from this var; the web UI
  reads its equivalent from `localStorage`.

For dev they happen to be the same string (the bootstrap key) — for
a real deployment you'd create per-user `evk_…` keys via
`POST /v1/orgs/{id}/api_keys` and hand those out, never the
bootstrap key.

Then in the browser at http://localhost:3000:
1. Open **Settings**, paste `http://localhost:8787` as the Server URL
   and the API key as the token.
2. Click **Test connection** — `mode: auth` and the db kind.
3. **Save**.  Navigate to **Runs** — empty list until you push a run
   with `evalguard push --last`.

## Quickstart — production-ish (static + nginx)

```bash
docker build -t evalguard-web -f apps/web/Dockerfile .
docker run --rm -p 8080:80 evalguard-web
# → http://localhost:8080
```

Point Settings at the API server's public URL.  Make sure the API's
`EVALGUARD_CORS_ORIGINS` includes the UI's origin.

## Tests

### Vitest (unit + component)

```bash
npm test           # one-shot
npm run test:watch # development
npm run type-check # tsc --noEmit
```

Tests live next to the code they cover:

- `src/lib/__tests__/api.test.ts` — every endpoint's URL composition,
  bearer injection, error translation, 204 handling, the 30-s
  default timeout.
- `src/lib/__tests__/auth.test.ts` — localStorage round-trip.
- `src/components/__tests__/*.test.tsx` — `Badge` (tones + statusTone),
  `ConnectionGate` (renders Settings prompt when unconfigured),
  `DriftBody` (formatting helpers + direction-of-regression tones),
  `ReviewItem` (Submit gated by verdict, trims note on submit),
  `AssetVersionsTable` (truncation, source badge, data-* attrs),
  `Tabs` (ARIA tablist semantics, roving tabindex, ArrowLeft/Right
  + Home/End keyboard navigation).

### Playwright (e2e)

```bash
npm run e2e:install     # one-time: install chromium
npm run e2e             # boots `npx serve out` + runs spec
```

One spec — `e2e/smoke.spec.ts` — walks Settings → Runs → Run detail
→ Assets → Asset detail → Calls stream → call detail.  API
responses are mocked via `page.route()` so the workflow doesn't
need a live FastAPI.

The `web-e2e` GitHub workflow runs this on every PR that touches
`apps/web/**`.  See [`.github/workflows/web-e2e.yml`](../../.github/workflows/web-e2e.yml).

### What the e2e spec doesn't cover (intentionally)

- The actual `/v1/*` server contract — that's the Python test
  suite's job (`pytest tests/api/`).
- Performance, accessibility — separate tooling.
- Real auth flow — bearer paste is the supported path; OAuth /
  SSO is enterprise-tier.

## Static-export caveats

The static export model has two implications for routing:

1. **No dynamic segments** at unknown values.  `/runs/[run_id]/`
   would require listing every run_id at build time.  Instead, the
   app uses `/runs/detail/?id=run_xxx` — a single static page that
   reads the param client-side.  Same pattern for `/assets/detail/`,
   `/reviews?run_id=…`, `/runs/detail/?baseline=…`.
2. **No server-side rendering of authenticated content.**  Every
   protected page renders inside a `ConnectionGate` that reads
   `localStorage` after mount.  The first paint is the gate; the
   real content paints once auth resolves.  Users on slow networks
   see a brief "Loading…".

## Operations

| Command | What it does |
|---|---|
| `npm run dev` | Next dev server at :3000 |
| `npm run build` | Static export to `out/` |
| `npm test` | Vitest one-shot |
| `npm run test:watch` | Vitest watch mode |
| `npm run type-check` | `tsc --noEmit` |
| `npm run e2e` | Playwright run (needs `e2e:install` first) |
| `npm run e2e:install` | `playwright install chromium --with-deps` |

## License

Apache-2.0.  The MIT-licensed CLI / evaluators / schemas are
unaffected.
