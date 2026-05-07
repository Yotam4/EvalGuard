"""``/v1/assets`` — cross-run asset aggregation.

Pins:

- The endpoint groups by ``(project_id, kind, asset_id)`` with
  correct ``version_count`` / ``run_count`` / ``last_seen``.
- Kind filter rejects unknown kinds with 400.
- Project filter narrows the listing.
- Cross-tenant isolation: a member of org-A asking for /v1/assets
  never sees rows whose project belongs to org-B (mirrors
  /v1/runs's silent scoping).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from evalguard_cli.local.local_executor import execute
from evalguard_cli.local.serializer import run_to_dict
from evalguard_cli.local.sqlite_store import SqliteStore
from evalguard_cli.local.yaml_loader import load_config


# ---------------------------------------------------------------------------
# helpers


def _run_with_assets(tmp_path: Path, project: str = "p", judge_score: float = 4.5) -> dict:
    """Build a real CLI run with prompts/dataset/judge/heuristic
    assets so the aggregation endpoint has interesting data to crunch."""
    base = tmp_path / "cli"
    (base / "datasets").mkdir(parents=True)
    (base / "datasets" / "g.jsonl").write_text(
        '{"id":"r1","input":"hi"}\n{"id":"r2","input":"yo"}\n'
    )
    (base / "evalguard.yaml").write_text(
        "version: 1\n"
        f"project: {project}\n"
        "providers: [{ id: 'mock:m', config: { mode: echo, latency_ms: 0 } }]\n"
        "datasets: [{ id: g, file: datasets/g.jsonl }]\n"
        "heuristics: [{ id: len, type: length, max: 10000 }]\n"
        f"judges: [{{ id: q, type: mock_pointwise, score: {judge_score}, threshold: 4.0 }}]\n"
    )
    cfg = load_config(base / "evalguard.yaml")
    store = SqliteStore(base / "local.db")
    store.init_schema()
    record = asyncio.run(execute(cfg, store=store, quiet=True))
    return run_to_dict(store, record.run_id, include_rows=True, include_scores=True)


# ---------------------------------------------------------------------------


def test_assets_list_returns_one_row_per_kind_asset_pair(client, auth_headers, tmp_path):
    """A single run has multiple distinct (kind, asset_id) tuples
    (one dataset, one heuristic, one judge). After ingest, /v1/assets
    surfaces exactly those tuples — never one row per ``assets`` table
    row."""
    payload = _run_with_assets(tmp_path)
    client.post("/v1/runs", json=payload, headers=auth_headers)

    r = client.get("/v1/assets", headers=auth_headers)
    assert r.status_code == 200
    pairs = {(a["kind"], a["asset_id"]) for a in r.json()["assets"]}
    # The CLI emits one ``judge`` and one ``heuristic`` and one
    # ``dataset`` asset for this config; possibly schemas/rubrics if
    # the evaluator declared any. We assert at least the explicit
    # three are present.
    assert ("judge", "q")    in pairs
    assert ("heuristic", "len") in pairs
    assert ("dataset", "g")  in pairs


def test_assets_filter_by_kind(client, auth_headers, tmp_path):
    payload = _run_with_assets(tmp_path)
    client.post("/v1/runs", json=payload, headers=auth_headers)
    r = client.get("/v1/assets?kind=judge", headers=auth_headers)
    assert r.status_code == 200
    kinds = {a["kind"] for a in r.json()["assets"]}
    assert kinds == {"judge"}


def test_assets_unknown_kind_returns_400(client, auth_headers):
    r = client.get("/v1/assets?kind=nonsense", headers=auth_headers)
    assert r.status_code == 400


def test_assets_filter_by_project(client, auth_headers, tmp_path):
    """Two runs in two projects → ?project=alpha only returns assets
    from project alpha."""
    p_alpha = _run_with_assets(tmp_path / "a", project="alpha")
    p_beta  = _run_with_assets(tmp_path / "b", project="beta")
    client.post("/v1/runs", json=p_alpha, headers=auth_headers)
    client.post("/v1/runs", json=p_beta,  headers=auth_headers)

    r = client.get("/v1/assets?project=alpha", headers=auth_headers)
    assert r.status_code == 200
    project_names = {a["project_name"] for a in r.json()["assets"]}
    assert project_names == {"alpha"}


def test_assets_version_count_aggregates_across_runs(client, auth_headers, tmp_path):
    """Two runs with the SAME judge asset_id but different
    ``judge_score`` configs → distinct version_id (the spec hash
    incorporates score), so version_count should be 2 and run_count
    should be 2."""
    p1 = _run_with_assets(tmp_path / "v1", judge_score=4.5)
    p2 = _run_with_assets(tmp_path / "v2", judge_score=4.7)
    client.post("/v1/runs", json=p1, headers=auth_headers)
    client.post("/v1/runs", json=p2, headers=auth_headers)

    r = client.get("/v1/assets?kind=judge", headers=auth_headers)
    judge_rows = [a for a in r.json()["assets"] if a["asset_id"] == "q"]
    assert len(judge_rows) == 1
    row = judge_rows[0]
    assert row["version_count"] == 2
    assert row["run_count"] == 2
    # last_seen is the most recent ingest of any version.
    assert row["last_seen"]
    assert row["last_run_id"] in (p1["run_id"], p2["run_id"])
    assert row["last_version_id"]


def test_assets_member_only_sees_own_org(
    client, auth_headers, make_org, make_member_token, tmp_path,
):
    """An admin pushes a run into org_acme; a member of org_default
    must NOT see its assets through /v1/assets. Same isolation
    contract as /v1/runs."""
    make_org("acme")
    member_default = make_member_token("org_default", name="d")
    member_acme    = make_member_token("org_acme",    name="a")

    p_default = _run_with_assets(tmp_path / "d", project="default-proj")
    p_acme    = _run_with_assets(tmp_path / "a", project="acme-proj")
    client.post("/v1/runs", json=p_default,
                headers={"Authorization": f"Bearer {member_default}"})
    client.post("/v1/runs", json=p_acme,
                headers={"Authorization": f"Bearer {member_acme}"})

    # Member of default sees only assets from their own org's runs.
    r = client.get(
        "/v1/assets",
        headers={"Authorization": f"Bearer {member_default}"},
    )
    project_names = {a["project_name"] for a in r.json()["assets"]}
    assert "default-proj" in project_names
    assert "acme-proj" not in project_names

    # Admin sees both.
    r2 = client.get("/v1/assets", headers=auth_headers)
    admin_projects = {a["project_name"] for a in r2.json()["assets"]}
    assert {"default-proj", "acme-proj"} <= admin_projects


def test_assets_limit_caps_results(client, auth_headers, tmp_path):
    payload = _run_with_assets(tmp_path)
    client.post("/v1/runs", json=payload, headers=auth_headers)
    r = client.get("/v1/assets?limit=1", headers=auth_headers)
    assert r.status_code == 200
    assert len(r.json()["assets"]) == 1


def test_assets_invalid_limit_returns_422(client, auth_headers):
    r = client.get("/v1/assets?limit=0", headers=auth_headers)
    assert r.status_code == 422
    r2 = client.get("/v1/assets?limit=99999", headers=auth_headers)
    assert r2.status_code == 422
