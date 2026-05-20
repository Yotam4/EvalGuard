"""``/v1/golden/*`` — Phase OBS-4 golden-candidates staging.

Three surfaces (POST / GET / DELETE) — these tests pin the
UPSERT-per-reviewer contract, the project-listing visibility, the
delete-by-promoter-or-admin rule, and the anti-enumeration shape
on the error paths.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from evalguard_cli.local.local_executor import execute
from evalguard_cli.local.serializer import run_to_dict
from evalguard_cli.local.sqlite_store import SqliteStore
from evalguard_cli.local.yaml_loader import load_config


def _produce_run(tmp_path: Path, project: str = "p", rows: int = 1) -> dict:
    """Tiny CLI run with ``rows`` data rows.  Reuses the same shape
    as ``tests/api/test_runs.py:_produce_real_run`` but parameterised
    so a golden test can promote a specific row_id."""
    import json as _json
    base = tmp_path / "cli"
    base.mkdir(parents=True, exist_ok=True)
    (base / "datasets").mkdir(parents=True, exist_ok=True)
    dataset = "\n".join(
        _json.dumps({"id": f"r{i}", "input": f"q{i}"}) for i in range(rows)
    ) + "\n"
    (base / "datasets" / "g.jsonl").write_text(dataset)
    (base / "evalguard.yaml").write_text(
        "version: 1\n"
        f"project: {project}\n"
        "providers: [{ id: 'mock:m', config: { mode: echo, latency_ms: 0 } }]\n"
        "datasets: [{ id: g, file: datasets/g.jsonl }]\n"
    )
    cfg = load_config(base / "evalguard.yaml")
    store = SqliteStore(base / "local.db")
    store.init_schema()
    record = asyncio.run(execute(cfg, store=store, quiet=True))
    return run_to_dict(store, record.run_id, include_rows=True, include_scores=True)


def _post_run(client, headers, payload) -> None:
    r = client.post("/v1/runs", json=payload, headers=headers)
    assert r.status_code == 201, r.text


# ---------------------------------------------------------------------------
# POST /v1/golden/candidates


def test_promote_returns_201_with_full_record(client, auth_headers, tmp_path):
    payload = _produce_run(tmp_path, project="gold-promote", rows=1)
    _post_run(client, auth_headers, payload)
    r = client.post(
        "/v1/golden/candidates",
        json={"run_id": payload["run_id"], "row_id": "r0",
              "note": "  a real regression case  "},
        headers=auth_headers,
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["run_id"] == payload["run_id"]
    assert body["row_id"] == "r0"
    # ``note`` normalised: surrounding whitespace stripped.
    assert body["note"]   == "a real regression case"
    assert body["promoted_by"].startswith("key_")
    assert body["created_at"]


def test_repromote_upserts_and_updates_note(client, auth_headers, tmp_path):
    """Same reviewer + same (run, row) ⇒ same row, note refreshed."""
    payload = _produce_run(tmp_path, project="gold-upsert", rows=1)
    _post_run(client, auth_headers, payload)

    first = client.post(
        "/v1/golden/candidates",
        json={"run_id": payload["run_id"], "row_id": "r0", "note": "v1"},
        headers=auth_headers,
    )
    second = client.post(
        "/v1/golden/candidates",
        json={"run_id": payload["run_id"], "row_id": "r0", "note": "v2 — updated"},
        headers=auth_headers,
    )
    assert first.status_code  == 201
    assert second.status_code == 201
    # Same ID (UPSERT, not new insert).
    assert first.json()["id"]         == second.json()["id"]
    # Note updated in place.
    assert second.json()["note"]      == "v2 — updated"
    # ``created_at`` preserved.
    assert first.json()["created_at"] == second.json()["created_at"]


def test_two_reviewers_promoting_same_row_produce_two_records(
    client, auth_headers, make_member_token, tmp_path,
):
    """Each reviewer's promotion captures their own note → cross-
    annotator agreement starts here, same as ``row_reviews``."""
    payload = _produce_run(tmp_path, project="gold-multi", rows=1)
    _post_run(client, auth_headers, payload)
    member = make_member_token("org_default", name="other-reviewer")

    r1 = client.post(
        "/v1/golden/candidates",
        json={"run_id": payload["run_id"], "row_id": "r0", "note": "admin says good"},
        headers=auth_headers,
    )
    r2 = client.post(
        "/v1/golden/candidates",
        json={"run_id": payload["run_id"], "row_id": "r0", "note": "member says edge case"},
        headers={"Authorization": f"Bearer {member}"},
    )
    assert r1.status_code == r2.status_code == 201
    assert r1.json()["id"] != r2.json()["id"]
    assert r1.json()["promoted_by"] != r2.json()["promoted_by"]


def test_promote_unknown_row_returns_404(client, auth_headers, tmp_path):
    payload = _produce_run(tmp_path, project="gold-badrow", rows=1)
    _post_run(client, auth_headers, payload)
    r = client.post(
        "/v1/golden/candidates",
        json={"run_id": payload["run_id"], "row_id": "nope"},
        headers=auth_headers,
    )
    assert r.status_code == 404
    assert "nope" in r.json()["detail"]


def test_promote_unknown_run_returns_404(client, auth_headers):
    r = client.post(
        "/v1/golden/candidates",
        json={"run_id": "run_doesnotexist", "row_id": "r0"},
        headers=auth_headers,
    )
    assert r.status_code == 404


def test_promote_cross_org_run_returns_404(
    client, auth_headers, make_org, make_member_token, tmp_path,
):
    make_org("acme")
    member_default = make_member_token("org_default", name="d")
    member_acme    = make_member_token("org_acme",    name="a")
    payload = _produce_run(tmp_path, project="acme-gold-priv", rows=1)
    _post_run(client, {"Authorization": f"Bearer {member_acme}"}, payload)

    r = client.post(
        "/v1/golden/candidates",
        json={"run_id": payload["run_id"], "row_id": "r0"},
        headers={"Authorization": f"Bearer {member_default}"},
    )
    assert r.status_code == 404


def test_promote_rejects_oversize_note(client, auth_headers, tmp_path):
    payload = _produce_run(tmp_path, project="gold-bignote", rows=1)
    _post_run(client, auth_headers, payload)
    r = client.post(
        "/v1/golden/candidates",
        json={"run_id": payload["run_id"], "row_id": "r0", "note": "x" * 4_500},
        headers=auth_headers,
    )
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# GET /v1/projects/{slug}/golden/candidates


def test_list_returns_newest_first(client, auth_headers, tmp_path):
    payload = _produce_run(tmp_path, project="gold-list", rows=2)
    _post_run(client, auth_headers, payload)
    # Promote both rows; second one created later → newest first.
    client.post(
        "/v1/golden/candidates",
        json={"run_id": payload["run_id"], "row_id": "r0", "note": "first"},
        headers=auth_headers,
    )
    client.post(
        "/v1/golden/candidates",
        json={"run_id": payload["run_id"], "row_id": "r1", "note": "second"},
        headers=auth_headers,
    )
    r = client.get(
        "/v1/projects/gold-list/golden/candidates", headers=auth_headers,
    )
    assert r.status_code == 200, r.text
    body = r.json()["candidates"]
    assert len(body) == 2
    # Newest first — second-promoted row at index 0.
    notes = [c["note"] for c in body]
    assert notes == ["second", "first"]


def test_list_unknown_project_returns_404(client, auth_headers):
    r = client.get(
        "/v1/projects/no-such-project/golden/candidates",
        headers=auth_headers,
    )
    assert r.status_code == 404


def test_list_cross_org_returns_404_for_member(
    client, auth_headers, make_org, make_member_token, tmp_path,
):
    make_org("acme")
    member_default = make_member_token("org_default", name="d")
    member_acme    = make_member_token("org_acme",    name="a")
    payload = _produce_run(tmp_path, project="acme-gold-listpriv", rows=1)
    _post_run(client, {"Authorization": f"Bearer {member_acme}"}, payload)
    client.post(
        "/v1/golden/candidates",
        json={"run_id": payload["run_id"], "row_id": "r0"},
        headers={"Authorization": f"Bearer {member_acme}"},
    )

    r = client.get(
        "/v1/projects/acme-gold-listpriv/golden/candidates",
        headers={"Authorization": f"Bearer {member_default}"},
    )
    assert r.status_code == 404


def test_list_requires_auth(client):
    r = client.get("/v1/projects/x/golden/candidates")
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# DELETE /v1/golden/candidates/{id}


def test_delete_by_original_promoter_succeeds(client, auth_headers, tmp_path):
    payload = _produce_run(tmp_path, project="gold-del", rows=1)
    _post_run(client, auth_headers, payload)
    created = client.post(
        "/v1/golden/candidates",
        json={"run_id": payload["run_id"], "row_id": "r0"},
        headers=auth_headers,
    )
    cid = created.json()["id"]

    r = client.delete(
        f"/v1/golden/candidates/{cid}", headers=auth_headers,
    )
    assert r.status_code == 204

    # Listing no longer shows it.
    listing = client.get(
        "/v1/projects/gold-del/golden/candidates", headers=auth_headers,
    ).json()["candidates"]
    assert listing == []


def test_delete_by_other_member_returns_403(
    client, auth_headers, make_member_token, tmp_path,
):
    """A different member of the same org sees the candidate (it's
    in the project listing) so 404 would be a lie — return 403 to
    say "you can see it but not delete it"."""
    payload = _produce_run(tmp_path, project="gold-del-403", rows=1)
    _post_run(client, auth_headers, payload)
    created = client.post(
        "/v1/golden/candidates",
        json={"run_id": payload["run_id"], "row_id": "r0"},
        headers=auth_headers,
    )
    cid = created.json()["id"]

    other = make_member_token("org_default", name="someone-else")
    r = client.delete(
        f"/v1/golden/candidates/{cid}",
        headers={"Authorization": f"Bearer {other}"},
    )
    assert r.status_code == 403


def test_delete_admin_can_delete_anyones(
    client, auth_headers, make_member_token, tmp_path,
):
    """Admin override — needed so the audit-trail can be curated
    even when the original promoter is gone."""
    member = make_member_token("org_default", name="member-promoter")
    payload = _produce_run(tmp_path, project="gold-del-admin", rows=1)
    _post_run(client, {"Authorization": f"Bearer {member}"}, payload)
    created = client.post(
        "/v1/golden/candidates",
        json={"run_id": payload["run_id"], "row_id": "r0"},
        headers={"Authorization": f"Bearer {member}"},
    )
    cid = created.json()["id"]

    r = client.delete(
        f"/v1/golden/candidates/{cid}", headers=auth_headers,
    )
    assert r.status_code == 204


def test_delete_cross_org_returns_404(
    client, auth_headers, make_org, make_member_token, tmp_path,
):
    make_org("acme")
    member_acme = make_member_token("org_acme", name="a")
    member_other = make_member_token("org_default", name="d")
    payload = _produce_run(tmp_path, project="acme-gold-deletepriv", rows=1)
    _post_run(client, {"Authorization": f"Bearer {member_acme}"}, payload)
    created = client.post(
        "/v1/golden/candidates",
        json={"run_id": payload["run_id"], "row_id": "r0"},
        headers={"Authorization": f"Bearer {member_acme}"},
    )
    cid = created.json()["id"]

    # default-org member tries to delete acme's candidate — 404
    # (anti-enumeration, NOT 403 which would confirm existence).
    r = client.delete(
        f"/v1/golden/candidates/{cid}",
        headers={"Authorization": f"Bearer {member_other}"},
    )
    assert r.status_code == 404


def test_delete_unknown_id_returns_404(client, auth_headers):
    r = client.delete(
        "/v1/golden/candidates/9999999", headers=auth_headers,
    )
    assert r.status_code == 404
