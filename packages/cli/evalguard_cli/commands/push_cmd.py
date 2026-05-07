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
import time
import urllib.error
import urllib.request
from pathlib import Path

import typer

from evalguard_cli.console import console
from evalguard_cli.local.serializer import SCHEMA_VERSION, run_to_dict
from evalguard_cli.local.sqlite_store import SqliteStore


_DEFAULT_DB = Path(".evalguard/local.db")
ENV_SERVER = "EVALGUARD_SERVER"
ENV_TOKEN = "EVALGUARD_API_TOKEN"
PUSH_PATH = "/v1/runs"

# Tiny retry budget for transient HTTP errors (502/503/504/timeout).
# This is intentionally small — push lives on the *CI happy path*,
# not the LLM eval hot path, so we want to ride out a single LB blip
# but not paper over a real outage.
_PUSH_RETRY_STATUS = (408, 425, 500, 502, 503, 504)
_PUSH_MAX_RETRIES = 3
_PUSH_BASE_DELAY_S = 1.0


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

    # Disambiguate prefix matches up front. Silently picking the
    # first match was a foot-gun: with thousands of runs in the
    # store, a 4-character prefix can hit multiple runs and operator
    # would never see the wrong one go up.
    matches = [r for r in runs if r["run_id"].startswith(run_id)]
    if not matches:
        console.print(f"[red]no run found matching {run_id}[/red]")
        raise typer.Exit(1)
    if len(matches) > 1:
        console.print(
            f"[red]ambiguous run prefix[/red] [cyan]{run_id}[/cyan] — "
            f"{len(matches)} runs match. Be more specific:"
        )
        for r in matches[:10]:
            console.print(f"  [dim]{r['run_id']}[/dim]  ({r.get('started_at', '')})")
        if len(matches) > 10:
            console.print(f"  [dim]…and {len(matches) - 10} more[/dim]")
        raise typer.Exit(1)
    full = matches[0]

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

    def _build_request() -> urllib.request.Request:
        # Build a fresh ``Request`` per attempt — urllib mutates the
        # request object during open (e.g. setting ``host``) so reuse
        # across retries is unsafe.
        req = urllib.request.Request(
            url, data=body, method="POST",
            headers={
                "content-type": "application/json",
                # Idempotency-Key lets the server dedupe a re-push
                # of the same run (CI re-run, network blip retry).
                # Servers that don't recognize the header silently
                # ignore it, so this is forward-compatible.
                "idempotency-key": full["run_id"],
                # Schema-version handshake so the server can 409 on
                # mismatch with a helpful message instead of silently
                # accepting and misinterpreting fields.
                "x-evalguard-schema-version": SCHEMA_VERSION,
            },
        )
        if token:
            req.add_header("authorization", f"Bearer {token}")
        return req

    last_err: Exception | None = None
    for attempt in range(_PUSH_MAX_RETRIES + 1):
        try:
            with urllib.request.urlopen(_build_request(), timeout=30) as resp:  # noqa: S310
                status = resp.status
                response_body = resp.read().decode("utf-8", errors="replace")
            break   # success
        except urllib.error.HTTPError as e:
            detail = (e.read().decode("utf-8", errors="replace")[:300]
                       if e.fp else "")
            # 5xx / 408 / 425 are retryable; 4xx (auth, validation,
            # idempotency conflict) is not — fail fast.
            if e.code in _PUSH_RETRY_STATUS and attempt < _PUSH_MAX_RETRIES:
                last_err = e
                delay = _PUSH_BASE_DELAY_S * (2 ** attempt)
                console.print(
                    f"[yellow]push attempt {attempt + 1} failed[/yellow] "
                    f"({e.code} {e.reason}); retrying in {delay:.0f}s"
                )
                time.sleep(delay)
                continue
            console.print(
                f"[red]push failed[/red] · {e.code} {e.reason}\n"
                f"  url: {url}\n"
                f"  body: {detail}"
            )
            raise typer.Exit(1) from e
        except urllib.error.URLError as e:
            # Connection refused / DNS / TLS errors are usually
            # transient at the LB level — retry within budget.
            if attempt < _PUSH_MAX_RETRIES:
                last_err = e
                delay = _PUSH_BASE_DELAY_S * (2 ** attempt)
                console.print(
                    f"[yellow]push attempt {attempt + 1} failed[/yellow] "
                    f"({e.reason}); retrying in {delay:.0f}s"
                )
                time.sleep(delay)
                continue
            console.print(f"[red]push failed:[/red] {e.reason}\n  url: {url}")
            raise typer.Exit(1) from e
    else:
        # Exhausted retries — typer.Exit raised inside the loop, but
        # belt-and-braces.
        raise typer.Exit(1) from last_err

    console.print(
        f"[green]pushed[/green] {full['run_id']} → {url} "
        f"[dim]({status})[/dim]"
    )
    if response_body.strip():
        console.print(f"[dim]{response_body[:300]}[/dim]")
