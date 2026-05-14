#!/usr/bin/env bash
# EvalGuard GitHub Action entrypoint.
#
# 1. Validate config (fast pre-flight; exits 1 in <1s on schema errors).
# 2. Run evalguard, optionally with --baseline / --save-baseline.
# 3. Push the run to ``server`` if configured (v1 — Phase 1.5).
# 4. Read ``view --last --json`` to populate gate_status / cost_usd /
#    url outputs and write a step-summary markdown block (v1).
# 5. If a PR number is available, render a sticky comment from the same
#    JSON contract the CLI exposes, and POST/PATCH it via the GitHub
#    REST API. Posting failures don't fail the build — only gate
#    failures matched by ``fail-on`` do (v1).
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

# v1 (Phase 1.5)
SERVER="${EVALGUARD_INPUT_SERVER:-}"
TOKEN="${EVALGUARD_INPUT_TOKEN:-}"
PUSH="${EVALGUARD_INPUT_PUSH:-true}"
FAIL_ON="${EVALGUARD_INPUT_FAIL_ON:-gate_failed}"

WORKSPACE="${GITHUB_WORKSPACE:-$(pwd)}"
cd "$WORKSPACE"

emit() { echo "::group::$1"; }
endg() { echo "::endgroup::"; }

# Append a line to ``$GITHUB_OUTPUT``. Centralised so a future
# refactor to the heredoc-form (for multiline values) lands in
# one place. /dev/null fallback so the script runs standalone in
# tests without a GITHUB_OUTPUT file pointer.
gh_output() {
  echo "$1=$2" >> "${GITHUB_OUTPUT:-/dev/null}"
}

# Append markdown to the workflow step summary (rendered in the
# GitHub UI even when there's no PR to comment on).
gh_summary() {
  if [ -n "${GITHUB_STEP_SUMMARY:-}" ]; then
    cat >> "$GITHUB_STEP_SUMMARY"
  else
    cat > /dev/null
  fi
}

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

gh_output "exit_code" "$RUN_EXIT"
gh_output "run_id"    "${RUN_ID:-}"

# ─── 2½. push to server (Phase 1.5) ──────────────────────────────────
# Only when all three knobs line up:
#   - ``push`` input is truthy
#   - ``server`` is non-empty
#   - ``token``  is non-empty
# Anything else (forks without secrets, server-less smoke runs, etc.)
# skips the push silently — the comment + step-summary surfaces still
# render from the local run.
SERVER_URL=""
if [ "$PUSH" = "true" ] && [ -n "$SERVER" ] && [ -n "$TOKEN" ] && [ -n "${RUN_ID:-}" ]; then
  emit "evalguard push --last"
  # ``set +x`` so even an ``ACTIONS_STEP_DEBUG=true`` runner doesn't
  # echo the token via env-block expansion. Token isn't on the
  # cmdline, but the env shows up in some runners' step traces.
  set +x
  set +e
  EVALGUARD_SERVER="$SERVER" EVALGUARD_API_TOKEN="$TOKEN" \
    evalguard push --last
  PUSH_EXIT=$?
  set -e
  endg
  if [ "$PUSH_EXIT" -eq 0 ]; then
    # Compute the run URL client-side from the server input + run_id.
    # Don't grep the CLI's stdout — its format isn't a stability
    # contract.  Strip any trailing slash so ``server`` and
    # ``server/`` produce the same URL.
    SERVER_URL="${SERVER%/}/v1/runs/${RUN_ID}"
  else
    echo "::warning::evalguard push exited $PUSH_EXIT; URL output left empty."
  fi
fi
gh_output "url" "$SERVER_URL"

# ─── 2¾. read JSON outputs + write step summary (Phase 1.5) ──────────
# Use ``view --last --json`` for the wire-stable contract.  Capture
# stderr so a missing local DB doesn't pollute the action's log.
GATE_STATUS=""
COST_USD=""
if [ -n "${RUN_ID:-}" ]; then
  RUN_JSON="$(evalguard view --last --json 2>/dev/null || true)"
  if [ -n "$RUN_JSON" ]; then
    # One python invocation extracts both fields; ``jq``-equivalent
    # but tolerates missing keys without a NULL-cast dance.
    GATE_STATUS="$(echo "$RUN_JSON" | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(0)
v = d.get("gate_status")
if v: print(v)
' || true)"
    COST_USD="$(echo "$RUN_JSON" | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(0)
v = d.get("cost_usd")
if v is None:
    sys.exit(0)
try:
    print(f"{float(v):.4f}")
except (TypeError, ValueError):
    pass
' || true)"
  fi
fi
gh_output "gate_status" "$GATE_STATUS"
gh_output "cost_usd"    "$COST_USD"

# Markdown summary — shown in the workflow UI even without a PR.
# ``set -o pipefail`` + ``[ -n "$x" ] && echo`` would propagate the
# test's non-zero exit when ``$x`` is empty, killing the script;
# use ``if`` blocks so an unset variable simply skips its line.
{
  echo "### EvalGuard"
  echo
  if [ -n "${RUN_ID:-}" ]; then
    echo "**run:** \`$RUN_ID\`  "
    if [ -n "$GATE_STATUS" ]; then echo "**gate_status:** \`$GATE_STATUS\`  "; fi
    if [ -n "$COST_USD" ];    then echo "**cost:** \`\$${COST_USD}\`  ";       fi
    if [ -n "$SERVER_URL" ];  then echo "**server:** [$SERVER_URL]($SERVER_URL)  "; fi
  else
    echo "_evalguard run did not produce a row (exit $RUN_EXIT)._"
  fi
} | gh_summary

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
    gh_output "comment_url" "$COMMENT_URL"
    endg
  fi
fi

# ─── 4. apply ``fail-on`` (Phase 1.5) ─────────────────────────────────
# The exit policy decouples three concerns the v0 action conflated:
#
#  - **Infra error**  (RUN_EXIT = 1, or non-zero without a gate_status
#                      to interpret).  Always fails.  ``fail-on`` never
#                      applies here — a crashed CLI is never silently
#                      a green build.
#  - **Gate result**  (gate_status from the JSON).  Filtered through
#                     ``fail-on`` — that's the knob operators tune.
#  - **Override**     ``fail-on: never`` short-circuits gate matching,
#                     used for diagnostic runs that should never block.
#                     Infra errors still fail under ``never``.
#
# Note: ``evalguard run`` documents exit codes 0=pass, 2=gate fail,
# 1=infra error.  We don't trust those exits exclusively because some
# evaluator plugins can produce a real run that the CLI then exits
# non-zero on for unrelated reasons; the canonical truth is the
# gate_status in the JSON.

EXIT_CODE=0

# v0 used exit 2 for "gate failed" and exit 1 for "infra error" so
# consumer workflows could distinguish them via the step's exit
# code.  Preserve that contract: a fail-on match exits 2 (gate),
# anything else non-zero is 1 (infra).
if [ "$RUN_EXIT" -ne 0 ] && [ -z "$GATE_STATUS" ]; then
  # Infra error — preserve RUN_EXIT verbatim (1 in practice).
  EXIT_CODE="$RUN_EXIT"
elif [ "$FAIL_ON" = "never" ]; then
  EXIT_CODE=0
elif [ -n "$GATE_STATUS" ]; then
  # Comma-separated match against ``fail-on``.  We don't care about
  # whitespace inside (a user-typed list might have ``"gate_failed,
  # warned"``); normalise.
  FAIL_ON_NORM="$(echo "$FAIL_ON" | tr -d '[:space:]')"
  if [[ ",$FAIL_ON_NORM," == *,"$GATE_STATUS",* ]]; then
    EXIT_CODE=2
  fi
fi

exit "$EXIT_CODE"
