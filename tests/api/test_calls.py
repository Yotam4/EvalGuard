"""``GET /v1/projects/{slug}/calls`` — Phase OBS-1 stream endpoint.

The endpoint serves the customer-service-style observability stream:
one ``run_rows`` entry per "call", paginated by an opaque cursor,
with a ``tab=recent|failures`` toggle.  These tests pin the data
plane (index, columns, ordering, cursor round-trip) AND the contract
shape the UI relies on.

Each test ingests via the CLI executor → ``run_to_dict`` → POST
``/v1/runs`` so the data flows through the production
``_persist_run`` path that stamps the new ``ingested_at`` and
``output_preview`` columns.  Synthetic payloads are used when the
test needs more rows than the executor's two-row default produces
(e.g. cursor pagination).
"""

from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path

from evalguard_cli.local.local_executor import execute
from evalguard_cli.local.serializer import run_to_dict
from evalguard_cli.local.sqlite_store import SqliteStore
from evalguard_cli.local.yaml_loader import load_config


# ---------------------------------------------------------------------------
# Helpers


def _produce_real_run(
    tmp_path: Path,
    project: str = "p",
    rows: int = 2,
) -> dict:
    """Run the CLI executor with N rows and serialize.  Reuses the
    same shape as ``tests/api/test_runs.py:_produce_real_run`` but
    parameterised so the cursor-pagination tests can ingest more
    than two rows."""
    base = tmp_path / "cli"
    base.mkdir(parents=True, exist_ok=True)
    (base / "datasets").mkdir(parents=True, exist_ok=True)
    dataset = "\n".join(
        json.dumps({"id": f"r{i}", "input": f"q{i}", "tags": ["calls-test"]})
        for i in range(rows)
    ) + "\n"
    (base / "datasets" / "g.jsonl").write_text(dataset)
    (base / "evalguard.yaml").write_text(
        "version: 1\n"
        f"project: {project}\n"
        "providers: [{ id: 'mock:m', config: { mode: echo, latency_ms: 0 } }]\n"
        "datasets: [{ id: g, file: datasets/g.jsonl }]\n"
        "heuristics: [{ id: len, type: length, max: 10000 }]\n"
        "judges: [{ id: q, type: mock_pointwise, score: 4.5, threshold: 4.0 }]\n"
    )
    cfg = load_config(base / "evalguard.yaml")
    store = SqliteStore(base / "local.db")
    store.init_schema()
    record = asyncio.run(execute(cfg, store=store, quiet=True))
    return run_to_dict(store, record.run_id, include_rows=True, include_scores=True)


def _synth_run_with_outcomes(
    run_id: str, project: str, outcomes: list[bool], output: str = "ok",
) -> dict:
    """Synthetic payload: one trial, ``len(outcomes)`` rows, each
    row's ``passed`` flag taken from the corresponding list entry.
    Used by tests that need precise pass/fail distributions (e.g.
    ``tab=failures`` filter coverage)."""
    n = len(outcomes)
    pass_count = sum(1 for x in outcomes if x)
    trial_id = "trial_" + run_id[len("run_"):][:12].lower().replace("_", "0")
    return {
        "schema_version": "1.0.0",
        "run_id":         run_id,
        "project":        project,
        "row_count":      n,
        "row_pass_count": pass_count,
        "row_fail_count": n - pass_count,
        "trials": [{
            "trial_id":       trial_id,
            "provider_id":    "mock:m",
            "provider":       "mock",
            "model":          "m",
            "row_count":      n,
            "row_pass_count": pass_count,
            "row_fail_count": n - pass_count,
            "rows": [
                {
                    "row_id":     f"r{i}",
                    "passed":     bool(outcomes[i]),
                    "n_scores":   1,
                    "latency_ms": 100 + i,
                    "cost_usd":   0.001,
                    "output":     output,
                }
                for i in range(n)
            ],
            "gates": [],
        }],
    }


def _post(client, headers, payload) -> None:
    r = client.post("/v1/runs", json=payload, headers=headers)
    assert r.status_code == 201, r.text


def _project_slug_from_listing(client, auth_headers, project: str) -> str:
    """The endpoint takes a slug; the post-ingest listing surfaces
    project names.  Resolve the slug via the projects endpoint so
    the test never assumes the slug → name relationship."""
    r = client.get("/v1/projects", headers=auth_headers)
    assert r.status_code == 200, r.text
    matches = [p for p in r.json()["projects"] if p["name"] == project]
    assert matches, f"project {project!r} not found"
    return matches[0]["slug"]


# ---------------------------------------------------------------------------
# Happy path


def test_recent_tab_returns_newest_first(client, auth_headers, tmp_path):
    p = _produce_real_run(tmp_path, project="calls-recent", rows=3)
    _post(client, auth_headers, p)
    slug = _project_slug_from_listing(client, auth_headers, "calls-recent")
    r = client.get(
        f"/v1/projects/{slug}/calls?tab=recent", headers=auth_headers,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["calls"]) == 3
    # Same ingested_at on every row of one run, but ``id DESC``
    # tiebreaker stabilises ordering across DBs.  Pin only that
    # the timestamps are non-increasing.
    timestamps = [c["ingested_at"] for c in body["calls"]]
    assert timestamps == sorted(timestamps, reverse=True)
    # All carry the denormalised fields.
    for c in body["calls"]:
        assert c["ingested_at"] is not None
        assert c["output_preview"] is not None
        assert c["project_id"]


def test_failures_tab_filters_to_failing_rows_only(client, auth_headers, tmp_path):
    # Three rows: pass / fail / pass.  Synthetic so we control the
    # ``passed`` flag precisely.
    payload = _synth_run_with_outcomes(
        run_id="run_failtab00001",
        project="calls-failures",
        outcomes=[True, False, True],
    )
    _post(client, auth_headers, payload)
    slug = _project_slug_from_listing(client, auth_headers, "calls-failures")

    r_all = client.get(
        f"/v1/projects/{slug}/calls?tab=recent", headers=auth_headers,
    )
    assert len(r_all.json()["calls"]) == 3

    r_fail = client.get(
        f"/v1/projects/{slug}/calls?tab=failures", headers=auth_headers,
    )
    assert r_fail.status_code == 200
    calls = r_fail.json()["calls"]
    assert len(calls) == 1
    assert calls[0]["passed"] is False
    assert calls[0]["row_id"] == "r1"


def test_output_preview_is_truncated_to_240_chars(client, auth_headers, tmp_path):
    """OBS-1: ``output_preview`` is the first 240 chars of the
    row's output, capped at the storage layer.  Longer outputs get
    sliced; the full text remains in ``payload_json`` for the
    detail endpoint to surface."""
    long_output = "x" * 1000
    payload = _synth_run_with_outcomes(
        run_id="run_preview00001",
        project="calls-preview",
        outcomes=[True],
        output=long_output,
    )
    _post(client, auth_headers, payload)
    slug = _project_slug_from_listing(client, auth_headers, "calls-preview")
    r = client.get(
        f"/v1/projects/{slug}/calls?tab=recent", headers=auth_headers,
    )
    preview = r.json()["calls"][0]["output_preview"]
    assert preview is not None
    assert len(preview) == 240
    assert preview == "x" * 240


def test_source_filter_combines_with_tab(client, auth_headers, tmp_path):
    """``?source=cli`` + ``?tab=recent`` is the AND of both.  Pinned
    so a future refactor that turns the WHERE list into a
    comprehension can't accidentally swap to OR."""
    payload = _produce_real_run(tmp_path, project="calls-source", rows=2)
    _post(client, auth_headers, payload)
    slug = _project_slug_from_listing(client, auth_headers, "calls-source")
    r = client.get(
        f"/v1/projects/{slug}/calls?tab=recent&source=cli",
        headers=auth_headers,
    )
    assert r.status_code == 200
    calls = r.json()["calls"]
    assert len(calls) == 2
    # ``source`` isn't on the response; we verify via the negative
    # case — ?source=otlp on a CLI-only project returns empty.
    r2 = client.get(
        f"/v1/projects/{slug}/calls?tab=recent&source=otlp",
        headers=auth_headers,
    )
    assert r2.status_code == 200
    assert r2.json()["calls"] == []


# ---------------------------------------------------------------------------
# Cursor pagination


def test_cursor_paginates_and_terminates(client, auth_headers, tmp_path):
    """5 rows + limit 2 ⇒ pages of 2, 2, 1.  Final page has
    ``next_cursor=None``."""
    payload = _synth_run_with_outcomes(
        run_id="run_paginat00001",
        project="calls-pages",
        outcomes=[True, True, False, True, False],
    )
    _post(client, auth_headers, payload)
    slug = _project_slug_from_listing(client, auth_headers, "calls-pages")

    seen_row_ids: list[str] = []
    cursor: str | None = None
    pages = 0
    while True:
        qs = "tab=recent&limit=2"
        if cursor is not None:
            qs += f"&cursor={cursor}"
        r = client.get(f"/v1/projects/{slug}/calls?{qs}", headers=auth_headers)
        assert r.status_code == 200, r.text
        body = r.json()
        for c in body["calls"]:
            seen_row_ids.append(c["row_id"])
        cursor = body["next_cursor"]
        pages += 1
        if cursor is None:
            break
        assert pages < 10, "runaway pagination"

    # We saw every row exactly once.
    assert sorted(seen_row_ids) == ["r0", "r1", "r2", "r3", "r4"]
    # 5 rows / limit 2 → 3 pages.
    assert pages == 3


def test_cursor_corrupted_returns_400(client, auth_headers, tmp_path):
    """A tampered cursor must 400, not silently re-page from the
    start (which would loop a buggy client forever)."""
    payload = _produce_real_run(tmp_path, project="calls-badcursor", rows=2)
    _post(client, auth_headers, payload)
    slug = _project_slug_from_listing(client, auth_headers, "calls-badcursor")
    r = client.get(
        f"/v1/projects/{slug}/calls?tab=recent&cursor=not-a-real-cursor",
        headers=auth_headers,
    )
    assert r.status_code == 400
    assert "cursor" in r.json()["detail"].lower()


def test_cursor_opaque_format_is_not_a_public_contract(client, auth_headers, tmp_path):
    """The decoder accepts the encoder's output round-tripped, but
    the format itself is undocumented to clients.  This test pins
    that the cursor's payload structure (``{t, i}``) round-trips —
    if a future refactor changes the keys, the previous form must
    still decode for the rollout window."""
    payload = _produce_real_run(tmp_path, project="calls-cursorfmt", rows=2)
    _post(client, auth_headers, payload)
    slug = _project_slug_from_listing(client, auth_headers, "calls-cursorfmt")
    r = client.get(
        f"/v1/projects/{slug}/calls?tab=recent&limit=1",
        headers=auth_headers,
    )
    cursor = r.json()["next_cursor"]
    assert cursor is not None
    # Decode + introspect (TEST-ONLY — clients must NOT do this).
    padded = cursor + "=" * (-len(cursor) % 4)
    data = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
    assert set(data.keys()) == {"t", "i"}
    assert isinstance(data["i"], int)


# ---------------------------------------------------------------------------
# Validation


def test_unknown_tab_returns_422(client, auth_headers):
    # FastAPI's Literal type rejects at the validation layer.
    r = client.get(
        "/v1/projects/x/calls?tab=lookgoodtome", headers=auth_headers,
    )
    assert r.status_code == 422


def test_unknown_source_returns_400(client, auth_headers, tmp_path):
    payload = _produce_real_run(tmp_path, project="calls-badsource", rows=1)
    _post(client, auth_headers, payload)
    slug = _project_slug_from_listing(client, auth_headers, "calls-badsource")
    r = client.get(
        f"/v1/projects/{slug}/calls?tab=recent&source=nonsense",
        headers=auth_headers,
    )
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert "nonsense" in detail
    assert "cli" in detail and "otlp" in detail


def test_limit_out_of_range_returns_422(client, auth_headers):
    for bad in (0, 201, -5):
        r = client.get(
            f"/v1/projects/x/calls?tab=recent&limit={bad}",
            headers=auth_headers,
        )
        assert r.status_code == 422, bad


# ---------------------------------------------------------------------------
# Tenant scoping


def test_unknown_project_returns_404(client, auth_headers):
    r = client.get(
        "/v1/projects/no-such-project/calls?tab=recent",
        headers=auth_headers,
    )
    assert r.status_code == 404


def test_cross_org_project_returns_404_for_member(
    client, auth_headers, make_org, make_member_token, tmp_path,
):
    """A member of org_default looking at a project in org_acme
    must get 404, even if they know the slug — same anti-enumeration
    shape used elsewhere."""
    make_org("acme")
    member_default = make_member_token("org_default", name="d")
    member_acme    = make_member_token("org_acme",    name="a")
    payload = _produce_real_run(tmp_path / "a", project="acme-private", rows=1)
    _post(client, {"Authorization": f"Bearer {member_acme}"}, payload)

    # default-org member can't see acme's project.
    r = client.get(
        "/v1/projects/acme-private/calls?tab=recent",
        headers={"Authorization": f"Bearer {member_default}"},
    )
    assert r.status_code == 404

    # Admin can.
    r2 = client.get(
        "/v1/projects/acme-private/calls?tab=recent",
        headers=auth_headers,
    )
    assert r2.status_code == 200


def test_requires_auth(client):
    r = client.get("/v1/projects/x/calls?tab=recent")
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# OBS-2: GET /v1/projects/{slug}/calls/{run_id}/{row_id}
#
# Drill-down for a single call.  Source of truth is
# ``payload_json``; the stream paginator never has to load these
# heavy fields.


def test_call_detail_returns_input_output_scores(client, auth_headers, tmp_path):
    """Happy path: the CLI executor produces a real run with input/
    output/scores, and the detail endpoint surfaces all three."""
    payload = _produce_real_run(tmp_path, project="detail-happy", rows=2)
    _post(client, auth_headers, payload)
    slug = _project_slug_from_listing(client, auth_headers, "detail-happy")

    # Discover the row_id from the stream — never hard-code, the
    # CLI executor decides them.
    listing = client.get(
        f"/v1/projects/{slug}/calls?tab=recent",
        headers=auth_headers,
    ).json()["calls"]
    assert listing, "expected at least one call in the stream"
    first = listing[0]

    r = client.get(
        f"/v1/projects/{slug}/calls/{first['run_id']}/{first['row_id']}",
        headers=auth_headers,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["run_id"]  == first["run_id"]
    assert body["row_id"]  == first["row_id"]
    assert body["passed"]  in (True, False)
    # Provider/model denormalised from the parent trial.
    assert body["provider"] == "mock"
    assert body["model"]    == "m"
    # The "actual answer" surfaces; with the CLI executor + mock
    # provider the input is the dataset row's ``input`` field.
    assert body["input"] is not None
    assert body["output"] is not None
    # Scores list is populated (the test config defines a heuristic
    # + a judge, so at least 2 scores per row).
    assert len(body["scores"]) >= 1
    # Trial gates surface as context — the test config has no
    # explicit gates, so this is just a shape check.
    assert isinstance(body["trial_gates"], list)


def test_call_detail_unknown_row_returns_404(client, auth_headers, tmp_path):
    payload = _produce_real_run(tmp_path, project="detail-missing", rows=1)
    _post(client, auth_headers, payload)
    slug = _project_slug_from_listing(client, auth_headers, "detail-missing")
    listing = client.get(
        f"/v1/projects/{slug}/calls?tab=recent", headers=auth_headers,
    ).json()["calls"]
    run_id = listing[0]["run_id"]
    r = client.get(
        f"/v1/projects/{slug}/calls/{run_id}/nope",
        headers=auth_headers,
    )
    assert r.status_code == 404
    detail = r.json()["detail"]
    # Detail names BOTH the row_id and the project so an operator
    # debugging a 404 sees enough to fix without diving into logs.
    assert "nope" in detail
    assert "detail-missing" in detail


def test_call_detail_unknown_run_returns_404(client, auth_headers, tmp_path):
    payload = _produce_real_run(tmp_path, project="detail-norun", rows=1)
    _post(client, auth_headers, payload)
    slug = _project_slug_from_listing(client, auth_headers, "detail-norun")
    r = client.get(
        f"/v1/projects/{slug}/calls/run_doesnotexist/r0",
        headers=auth_headers,
    )
    assert r.status_code == 404


def test_call_detail_mismatched_project_returns_404(
    client, auth_headers, tmp_path,
):
    """A run that exists in project A but is requested under
    project B's URL must 404 — same anti-enumeration shape as the
    other endpoints."""
    payload_a = _produce_real_run(tmp_path / "a", project="detail-real", rows=1)
    payload_b = _produce_real_run(tmp_path / "b", project="detail-other", rows=1)
    _post(client, auth_headers, payload_a)
    _post(client, auth_headers, payload_b)
    listing = client.get(
        f"/v1/projects/{_project_slug_from_listing(client, auth_headers, 'detail-real')}/calls?tab=recent",
        headers=auth_headers,
    ).json()["calls"]
    run_a = listing[0]["run_id"]
    row_a = listing[0]["row_id"]

    # Request run_a under the WRONG project (detail-other) — must 404.
    other_slug = _project_slug_from_listing(client, auth_headers, "detail-other")
    r = client.get(
        f"/v1/projects/{other_slug}/calls/{run_a}/{row_a}",
        headers=auth_headers,
    )
    assert r.status_code == 404


def test_call_detail_cross_org_returns_404_for_member(
    client, auth_headers, make_org, make_member_token, tmp_path,
):
    make_org("acme")
    member_default = make_member_token("org_default", name="d")
    member_acme    = make_member_token("org_acme",    name="a")
    payload = _produce_real_run(tmp_path, project="detail-acmep", rows=1)
    _post(client, {"Authorization": f"Bearer {member_acme}"}, payload)

    # Discover the row via the acme member's own listing.
    listing = client.get(
        "/v1/projects/detail-acmep/calls?tab=recent",
        headers={"Authorization": f"Bearer {member_acme}"},
    ).json()["calls"]
    run_id = listing[0]["run_id"]
    row_id = listing[0]["row_id"]

    # default-org member tries to read the acme row.
    r = client.get(
        f"/v1/projects/detail-acmep/calls/{run_id}/{row_id}",
        headers={"Authorization": f"Bearer {member_default}"},
    )
    assert r.status_code == 404

    # Admin can.
    r2 = client.get(
        f"/v1/projects/detail-acmep/calls/{run_id}/{row_id}",
        headers=auth_headers,
    )
    assert r2.status_code == 200


def test_call_detail_requires_auth(client):
    r = client.get("/v1/projects/x/calls/run_x/r0")
    assert r.status_code == 401
