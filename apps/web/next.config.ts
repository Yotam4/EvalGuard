import type { NextConfig } from "next";

/**
 * EvalGuard web is a pure SPA against the FastAPI server's JSON
 * contract. Server-side rendering would force us to run a Node
 * process in production for zero benefit (the data lives behind a
 * bearer-authed API the browser already knows how to call). Static
 * export drops the Node runtime entirely — the bundle ships from
 * any CDN, an S3 bucket, or even straight from the FastAPI server's
 * static-files mount.
 *
 * The trade-off: ``next/image`` server optimization, dynamic API
 * routes, and on-demand ISR are off the table. None matter for an
 * admin UI that only reads from a known API.
 */
const config: NextConfig = {
  output: "export",
  // ``trailingSlash`` makes static-host routing robust under
  // dumb file servers (S3, GitHub Pages) that need ``/foo/`` to
  // serve ``/foo/index.html``.
  trailingSlash: true,
  reactStrictMode: true,
  // The runtime API URL is supplied per-deployment by the user via
  // localStorage (Settings page). No build-time NEXT_PUBLIC_API_URL
  // — that would bake the URL into the bundle and prevent reusing a
  // single static deployment across staging/prod.
};

export default config;
