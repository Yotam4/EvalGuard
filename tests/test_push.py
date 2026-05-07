"""Tests for ``evalguard push``.

The Phase-2 server doesn't ship with this repo, so these tests verify
the wiring rather than a real upload:

1. No ``EVALGUARD_SERVER`` (and no ``--server``) → exits 0 with a hint.
2. ``--dry-run`` against a seeded local run prints valid JSON matching
   the run-output schema and exits 0.
3. ``--server`` pointed at an unreachable URL exits 1 with an error.
4. Pointed at a tiny stdlib HTTP server, the payload that arrives on
   the wire matches the ``view --json`` shape, and the bearer token is
   forwarded.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import jsonschema

from evalguard_cli.local.local_executor import execute
from evalguard_cli.local.sqlite_store import SqliteStore
from evalguard_cli.local.yaml_loader import load_config


_RUN_SCHEMA_PATH = Path(__file__).resolve().parents[1] / "packages" / "schemas" / "evalguard.run.schema.json"


def _seed_run(base: Path) -> tuple[Path, str]:
    """Run a tiny pipeline so there's at least one row in ``local.db``."""
    (base / "datasets").mkdir()
    (base / "datasets" / "g.jsonl").write_text('{"id":"r1","input":"hi"}\n')
    cfg_path = base / "evalguard.yaml"
    cfg_path.write_text(
        "version: 1\nproject: t\n"
        "providers: [{ id: 'mock:m', config: { mode: echo, latency_ms: 0 } }]\n"
        "datasets: [{ id: g, file: datasets/g.jsonl }]\n"
    )
    cfg = load_config(cfg_path)
    db = base / ".evalguard" / "local.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    store = SqliteStore(db)
    store.init_schema()
    record = asyncio.run(execute(cfg, store=store, quiet=True))
    return db, record.run_id


def _push(*args: str, env: dict[str, str] | None = None, cwd: Path | None = None) -> subprocess.CompletedProcess:
    """Run ``python -m evalguard_cli.main push ...`` with isolated env."""
    base_env = {
        # Strip CI-detection vars so the actor resolution doesn't trip
        # up the cli runner inside CI; tests own ``EVALGUARD_SERVER``.
        k: v for k, v in os.environ.items()
        if k not in {"EVALGUARD_SERVER", "EVALGUARD_API_TOKEN"}
    }
    if env:
        base_env.update(env)
    return subprocess.run(
        [sys.executable, "-m", "evalguard_cli.main", "push", *args],
        capture_output=True, text=True, timeout=15,
        env=base_env, cwd=str(cwd) if cwd else None,
    )


def test_push_without_server_exits_0_with_hint(tmp_path: Path):
    """No ``EVALGUARD_SERVER`` → no-op fallback path. Build never blocks."""
    result = _push("--last", cwd=tmp_path)
    assert result.returncode == 0, result.stderr
    assert "no server configured" in (result.stdout + result.stderr).lower()


def test_push_dry_run_emits_run_schema(tmp_path: Path):
    """``--dry-run`` prints the canonical run JSON. Schema-validate it
    so a payload that drifts from the contract fails this test."""
    _seed_run(tmp_path)
    result = _push("--last", "--dry-run", cwd=tmp_path)
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    schema = json.loads(_RUN_SCHEMA_PATH.read_text())
    jsonschema.validate(payload, schema)
    assert payload["project"] == "t"
    assert payload["trials"]


def test_push_to_unreachable_server_fails(tmp_path: Path):
    """A real attempt against a closed port surfaces a non-zero exit."""
    _seed_run(tmp_path)
    # Pick an arbitrary closed port — bind+release to claim then drop.
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    closed_port = s.getsockname()[1]
    s.close()
    server = f"http://127.0.0.1:{closed_port}"
    result = _push("--last", "--server", server, cwd=tmp_path)
    assert result.returncode == 1, result.stdout
    assert "push failed" in (result.stdout + result.stderr).lower()


def test_push_to_stub_server_forwards_payload_and_token(tmp_path: Path):
    """Spin a stdlib HTTP server, push to it, and assert the request
    body matches the run schema and the bearer token is forwarded."""
    _seed_run(tmp_path)

    received: dict[str, object] = {}

    class _Handler(BaseHTTPRequestHandler):
        def log_message(self, *_a, **_kw): pass

        def do_POST(self):
            length = int(self.headers.get("content-length", "0"))
            body = self.rfile.read(length)
            received["path"] = self.path
            received["auth"] = self.headers.get("authorization")
            received["body"] = json.loads(body.decode("utf-8"))
            self.send_response(201)
            self.send_header("content-type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"ok": true}')

    httpd = HTTPServer(("127.0.0.1", 0), _Handler)
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        result = _push(
            "--last",
            "--server", f"http://127.0.0.1:{port}",
            "--token", "tok-secret",
            cwd=tmp_path,
        )
    finally:
        httpd.shutdown()
        t.join(timeout=2)

    assert result.returncode == 0, result.stderr or result.stdout
    assert received["path"] == "/v1/runs"
    assert received["auth"] == "Bearer tok-secret"
    body = received["body"]
    assert isinstance(body, dict)
    schema = json.loads(_RUN_SCHEMA_PATH.read_text())
    jsonschema.validate(body, schema)
    assert body["project"] == "t"


# ---------------------------------------------------------------------------
# B.3 round-3 regressions


def test_push_sends_idempotency_and_schema_version_headers(tmp_path: Path):
    """Re-pushing the same run must dedupe server-side. The CLI sends
    ``Idempotency-Key: <run_id>`` AND ``X-EvalGuard-Schema-Version`` so
    the server can 409 on either drift."""
    _seed_run(tmp_path)
    received: dict[str, object] = {}

    class _Handler(BaseHTTPRequestHandler):
        def log_message(self, *_a, **_kw): pass

        def do_POST(self):
            length = int(self.headers.get("content-length", "0"))
            self.rfile.read(length)
            # Headers are lower-cased by stdlib's BaseHTTPRequestHandler.
            received["idempotency"] = self.headers.get("idempotency-key")
            received["schema_version"] = self.headers.get("x-evalguard-schema-version")
            self.send_response(201)
            self.send_header("content-type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"ok": true}')

    httpd = HTTPServer(("127.0.0.1", 0), _Handler)
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        result = _push(
            "--last",
            "--server", f"http://127.0.0.1:{port}",
            cwd=tmp_path,
        )
    finally:
        httpd.shutdown()
        t.join(timeout=2)

    assert result.returncode == 0, result.stderr or result.stdout
    assert received.get("idempotency", "").startswith("run_")
    # Schema version must be a semver-ish string (1.0.0, 1.1.0, ...)
    sv = received.get("schema_version") or ""
    assert sv and sv.count(".") == 2


def test_push_rejects_ambiguous_prefix(tmp_path: Path):
    """A 4-char prefix that matches multiple runs in the local store
    must error rather than silently picking one. Previous behaviour
    was a silent ``next(...)`` foot-gun."""
    # Seed two distinct runs so the prefix matches both.
    db, run_a = _seed_run(tmp_path)
    # Re-run the executor a second time so the DB has 2+ runs.
    cfg = load_config(tmp_path / "evalguard.yaml")
    store = SqliteStore(db)
    store.init_schema()
    record_b = asyncio.run(execute(cfg, store=store, quiet=True))
    run_b = record_b.run_id
    # Find a common prefix between the two run_ids; if none exists
    # (different first hex char by random chance), assert with an
    # explicit short prefix that matches both.
    common = "run_"   # all evalguard run_ids start with this
    assert run_a.startswith(common) and run_b.startswith(common)

    result = _push(
        common,
        "--server", "http://127.0.0.1:9",   # unreachable, never used
        cwd=tmp_path,
    )
    assert result.returncode == 1
    out = (result.stdout or "") + (result.stderr or "")
    assert "ambiguous" in out.lower()


def test_push_retries_on_transient_5xx(tmp_path: Path):
    """503 from the LB must not abort the push immediately — the CLI
    rides out a single transient blip with a small retry budget."""
    _seed_run(tmp_path)
    state = {"calls": 0}

    class _Handler(BaseHTTPRequestHandler):
        def log_message(self, *_a, **_kw): pass

        def do_POST(self):
            length = int(self.headers.get("content-length", "0"))
            self.rfile.read(length)
            state["calls"] += 1
            if state["calls"] < 2:
                self.send_response(503)
                self.send_header("content-type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"error": "service unavailable"}')
            else:
                self.send_response(201)
                self.send_header("content-type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"ok": true}')

    httpd = HTTPServer(("127.0.0.1", 0), _Handler)
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        # Override the base delay so the test runs fast (push module
        # reads it as a module-level constant; we monkeypatch via env
        # is overkill — just accept a small real wait).
        result = _push(
            "--last",
            "--server", f"http://127.0.0.1:{port}",
            cwd=tmp_path,
        )
    finally:
        httpd.shutdown()
        t.join(timeout=2)

    assert result.returncode == 0, result.stderr or result.stdout
    assert state["calls"] >= 2, "expected the CLI to retry the 503"
