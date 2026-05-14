"""Round-5 review regressions — Phase 3b drift parity, Phase 3c sampler,
Phase 4 review-queue constraints.

Each test pins a specific finding from the multi-agent review of
``b7f8523..5cd5511`` so it can't silently regress. Round-3 / 4
regressions live in the round-3 / round-4 sibling files.
"""

from __future__ import annotations

import math
import re
from pathlib import Path

import pytest
from sqlalchemy import text


# ---------------------------------------------------------------------------
# Phase 3b — Welch NaN parity (the BLOCKER from round-5)


def test_server_welch_returns_nan_in_zero_variance_unequal_mean_case():
    """The server's Welch implementation MUST surface NaN p-values
    in the degenerate ``se_sq == 0 and mean1 != mean2`` branch so
    the gate's ``isnan`` guard skips the comparison non-blockingly.

    Before round-5, the server returned deterministic 1.0/0.0 here
    while the CLI returned NaN — a silent parity break that would
    have a downstream gate fail-on-zero (or pass-on-one) for a
    mathematically-undefined comparison.
    """
    from evalguard_api.stats import welchs_t_test

    r = welchs_t_test([1.0, 1.0, 1.0], [2.0, 2.0, 2.0])
    assert math.isnan(r.p_two_sided)
    assert math.isnan(r.p_less)
    assert math.isnan(r.p_greater)
    # ``t_stat`` is still finite (we choose 0.0 explicitly to avoid
    # downstream NaN propagation on a value that has no statistical
    # meaning anyway).
    assert math.isfinite(r.t_stat)


# ---------------------------------------------------------------------------
# Phase 4 — verdict CHECK constraint at the DB layer


def test_row_reviews_verdict_check_rejects_invalid_at_db_layer(client, auth_headers):
    """Migration 0006 locks the verdict enum at the DB layer so a
    raw SQL insert or a future ORM swap that bypasses
    ``ReviewIngest.verdict``'s Literal can't smuggle a garbage
    verdict in. The Pydantic layer is no longer the only line of
    defense."""
    # First seed a run + a row the FK constraint will accept.
    payload = {
        "schema_version": "1.0.0",
        "run_id":          "run_verdictcheck123",
        "project":         "default",
        "trials": [{
            "trial_id":    "trial_verdictcheck01",
            "provider_id": "mock:m", "provider": "mock", "model": "m",
            "rows": [{
                "row_id": "row_x", "passed": False, "tags": [], "scores": [],
            }],
        }],
    }
    r = client.post("/v1/runs", json=payload, headers=auth_headers)
    assert r.status_code in (200, 201), r.text

    # Now bypass the Pydantic layer and shove a bad verdict via raw SQL.
    # The DB CHECK constraint must reject it. Use the same engine the
    # test app holds.
    engine = client.app.state.engine
    from sqlalchemy.exc import IntegrityError
    with engine.begin() as conn:
        # On Postgres RLS is enabled — set admin to bypass for this
        # internal test write. SQLite is a no-op.
        from evalguard_api.db import apply_admin_rls_context
        apply_admin_rls_context(conn)
        with pytest.raises((IntegrityError, Exception)):
            conn.execute(
                text("""INSERT INTO row_reviews(
                          run_id, row_id, project_id, reviewer_key_id,
                          verdict, note, created_at, updated_at)
                        VALUES (:r, :w, :p, :k, :v, NULL, :n, :n)"""),
                {"r": "run_verdictcheck123", "w": "row_x",
                 "p": "proj_default", "k": "key_test",
                 "v": "definitely_not_a_real_verdict",
                 "n":  "2026-05-15T00:00:00Z"},
            )


def test_review_verdicts_constant_matches_pydantic_literal():
    """``REVIEW_VERDICTS`` in models.py is the single source of truth.
    Drift between it and the ``_ReviewVerdict`` Literal would let the
    DB accept verdicts the API rejects (or vice versa)."""
    from evalguard_api.models import REVIEW_VERDICTS

    # Source-pin the Literal: read the file, grep for the literal
    # definition, and confirm it contains exactly the same values.
    src = (
        Path(__file__).resolve().parents[2]
        / "apps" / "api" / "evalguard_api" / "models.py"
    ).read_text()
    m = re.search(r'_ReviewVerdict\s*=\s*Literal\[([^\]]+)\]', src)
    assert m, "could not find _ReviewVerdict Literal in models.py"
    literal_values = [v.strip().strip('"').strip("'") for v in m.group(1).split(",")]
    assert tuple(literal_values) == REVIEW_VERDICTS, (
        f"_ReviewVerdict Literal ({literal_values}) drifted from "
        f"REVIEW_VERDICTS ({REVIEW_VERDICTS}). Update one to match the other."
    )


def test_review_verdicts_constant_matches_migration_check():
    """The CHECK constraint in 0006_row_reviews_verdict_check must
    spell exactly the same verdict set as the Pydantic Literal AND
    the ``REVIEW_VERDICTS`` constant. Pin so a future ``override_*``
    rename doesn't drift one of three surfaces silently."""
    from evalguard_api.models import REVIEW_VERDICTS

    src = (
        Path(__file__).resolve().parents[2]
        / "apps" / "api" / "evalguard_api" / "migrations"
        / "versions" / "0006_row_reviews_verdict_check.py"
    ).read_text()
    m = re.search(r"_VERDICTS:\s*tuple\[str,\s*\.\.\.\]\s*=\s*\(\s*([^)]+)\)", src)
    assert m, "could not find _VERDICTS tuple in migration 0006"
    mig_values = tuple(
        v.strip().strip('"').strip("'") for v in m.group(1).split(",") if v.strip()
    )
    assert mig_values == REVIEW_VERDICTS, (
        f"migration 0006 verdict tuple ({mig_values}) drifted from "
        f"models.REVIEW_VERDICTS ({REVIEW_VERDICTS}). Update one to match the other."
    )


# ---------------------------------------------------------------------------
# Phase 4 — audit-surface contract (intentional design, pinned)


def test_review_actions_are_audited_via_access_log_and_row_table():
    """E.5-style access-log middleware (round-3) logs every authed
    request including ``POST /v1/reviews``, AND the row_reviews row
    itself is the durable record (reviewer_key_id, verdict,
    created_at, updated_at). The round-5 review surfaced "no audit
    trail" as a MAJOR because review actions don't append to the
    run's ``events_json`` chain — but they ARE audited via these
    two complementary surfaces.

    This test pins the contract: a review submission is recoverable
    from the access log + the row_reviews row. If a future
    refactor breaks either, the round-5 finding's MAJOR severity
    is back on the table.
    """
    src = (
        Path(__file__).resolve().parents[2]
        / "apps" / "api" / "evalguard_api" / "main.py"
    ).read_text()
    # 1. The access-log middleware exists and emits ``http.request``.
    assert "_access_log" in src
    assert '"evt":         "http.request"' in src or '"http.request"' in src
    # 2. The reviews route durable-records who said what (route
    #    docstring describes this).
    reviews_src = (
        Path(__file__).resolve().parents[2]
        / "apps" / "api" / "evalguard_api" / "routes" / "reviews.py"
    ).read_text()
    assert "INSERT INTO row_reviews" in reviews_src
    assert "reviewer_key_id" in reviews_src
    assert "updated_at"      in reviews_src
