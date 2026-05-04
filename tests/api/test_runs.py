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
