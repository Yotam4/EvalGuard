FROM python:3.11-slim

# curl + jq for the GitHub-API call in entrypoint.sh; git so the audit
# log captures branch/sha. Everything else is pure-Python.
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl jq git ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/evalguard

# Layer the install: package manifests first (cache-friendly), then code.
COPY packages/cli/pyproject.toml         packages/cli/pyproject.toml
COPY packages/evaluators/pyproject.toml  packages/evaluators/pyproject.toml
COPY packages/cli/evalguard_cli          packages/cli/evalguard_cli
COPY packages/evaluators/evalguard_evaluators  packages/evaluators/evalguard_evaluators
COPY packages/schemas                    packages/schemas

RUN pip install --no-cache-dir \
        -e packages/evaluators \
        -e packages/cli

COPY packages/action/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
