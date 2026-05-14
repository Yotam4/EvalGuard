"""``/v1/reviews/*`` — Phase 4 human review queue.

Strategy: ingest a synthetic run with one passing row and one failing
row (the failing one has an associated failing gate so the queue's
"failing rows" policy picks it up). Drive the three endpoints through
the TestClient against a real Alembic-migrated DB.
"""

from __future__ import annotations


# ---------------------------------------------------------------------------
# Synthetic-payload helper — small RunIngest with controlled per-row
# pass/fail + a gate that fails so the queue policy has something
# to surface. trial_id derives from run_id so two runs in one test
# never collide on the global trials PK.


def _make_run_with_failing_row(run_id: str, project: str) -> dict:
    return {
        "schema_version": "1.0.0",
        "run_id": run_id,
        "project": project,
        "row_count":      2,
        "row_pass_count": 1,
        "row_fail_count": 1,
        "trials": [{
            "trial_id":    "trial_" + run_id[len("run_"):][:12].lower().replace("_", "0"),
            "provider_id": "mock:m",
            "provider":    "mock",
            "model":       "m",
            "row_count":      2,
            "row_pass_count": 1,
            "row_fail_count": 1,
            "rows": [
                {"row_id": "r-pass", "passed": True,  "n_scores": 1,
                 "latency_ms": 50,  "cost_usd": 0.001},
                {"row_id": "r-fail", "passed": False, "n_scores": 1,
                 "latency_ms": 200, "cost_usd": 0.002,
                 "tags": ["edge"]},
            ],
            # Trial-scoped gate that fails — the queue picks rows up
            # when there is any failing gate on the same (run, trial).
            "gates": [{
                "gate_name": "min_pass_rate",
                "severity":  "block",
                "blocking":  True,
                "passed":    False,
                "layer":     2,
                "details":   [],
            }],
        }],
    }


def _post(client, headers, payload) -> None:
    r = client.post("/v1/runs", json=payload, headers=headers)
    assert r.status_code == 201, r.text


# ---------------------------------------------------------------------------
# Queue


def test_queue_surfaces_only_failing_rows_not_yet_reviewed(client, auth_headers):
    payload = _make_run_with_failing_row("run_queue000000", "review-q")
    _post(client, auth_headers, payload)

    r = client.get(
        f"/v1/reviews/queue?run_id={payload['run_id']}",
        headers=auth_headers,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["run_id"] == payload["run_id"]
    # ``r-pass`` doesn't carry a failing gate ⇒ excluded from the queue.
    # ``r-fail`` is associated with the trial-failing gate ⇒ included.
    row_ids = [it["row_id"] for it in body["items"]]
    assert row_ids == ["r-fail"]
    item = body["items"][0]
    assert item["passed"]        is False
    assert item["failing_gates"] == ["min_pass_rate"]
    assert item["tags"]          == ["edge"]


def test_queue_drops_a_row_once_the_caller_has_reviewed_it(client, auth_headers):
    payload = _make_run_with_failing_row("run_dropafter01", "review-q")
    _post(client, auth_headers, payload)

    # Submit a verdict — the queue must then exclude that row for the
    # same reviewer (key_id). A different reviewer would still see it.
    r = client.post(
        "/v1/reviews",
        json={"run_id": payload["run_id"], "row_id": "r-fail",
              "verdict": "agree"},
        headers=auth_headers,
    )
    assert r.status_code == 201, r.text

    r = client.get(
        f"/v1/reviews/queue?run_id={payload['run_id']}",
        headers=auth_headers,
    )
    assert r.status_code == 200
    assert r.json()["items"] == []


def test_queue_cross_org_run_returns_404(
    client, auth_headers, make_org, make_member_token,
):
    """A run in another org must 404 even if the caller knows the
    run_id — same shape as ``GET /v1/runs/{id}``."""
    make_org("acme")
    member_default = make_member_token("org_default", name="d")
    member_acme    = make_member_token("org_acme",    name="a")
    p_acme = _make_run_with_failing_row("run_xorgaaaaaa", "acme-proj")
    _post(client, {"Authorization": f"Bearer {member_acme}"}, p_acme)

    r = client.get(
        f"/v1/reviews/queue?run_id={p_acme['run_id']}",
        headers={"Authorization": f"Bearer {member_default}"},
    )
    assert r.status_code == 404


def test_queue_unknown_run_returns_404(client, auth_headers):
    r = client.get(
        "/v1/reviews/queue?run_id=run_doesnotexist",
        headers=auth_headers,
    )
    assert r.status_code == 404


def test_queue_requires_auth(client):
    r = client.get("/v1/reviews/queue?run_id=run_x")
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# POST /v1/reviews


def test_submit_review_returns_201_with_full_record(client, auth_headers):
    payload = _make_run_with_failing_row("run_submitone0", "review-s")
    _post(client, auth_headers, payload)

    r = client.post(
        "/v1/reviews",
        json={"run_id": payload["run_id"], "row_id": "r-fail",
              "verdict": "override_pass", "note": " false-positive "},
        headers=auth_headers,
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["run_id"]   == payload["run_id"]
    assert body["row_id"]   == "r-fail"
    assert body["verdict"]  == "override_pass"
    # ``.strip()`` normalisation on the server side.
    assert body["note"]     == "false-positive"
    assert body["reviewer_key_id"].startswith("key_")
    assert body["created_at"] == body["updated_at"]


def test_resubmitting_the_same_review_upserts_in_place(client, auth_headers):
    payload = _make_run_with_failing_row("run_upsertaaaa", "review-s")
    _post(client, auth_headers, payload)

    first = client.post(
        "/v1/reviews",
        json={"run_id": payload["run_id"], "row_id": "r-fail",
              "verdict": "skip"},
        headers=auth_headers,
    )
    second = client.post(
        "/v1/reviews",
        json={"run_id": payload["run_id"], "row_id": "r-fail",
              "verdict": "override_fail", "note": "actually a real failure"},
        headers=auth_headers,
    )
    assert first.status_code == second.status_code == 201
    # Same row id (UPSERT, not new insert).
    assert first.json()["id"] == second.json()["id"]
    # Verdict and note updated; ``created_at`` is the original;
    # ``updated_at`` advances.
    assert second.json()["verdict"] == "override_fail"
    assert second.json()["note"]    == "actually a real failure"
    assert second.json()["created_at"] == first.json()["created_at"]


def test_two_reviewers_produce_two_separate_reviews(
    client, auth_headers, make_member_token,
):
    """Different ``reviewer_key_id`` ⇒ different rows in
    ``row_reviews``. Cross-annotator agreement starts here."""
    payload = _make_run_with_failing_row("run_tworeviewer", "review-s")
    _post(client, auth_headers, payload)

    member = make_member_token("org_default", name="reviewer-2")

    # Admin reviews.
    r1 = client.post(
        "/v1/reviews",
        json={"run_id": payload["run_id"], "row_id": "r-fail",
              "verdict": "agree"},
        headers=auth_headers,
    )
    # A second reviewer (different key_id) reviews the same row.
    r2 = client.post(
        "/v1/reviews",
        json={"run_id": payload["run_id"], "row_id": "r-fail",
              "verdict": "override_pass"},
        headers={"Authorization": f"Bearer {member}"},
    )
    assert r1.status_code == r2.status_code == 201
    assert r1.json()["id"] != r2.json()["id"]


def test_submit_review_for_unknown_row_returns_404(client, auth_headers):
    payload = _make_run_with_failing_row("run_badrowxxxx", "review-s")
    _post(client, auth_headers, payload)
    r = client.post(
        "/v1/reviews",
        json={"run_id": payload["run_id"], "row_id": "nope",
              "verdict": "agree"},
        headers=auth_headers,
    )
    assert r.status_code == 404
    assert "nope" in r.json()["detail"]


def test_submit_review_for_cross_org_run_returns_404(
    client, auth_headers, make_org, make_member_token,
):
    make_org("acme")
    member_default = make_member_token("org_default", name="d")
    member_acme    = make_member_token("org_acme",    name="a")
    p_acme = _make_run_with_failing_row("run_xorgrevsub", "acme-proj")
    _post(client, {"Authorization": f"Bearer {member_acme}"}, p_acme)
    r = client.post(
        "/v1/reviews",
        json={"run_id": p_acme["run_id"], "row_id": "r-fail",
              "verdict": "agree"},
        headers={"Authorization": f"Bearer {member_default}"},
    )
    assert r.status_code == 404


def test_submit_review_rejects_unknown_verdict(client, auth_headers):
    payload = _make_run_with_failing_row("run_badverdict0", "review-s")
    _post(client, auth_headers, payload)
    r = client.post(
        "/v1/reviews",
        json={"run_id": payload["run_id"], "row_id": "r-fail",
              "verdict": "lookgoodtome"},
        headers=auth_headers,
    )
    assert r.status_code == 422


def test_submit_review_rejects_oversize_note(client, auth_headers):
    payload = _make_run_with_failing_row("run_bignoteaaa", "review-s")
    _post(client, auth_headers, payload)
    r = client.post(
        "/v1/reviews",
        json={"run_id": payload["run_id"], "row_id": "r-fail",
              "verdict": "agree", "note": "x" * 4_500},
        headers=auth_headers,
    )
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# GET /v1/runs/{run_id}/reviews


def test_list_reviews_returns_every_reviewer(
    client, auth_headers, make_member_token,
):
    payload = _make_run_with_failing_row("run_listreviewz", "review-l")
    _post(client, auth_headers, payload)
    member = make_member_token("org_default", name="member-listr")
    client.post(
        "/v1/reviews",
        json={"run_id": payload["run_id"], "row_id": "r-fail",
              "verdict": "agree"},
        headers=auth_headers,
    )
    client.post(
        "/v1/reviews",
        json={"run_id": payload["run_id"], "row_id": "r-fail",
              "verdict": "override_pass", "note": "looks fine"},
        headers={"Authorization": f"Bearer {member}"},
    )
    r = client.get(
        f"/v1/runs/{payload['run_id']}/reviews",
        headers=auth_headers,
    )
    assert r.status_code == 200
    revs = r.json()["reviews"]
    assert len(revs) == 2
    verdicts = sorted(v["verdict"] for v in revs)
    assert verdicts == ["agree", "override_pass"]


def test_list_reviews_cross_org_returns_404(
    client, auth_headers, make_org, make_member_token,
):
    make_org("acme")
    member_default = make_member_token("org_default", name="d")
    member_acme    = make_member_token("org_acme",    name="a")
    p_acme = _make_run_with_failing_row("run_xorglistrev", "acme-proj")
    _post(client, {"Authorization": f"Bearer {member_acme}"}, p_acme)
    r = client.get(
        f"/v1/runs/{p_acme['run_id']}/reviews",
        headers={"Authorization": f"Bearer {member_default}"},
    )
    assert r.status_code == 404
