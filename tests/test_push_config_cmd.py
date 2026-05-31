"""Tests for ``evalguard push-config``.

PROXY-1.  The server stores the raw YAML bytes verbatim; the CLI's
job is to read the file, extract the project slug, and POST.  Tests
mirror ``test_push.py`` — same stdlib HTTPServer stub pattern.
"""

from __future__ import annotations

import hashlib
import json
import os
import socket
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path


_CONFIG_YAML = (
    "version: 1\n"
    "project: customer-service\n"
    "providers: [{ id: 'openai:gpt-4o-mini' }]\n"
    "datasets: [{ id: g, file: datasets/g.jsonl }]\n"
)


def _push_config(*args: str, env: dict[str, str] | None = None, cwd: Path | None = None) -> subprocess.CompletedProcess:
    base_env = {
        k: v for k, v in os.environ.items()
        if k not in {"EVALGUARD_SERVER", "EVALGUARD_API_TOKEN"}
    }
    if env:
        base_env.update(env)
    return subprocess.run(
        [sys.executable, "-m", "evalguard_cli.main", "push-config", *args],
        capture_output=True, text=True, timeout=15,
        env=base_env, cwd=str(cwd) if cwd else None,
    )


def _write_config(tmp: Path, content: str = _CONFIG_YAML) -> Path:
    path = tmp / "evalguard.yaml"
    path.write_text(content)
    return path


# ---------------------------------------------------------------------------
# No-server fallback


def test_push_config_without_server_exits_0_with_hint(tmp_path: Path):
    """No ``EVALGUARD_SERVER`` → no-op fallback, same as ``push``."""
    _write_config(tmp_path)
    result = _push_config(cwd=tmp_path)
    assert result.returncode == 0, result.stderr
    assert "no server configured" in (result.stdout + result.stderr).lower()


# ---------------------------------------------------------------------------
# Dry-run


def test_push_config_dry_run_prints_sha_and_slug(tmp_path: Path):
    _write_config(tmp_path)
    result = _push_config("--dry-run", cwd=tmp_path)
    assert result.returncode == 0, result.stderr
    expected_sha = hashlib.sha256(_CONFIG_YAML.encode()).hexdigest()
    out = result.stdout + result.stderr
    assert expected_sha in out
    assert "customer-service" in out


def test_push_config_missing_file_exits_1(tmp_path: Path):
    result = _push_config("--dry-run", cwd=tmp_path)
    assert result.returncode == 1, result.stdout
    assert "config not found" in (result.stdout + result.stderr).lower()


def test_push_config_missing_project_slug_exits_1(tmp_path: Path):
    """A YAML without ``project:`` can't infer the upload target."""
    (tmp_path / "evalguard.yaml").write_text("version: 1\nproviders: []\n")
    result = _push_config("--dry-run", cwd=tmp_path)
    assert result.returncode == 1, result.stdout
    assert "no project slug" in (result.stdout + result.stderr).lower()


def test_push_config_explicit_project_override(tmp_path: Path):
    """--project beats the YAML's project: field."""
    _write_config(tmp_path)
    result = _push_config(
        "--project", "explicit-override", "--dry-run", cwd=tmp_path,
    )
    assert result.returncode == 0
    assert "explicit-override" in (result.stdout + result.stderr)


# ---------------------------------------------------------------------------
# Real upload against a stdlib stub


def test_push_config_uploads_to_stub_with_bearer(tmp_path: Path):
    """Spin a stdlib HTTP server, push to it, assert URL + bearer +
    body shape match."""
    _write_config(tmp_path)
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
            self.wfile.write(
                b'{"id": 7, "project_id": "p_x", "content_sha256": "abc",'
                b' "content": "...", "pushed_by": "k_x", "pushed_at": "now"}'
            )

    httpd = HTTPServer(("127.0.0.1", 0), _Handler)
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        result = _push_config(
            "--server", f"http://127.0.0.1:{port}",
            "--token", "tok-secret",
            cwd=tmp_path,
        )
    finally:
        httpd.shutdown()
        t.join(timeout=2)

    assert result.returncode == 0, result.stderr or result.stdout
    assert received["path"] == "/v1/projects/customer-service/config"
    assert received["auth"] == "Bearer tok-secret"
    body = received["body"]
    assert isinstance(body, dict)
    # Server stores bytes verbatim — the CLI must not re-serialize or
    # canonicalize YAML on the way out.
    assert body["content"] == _CONFIG_YAML


def test_push_config_to_unreachable_server_fails(tmp_path: Path):
    _write_config(tmp_path)
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    closed_port = s.getsockname()[1]
    s.close()
    result = _push_config(
        "--server", f"http://127.0.0.1:{closed_port}",
        cwd=tmp_path,
    )
    assert result.returncode == 1, result.stdout
    assert "push-config failed" in (result.stdout + result.stderr).lower()


def test_push_config_200_response_is_treated_as_existing_revision(tmp_path: Path):
    """The server returns 200 when re-pushing identical bytes.  The
    CLI must succeed (exit 0) and surface ``matched existing
    revision`` to distinguish from a fresh upload."""
    _write_config(tmp_path)

    class _Handler(BaseHTTPRequestHandler):
        def log_message(self, *_a, **_kw): pass

        def do_POST(self):
            length = int(self.headers.get("content-length", "0"))
            self.rfile.read(length)
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.end_headers()
            self.wfile.write(
                b'{"id": 7, "project_id": "p_x", "content_sha256": "abc",'
                b' "content": "...", "pushed_by": "k_x", "pushed_at": "now"}'
            )

    httpd = HTTPServer(("127.0.0.1", 0), _Handler)
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        result = _push_config(
            "--server", f"http://127.0.0.1:{port}",
            cwd=tmp_path,
        )
    finally:
        httpd.shutdown()
        t.join(timeout=2)

    assert result.returncode == 0, result.stderr
    assert "matched existing revision" in (result.stdout + result.stderr).lower()
