"""``evalguard golden`` — bridge the server-side golden-candidates
staging table to a local JSONL dataset the operator can re-use as
ground truth.

Subcommands:

    evalguard golden list   --project demo
    evalguard golden export --project demo --to ./golden.jsonl

The full workflow:

1. Reviewer clicks "Promote to golden" on a row in the UI; a
   ``golden_candidates`` row lands on the server.
2. Operator runs ``evalguard golden export --project demo --to
   datasets/golden.jsonl`` to materialise the staged candidates as
   a JSONL file (one row per line, ``{id, input, expected,
   _provenance: {run_id, promoted_by, note, created_at}}``).
3. The new JSONL feeds back into ``evalguard.yaml`` as a dataset
   on the next eval run.

Server connection comes from the same env-var contract that
``evalguard push`` and ``evalguard assets`` use:

- ``EVALGUARD_SERVER`` — base URL.
- ``EVALGUARD_API_TOKEN`` — bearer token.

Both can be overridden per-call via ``--server`` / ``--token``.

Why stdlib ``urllib.request`` (no httpx)?  Same reason as
``push_cmd`` / ``assets_cmd``: the CLI ships dep-free so air-gapped
installs work out of the box.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.table import Table

from evalguard_cli.console import console


golden_app = typer.Typer(help="Manage server-side golden-candidate promotions.")


ENV_SERVER = "EVALGUARD_SERVER"
ENV_TOKEN  = "EVALGUARD_API_TOKEN"

# Errors and progress notes go to STDERR so ``--json`` mode keeps
# stdout machine-readable for a downstream ``jq``.  Same pattern as
# ``assets_cmd``.
_stderr = Console(stderr=True)


# ---------------------------------------------------------------------------
# Server-resolution helpers (mirrors ``assets_cmd``)


def _resolve_server(server: str | None) -> str:
    server = server or os.environ.get(ENV_SERVER)
    if not server:
        _stderr.print(
            f"[red]No server configured.[/red]  Set [cyan]${ENV_SERVER}[/cyan] "
            f"or pass [cyan]--server[/cyan]."
        )
        raise typer.Exit(2)
    return server.rstrip("/")


def _resolve_token(token: str | None) -> str | None:
    """Return the resolved bearer token, or None if unset.  The
    explicit-empty-string case (``EVALGUARD_API_TOKEN=`` in a Docker
    env block) is caught here so the operator sees a clear local
    error instead of a server 401."""
    if token is not None:
        return token
    env_token = os.environ.get(ENV_TOKEN)
    if env_token is None:
        return None
    if env_token == "":
        _stderr.print(
            f"[red]${ENV_TOKEN} is set to an empty string.[/red]  "
            f"Either unset it or provide a real token."
        )
        raise typer.Exit(2)
    return env_token


def _get(url: str, token: str | None) -> dict:
    """GET ``url`` and decode JSON.  Maps HTTP error codes to typer
    exits using the same convention as ``assets_cmd``:

    - 0  success
    - 1  not-found / network / 5xx
    - 2  401 / 403 (auth) and no-server / empty-token (config bugs)
    """
    req = urllib.request.Request(
        url, method="GET",
        headers={"accept": "application/json"},
    )
    if token:
        req.add_header("authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310
            raw_body = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
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
        _stderr.print(f"[red]Could not reach server[/red]: {e.reason}")
        raise typer.Exit(1) from e
    try:
        return json.loads(raw_body)
    except (TypeError, ValueError) as e:
        snippet = raw_body[:200].replace("\n", " ").strip()
        _stderr.print(
            f"[red]Server returned non-JSON 200 response.[/red]  "
            f"First 200 bytes: {snippet!r}"
        )
        raise typer.Exit(1) from e


def _read_error_detail(e: urllib.error.HTTPError) -> str:
    """Same FastAPI-detail extraction as ``assets_cmd._read_error_detail``."""
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
        return "; ".join(
            f"{'.'.join(map(str, d.get('loc', [])))}: {d.get('msg', '')}"
            for d in detail
            if isinstance(d, dict)
        ) or raw[:200]
    return raw[:200] or e.reason or str(e.code)


# ---------------------------------------------------------------------------
# Subcommand: list


@golden_app.command("list")
def list_candidates(
    project: str = typer.Option(..., "--project",
                                help="Project slug to list candidates for."),
    limit:   int = typer.Option(100, "--limit", min=1, max=500),
    server:  str | None = typer.Option(None, "--server",
                                       help=f"Server URL (default: ${ENV_SERVER})."),
    token:   str | None = typer.Option(None, "--token",
                                       help=f"Bearer token (default: ${ENV_TOKEN})."),
    as_json: bool = typer.Option(False, "--json",
                                 help="Emit the raw JSON response to stdout."),
) -> None:
    """List the server-side golden candidates for one project,
    newest-first."""
    server_url = _resolve_server(server)
    token_v    = _resolve_token(token)
    qs = urllib.parse.urlencode({"limit": limit})
    url = (
        f"{server_url}/v1/projects/{urllib.parse.quote(project, safe='')}"
        f"/golden/candidates?{qs}"
    )
    body = _get(url, token_v)

    if as_json:
        typer.echo(json.dumps(body, indent=2, sort_keys=True))
        return

    candidates = body.get("candidates") or []
    console.print(
        f"[bold]golden candidates[/bold]  "
        f"in [cyan]{project}[/cyan]  "
        f"[dim]({len(candidates)} of ≤{limit})[/dim]"
    )
    if not candidates:
        console.print("[dim]No candidates yet.[/dim]")
        return

    table = Table(show_header=True, header_style="bold")
    table.add_column("Run",       overflow="fold")
    table.add_column("Row",       overflow="fold")
    table.add_column("Promoted by")
    table.add_column("Note",      overflow="fold")
    table.add_column("At")
    for c in candidates:
        table.add_row(
            _truncate(str(c.get("run_id", "")), 20),
            _truncate(str(c.get("row_id", "")), 24),
            _truncate(str(c.get("promoted_by", "")), 18),
            _truncate(str(c.get("note") or ""), 50),
            str(c.get("created_at", "")),
        )
    console.print(table)


# ---------------------------------------------------------------------------
# Subcommand: export


@golden_app.command("export")
def export(
    project: str = typer.Option(..., "--project",
                                help="Project slug whose candidates to export."),
    to:      Path = typer.Option(..., "--to",
        help="Destination JSONL path.  Use ``--mode merge`` to append "
             "while skipping rows already present."),
    mode:    str = typer.Option("overwrite", "--mode",
        help="``overwrite`` (truncate target) or ``merge`` (append, "
             "skip rows whose ``id`` already exists in the target)."),
    limit:   int = typer.Option(500, "--limit", min=1, max=500,
        help="Server-side cap on candidates fetched in one call."),
    server:  str | None = typer.Option(None, "--server"),
    token:   str | None = typer.Option(None, "--token"),
) -> None:
    """Materialise the server's staged candidates into a local
    JSONL file ready to consume as a dataset in ``evalguard.yaml``.

    Output shape — one row per line:

      ``{"id": "<row_id>", "input": <input>, "expected": <expected>,
         "_provenance": {"run_id": ..., "promoted_by": ...,
                         "note": ..., "created_at": ...}}``

    Rows that lack ``input`` on the server side are skipped (a
    candidate promoted from an OTLP-derived row may not carry one);
    a stderr warning lists how many were dropped so the operator
    can investigate.
    """
    if mode not in {"overwrite", "merge"}:
        _stderr.print(
            f"[red]Unknown mode {mode!r}.[/red]  Allowed: overwrite, merge."
        )
        raise typer.Exit(2)

    server_url = _resolve_server(server)
    token_v    = _resolve_token(token)

    # 1. List candidates.
    list_url = (
        f"{server_url}/v1/projects/{urllib.parse.quote(project, safe='')}"
        f"/golden/candidates?limit={limit}"
    )
    list_body = _get(list_url, token_v)
    candidates = list_body.get("candidates") or []
    if not candidates:
        _stderr.print(f"[yellow]No candidates for project[/yellow] [cyan]{project}[/cyan].")
        # ``overwrite`` with zero candidates would truncate the file
        # to zero rows, which is almost certainly NOT what the user
        # wanted.  Skip the write entirely and exit 0 — same outcome
        # as merge-with-no-new-rows.
        return

    # 2. For each candidate, fetch its row detail (N+1 fan-out).
    # Acceptable at ≤500 rows; if scale ever grows we can add a
    # server-side ``?expand=row`` switch on the list endpoint.
    existing_ids: set[str] = set()
    if mode == "merge" and to.exists():
        # Read the existing JSONL once into a set of ids so we can
        # skip duplicates without parsing the file per candidate.
        existing_ids = _read_existing_ids(to)

    out_lines: list[str] = []
    appended = skipped_dup = skipped_no_input = fetch_failures = 0
    for c in candidates:
        run_id, row_id = c.get("run_id"), c.get("row_id")
        if not run_id or not row_id:
            continue
        if row_id in existing_ids:
            skipped_dup += 1
            continue
        detail_url = (
            f"{server_url}/v1/projects/{urllib.parse.quote(project, safe='')}"
            f"/calls/{urllib.parse.quote(str(run_id), safe='')}"
            f"/{urllib.parse.quote(str(row_id), safe='')}"
        )
        try:
            row = _get(detail_url, token_v)
        except typer.Exit as e:
            # A per-row 404 / 5xx / network blip (exit 1) shouldn't
            # abort the whole export — count and continue so one
            # deleted run doesn't lose 499 good rows.  But an auth
            # failure (exit 2) means the token is broken for the
            # detail endpoint; every remaining fetch will fail the
            # same way, so abort loudly instead of silently masking
            # it as N "fetch-failures".
            if e.exit_code == 2:
                _stderr.print(
                    "[red]Aborting export[/red] — authentication failed on "
                    "the per-call detail fetch.  Check the token's scope."
                )
                raise
            fetch_failures += 1
            continue
        if row.get("input") is None:
            skipped_no_input += 1
            continue
        out_lines.append(_format_jsonl_row(row, c))
        appended += 1

    # 3. Write to disk.  ``overwrite`` truncates; ``merge`` appends.
    to.parent.mkdir(parents=True, exist_ok=True)
    if mode == "overwrite":
        to.write_text("\n".join(out_lines) + ("\n" if out_lines else ""))
    else:
        with to.open("a", encoding="utf-8") as f:
            for line in out_lines:
                f.write(line + "\n")

    # 4. Summary to stderr (so ``--json``-style consumers wouldn't
    # be confused if they pipe golden export elsewhere — not that
    # it accepts ``--json`` today).
    _stderr.print(
        f"[green]Exported[/green] [bold]{appended}[/bold] row"
        f"{'' if appended == 1 else 's'} to [cyan]{to}[/cyan] "
        f"(mode: {mode}).  "
        f"Skipped: [bold]{skipped_dup}[/bold] duplicate"
        f"{'' if skipped_dup == 1 else 's'}, "
        f"[bold]{skipped_no_input}[/bold] no-input row"
        f"{'' if skipped_no_input == 1 else 's'}, "
        f"[bold]{fetch_failures}[/bold] fetch-failure"
        f"{'' if fetch_failures == 1 else 's'}."
    )


# ---------------------------------------------------------------------------
# Helpers


def _format_jsonl_row(row: dict, candidate: dict) -> str:
    """Compose the JSONL line.  Keep keys stable so re-running
    ``export`` produces a diffable file (``sort_keys=True``)."""
    out: dict[str, Any] = {
        "id":       row.get("row_id"),
        "input":    row.get("input"),
        "expected": row.get("expected"),
        "_provenance": {
            "run_id":      candidate.get("run_id"),
            "promoted_by": candidate.get("promoted_by"),
            "note":        candidate.get("note"),
            "created_at":  candidate.get("created_at"),
        },
    }
    return json.dumps(out, sort_keys=True, ensure_ascii=False)


def _read_existing_ids(path: Path) -> set[str]:
    """Pull the ``id`` field from each line of an existing JSONL.
    Malformed lines are skipped silently — the goal is "what's
    already there?", not validation."""
    ids: set[str] = set()
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return ids
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except (TypeError, ValueError):
            continue
        if isinstance(d, dict) and "id" in d:
            ids.add(str(d["id"]))
    return ids


def _truncate(s: str, n: int) -> str:
    if len(s) <= n:
        return s
    return s[:n] + "…"
