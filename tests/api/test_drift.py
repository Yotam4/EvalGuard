"""``GET /v1/runs/{run_id}/drift`` — Welch's t-test across two runs.

Strategy: ingest two synthetic ``RunIngest`` payloads with controlled
per-row ``latency_ms`` / ``cost_usd`` / ``passed`` distributions, then
hit the drift endpoint and assert on the verdict. Synthetic payloads
let us fix the populations precisely (no flaky randomness) — the CLI-
executor route used by ``test_runs.py`` only emits two rows per run,
which is below Welch's ``min_n=2`` floor and gives no statistical
power anyway.

The "parity with CLI" test pins the inlined Welch implementation
against the original in ``evalguard_cli.local.stats`` — the
``apps/api/evalguard_api/stats.py`` docstring promises identical
behaviour, this test makes that promise enforceable.
"""

from __future__ import annotations

import pytest


# ---------------------------------------------------------------------------
# Synthetic-payload helper


def _make_ingest_payload(
    *,
    run_id: str,
    project: str,
    rows: list[dict],
) -> dict:
    """Build a minimal ``RunIngest``-shaped dict from a list of row
    spec dicts. Each row spec sets ``passed`` / ``latency_ms`` /
    ``cost_usd``; everything else gets a sensible default.

    The ingest validator enforces nested cardinality caps and shape;
    keeping this helper minimal means a future schema change surfaces
    here as a 422 rather than silent test rot.

    ``trial_id`` is derived from ``run_id`` so two runs in the same
    test never collide on the global ``trials.trial_id`` PK.
    """
    n = len(rows)
    pass_count = sum(1 for r in rows if r["passed"])
    # Derive a unique-per-run trial_id matching ``^trial_[a-z0-9]{8,}$``.
    trial_id = "trial_" + run_id[len("run_"):][:12].lower().replace("_", "0")
    return {
        "schema_version": "1.0.0",
        "run_id": run_id,
        "project": project,
        "row_count":      n,
        "row_pass_count": pass_count,
        "row_fail_count": n - pass_count,
        "trials": [
            {
                "trial_id":    trial_id,
                "provider_id": "mock:m",
                "provider":    "mock",
                "model":       "m",
                "row_count":   n,
                "row_pass_count": pass_count,
                "row_fail_count": n - pass_count,
                "rows": [
                    {
                        "row_id":     f"r{i}",
                        "passed":     bool(spec["passed"]),
                        "n_scores":   1,
                        "latency_ms": int(spec["latency_ms"]),
                        "cost_usd":   float(spec["cost_usd"]),
                    }
                    for i, spec in enumerate(rows)
                ],
                "gates": [],
            },
        ],
    }


def _post(client, headers, payload) -> None:
    r = client.post("/v1/runs", json=payload, headers=headers)
    assert r.status_code == 201, r.text


def _identical_rows(n: int = 12) -> list[dict]:
    """All rows: passed=True, latency=100ms, cost=$0.001. Both sides
    of an "identical" comparison use this so Welch sees zero variance
    and the helper short-circuits to p_two_sided=1.0."""
    return [{"passed": True, "latency_ms": 100, "cost_usd": 0.001}] * n


def _shifted_rows(n: int = 12, *, latency_base: int) -> list[dict]:
    """Rows with latency centred on ``latency_base`` and a tiny per-row
    jitter so variance > 0 (Welch's zero-variance fast path otherwise
    suppresses the signal). pass / cost held constant."""
    return [
        {
            "passed":     True,
            "latency_ms": latency_base + (i % 3),  # 0/1/2 ms jitter
            "cost_usd":   0.001,
        }
        for i in range(n)
    ]


# ---------------------------------------------------------------------------
# Happy-path verdicts


def test_identical_runs_show_no_significant_drift(client, auth_headers):
    cur = _make_ingest_payload(
        run_id="run_currentaa",
        project="drift-test",
        rows=_identical_rows(),
    )
    base = _make_ingest_payload(
        run_id="run_baselineaa",
        project="drift-test",
        rows=_identical_rows(),
    )
    _post(client, auth_headers, cur)
    _post(client, auth_headers, base)

    r = client.get(
        f"/v1/runs/{cur['run_id']}/drift?vs={base['run_id']}",
        headers=auth_headers,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["current_run_id"]  == cur["run_id"]
    assert body["baseline_run_id"] == base["run_id"]
    assert body["alpha"] == 0.05

    by_name = {m["name"]: m for m in body["metrics"]}
    # All three metrics are computable (≥2 rows per side).
    assert set(by_name) == {"latency_ms", "cost_usd", "passed"}
    assert body["skipped"] == []
    for m in body["metrics"]:
        assert m["delta_mean"] == 0.0
        assert m["significant_at_alpha"] is False


def test_latency_shifted_run_flags_significant_drift(client, auth_headers):
    """Current run runs ~50 % slower than baseline — Welch must flag
    latency_ms as significantly different at α=0.05."""
    cur = _make_ingest_payload(
        run_id="run_slowxxxxxx",
        project="drift-test",
        rows=_shifted_rows(latency_base=150),
    )
    base = _make_ingest_payload(
        run_id="run_fastxxxxxx",
        project="drift-test",
        rows=_shifted_rows(latency_base=100),
    )
    _post(client, auth_headers, cur)
    _post(client, auth_headers, base)

    r = client.get(
        f"/v1/runs/{cur['run_id']}/drift?vs={base['run_id']}",
        headers=auth_headers,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    by_name = {m["name"]: m for m in body["metrics"]}
    lat = by_name["latency_ms"]
    assert lat["delta_mean"] > 0           # current is slower
    assert lat["significant_at_alpha"] is True
    assert lat["p_two_sided"] < 0.05
    # ``p_greater`` is the one-sided p-value for H1: current > baseline.
    # When the observed shift IS upward, that one-sided test rejects
    # H0 strongly — p_greater is small. ``p_less`` (H1: current <
    # baseline) is the complementary "implausible direction", ~1.
    assert lat["p_greater"] < 0.05
    assert lat["p_less"]    > 0.95
    # cost_usd was held constant, so it should NOT be flagged.
    assert by_name["cost_usd"]["significant_at_alpha"] is False


# ---------------------------------------------------------------------------
# Error paths


def test_drift_against_self_returns_400(client, auth_headers):
    """A user copy-pasting the same id into ``vs`` is a UI mistake;
    400 is more useful than a meaningless 0.0 verdict."""
    cur = _make_ingest_payload(
        run_id="run_selfaaaaaa",
        project="drift-test",
        rows=_identical_rows(),
    )
    _post(client, auth_headers, cur)
    r = client.get(
        f"/v1/runs/{cur['run_id']}/drift?vs={cur['run_id']}",
        headers=auth_headers,
    )
    assert r.status_code == 400
    assert "itself" in r.json()["detail"]


def test_drift_with_unknown_current_run_returns_404(client, auth_headers):
    base = _make_ingest_payload(
        run_id="run_baselineaa",
        project="drift-test",
        rows=_identical_rows(),
    )
    _post(client, auth_headers, base)
    r = client.get(
        f"/v1/runs/run_doesnotexist/drift?vs={base['run_id']}",
        headers=auth_headers,
    )
    assert r.status_code == 404
    assert "run_doesnotexist" in r.json()["detail"]


def test_drift_with_unknown_baseline_returns_404(client, auth_headers):
    cur = _make_ingest_payload(
        run_id="run_currentaa",
        project="drift-test",
        rows=_identical_rows(),
    )
    _post(client, auth_headers, cur)
    r = client.get(
        f"/v1/runs/{cur['run_id']}/drift?vs=run_alsomissing",
        headers=auth_headers,
    )
    assert r.status_code == 404
    assert "run_alsomissing" in r.json()["detail"]


def test_drift_rejects_alpha_outside_0_1(client, auth_headers):
    """``alpha`` is constrained ``gt=0, lt=1`` on the route; FastAPI
    returns 422 on the boundary."""
    cur = _make_ingest_payload(
        run_id="run_aaaaaaaaaa",
        project="drift-test",
        rows=_identical_rows(),
    )
    base = _make_ingest_payload(
        run_id="run_bbbbbbbbbb",
        project="drift-test",
        rows=_identical_rows(),
    )
    _post(client, auth_headers, cur)
    _post(client, auth_headers, base)
    for bad in (0.0, 1.0, -0.1, 5):
        r = client.get(
            f"/v1/runs/{cur['run_id']}/drift?vs={base['run_id']}&alpha={bad}",
            headers=auth_headers,
        )
        assert r.status_code == 422, (bad, r.text)


def test_drift_skips_metric_with_too_few_rows(client, auth_headers):
    """A run with one row has insufficient samples for Welch (needs
    ≥2 per side). The endpoint must surface that as a ``skipped``
    entry rather than failing the whole report."""
    cur = _make_ingest_payload(
        run_id="run_singlerow0",
        project="drift-test",
        rows=[{"passed": True, "latency_ms": 100, "cost_usd": 0.001}],
    )
    base = _make_ingest_payload(
        run_id="run_baselineaa",
        project="drift-test",
        rows=_identical_rows(),
    )
    _post(client, auth_headers, cur)
    _post(client, auth_headers, base)
    r = client.get(
        f"/v1/runs/{cur['run_id']}/drift?vs={base['run_id']}",
        headers=auth_headers,
    )
    assert r.status_code == 200
    body = r.json()
    assert body["metrics"] == []
    skipped_names = {s["name"] for s in body["skipped"]}
    assert skipped_names == {"latency_ms", "cost_usd", "passed"}
    for s in body["skipped"]:
        assert "≥2" in s["reason"]


# ---------------------------------------------------------------------------
# Cross-tenant isolation


def test_drift_cross_org_returns_404_for_member(
    client, auth_headers, make_org, make_member_token,
):
    """A member of org-A asking about a run in org-B (or comparing one
    of theirs against a B-owned baseline) must get 404 — same shape
    as ``GET /v1/runs/{id}`` so the endpoint can't be used for cross-
    tenant enumeration."""
    make_org("acme")
    member_default = make_member_token("org_default", name="d")
    member_acme    = make_member_token("org_acme",    name="a")

    p_default = _make_ingest_payload(
        run_id="run_indefault0",
        project="drift-default",
        rows=_identical_rows(),
    )
    p_acme = _make_ingest_payload(
        run_id="run_inacmexxxx",
        project="drift-acme",
        rows=_identical_rows(),
    )
    _post(client, {"Authorization": f"Bearer {member_default}"}, p_default)
    _post(client, {"Authorization": f"Bearer {member_acme}"},    p_acme)

    # default member → asks about acme's run as ``current``.
    r = client.get(
        f"/v1/runs/{p_acme['run_id']}/drift?vs={p_default['run_id']}",
        headers={"Authorization": f"Bearer {member_default}"},
    )
    assert r.status_code == 404
    # default member → asks about acme's run as ``vs``.
    r2 = client.get(
        f"/v1/runs/{p_default['run_id']}/drift?vs={p_acme['run_id']}",
        headers={"Authorization": f"Bearer {member_default}"},
    )
    assert r2.status_code == 404

    # Admin sees both — cross-org comparison succeeds.
    r3 = client.get(
        f"/v1/runs/{p_default['run_id']}/drift?vs={p_acme['run_id']}",
        headers=auth_headers,
    )
    assert r3.status_code == 200, r3.text


def test_drift_requires_authentication(client, tmp_path):
    cur = _make_ingest_payload(
        run_id="run_needauthxx",
        project="drift-test",
        rows=_identical_rows(),
    )
    base = _make_ingest_payload(
        run_id="run_needauthyy",
        project="drift-test",
        rows=_identical_rows(),
    )
    _post(client, {"Authorization": "Bearer test-secret"}, cur)
    _post(client, {"Authorization": "Bearer test-secret"}, base)
    r = client.get(f"/v1/runs/{cur['run_id']}/drift?vs={base['run_id']}")
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# Welch parity with the CLI implementation


def _flatten_welch(result) -> tuple[float, ...]:
    return (
        result.t_stat, result.dof,
        result.p_two_sided, result.p_less, result.p_greater,
        result.n1, result.n2,
        result.mean1, result.mean2,
        result.var1, result.var2,
    )


@pytest.mark.parametrize(
    "sample1, sample2",
    [
        # Symmetric, well-separated — non-trivial t-stat both sides have signal.
        ([1.0, 2.0, 3.0, 4.0, 5.0], [3.0, 4.0, 5.0, 6.0, 7.0]),
        # Same mean, different variance — t≈0, p_two_sided≈1.
        ([2.0, 2.5, 3.0, 3.5, 4.0], [1.0, 2.5, 3.0, 3.5, 5.0]),
        # Highly asymmetric n.
        ([1.0, 2.0], [1.0, 1.5, 2.0, 2.5, 3.0, 3.5, 4.0, 4.5]),
        # Boolean-ish (matches the ``passed`` metric distribution).
        ([1.0, 1.0, 0.0, 1.0, 1.0, 0.0, 1.0],
         [0.0, 0.0, 1.0, 0.0, 1.0, 0.0, 0.0]),
    ],
)
def test_welch_parity_with_cli_implementation(sample1, sample2):
    """The server's inlined Welch (``apps/api/evalguard_api/stats.py``)
    must produce bit-for-bit identical output to the CLI's original
    (``packages/cli/evalguard_cli/local/stats.py``) for the same
    inputs. Both files exist for release-cadence reasons; this test
    is the load-bearing constraint that says they must stay in lockstep.
    """
    from evalguard_api.stats import welchs_t_test as server_welch
    from evalguard_cli.local.stats import welchs_t_test as cli_welch

    server_out = server_welch(sample1, sample2)
    cli_out    = cli_welch(sample1, sample2)
    assert _flatten_welch(server_out) == _flatten_welch(cli_out)
