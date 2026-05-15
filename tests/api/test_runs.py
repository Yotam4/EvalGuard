"""``/v1/runs`` end-to-end — POST, GET, list, conflict, project scoping.

A "real" run payload is built by running the CLI executor under the
hood, then ``run_to_dict`` is the source of truth for the wire shape.
This guarantees the ingest contract stays aligned with what
``evalguard push`` actually sends.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from evalguard_cli.local.local_executor import execute
from evalguard_cli.local.serializer import run_to_dict
from evalguard_cli.local.sqlite_store import SqliteStore
from evalguard_cli.local.yaml_loader import load_config


# ---------------------------------------------------------------------------
# helpers


def _produce_real_run(tmp_path: Path, project: str = "p") -> dict:
    """Run the CLI executor on a tiny mock-provider config and serialize
    the result via ``run_to_dict``. The returned dict is byte-for-byte
    what ``evalguard push`` would POST."""
    base = tmp_path / "cli"
    (base / "datasets").mkdir(parents=True)
    (base / "datasets" / "g.jsonl").write_text(
        '{"id":"r1","input":"hi","tags":["normal"]}\n'
        '{"id":"r2","input":"yo","tags":["edge"]}\n'
    )
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


# ---------------------------------------------------------------------------
# POST /v1/runs


def test_post_run_returns_201_with_location(client, auth_headers, tmp_path):
    payload = _produce_real_run(tmp_path)
    r = client.post("/v1/runs", json=payload, headers=auth_headers)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["run_id"] == payload["run_id"]
    assert body["url"] == f"/v1/runs/{payload['run_id']}"
    assert r.headers["location"] == body["url"]


def test_post_run_rejects_unknown_top_level_field(client, auth_headers):
    """Pydantic ``extra='forbid'`` on the ingest model — unknown
    fields surface as a 422 so client bugs don't go silent."""
    bad = {
        "schema_version": "1.0.0",
        "run_id": "run_extrafield000",
        "project": "p",
        "trials": [],
        "totally_unexpected": "x",
    }
    r = client.post("/v1/runs", json=bad, headers=auth_headers)
    assert r.status_code == 422


def test_post_run_rejects_malformed_run_id(client, auth_headers):
    bad = {
        "schema_version": "1.0.0",
        "run_id": "not_a_run_id",   # missing run_ prefix / pattern
        "project": "p",
        "trials": [],
    }
    r = client.post("/v1/runs", json=bad, headers=auth_headers)
    assert r.status_code == 422


def test_duplicate_run_id_returns_409(client, auth_headers, tmp_path):
    payload = _produce_real_run(tmp_path)
    first = client.post("/v1/runs", json=payload, headers=auth_headers)
    assert first.status_code == 201
    second = client.post("/v1/runs", json=payload, headers=auth_headers)
    assert second.status_code == 409
    assert "already exists" in second.json()["detail"]


# ---------------------------------------------------------------------------
# GET /v1/runs/{run_id}


def test_get_run_roundtrips_full_payload(client, auth_headers, tmp_path):
    payload = _produce_real_run(tmp_path)
    client.post("/v1/runs", json=payload, headers=auth_headers)
    r = client.get(f"/v1/runs/{payload['run_id']}", headers=auth_headers)
    assert r.status_code == 200
    out = r.json()
    # Every input field round-trips byte-for-byte (modulo None/default
    # normalization done by Pydantic).
    assert out["run_id"]    == payload["run_id"]
    assert out["project"]   == payload["project"]
    assert out["row_count"] == payload["row_count"]
    assert len(out["trials"]) == len(payload["trials"])
    # Server envelope tells the operator how the run got here.
    assert out["server"]["ingested_at"]
    assert out["server"]["ingested_by"]
    assert out["server"]["project_id"]


def test_get_unknown_run_returns_404(client, auth_headers):
    r = client.get("/v1/runs/run_doesnotexist00", headers=auth_headers)
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# GET /v1/runs (list)


def test_list_runs_returns_recent_first(client, auth_headers, tmp_path):
    p1 = _produce_real_run(tmp_path / "a", "alpha")
    p2 = _produce_real_run(tmp_path / "b", "beta")
    client.post("/v1/runs", json=p1, headers=auth_headers)
    client.post("/v1/runs", json=p2, headers=auth_headers)
    r = client.get("/v1/runs", headers=auth_headers)
    assert r.status_code == 200
    runs = r.json()["runs"]
    assert len(runs) == 2
    # Most recent ingest first.
    assert runs[0]["run_id"] == p2["run_id"]
    assert runs[1]["run_id"] == p1["run_id"]


def test_list_runs_filters_by_project(client, auth_headers, tmp_path):
    p_alpha = _produce_real_run(tmp_path / "a", "alpha")
    p_beta  = _produce_real_run(tmp_path / "b", "beta")
    client.post("/v1/runs", json=p_alpha, headers=auth_headers)
    client.post("/v1/runs", json=p_beta,  headers=auth_headers)
    r = client.get("/v1/runs?project=alpha", headers=auth_headers)
    runs = r.json()["runs"]
    assert len(runs) == 1
    assert runs[0]["project"] == "alpha"


def test_list_runs_caps_at_limit(client, auth_headers, tmp_path):
    for i in range(5):
        p = _produce_real_run(tmp_path / f"r{i}", f"proj{i}")
        client.post("/v1/runs", json=p, headers=auth_headers)
    r = client.get("/v1/runs?limit=3", headers=auth_headers)
    assert len(r.json()["runs"]) == 3


def test_list_runs_rejects_invalid_limit(client, auth_headers):
    r = client.get("/v1/runs?limit=0", headers=auth_headers)
    assert r.status_code == 422
    r = client.get("/v1/runs?limit=999999", headers=auth_headers)
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# ``?source=`` filter (Phase-3a column, surface added later)
#
# Two ingest paths feed the same ``runs`` table: ``evalguard push``
# stamps ``source='cli'`` (the column default), the OTLP route
# stamps ``source='otlp'``.  The filter lets a UI tab partition the
# listing without joining payload_json.


def _otlp_body_for_filter(span_id: str) -> dict:
    """Smallest legal OTLP/HTTP body that lands as one synthetic run.
    Inlined here (not imported from test_otlp.py) so the runs-test
    file stays self-contained — if test_otlp's fixture shape evolves,
    the listing-filter tests don't accidentally drift with it."""
    return {
        "resourceSpans": [{
            "resource": {"attributes": [
                {"key": "service.name",
                 "value": {"stringValue": "otlp-filter-svc"}},
            ]},
            "scopeSpans": [{
                "scope": {"name": "test", "version": "0"},
                "spans": [{
                    "traceId":           "deadbeef" * 4,
                    "spanId":            span_id,
                    "name":              "chat openai gpt-4o-mini",
                    "kind":              2,
                    "startTimeUnixNano": "1700000000000000000",
                    "endTimeUnixNano":   "1700000000100000000",
                    "attributes": [
                        {"key": "gen_ai.system",
                         "value": {"stringValue": "openai"}},
                        {"key": "gen_ai.request.model",
                         "value": {"stringValue": "gpt-4o-mini"}},
                    ],
                    "status": {"code": 1},
                }],
            }],
        }],
    }


def test_list_runs_source_filter_returns_only_matching(
    client, auth_headers, tmp_path,
):
    """One CLI run + one OTLP run ingested into the same org; each
    ``?source=`` filter returns exactly its half."""
    # CLI run.
    cli_payload = _produce_real_run(tmp_path / "cli-side", project="filter-cli")
    client.post("/v1/runs", json=cli_payload, headers=auth_headers)
    # OTLP run.
    client.post(
        "/v1/otlp/v1/traces",
        json=_otlp_body_for_filter("span0001"),
        headers=auth_headers,
    )

    r_cli = client.get("/v1/runs?source=cli", headers=auth_headers)
    assert r_cli.status_code == 200
    cli_runs = r_cli.json()["runs"]
    assert len(cli_runs) >= 1
    assert all(run["source"] == "cli" for run in cli_runs)

    r_otlp = client.get("/v1/runs?source=otlp", headers=auth_headers)
    assert r_otlp.status_code == 200
    otlp_runs = r_otlp.json()["runs"]
    assert len(otlp_runs) == 1
    assert otlp_runs[0]["source"] == "otlp"

    # No filter ⇒ both surface.
    r_all = client.get("/v1/runs", headers=auth_headers)
    sources = {run["source"] for run in r_all.json()["runs"]}
    assert sources == {"cli", "otlp"}


def test_list_runs_source_filter_unknown_value_returns_400(
    client, auth_headers, tmp_path,
):
    # No setup needed — the whitelist guard fires before the SQL runs.
    r = client.get("/v1/runs?source=nonsense", headers=auth_headers)
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert "nonsense" in detail
    # Helpful: the error names the allowed values.
    assert "cli"  in detail
    assert "otlp" in detail


def test_list_runs_source_filter_combines_with_project(
    client, auth_headers, tmp_path,
):
    """``?source=cli&project=X`` is the AND of both filters — pinned
    here so a future refactor that turns the WHERE clause into a
    list-comprehension can't accidentally swap to OR."""
    cli_alpha = _produce_real_run(tmp_path / "a", project="alpha")
    cli_beta  = _produce_real_run(tmp_path / "b", project="beta")
    client.post("/v1/runs", json=cli_alpha, headers=auth_headers)
    client.post("/v1/runs", json=cli_beta,  headers=auth_headers)
    client.post(
        "/v1/otlp/v1/traces",
        json=_otlp_body_for_filter("span0001"),
        headers=auth_headers,
    )

    r = client.get(
        "/v1/runs?source=cli&project=alpha", headers=auth_headers,
    )
    runs = r.json()["runs"]
    projects = {run["project"] for run in runs}
    sources  = {run["source"]  for run in runs}
    assert projects == {"alpha"}
    assert sources  == {"cli"}


def test_list_runs_source_filter_respects_org_scoping(
    client, auth_headers, make_org, make_member_token, tmp_path,
):
    """The source filter must compose with the implicit org filter —
    a member of org-A asking for ``?source=otlp`` must never see an
    OTLP run from org-B."""
    make_org("acme")
    member_default = make_member_token("org_default", name="d")
    member_acme    = make_member_token("org_acme",    name="a")

    # ACME pushes an OTLP run.
    client.post(
        "/v1/otlp/v1/traces",
        json=_otlp_body_for_filter("spanACMEx1"),
        headers={"Authorization": f"Bearer {member_acme}"},
    )
    # Default-org member queries OTLP runs.
    r = client.get(
        "/v1/runs?source=otlp",
        headers={"Authorization": f"Bearer {member_default}"},
    )
    assert r.status_code == 200
    # Empty — the filter doesn't bypass the implicit org scoping.
    assert r.json()["runs"] == []


# ---------------------------------------------------------------------------
# Project provisioning — server auto-creates by name on first push


def test_first_push_to_a_new_project_provisions_it(client, auth_headers, tmp_path):
    """No explicit project create call needed — the run's
    ``project: <name>`` field upserts a project by slug."""
    p = _produce_real_run(tmp_path, "brand-new-project")
    r = client.post("/v1/runs", json=p, headers=auth_headers)
    assert r.status_code == 201
    project_id = r.json()["project_id"]
    # Subsequent pushes to the same name resolve to the same project.
    p2 = _produce_real_run(tmp_path / "second", "brand-new-project")
    r2 = client.post("/v1/runs", json=p2, headers=auth_headers)
    assert r2.status_code == 201
    assert r2.json()["project_id"] == project_id


# ---------------------------------------------------------------------------
# Persistence — the denormalized tables shadow payload_json correctly


# ---------------------------------------------------------------------------
# Cross-tenant isolation
#
# These are the most important tests in the file: they pin that one
# tenant's runs are invisible to another. Every regression here is a
# multi-tenancy security incident.


def test_member_cannot_see_run_pushed_by_another_org(
    client, auth_headers, make_org, make_member_token, tmp_path,
):
    """An admin pushes a run into org_acme; a member of org_default
    must get a 404 (not 403, not the data) when GETting it. 404 is
    deliberate — exposing the existence of the run id elsewhere
    would leak across tenants."""
    make_org("acme")
    # Mint member tokens in BOTH orgs so we exercise both directions.
    member_default = make_member_token("org_default", name="d")
    member_acme    = make_member_token("org_acme",    name="a")

    # Push a run through the admin token, scoped to acme via that
    # member's org context.
    payload = _produce_real_run(tmp_path, "secret-acme-project")
    r = client.post(
        "/v1/runs", json=payload,
        headers={"Authorization": f"Bearer {member_acme}"},
    )
    assert r.status_code == 201

    # Member of org_default can't see it.
    r404 = client.get(
        f"/v1/runs/{payload['run_id']}",
        headers={"Authorization": f"Bearer {member_default}"},
    )
    assert r404.status_code == 404
    assert "not found" in r404.json()["detail"].lower()

    # Member of org_acme CAN see it.
    r200 = client.get(
        f"/v1/runs/{payload['run_id']}",
        headers={"Authorization": f"Bearer {member_acme}"},
    )
    assert r200.status_code == 200


def test_list_does_not_leak_other_orgs_runs(
    client, auth_headers, make_org, make_member_token, tmp_path,
):
    """The default ``GET /v1/runs`` listing for a member must include
    only their own org's runs, no matter what's in other orgs."""
    make_org("acme")
    member_default = make_member_token("org_default", name="d")
    member_acme    = make_member_token("org_acme",    name="a")

    p_default = _produce_real_run(tmp_path / "d", "default-proj")
    p_acme    = _produce_real_run(tmp_path / "a", "acme-proj")
    client.post("/v1/runs", json=p_default,
                headers={"Authorization": f"Bearer {member_default}"})
    client.post("/v1/runs", json=p_acme,
                headers={"Authorization": f"Bearer {member_acme}"})

    # Member of default sees only their run.
    r = client.get(
        "/v1/runs",
        headers={"Authorization": f"Bearer {member_default}"},
    )
    visible_ids = {x["run_id"] for x in r.json()["runs"]}
    assert p_default["run_id"] in visible_ids
    assert p_acme["run_id"] not in visible_ids

    # Admin sees both.
    r2 = client.get("/v1/runs", headers=auth_headers)
    visible_admin = {x["run_id"] for x in r2.json()["runs"]}
    assert {p_default["run_id"], p_acme["run_id"]} <= visible_admin


def test_same_project_name_in_different_orgs_does_not_collide(
    client, auth_headers, make_org, make_member_token, tmp_path,
):
    """Two orgs may both have a project named 'demo' — runs land
    in distinct project_ids and don't show up in each other's
    listings."""
    make_org("acme")
    member_default = make_member_token("org_default", name="d")
    member_acme    = make_member_token("org_acme",    name="a")

    p_default = _produce_real_run(tmp_path / "d", "demo")
    p_acme    = _produce_real_run(tmp_path / "a", "demo")
    r1 = client.post("/v1/runs", json=p_default,
                     headers={"Authorization": f"Bearer {member_default}"})
    r2 = client.post("/v1/runs", json=p_acme,
                     headers={"Authorization": f"Bearer {member_acme}"})
    assert r1.json()["project_id"] != r2.json()["project_id"]

    # Each member's filter on project=demo returns ONLY their run.
    r_d = client.get(
        "/v1/runs?project=demo",
        headers={"Authorization": f"Bearer {member_default}"},
    )
    ids_d = {x["run_id"] for x in r_d.json()["runs"]}
    assert ids_d == {p_default["run_id"]}

    r_a = client.get(
        "/v1/runs?project=demo",
        headers={"Authorization": f"Bearer {member_acme}"},
    )
    ids_a = {x["run_id"] for x in r_a.json()["runs"]}
    assert ids_a == {p_acme["run_id"]}


def test_trials_and_rows_are_denormalized(client, auth_headers, tmp_path):
    """Querying the DB beyond payload_json must work — that's why the
    server denormalizes trials / rows / gates / assets at ingest. This
    test exercises the joinable shape via a back-door SELECT to catch
    accidental persistence regressions."""
    import sqlite3

    payload = _produce_real_run(tmp_path)
    client.post("/v1/runs", json=payload, headers=auth_headers)

    settings = client.app.state.settings
    db_path = settings.sqlite_path
    raw = sqlite3.connect(db_path)
    raw.row_factory = sqlite3.Row
    try:
        run = raw.execute("SELECT COUNT(*) AS n FROM runs").fetchone()
        assert run["n"] == 1
        trials = raw.execute(
            "SELECT trial_id FROM trials WHERE run_id=?", (payload["run_id"],)
        ).fetchall()
        assert len(trials) == len(payload["trials"])
        rows = raw.execute(
            "SELECT row_id, n_scores FROM run_rows WHERE run_id=?", (payload["run_id"],)
        ).fetchall()
        assert {r["row_id"] for r in rows} >= {"r1", "r2"}
        assets = raw.execute(
            "SELECT kind FROM assets WHERE run_id=?", (payload["run_id"],)
        ).fetchall()
        assert any(a["kind"] in {"prompt", "dataset", "judge", "heuristic"} for a in assets)
    finally:
        raw.close()
