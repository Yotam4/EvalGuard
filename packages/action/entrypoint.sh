#!/usr/bin/env bash
# EvalGuard GitHub Action entrypoint.
#
# 1. Validate config (fast pre-flight; exits 1 in <1s on schema errors).
# 2. Run evalguard, optionally with --baseline / --save-baseline.
# 3. If a PR number is available, render a sticky comment from the same
#    JSON contract the CLI exposes, and POST/PATCH it via the GitHub
#    REST API. Posting failures don't fail the build — only gate
#    failures (run exit 2) do.
#
# Inputs come in as EVALGUARD_INPUT_* env vars set by action.yml.

set -euo pipefail

CONFIG="${EVALGUARD_INPUT_CONFIG:-evalguard.yaml}"
BASELINE="${EVALGUARD_INPUT_BASELINE:-}"
SAVE_BASELINE="${EVALGUARD_INPUT_SAVE_BASELINE:-}"
POST_COMMENT="${EVALGUARD_INPUT_COMMENT:-true}"
MARKER="${EVALGUARD_INPUT_MARKER:-<!-- evalguard:pr-comment -->}"
PR_NUMBER="${EVALGUARD_INPUT_PR_NUMBER:-}"
FAIL_FAST="${EVALGUARD_INPUT_FAIL_FAST:-false}"

WORKSPACE="${GITHUB_WORKSPACE:-$(pwd)}"
cd "$WORKSPACE"

emit() { echo "::group::$1"; }
endg() { echo "::endgroup::"; }

# ─── 1. validate ──────────────────────────────────────────────────────
emit "evalguard validate"
evalguard validate -c "$CONFIG"
endg

# ─── 2. run ───────────────────────────────────────────────────────────
RUN_ARGS=(-c "$CONFIG")
[ "$FAIL_FAST"   = "true" ] && RUN_ARGS+=(--fail-fast)
[ -n "$BASELINE"      ]    && RUN_ARGS+=(--baseline "$BASELINE")
[ -n "$SAVE_BASELINE" ]    && RUN_ARGS+=(--save-baseline "$SAVE_BASELINE")

emit "evalguard run ${RUN_ARGS[*]}"
set +e
evalguard run "${RUN_ARGS[@]}"
RUN_EXIT=$?
set -e
endg

# Capture the most-recent run_id for output + comment rendering.
RUN_ID="$(python3 -c '
import sqlite3, sys, pathlib
db = pathlib.Path(".evalguard/local.db")
if not db.exists(): sys.exit(0)
c = sqlite3.connect(db); c.row_factory = sqlite3.Row
r = c.execute("SELECT run_id FROM runs ORDER BY started_at DESC LIMIT 1").fetchone()
if r: print(r["run_id"])
')"

# Auto-detect PR number from the GH event payload if the user didn't
# pass one explicitly. ``GITHUB_EVENT_PATH`` exists for any event; only
# pull_request events carry ``.pull_request.number``.
if [ -z "$PR_NUMBER" ] && [ -n "${GITHUB_EVENT_PATH:-}" ] && [ -f "$GITHUB_EVENT_PATH" ]; then
  PR_NUMBER="$(jq -r '.pull_request.number // empty' "$GITHUB_EVENT_PATH" 2>/dev/null || true)"
fi

# Outputs (consumed by downstream steps via ``${{ steps.X.outputs.* }}``).
{
  echo "exit_code=$RUN_EXIT"
  echo "run_id=${RUN_ID:-}"
} >> "${GITHUB_OUTPUT:-/dev/null}"

# ─── 3. comment ───────────────────────────────────────────────────────
COMMENT_URL=""
if [ "$POST_COMMENT" = "true" ] && [ -n "$PR_NUMBER" ] && [ -n "${RUN_ID:-}" ]; then
  emit "evalguard comment"
  COMMENT_FILE="$(mktemp)"
  COMMENT_ARGS=(--last --marker "$MARKER" --out "$COMMENT_FILE")
  [ -n "$BASELINE" ] && COMMENT_ARGS+=(--baseline "$BASELINE")
  evalguard comment "${COMMENT_ARGS[@]}"
  endg

  if [ -z "${GITHUB_TOKEN:-}" ]; then
    echo "::warning::GITHUB_TOKEN not set; skipping PR comment."
  else
    emit "post sticky PR comment"
    REPO="${GITHUB_REPOSITORY}"
    API="https://api.github.com"
    BODY="$(jq -Rs . < "$COMMENT_FILE")"

    # Find existing comment matching the marker. List up to 100 — for
    # PRs with thousands of comments this'll need pagination, but
    # that's a Phase 1.5 polish.
    EXISTING_ID="$(curl -sS \
      -H "Authorization: Bearer $GITHUB_TOKEN" \
      -H "Accept: application/vnd.github+json" \
      "$API/repos/$REPO/issues/$PR_NUMBER/comments?per_page=100" \
      | jq -r --arg marker "$MARKER" \
        '[.[] | select(.body | startswith($marker))] | .[0].id // empty')"

    if [ -n "$EXISTING_ID" ]; then
      RESP="$(curl -sS -X PATCH \
        -H "Authorization: Bearer $GITHUB_TOKEN" \
        -H "Accept: application/vnd.github+json" \
        "$API/repos/$REPO/issues/comments/$EXISTING_ID" \
        -d "{\"body\": $BODY}")"
    else
      RESP="$(curl -sS -X POST \
        -H "Authorization: Bearer $GITHUB_TOKEN" \
        -H "Accept: application/vnd.github+json" \
        "$API/repos/$REPO/issues/$PR_NUMBER/comments" \
        -d "{\"body\": $BODY}")"
    fi
    COMMENT_URL="$(echo "$RESP" | jq -r '.html_url // empty')"
    echo "comment_url=$COMMENT_URL" >> "${GITHUB_OUTPUT:-/dev/null}"
    endg
  fi
fi

exit "$RUN_EXIT"
