# EvalGuard Web — Apache-2.0 (server core licence — same as apps/api).
#
# Multi-stage:
#   1. Node builder produces ``out/`` via ``next build`` (static export).
#   2. nginx:alpine serves the static bundle.
#
# The runtime image has no Node — just nginx serving prebuilt files.
# That keeps the image small (~50 MB) and removes a whole runtime
# from the prod surface area.
#
# Build from the repo root so the build context can include any
# future shared assets:
#
#     docker build -t evalguard-web -f apps/web/Dockerfile .
#
# Run with:
#
#     docker run --rm -p 8080:80 evalguard-web
#
# Then point Settings → "Server URL" at the FastAPI server's URL.

FROM node:22-alpine AS builder

WORKDIR /app

# Copy lockfile + package.json first for better layer caching.
COPY apps/web/package.json apps/web/package-lock.json* ./
RUN npm ci --no-audit --no-fund

COPY apps/web/ ./

RUN npm run build


FROM nginx:1.27-alpine AS runtime

# nginx config: serve the static export, fall back to index.html
# for SPA-style client-side routing (React Query / Next routing
# handles the rest after the initial bundle loads).
RUN <<EOF cat > /etc/nginx/conf.d/default.conf
server {
  listen      80;
  server_name _;
  root        /usr/share/nginx/html;
  index       index.html;

  # Static-export pages already include trailingSlash. SPA fallback
  # for direct hits on a not-yet-exported path or a trimmed slug.
  location / {
    try_files \$uri \$uri/ \$uri.html /index.html;
  }

  # Aggressive caching for hashed assets, none for HTML.
  location ~* \\.(js|css|svg|png|jpg|jpeg|gif|ico|woff2?)\$ {
    expires 1y;
    add_header Cache-Control "public, immutable";
  }

  location ~* \\.html\$ {
    expires -1;
    add_header Cache-Control "no-store";
  }
}
EOF

COPY --from=builder /app/out /usr/share/nginx/html

EXPOSE 80
HEALTHCHECK --interval=10s --timeout=2s --start-period=2s \
  CMD wget -q -O - http://127.0.0.1/ > /dev/null || exit 1
