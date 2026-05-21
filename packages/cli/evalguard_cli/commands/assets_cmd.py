"""``evalguard assets`` — query the server's cross-run asset surface.

Wraps the Phase 2.6 endpoints:

    evalguard assets versions <kind> <asset_id> --project-id <pid>

Hits ``GET /v1/assets/{kind}/{asset_id}/versions`` and renders the
``(version_id, run_id, ingested_at, source)`` records as a Rich
table.  Use ``--json`` to emit the raw response for piping.

Server connection comes from the same env-var contract that
``evalguard push`` uses:

- ``EVALGUARD_SERVER`` — base URL (https://eval.your-corp.com)
- ``EVALGUARD_API_TOKEN`` — bearer

Both can be overridden per-call via ``--server`` / ``--token``.

Why stdlib ``urllib.request`` (not requests / httpx)?  The CLI
ships without external HTTP deps so air-gapped installs and pip-
restricted environments work out-of-box.  Same choice as
``push_cmd.py``.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request

import typer
from rich.console import Console
from rich.table import Table

from evalguard_cli.console import console


# Errors and progress notes go to STDERR so ``--json`` mode keeps
# stdout machine-readable for a downstream ``jq``.  The shared
# ``console`` (stdout) stays the channel for tables and ``--json``
# output.  Without this, any error rendered via ``console.print``
# would corrupt a piped JSON payload.
_stderr = Console(stderr=True)


assets_app = typer.Typer(help="Query the server's asset surface.")


ENV_SERVER = "EVALGUARD_SERVER"
ENV_TOKEN  = "EVALGUARD_API_TOKEN"
# Same whitelist the server uses.  Caught client-side too so an
# obvious typo gets a friendly local error instead of a 400 round-
# trip.
_KNOWN_KINDS: frozenset[str] = frozenset({
    "prompt", "dataset", "schema", "rubric",
    "judge", "heuristic", "metric",
})


@assets_app.command("versions")
def versions(
    kind:       str = typer.Argument(..., help="Asset kind: prompt / dataset / judge / heuristic / metric / schema / rubric."),
    asset_id:   str = typer.Argument(..., help="The asset's ``asset_id`` (NOT the version_id)."),
    project_id: str = typer.Option(..., "--project-id", help="The project the asset belongs to."),
    limit:      int = typer.Option(200, "--limit", min=1, max=1000),
    server:     str | None = typer.Option(None, "--server",
                                          help=f"Server URL (default: ${ENV_SERVER})."),
    token:      str | None = typer.Option(None, "--token",
                                          help=f"Bearer token (default: ${ENV_TOKEN})."),
    as_json:    bool = typer.Option(False, "--json",
                                    help="Emit the raw JSON response to stdout."),
) -> None:
    """Every ``(version_id, run_id, ingested_at, source)`` tuple for
    one asset inside one project, newest-first."""
    if kind not in _KNOWN_KINDS:
        _stderr.print(
            f"[red]Unknown kind[/red] [cyan]{kind}[/cyan].  "
            f"Allowed: {', '.join(sorted(_KNOWN_KINDS))}"
        )
        raise typer.Exit(2)

    server = server or os.environ.get(ENV_SERVER)
    if not server:
        _stderr.print(
            f"[red]No server configured.[/red]  Set [cyan]${ENV_SERVER}[/cyan] "
            f"or pass [cyan]--server[/cyan]."
        )
        raise typer.Exit(2)

    # ``token or os.environ.get(...)`` would silently fall back when
    # the env var is set to the empty string (``EVALGUARD_API_TOKEN=``
    # in a Docker env block is a common foot-gun), and the server's
    # subsequent 401 doesn't tell the operator the cause.  Detect
    # the explicit-empty case here.
    if token is None:
        env_token = os.environ.get(ENV_TOKEN)
        if env_token is None:
            token = None
        elif env_token == "":
            _stderr.print(
                f"[red]${ENV_TOKEN} is set to an empty string.[/red]  "
                f"Either unset it or provide a real token."
            )
            raise typer.Exit(2)
        else:
            token = env_token

    qs = urllib.parse.urlencode({"project_id": project_id, "limit": limit})
    path = (
        f"/v1/assets/{urllib.parse.quote(kind, safe='')}"
        f"/{urllib.parse.quote(asset_id, safe='')}"
        f"/versions?{qs}"
    )
    url = server.rstrip("/") + path

    req = urllib.request.Request(url, method="GET",
                                  headers={"accept": "application/json"})
    if token:
        req.add_header("authorization", f"Bearer {token}")

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 — explicit user-supplied URL
            raw_body = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        # Map server error shapes to specific exits so a CI workflow
        # can act on the result without parsing stderr.
        detail = _read_error_detail(e)
        if e.code in (401, 403):
            _stderr.print(f"[red]Authentication failed[/red] ({e.code}) — {detail}")
            raise typer.Exit(2) from e
        if e.code == 404:
            _stderr.print(f"[red]Not found[/red] — {detail}")
            raise typer.Exit(1) from e
        _stderr.print(f"[red]Request failed[/red] ({e.code}) — {detail}")
        raise typer.Exit(1) from e
    except urllib.error.URLError as e:
        _stderr.print(f"[red]Could not reach server[/red] {server}: {e.reason}")
        raise typer.Exit(1) from e

    # A misconfigured reverse-proxy can return a 200 with an HTML
    # error page or an empty body.  Without this guard the JSON parse
    # raises and the user sees a Python traceback instead of a
    # readable error.
    try:
        body = json.loads(raw_body)
    except (TypeError, ValueError) as e:
        snippet = raw_body[:200].replace("\n", " ").strip()
        _stderr.print(
            f"[red]Server returned non-JSON 200 response.[/red]  "
            f"First 200 bytes: {snippet!r}"
        )
        raise typer.Exit(1) from e

    if as_json:
        # ``json.dumps`` (not ``print``) so the operator can pipe to
        # jq / a file without Rich's ANSI escapes contaminating the
        # output.  ``sort_keys=True`` for diffable output.
        typer.echo(json.dumps(body, indent=2, sort_keys=True))
        return

    _render_table(body)


def _read_error_detail(e: urllib.error.HTTPError) -> str:
    """Pull ``{"detail": "..."}`` out of the response body; fall
    back to the status reason if the body isn't JSON.  Short-circuit
    on the FastAPI ``detail`` shape that every error route in the
    server uses (anti-enumeration 404s, 400 / 422 validation, etc.)."""
    try:
        raw = e.read().decode("utf-8", errors="replace")
    except Exception:
        return e.reason or str(e.code)
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return raw[:200] or e.reason or str(e.code)
    detail = data.get("detail")
    if isinstance(detail, str):
        return detail
    if isinstance(detail, list) and detail:
        # FastAPI validation 422 — a list of error dicts.  Compose
        # a one-line summary; an operator who needs the full thing
        # can re-run with --json.
        return "; ".join(
            f"{'.'.join(map(str, d.get('loc', [])))}: {d.get('msg', '')}"
            for d in detail
            if isinstance(d, dict)
        ) or raw[:200]
    return raw[:200] or e.reason or str(e.code)


def _render_table(body: dict) -> None:
    """Rich-format the AssetVersionsResponse.

    Header:   kind · asset_id · project · count
    Body:     version_id (truncated) · run_id (truncated) · ingested · source
    """
    versions = body.get("versions") or []
    kind         = body.get("kind", "—")
    asset_id     = body.get("asset_id", "—")
    project_name = body.get("project_name", "—")
    project_id   = body.get("project_id", "—")

    console.print(
        f"[bold]{asset_id}[/bold]  "
        f"[dim]{kind}[/dim]  "
        f"in [cyan]{project_name}[/cyan] [dim]({project_id})[/dim]"
    )
    if not versions:
        # The server 404s when there are zero versions, so reaching
        # this branch means a deliberate "empty but defined" response —
        # surface that rather than render an empty table.
        console.print("[dim]No version records.[/dim]")
        return

    table = Table(show_header=True, header_style="bold")
    table.add_column("Version",  overflow="fold")
    table.add_column("Run",      overflow="fold")
    table.add_column("Ingested", overflow="fold")
    table.add_column("Source")

    for v in versions:
        table.add_row(
            _truncate(str(v.get("version_id", "")), 24),
            _truncate(str(v.get("run_id", "")),     20),
            str(v.get("ingested_at", "")),
            str(v.get("source", "")),
        )
    console.print(table)
    console.print(f"[dim]{len(versions)} record{'s' if len(versions) != 1 else ''}[/dim]")


def _truncate(s: str, n: int) -> str:
    if len(s) <= n:
        return s
    # U+2026 ellipsis — same convention as the UI's AssetVersionsTable
    # so the two surfaces line up visually.
    return s[:n] + "…"
