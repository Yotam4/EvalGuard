"""``evalguard golden`` — CLI smoke against a stub server.

Pattern mirrors ``tests/test_assets_cmd.py``: spin a stdlib HTTP
server with route-dispatching, invoke the CLI as a subprocess against
it, assert on the request shape + rendered output + exit code.

The export subcommand needs both ``/v1/projects/{slug}/golden/candidates``
(list) AND ``/v1/projects/{slug}/calls/{run_id}/{row_id}`` (detail) so
the stub routes on path.  No additional infra over the assets-CLI
test scaffolding.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path


def _golden(*args: str, env: dict[str, str] | None = None,
            cwd: Path | None = None) -> subprocess.CompletedProcess:
    """Run ``python -m evalguard_cli.main golden ...`` with isolated env."""
    base_env = {
        k: v for k, v in os.environ.items()
        if k not in {"EVALGUARD_SERVER", "EVALGUARD_API_TOKEN"}
    }
    if env:
        base_env.update(env)
    return subprocess.run(
        [sys.executable, "-m", "evalguard_cli.main", "golden", *args],
        capture_output=True, text=True, timeout=15,
        env=base_env, cwd=str(cwd) if cwd else None,
    )


def _stub_server(handler_factory):
    httpd = HTTPServer(("127.0.0.1", 0), handler_factory)
    port  = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    def _stop():
        httpd.shutdown()
        t.join(timeout=2)
    return port, _stop


def _make_routing_handler(routes: dict[str, tuple[int, bytes]], captured: dict):
    """Build a handler that picks a (status, body) pair based on the
    request's path-prefix.  ``captured`` collects every request path so
    tests can pin URL composition + call counts."""
    class _H(BaseHTTPRequestHandler):
        def log_message(self, *_a, **_kw): pass

        def do_GET(self):
            captured.setdefault("paths", []).append(self.path)
            captured["last_auth"] = self.headers.get("authorization")
            for prefix, (status, body) in routes.items():
                if self.path.startswith(prefix):
                    self.send_response(status)
                    self.send_header("content-type", "application/json")
                    self.end_headers()
                    self.wfile.write(body)
                    return
            self.send_response(404)
            self.send_header("content-type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"detail":"stub: no route"}')
    return _H


def _list_body(candidates: list[dict]) -> bytes:
    return json.dumps({"candidates": candidates}).encode("utf-8")


def _detail_body(row: dict) -> bytes:
    return json.dumps(row).encode("utf-8")


# ---------------------------------------------------------------------------
# golden list


def test_list_renders_rich_table(tmp_path: Path):
    captured: dict = {}
    body = _list_body([
        {"id": 1, "run_id": "run_a", "row_id": "r-1",
         "project_id": "proj", "promoted_by": "key_a",
         "note": "looks good", "created_at": "2026-05-15T07:30:00"},
        {"id": 2, "run_id": "run_b", "row_id": "r-2",
         "project_id": "proj", "promoted_by": "key_b",
         "note": None, "created_at": "2026-05-14T07:30:00"},
    ])
    routes = {"/v1/projects/demo/golden/candidates": (200, body)}
    port, stop = _stub_server(_make_routing_handler(routes, captured))
    try:
        result = _golden(
            "list", "--project", "demo",
            "--server", f"http://127.0.0.1:{port}",
            "--token", "tok-secret",
            cwd=tmp_path,
        )
    finally:
        stop()
    assert result.returncode == 0, result.stderr or result.stdout
    assert captured["paths"][0] == "/v1/projects/demo/golden/candidates?limit=100"
    assert captured["last_auth"] == "Bearer tok-secret"
    assert "run_a" in result.stdout
    assert "looks good" in result.stdout
    assert "2 of" in result.stdout   # header count


def test_list_json_mode_emits_raw_response(tmp_path: Path):
    captured: dict = {}
    raw = {"candidates": []}
    routes = {"/v1/projects/demo/golden/candidates":
              (200, json.dumps(raw).encode("utf-8"))}
    port, stop = _stub_server(_make_routing_handler(routes, captured))
    try:
        result = _golden(
            "list", "--project", "demo",
            "--server", f"http://127.0.0.1:{port}",
            "--json",
            cwd=tmp_path,
        )
    finally:
        stop()
    assert result.returncode == 0, result.stderr or result.stdout
    assert json.loads(result.stdout) == raw


def test_list_empty_says_so(tmp_path: Path):
    captured: dict = {}
    routes = {"/v1/projects/demo/golden/candidates":
              (200, _list_body([]))}
    port, stop = _stub_server(_make_routing_handler(routes, captured))
    try:
        result = _golden(
            "list", "--project", "demo",
            "--server", f"http://127.0.0.1:{port}",
            cwd=tmp_path,
        )
    finally:
        stop()
    assert result.returncode == 0
    assert "no candidates" in result.stdout.lower()


def test_list_404_propagates_detail(tmp_path: Path):
    captured: dict = {}
    routes = {"/v1/projects/missing/golden/candidates":
              (404, b'{"detail":"Project \'missing\' not found."}')}
    port, stop = _stub_server(_make_routing_handler(routes, captured))
    try:
        result = _golden(
            "list", "--project", "missing",
            "--server", f"http://127.0.0.1:{port}",
            cwd=tmp_path,
        )
    finally:
        stop()
    assert result.returncode == 1
    assert "missing" in (result.stdout + result.stderr).lower()


def test_list_without_server_exits_with_hint(tmp_path: Path):
    result = _golden("list", "--project", "demo", cwd=tmp_path)
    assert result.returncode == 2
    assert "no server configured" in (result.stdout + result.stderr).lower()


def test_list_uses_env_vars(tmp_path: Path):
    captured: dict = {}
    routes = {"/v1/projects/demo/golden/candidates":
              (200, _list_body([]))}
    port, stop = _stub_server(_make_routing_handler(routes, captured))
    try:
        result = _golden(
            "list", "--project", "demo",
            env={
                "EVALGUARD_SERVER":    f"http://127.0.0.1:{port}",
                "EVALGUARD_API_TOKEN": "env-token-secret",
            },
            cwd=tmp_path,
        )
    finally:
        stop()
    assert result.returncode == 0, result.stderr or result.stdout
    assert captured["last_auth"] == "Bearer env-token-secret"


# ---------------------------------------------------------------------------
# golden export


def _detail_routes_for(candidates: list[dict], rows: dict[tuple[str, str], dict]) -> dict:
    """Build per-(run_id, row_id) detail routes for the stub server."""
    routes = {}
    for c in candidates:
        key = (c["run_id"], c["row_id"])
        if key in rows:
            routes[f"/v1/projects/demo/calls/{c['run_id']}/{c['row_id']}"] = (
                200, _detail_body(rows[key]),
            )
        else:
            routes[f"/v1/projects/demo/calls/{c['run_id']}/{c['row_id']}"] = (
                404, b'{"detail":"row missing"}',
            )
    return routes


def test_export_writes_jsonl_with_provenance(tmp_path: Path):
    """Happy-path overwrite: two candidates, two row details, the
    output JSONL has two lines with ``_provenance`` blocks attached."""
    captured: dict = {}
    candidates = [
        {"id": 1, "run_id": "run_a", "row_id": "r-1",
         "project_id": "proj", "promoted_by": "key_a",
         "note": "fp", "created_at": "2026-05-15T07:30:00"},
        {"id": 2, "run_id": "run_b", "row_id": "r-2",
         "project_id": "proj", "promoted_by": "key_b",
         "note": None, "created_at": "2026-05-14T07:30:00"},
    ]
    rows = {
        ("run_a", "r-1"): {"run_id": "run_a", "row_id": "r-1",
                           "project_id": "proj", "project": "demo",
                           "input": "What is X?", "expected": "X is …",
                           "output": "X is foo"},
        ("run_b", "r-2"): {"run_id": "run_b", "row_id": "r-2",
                           "project_id": "proj", "project": "demo",
                           "input": "What is Y?", "expected": "Y is …",
                           "output": "Y is bar"},
    }
    routes = {"/v1/projects/demo/golden/candidates": (200, _list_body(candidates))}
    routes.update(_detail_routes_for(candidates, rows))
    port, stop = _stub_server(_make_routing_handler(routes, captured))
    out = tmp_path / "golden.jsonl"
    try:
        result = _golden(
            "export", "--project", "demo",
            "--to", str(out),
            "--server", f"http://127.0.0.1:{port}",
            cwd=tmp_path,
        )
    finally:
        stop()
    assert result.returncode == 0, result.stderr or result.stdout
    lines = out.read_text().strip().split("\n")
    assert len(lines) == 2
    parsed = [json.loads(line) for line in lines]
    ids = {p["id"] for p in parsed}
    assert ids == {"r-1", "r-2"}
    # Provenance attached on every line.
    for p in parsed:
        assert "_provenance" in p
        assert p["_provenance"]["run_id"]
        assert p["_provenance"]["promoted_by"]
    # stderr summary mentions the count.
    assert "exported" in result.stderr.lower()
    assert "2" in result.stderr


def test_export_merge_skips_existing_ids(tmp_path: Path):
    """``--mode merge`` preserves an existing JSONL and skips rows
    whose ``id`` already appears in it."""
    captured: dict = {}
    candidates = [
        {"id": 1, "run_id": "run_a", "row_id": "r-1",
         "project_id": "proj", "promoted_by": "key_a",
         "note": None, "created_at": "2026-05-15T07:30:00"},
        {"id": 2, "run_id": "run_b", "row_id": "r-2",
         "project_id": "proj", "promoted_by": "key_b",
         "note": None, "created_at": "2026-05-14T07:30:00"},
    ]
    rows = {
        ("run_a", "r-1"): {"run_id": "run_a", "row_id": "r-1",
                           "project_id": "proj", "project": "demo",
                           "input": "Q1", "expected": "A1"},
        ("run_b", "r-2"): {"run_id": "run_b", "row_id": "r-2",
                           "project_id": "proj", "project": "demo",
                           "input": "Q2", "expected": "A2"},
    }
    routes = {"/v1/projects/demo/golden/candidates": (200, _list_body(candidates))}
    routes.update(_detail_routes_for(candidates, rows))
    port, stop = _stub_server(_make_routing_handler(routes, captured))

    out = tmp_path / "golden.jsonl"
    # Pre-seed the target with one of the row ids — merge must skip
    # it and append the other.
    out.write_text(json.dumps({"id": "r-1", "input": "pre-existing"}) + "\n")
    try:
        result = _golden(
            "export", "--project", "demo",
            "--to", str(out), "--mode", "merge",
            "--server", f"http://127.0.0.1:{port}",
            cwd=tmp_path,
        )
    finally:
        stop()
    assert result.returncode == 0, result.stderr or result.stdout
    lines = out.read_text().strip().split("\n")
    # Two lines now: the original ``r-1`` and the newly-appended ``r-2``.
    assert len(lines) == 2
    parsed = [json.loads(line) for line in lines]
    assert parsed[0]["id"] == "r-1"
    assert parsed[0]["input"] == "pre-existing"  # unchanged
    assert parsed[1]["id"] == "r-2"
    # The summary names the skip count.
    assert "1 duplicate" in result.stderr


def test_export_skips_rows_with_null_input(tmp_path: Path):
    """A candidate whose row has ``input: null`` (OTLP-derived rows
    sometimes do) is silently dropped from the export with a stderr
    count.  An export that wrote ``"input": null`` would crash
    downstream evaluators."""
    captured: dict = {}
    candidates = [
        {"id": 1, "run_id": "run_a", "row_id": "r-1",
         "project_id": "proj", "promoted_by": "key_a",
         "note": None, "created_at": "2026-05-15T07:30:00"},
        {"id": 2, "run_id": "run_b", "row_id": "r-2",
         "project_id": "proj", "promoted_by": "key_b",
         "note": None, "created_at": "2026-05-14T07:30:00"},
    ]
    rows = {
        ("run_a", "r-1"): {"run_id": "run_a", "row_id": "r-1",
                           "project_id": "proj", "project": "demo",
                           "input": None, "expected": None},
        ("run_b", "r-2"): {"run_id": "run_b", "row_id": "r-2",
                           "project_id": "proj", "project": "demo",
                           "input": "Q2", "expected": "A2"},
    }
    routes = {"/v1/projects/demo/golden/candidates": (200, _list_body(candidates))}
    routes.update(_detail_routes_for(candidates, rows))
    port, stop = _stub_server(_make_routing_handler(routes, captured))
    out = tmp_path / "golden.jsonl"
    try:
        result = _golden(
            "export", "--project", "demo",
            "--to", str(out),
            "--server", f"http://127.0.0.1:{port}",
            cwd=tmp_path,
        )
    finally:
        stop()
    assert result.returncode == 0, result.stderr or result.stdout
    lines = out.read_text().strip().split("\n")
    assert len(lines) == 1
    assert json.loads(lines[0])["id"] == "r-2"
    assert "1 no-input" in result.stderr


def test_export_continues_past_individual_404(tmp_path: Path):
    """The candidate list might reference a row whose parent run was
    deleted between promote and export (CASCADE).  The export must
    count + continue, not abort the whole batch."""
    captured: dict = {}
    candidates = [
        {"id": 1, "run_id": "run_a", "row_id": "r-deleted",
         "project_id": "proj", "promoted_by": "key_a",
         "note": None, "created_at": "2026-05-15T07:30:00"},
        {"id": 2, "run_id": "run_b", "row_id": "r-ok",
         "project_id": "proj", "promoted_by": "key_b",
         "note": None, "created_at": "2026-05-14T07:30:00"},
    ]
    rows = {
        ("run_b", "r-ok"): {"run_id": "run_b", "row_id": "r-ok",
                            "project_id": "proj", "project": "demo",
                            "input": "Q2", "expected": "A2"},
    }
    routes = {"/v1/projects/demo/golden/candidates": (200, _list_body(candidates))}
    routes.update(_detail_routes_for(candidates, rows))
    # The (run_a, r-deleted) detail route was added as a 404 by the
    # helper.
    port, stop = _stub_server(_make_routing_handler(routes, captured))
    out = tmp_path / "golden.jsonl"
    try:
        result = _golden(
            "export", "--project", "demo",
            "--to", str(out),
            "--server", f"http://127.0.0.1:{port}",
            cwd=tmp_path,
        )
    finally:
        stop()
    # Whole batch exits 0 — the one missing row is recorded as a
    # fetch failure, the other writes out.
    assert result.returncode == 0, result.stderr or result.stdout
    lines = out.read_text().strip().split("\n")
    assert len(lines) == 1
    assert json.loads(lines[0])["id"] == "r-ok"
    assert "1 fetch-failure" in result.stderr


def test_export_zero_candidates_does_not_truncate_target(tmp_path: Path):
    """A bug-trap: ``--mode overwrite`` with zero candidates from the
    server should NOT silently wipe a pre-existing file the operator
    asked to be the target.  The export bails before opening for
    write."""
    captured: dict = {}
    routes = {"/v1/projects/demo/golden/candidates": (200, _list_body([]))}
    port, stop = _stub_server(_make_routing_handler(routes, captured))
    out = tmp_path / "golden.jsonl"
    out.write_text('{"id":"keep-me","input":"do not lose"}\n')
    try:
        result = _golden(
            "export", "--project", "demo",
            "--to", str(out),
            "--server", f"http://127.0.0.1:{port}",
            cwd=tmp_path,
        )
    finally:
        stop()
    assert result.returncode == 0
    # The file is untouched.
    assert "keep-me" in out.read_text()


def test_export_unknown_mode_rejected_client_side(tmp_path: Path):
    """A typo like ``--mode merg`` should fail locally before any
    network call — exit 2 with the allowed values."""
    result = _golden(
        "export", "--project", "demo", "--to", str(tmp_path / "x.jsonl"),
        "--mode", "merg",
        "--server", "http://127.0.0.1:1",
        cwd=tmp_path,
    )
    assert result.returncode == 2
    out = (result.stdout + result.stderr).lower()
    assert "unknown mode" in out
    assert "overwrite" in out and "merge" in out


def test_export_jsonl_lines_are_stable_under_sort_keys(tmp_path: Path):
    """The JSONL writer uses ``sort_keys=True`` so re-running export
    produces a diffable file.  Pinning this here so a refactor that
    drops the flag is caught immediately."""
    captured: dict = {}
    candidates = [{"id": 1, "run_id": "run_a", "row_id": "r-1",
                   "project_id": "proj", "promoted_by": "key_a",
                   "note": None, "created_at": "2026-05-15T07:30:00"}]
    rows = {("run_a", "r-1"):
            {"run_id": "run_a", "row_id": "r-1",
             "project_id": "proj", "project": "demo",
             "input": "Q1", "expected": "A1"}}
    routes = {"/v1/projects/demo/golden/candidates": (200, _list_body(candidates))}
    routes.update(_detail_routes_for(candidates, rows))
    port, stop = _stub_server(_make_routing_handler(routes, captured))
    out = tmp_path / "golden.jsonl"
    try:
        _golden(
            "export", "--project", "demo", "--to", str(out),
            "--server", f"http://127.0.0.1:{port}",
            cwd=tmp_path,
        )
    finally:
        stop()
    raw = out.read_text().strip()
    # Top-level keys appear in lexicographic order: ``_provenance``
    # (leading underscore sorts before letters in default ASCII),
    # ``expected``, ``id``, ``input``.
    parsed_line_obj = json.loads(raw)
    expected_key_order = ["_provenance", "expected", "id", "input"]
    assert list(parsed_line_obj.keys()) == expected_key_order
