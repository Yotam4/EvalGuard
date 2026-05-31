"""``evalguard push-config`` — upload the local ``evalguard.yaml`` to a
remote EvalGuard server.

Phase PROXY-1.  Companion to ``evalguard push`` (which uploads a
*run*) — this uploads the *config* the server will resolve from when
production code calls ``POST /v1/projects/{slug}/invoke``.

Behaviour mirrors ``push``:

- No ``EVALGUARD_SERVER`` env (and no ``--server``) → print a hint
  and exit 0. The CLI never blocks a build for a missing optional
  server.
- Server configured, ``--dry-run`` → print the SHA-256 + first few
  lines of the YAML to stdout, exit 0.
- Server configured, real upload → POST the raw YAML to
  ``{server}/v1/projects/{slug}/config`` and surface the response.

The project slug is read from the YAML's ``project:`` field; pass
``--project`` to override (mostly for tests).
"""

from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

import typer
import yaml

from evalguard_cli.console import console


_DEFAULT_CONFIG = Path("evalguard.yaml")
ENV_SERVER = "EVALGUARD_SERVER"
ENV_TOKEN = "EVALGUARD_API_TOKEN"


# Same retry budget shape as ``push_cmd``; config pushes are even less
# hot-path so we don't need a larger one.
_PUSH_RETRY_STATUS = (408, 425, 500, 502, 503, 504)
_PUSH_MAX_RETRIES = 3
_PUSH_BASE_DELAY_S = 1.0


def _extract_project_slug(content: str) -> str | None:
    """Parse the YAML just enough to pull the ``project:`` field.

    We deliberately don't run the full ``load_config`` here — that
    would require datasets / prompt files to be present on disk,
    which is too restrictive for a config-only push (the operator
    might be deploying just the proxy config from a thin checkout).
    A bare ``yaml.safe_load`` is enough; the server is the
    authoritative validator.
    """
    try:
        data = yaml.safe_load(content)
    except yaml.YAMLError:
        return None
    if not isinstance(data, dict):
        return None
    slug = data.get("project")
    if not isinstance(slug, str) or not slug.strip():
        return None
    return slug.strip()


def push_config(
    config_path: Path = typer.Option(
        _DEFAULT_CONFIG, "--file", "-f",
        help="Path to the evalguard.yaml to upload.",
    ),
    project: str | None = typer.Option(
        None, "--project",
        help="Project slug to push under (defaults to the YAML's ``project:`` field).",
    ),
    server: str | None = typer.Option(
        None, "--server",
        help=f"EvalGuard server base URL (default: ${ENV_SERVER}).",
    ),
    token: str | None = typer.Option(
        None, "--token",
        help=f"API token (default: ${ENV_TOKEN}).",
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run",
        help="Print the SHA-256 + summary instead of POSTing.",
    ),
) -> None:
    """Upload the local evalguard.yaml to a remote EvalGuard server."""
    server = server or os.environ.get(ENV_SERVER)
    if not server and not dry_run:
        console.print(
            f"[yellow]no server configured.[/yellow] set "
            f"[cyan]${ENV_SERVER}[/cyan] (or pass [cyan]--server[/cyan]) to "
            f"upload configs to an EvalGuard server."
        )
        raise typer.Exit(0)

    if not config_path.exists():
        console.print(f"[red]config not found:[/red] {config_path}")
        raise typer.Exit(1)

    # Read the raw text — *not* a re-serialised dump.  The server
    # stores the bytes verbatim so the operator can re-fetch exactly
    # what they pushed (matters for audit and for round-tripping
    # comments / formatting).
    try:
        content = config_path.read_text(encoding="utf-8")
    except UnicodeDecodeError as e:
        console.print(f"[red]config is not valid UTF-8:[/red] {e}")
        raise typer.Exit(1) from e

    if not content.strip():
        console.print(f"[red]config is empty:[/red] {config_path}")
        raise typer.Exit(1)

    slug = project or _extract_project_slug(content)
    if not slug:
        console.print(
            "[red]no project slug[/red] — set --project, or add "
            "a top-level [cyan]project: <slug>[/cyan] to the YAML."
        )
        raise typer.Exit(1)

    sha = hashlib.sha256(content.encode("utf-8")).hexdigest()

    if dry_run:
        # Show enough that the operator can sanity-check what would
        # go up without spamming a 500-line config to stdout.
        head = "\n".join(content.splitlines()[:6])
        console.print(
            f"[dim]would push:[/dim] {config_path}\n"
            f"  project: [cyan]{slug}[/cyan]\n"
            f"  sha256:  [dim]{sha}[/dim]\n"
            f"  bytes:   {len(content.encode('utf-8'))}\n"
            f"  head:\n{head}"
        )
        raise typer.Exit(0)

    url = server.rstrip("/") + f"/v1/projects/{slug}/config"
    token = token or os.environ.get(ENV_TOKEN)
    body = json.dumps({"content": content}).encode("utf-8")

    def _build_request() -> urllib.request.Request:
        req = urllib.request.Request(
            url, data=body, method="POST",
            headers={"content-type": "application/json"},
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
            break
        except urllib.error.HTTPError as e:
            detail = (e.read().decode("utf-8", errors="replace")[:300]
                       if e.fp else "")
            if e.code in _PUSH_RETRY_STATUS and attempt < _PUSH_MAX_RETRIES:
                last_err = e
                delay = _PUSH_BASE_DELAY_S * (2 ** attempt)
                console.print(
                    f"[yellow]push-config attempt {attempt + 1} failed[/yellow] "
                    f"({e.code} {e.reason}); retrying in {delay:.0f}s"
                )
                time.sleep(delay)
                continue
            console.print(
                f"[red]push-config failed[/red] · {e.code} {e.reason}\n"
                f"  url: {url}\n"
                f"  body: {detail}"
            )
            raise typer.Exit(1) from e
        except urllib.error.URLError as e:
            if attempt < _PUSH_MAX_RETRIES:
                last_err = e
                delay = _PUSH_BASE_DELAY_S * (2 ** attempt)
                console.print(
                    f"[yellow]push-config attempt {attempt + 1} failed[/yellow] "
                    f"({e.reason}); retrying in {delay:.0f}s"
                )
                time.sleep(delay)
                continue
            console.print(f"[red]push-config failed:[/red] {e.reason}\n  url: {url}")
            raise typer.Exit(1) from e
    else:
        raise typer.Exit(1) from last_err

    # The server returns 200 for "same bytes, existing revision" and
    # 201 for "new revision" — surface the distinction.
    verb = "matched existing revision" if status == 200 else "uploaded new revision"
    console.print(
        f"[green]{verb}[/green] · sha256 [dim]{sha[:12]}…[/dim] "
        f"→ {url} [dim]({status})[/dim]"
    )
    if response_body.strip():
        try:
            parsed = json.loads(response_body)
            console.print(
                f"  revision id: [cyan]{parsed.get('id')}[/cyan]  "
                f"pushed_at: [dim]{parsed.get('pushed_at')}[/dim]"
            )
        except json.JSONDecodeError:
            console.print(f"[dim]{response_body[:300]}[/dim]")
