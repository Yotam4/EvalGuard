# EvalGuard API server image — Apache-2.0 (separate from MIT-licensed CLI).
#
# Build from the repo root so the build context includes packages/schemas
# (the contract canary tests reference it):
#
#   docker build -t evalguard-api -f apps/api/Dockerfile .
#
# Run with at least:
#
#   -e EVALGUARD_API_KEY=...        # required for prod; absent → open mode
#   -v <host-data>:/data            # persistence
#   -e EVALGUARD_DATABASE_URL=sqlite:////data/server.db
#   -p 8787:8787

FROM python:3.11-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    EVALGUARD_HOST=0.0.0.0 \
    EVALGUARD_PORT=8787 \
    EVALGUARD_DATABASE_URL=sqlite:////data/server.db

WORKDIR /app

# Install only the API package — the CLI / evaluators aren't needed
# inside the server image. Pinning is via apps/api/pyproject.toml.
COPY apps/api /app/apps/api
RUN pip install /app/apps/api && pip install uvicorn

# Ship the JSON-Schema contracts so future ingest-side validation has
# them on disk; harmless if unused.
COPY packages/schemas /app/packages/schemas

RUN mkdir -p /data

EXPOSE 8787

# Healthcheck talks to /v1/health (unauthenticated by design).
HEALTHCHECK --interval=10s --timeout=2s --start-period=2s \
  CMD python -c "import urllib.request, sys; \
    sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8787/v1/health', timeout=1).status == 200 else 1)"

CMD ["uvicorn", "evalguard_api.main:app", \
     "--host", "0.0.0.0", "--port", "8787", \
     "--log-level", "info"]
