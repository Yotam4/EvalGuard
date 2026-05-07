# EvalGuard Web

Next.js 16 + React 19 + Tailwind 4 SPA that consumes the same JSON
contract `evalguard view --json` produces. Phase 2.6a from the
project plan; Apache-2.0 licensed (same family as `apps/api/`).

## Phase 2.6a scope

- `apps/web/` Next.js scaffold (App Router, TypeScript, Tailwind)
- API client (`src/lib/api.ts`) with typed mirrors of the
  `evalguard.run.schema.json` shapes
- **Settings page** — server URL + bearer token + connectivity probe
- **Runs list page** — `GET /v1/runs` with project filter + ingest
  metadata
- **Run detail page** — `GET /v1/runs/{id}` rendering trials,
  gates, assets, and per-trial gate detail
- Bearer-auth UX (token persisted in `localStorage`; pages gate on
  it via `<ConnectionGate>`)
- Static export (`output: 'export'`) so the bundle deploys behind
  any static host (S3, GitHub Pages, the FastAPI server's static
  mount, or the included nginx Dockerfile)

## What's NOT in scope yet (deferred to 2.6b)

- Datasets / Prompts / Judges pages — need new server endpoints first
- Org / Project / API-key management UI
- Tests (Vitest unit + Playwright e2e)
- Real-time updates (polling / SSE)
- Login flow beyond bearer-paste (OAuth / SSO is enterprise-tier)

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

Then in the browser at http://localhost:3000:
1. Open **Settings**, paste `http://localhost:8787` as the Server URL
   and the API key as the token.
2. Click **Test connection** — you should see `mode: auth` and the
   db kind.
3. Click **Save**. Navigate to **Runs** — empty list until you push
   a run with `evalguard push --last`.

## Quickstart — production-ish (static + nginx)

```bash
docker build -t evalguard-web -f apps/web/Dockerfile .
docker run --rm -p 8080:80 evalguard-web
# → http://localhost:8080
```

Point Settings at the API server's public URL. Make sure the API's
`EVALGUARD_CORS_ORIGINS` includes the UI's origin.

## Architecture decisions

| Decision | Choice | Why |
|---|---|---|
| Framework | Next.js App Router | Plan calls for it; modern React conventions |
| SSR | **Static export** (`output: 'export'`) | Server has the data; SSR adds Node-server complexity for zero benefit |
| Language | TypeScript strict | Catches API drift at the type level |
| Styling | Tailwind 4 | Default for Next; in-CSS `@theme` keeps tokens auditable |
| Data | `@tanstack/react-query` | Cache + retry + invalidation built-in |
| Auth | Bearer token in `localStorage` | Matches the CLI's `EVALGUARD_API_TOKEN`; no OAuth flow needed yet |
| Detail routing | `/runs/detail/?id=run_xxx` (query string) | Static export can't serve unknown dynamic segments without listing them at build time |
| Runtime URL | Configured via Settings page (no `NEXT_PUBLIC_*`) | One static bundle deploys against staging/prod |

## Layout

```
src/
  app/
    layout.tsx               # nav + react-query provider
    page.tsx                 # → /runs redirect
    globals.css              # tailwind + @theme tokens
    runs/
      page.tsx               # list
      detail/
        page.tsx             # detail (?id=run_xxx)
    settings/
      page.tsx               # token / URL / health probe
  lib/
    api.ts                   # typed fetch wrapper + run.schema.json mirrors
    auth.ts                  # localStorage helpers
    query.tsx                # QueryClient provider
  components/
    Nav.tsx                  # top nav
    Card.tsx                 # primitive
    Badge.tsx                # status / gate pill (+ statusTone)
    ConnectionGate.tsx       # render Settings prompt when no token
```

## Operations

- **Type check:** `npm run type-check`
- **Build:** `npm run build` (writes `out/`)
- **Dev server:** `npm run dev`

## License

Apache-2.0 (server-tier code, same family as `apps/api/`). The
MIT-licensed CLI / evaluators / schemas are unaffected.
