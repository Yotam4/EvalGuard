"""``evalguard assets versions`` — CLI smoke against a stub server.

Pattern mirrors ``tests/test_push.py``: spin a stdlib HTTP server,
invoke the CLI as a subprocess against it, assert on the request
shape + the rendered output + the exit code.

The stub serves canned responses based on the path so we exercise
every error-code branch (200 / 400 / 401 / 404 / 500) without
plumbing through the real server's auth + project lookup.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path


def _assets(*args: str, env: dict[str, str] | None = None,
            cwd: Path | None = None) -> subprocess.CompletedProcess:
    """Run ``python -m evalguard_cli.main assets ...`` with isolated env.

    Strip ``EVALGUARD_*`` from inherited env so a developer running
    these locally against a real server doesn't accidentally hit it.
    """
    base_env = {
        k: v for k, v in os.environ.items()
        if k not in {"EVALGUARD_SERVER", "EVALGUARD_API_TOKEN"}
    }
    if env:
        base_env.update(env)
    return subprocess.run(
        [sys.executable, "-m", "evalguard_cli.main", "assets", *args],
        capture_output=True, text=True, timeout=15,
        env=base_env, cwd=str(cwd) if cwd else None,
    )


def _stub_server(handler_factory):
    """Boot a stdlib HTTPServer with the supplied handler class.
    Returns ``(port, shutdown_callable)``."""
    httpd = HTTPServer(("127.0.0.1", 0), handler_factory)
    port  = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    def _stop():
        httpd.shutdown()
        t.join(timeout=2)
    return port, _stop


def _make_handler(status: int, body: bytes, captured: dict):
    """Build a BaseHTTPRequestHandler that records the request and
    returns a fixed response.  Factored out so each test can pin a
    different (status, body) pair."""
    class _H(BaseHTTPRequestHandler):
        def log_message(self, *_a, **_kw): pass

        def do_GET(self):
            captured["path"] = self.path
            captured["auth"] = self.headers.get("authorization")
            self.send_response(status)
            self.send_header("content-type", "application/json")
            self.end_headers()
            self.wfile.write(body)
    return _H


# ---------------------------------------------------------------------------
# Happy path


def test_versions_renders_table(tmp_path: Path):
    body = json.dumps({
        "kind": "judge",
        "asset_id": "q",
        "project_id": "proj_demo",
        "project_name": "demo-project",
        "versions": [
            {"version_id": "sha256-aaaa", "run_id": "run_aaaa1111",
             "project_name": "demo-project",
             "ingested_at": "2026-05-14T07:30:00", "source": "cli"},
            {"version_id": "sha256-bbbb", "run_id": "run_bbbb2222",
             "project_name": "demo-project",
             "ingested_at": "2026-05-13T07:30:00", "source": "otlp"},
        ],
    }).encode("utf-8")
    captured: dict = {}
    port, stop = _stub_server(_make_handler(200, body, captured))
    try:
        result = _assets(
            "versions", "judge", "q",
            "--project-id", "proj_demo",
            "--server", f"http://127.0.0.1:{port}",
            "--token", "tok-secret",
            cwd=tmp_path,
        )
    finally:
        stop()
    assert result.returncode == 0, result.stderr or result.stdout
    # URL composition — kind + asset_id percent-encoded path segments,
    # project_id + limit in the query string.
    assert captured["path"] == (
        "/v1/assets/judge/q/versions?project_id=proj_demo&limit=200"
    )
    assert captured["auth"] == "Bearer tok-secret"
    # Header line.
    assert "demo-project" in result.stdout
    assert "judge"        in result.stdout
    # The two version_id prefixes both surface in the table.
    assert "sha256-aaaa" in result.stdout
    assert "sha256-bbbb" in result.stdout
    # Record count footer.
    assert "2 record" in result.stdout


def test_versions_json_mode_emits_raw_response(tmp_path: Path):
    """``--json`` must emit only the JSON object — no Rich ANSI,
    no header line, no record-count footer — so piping to ``jq``
    works.
    """
    raw = {
        "kind": "dataset", "asset_id": "g",
        "project_id": "proj_x", "project_name": "demo",
        "versions": [],
    }
    captured: dict = {}
    port, stop = _stub_server(_make_handler(
        200, json.dumps(raw).encode("utf-8"), captured,
    ))
    try:
        result = _assets(
            "versions", "dataset", "g",
            "--project-id", "proj_x",
            "--server", f"http://127.0.0.1:{port}",
            "--json",
            cwd=tmp_path,
        )
    finally:
        stop()
    assert result.returncode == 0, result.stderr or result.stdout
    parsed = json.loads(result.stdout)
    assert parsed == raw


def test_versions_percent_encodes_path_segments(tmp_path: Path):
    """An ``asset_id`` containing ``/`` must reach the server as a
    single path segment, not a sub-resource."""
    captured: dict = {}
    port, stop = _stub_server(_make_handler(
        200,
        json.dumps({
            "kind": "judge", "asset_id": "q/strict",
            "project_id": "proj_demo", "project_name": "demo",
            "versions": [],
        }).encode("utf-8"),
        captured,
    ))
    try:
        result = _assets(
            "versions", "judge", "q/strict",
            "--project-id", "proj_demo",
            "--server", f"http://127.0.0.1:{port}",
            cwd=tmp_path,
        )
    finally:
        stop()
    assert result.returncode == 0, result.stderr or result.stdout
    assert "/v1/assets/judge/q%2Fstrict/versions" in captured["path"]


# ---------------------------------------------------------------------------
# Error paths


def test_versions_unknown_kind_caught_client_side(tmp_path: Path):
    """The CLI catches an obvious typo before round-tripping to the
    server — saves a 400 and gives the operator the allowed set
    inline."""
    result = _assets(
        "versions", "nonsense", "q",
        "--project-id", "proj_x",
        "--server", "http://127.0.0.1:1",   # nothing here; client-side check fires first
        cwd=tmp_path,
    )
    assert result.returncode == 2
    out = (result.stdout + result.stderr).lower()
    assert "unknown kind" in out
    assert "judge" in out   # the allowed set is listed
    assert "dataset" in out


def test_versions_without_server_exits_with_hint(tmp_path: Path):
    result = _assets(
        "versions", "judge", "q",
        "--project-id", "proj_x",
        cwd=tmp_path,
    )
    assert result.returncode == 2
    assert "no server configured" in (result.stdout + result.stderr).lower()


def test_versions_propagates_404_with_detail(tmp_path: Path):
    captured: dict = {}
    port, stop = _stub_server(_make_handler(
        404,
        json.dumps({"detail": "Asset judge/'q-missing' not found in project 'proj_x'."}).encode("utf-8"),
        captured,
    ))
    try:
        result = _assets(
            "versions", "judge", "q-missing",
            "--project-id", "proj_x",
            "--server", f"http://127.0.0.1:{port}",
            cwd=tmp_path,
        )
    finally:
        stop()
    assert result.returncode == 1
    out = result.stdout + result.stderr
    # The server's detail surfaces verbatim — the operator shouldn't
    # have to translate "404" into "the asset doesn't exist".
    assert "q-missing" in out
    assert "not found" in out.lower()


def test_versions_401_exits_with_auth_failed(tmp_path: Path):
    captured: dict = {}
    port, stop = _stub_server(_make_handler(
        401,
        json.dumps({"detail": "Missing Authorization header."}).encode("utf-8"),
        captured,
    ))
    try:
        result = _assets(
            "versions", "judge", "q",
            "--project-id", "proj_x",
            "--server", f"http://127.0.0.1:{port}",
            cwd=tmp_path,
        )
    finally:
        stop()
    # Distinct exit code (2) so a CI workflow can react to auth
    # failure separately from a generic 1.
    assert result.returncode == 2
    assert "authentication failed" in (result.stdout + result.stderr).lower()


def test_versions_5xx_exits_1_with_detail(tmp_path: Path):
    captured: dict = {}
    port, stop = _stub_server(_make_handler(
        500,
        json.dumps({"detail": "internal server error"}).encode("utf-8"),
        captured,
    ))
    try:
        result = _assets(
            "versions", "judge", "q",
            "--project-id", "proj_x",
            "--server", f"http://127.0.0.1:{port}",
            cwd=tmp_path,
        )
    finally:
        stop()
    assert result.returncode == 1
    out = (result.stdout + result.stderr).lower()
    assert "internal server error" in out


def test_versions_uses_env_vars_when_flags_omitted(tmp_path: Path):
    """``EVALGUARD_SERVER`` / ``EVALGUARD_API_TOKEN`` are the env-
    var contract that ``push`` already uses — pin parity here so a
    user who has ``EVALGUARD_*`` exported can run ``assets versions``
    without ``--server`` / ``--token``."""
    captured: dict = {}
    port, stop = _stub_server(_make_handler(
        200,
        json.dumps({
            "kind": "judge", "asset_id": "q",
            "project_id": "proj_x", "project_name": "demo",
            "versions": [],
        }).encode("utf-8"),
        captured,
    ))
    try:
        result = _assets(
            "versions", "judge", "q",
            "--project-id", "proj_x",
            env={
                "EVALGUARD_SERVER":    f"http://127.0.0.1:{port}",
                "EVALGUARD_API_TOKEN": "env-token-secret",
            },
            cwd=tmp_path,
        )
    finally:
        stop()
    assert result.returncode == 0, result.stderr or result.stdout
    assert captured["auth"] == "Bearer env-token-secret"


# ---------------------------------------------------------------------------
# Review-pass additions (audit findings)


def test_versions_explicit_limit_reaches_server(tmp_path: Path):
    """``--limit 25`` reaches the server as ``limit=25`` — pins
    the flag actually propagates instead of falling back to the
    200 default."""
    captured: dict = {}
    port, stop = _stub_server(_make_handler(
        200,
        json.dumps({
            "kind": "judge", "asset_id": "q",
            "project_id": "proj_demo", "project_name": "demo",
            "versions": [],
        }).encode("utf-8"),
        captured,
    ))
    try:
        result = _assets(
            "versions", "judge", "q",
            "--project-id", "proj_demo",
            "--limit", "25",
            "--server", f"http://127.0.0.1:{port}",
            cwd=tmp_path,
        )
    finally:
        stop()
    assert result.returncode == 0, result.stderr or result.stdout
    assert "limit=25" in captured["path"]


def test_versions_limit_above_max_rejected_client_side(tmp_path: Path):
    """``--limit 9999`` is above the CLI's typer ``max=1000`` and is
    rejected before any network call — fast-failure on a typo."""
    result = _assets(
        "versions", "judge", "q",
        "--project-id", "proj_demo",
        "--limit", "9999",
        "--server", "http://127.0.0.1:1",
        cwd=tmp_path,
    )
    assert result.returncode != 0
    # Typer's validation message mentions the bound.  We don't
    # pin the exact wording (it changes across Click/Typer
    # versions), only that the call is rejected.
    assert "1000" in (result.stdout + result.stderr)


def test_versions_unreachable_server_exits_with_urlerror_hint(tmp_path: Path):
    """``--server http://127.0.0.1:1`` points at a refused port —
    confirms the ``urllib.error.URLError`` branch surfaces a
    readable "Could not reach server" message rather than a raw
    traceback."""
    result = _assets(
        "versions", "judge", "q",
        "--project-id", "proj_demo",
        "--server", "http://127.0.0.1:1",
        cwd=tmp_path,
    )
    assert result.returncode == 1
    out = (result.stdout + result.stderr).lower()
    assert "could not reach server" in out


def test_versions_non_json_200_body_exits_with_clear_message(tmp_path: Path):
    """A reverse-proxy that returns a 200 with HTML (auth wall, error
    page) used to crash the CLI with a JSON-decode traceback.  Now
    surfaces a readable message and exit 1."""
    captured: dict = {}
    port, stop = _stub_server(_make_handler(
        200,
        b"<html><body>upstream not authenticated</body></html>",
        captured,
    ))
    try:
        result = _assets(
            "versions", "judge", "q",
            "--project-id", "proj_demo",
            "--server", f"http://127.0.0.1:{port}",
            cwd=tmp_path,
        )
    finally:
        stop()
    assert result.returncode == 1
    out = (result.stdout + result.stderr).lower()
    assert "non-json" in out


def test_versions_empty_token_env_var_caught_explicitly(tmp_path: Path):
    """``EVALGUARD_API_TOKEN=`` (empty string) used to silently
    omit the bearer header and produce a server 401 the operator
    has to debug.  Now the CLI catches the empty-string case
    locally and tells them what to fix."""
    result = _assets(
        "versions", "judge", "q",
        "--project-id", "proj_demo",
        "--server", "http://127.0.0.1:1",  # unreachable, but we shouldn't get there
        env={
            "EVALGUARD_SERVER":    "ignored",  # ``--server`` flag wins
            "EVALGUARD_API_TOKEN": "",
        },
        cwd=tmp_path,
    )
    assert result.returncode == 2
    out = (result.stdout + result.stderr).lower()
    assert "empty string" in out


def test_versions_json_mode_does_not_mix_errors_into_stdout(tmp_path: Path):
    """The whole point of ``--json`` is downstream ``jq``.  An error
    on a ``--json`` invocation must go to stderr, not corrupt the
    pipe."""
    captured: dict = {}
    port, stop = _stub_server(_make_handler(
        404,
        json.dumps({"detail": "Asset judge/'q' not found"}).encode("utf-8"),
        captured,
    ))
    try:
        result = _assets(
            "versions", "judge", "q",
            "--project-id", "proj_x",
            "--server", f"http://127.0.0.1:{port}",
            "--json",
            cwd=tmp_path,
        )
    finally:
        stop()
    assert result.returncode == 1
    # stdout must be EMPTY or pure-JSON (it's empty here because
    # the request failed before any JSON could be emitted).
    # ``jq < /dev/null`` succeeds; ``jq < "...error message..."``
    # would fail loudly — so the contract we pin is "no error
    # text on stdout".
    assert "not found" not in result.stdout.lower()
    assert "not found" in result.stderr.lower()
