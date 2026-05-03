"""``evalguard push`` — upload a local run to a remote EvalGuard server.

The Phase-2 server (FastAPI / Postgres / multi-tenant) is on the
roadmap; this command exists today so:

1. Operators planning a self-host deployment can validate connectivity
   and payload shape against a stub or proxy before the server lands.
2. CI workflows can adopt ``evalguard push`` now and have it become a
   real upload once ``EVALGUARD_SERVER`` points at a live API — no CI
   YAML changes required.

Behaviour:

- No ``EVALGUARD_SERVER`` env (and no ``--server``) → print a hint and
  exit 0. The CLI never blocks a build for a missing optional server.
- Server configured, ``--dry-run`` → print the JSON payload and exit 0.
- Server configured, real run → POST the run payload (the same shape
  ``view --json`` exposes) to ``{server}/v1/runs`` and surface the
  response status. Network / HTTP errors exit 1.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path

import typer

from evalguard_cli.console import console
from evalguard_cli.local.serializer import run_to_dict
from evalguard_cli.local.sqlite_store import SqliteStore


_DEFAULT_DB = Path(".evalguard/local.db")
ENV_SERVER = "EVALGUARD_SERVER"
ENV_TOKEN = "EVALGUARD_API_TOKEN"
PUSH_PATH = "/v1/runs"


def push(
    run_id: str | None = typer.Argument(None, help="Run ID (or prefix); omit with --last."),
    last: bool = typer.Option(False, "--last", help="Push the most recent run."),
    server: str | None = typer.Option(
        None, "--server",
        help=f"EvalGuard server base URL (default: ${ENV_SERVER}).",
    ),
    token: str | None = typer.Option(
        None, "--token",
        help=f"API token (default: ${ENV_TOKEN}).",
    ),
    include_events: bool = typer.Option(
        False, "--events",
        help="Include the audit timeline in the payload (heavy).",
    ),
    include_scores: bool = typer.Option(
        False, "--scores",
        help="Include per-row scores in the payload (heavy).",
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run",
        help="Print the payload to stdout instead of POSTing.",
    ),
    db_path: Path = typer.Option(_DEFAULT_DB, "--db", help="SQLite path."),
) -> None:
    """Push a local run to a remote EvalGuard server."""
    server = server or os.environ.get(ENV_SERVER)
    if not server and not dry_run:
        console.print(
            f"[yellow]no server configured.[/yellow] set "
            f"[cyan]${ENV_SERVER}[/cyan] (or pass [cyan]--server[/cyan]) to "
            f"upload runs to an EvalGuard server.\n"
            f"[dim]The Phase-2 server is on the roadmap; until then this "
            f"command no-ops with a hint so CI wiring can land early.[/dim]"
        )
        raise typer.Exit(0)

    if not db_path.exists():
        console.print(f"[red]no run history at {db_path}[/red]")
        raise typer.Exit(1)
    store = SqliteStore(db_path)

    runs = store.list_runs(limit=1000)
    if not runs:
        console.print("[red]no runs in store[/red]")
        raise typer.Exit(1)
    if last:
        run_id = runs[0]["run_id"]
    if run_id is None:
        console.print("[red]pass a run_id or --last[/red]")
        raise typer.Exit(1)

    full = next((r for r in runs if r["run_id"].startswith(run_id)), None)
    if full is None:
        console.print(f"[red]no run found matching {run_id}[/red]")
        raise typer.Exit(1)

    payload = run_to_dict(
        store, full["run_id"],
        include_rows=True,
        include_scores=include_scores,
        include_events=include_events,
    )

    if dry_run:
        print(json.dumps(payload, default=str, indent=2))
        raise typer.Exit(0)

    url = server.rstrip("/") + PUSH_PATH
    token = token or os.environ.get(ENV_TOKEN)
    body = json.dumps(payload, default=str).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={"content-type": "application/json"},
    )
    if token:
        req.add_header("authorization", f"Bearer {token}")

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 — explicit user-supplied URL
            status = resp.status
            response_body = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")[:300] if e.fp else ""
        console.print(
            f"[red]push failed[/red] · {e.code} {e.reason}\n"
            f"  url: {url}\n"
            f"  body: {detail}"
        )
        raise typer.Exit(1) from e
    except urllib.error.URLError as e:
        console.print(f"[red]push failed:[/red] {e.reason}\n  url: {url}")
        raise typer.Exit(1) from e

    console.print(
        f"[green]pushed[/green] {full['run_id']} → {url} "
        f"[dim]({status})[/dim]"
    )
    if response_body.strip():
        console.print(f"[dim]{response_body[:300]}[/dim]")
