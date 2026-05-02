"""``evalguard validate`` — fast, offline config sanity check.

Loads ``evalguard.yaml``, runs JSON-Schema validation, substitutes
``${ENV}`` references, resolves every asset (prompts, datasets,
schemas, rubrics, judge specs, heuristic specs) and prints their
content-hashed ``version_id``s.

No provider calls. Useful as a pre-flight in CI:

    - run: evalguard validate -c evalguard.yaml   # exit 1 in <1s if config bad
    - run: evalguard run -c evalguard.yaml        # only fires if validate passed
"""

from __future__ import annotations

from pathlib import Path

import typer
from rich.table import Table

from evalguard_cli.console import console
from evalguard_cli.local.yaml_loader import load_config

EXIT_OK = 0
EXIT_INVALID = 1


def validate(
    config: Path = typer.Option(Path("evalguard.yaml"), "--config", "-c", help="Path to evalguard.yaml"),
    quiet: bool = typer.Option(False, "--quiet", "-q", help="Print only on error."),
) -> None:
    """Validate config + resolve all assets without running any evaluators."""
    try:
        cfg = load_config(config)
    except Exception as e:  # noqa: BLE001 — user-facing surface
        console.print(f"[red]✗ config invalid:[/red] {e}")
        raise typer.Exit(EXIT_INVALID) from e

    if quiet:
        raise typer.Exit(EXIT_OK)

    console.print(f"[green]✓[/green] config [cyan]{config}[/cyan]  hash={cfg.config_hash[:16]}…")
    console.print(f"  project: [cyan]{cfg.project}[/cyan]")
    console.print(f"  providers: {len(cfg.raw.get('providers', []))}")
    console.print(f"  dataset rows: {sum(len(rows) for rows in cfg.dataset_rows.values())}")

    if cfg.assets:
        table = Table(title="Resolved assets", show_lines=False)
        table.add_column("kind", style="cyan", no_wrap=True)
        table.add_column("id", no_wrap=True)
        table.add_column("version_id", style="dim")
        table.add_column("source", style="dim")
        for asset in cfg.assets:
            table.add_row(
                asset.kind, asset.asset_id,
                f"{asset.version_id[:16]}…",
                asset.source,
            )
        console.print(table)

    extras: list[str] = []
    if cfg.raw.get("layers"):
        extras.append(f"{len(cfg.raw['layers'])} layer-gate(s)")
    if cfg.raw.get("gates"):
        extras.append(f"{len(cfg.raw['gates'])} legacy gate(s)")
    if "cost_cap_usd" in cfg.raw:
        extras.append(f"cost_cap=${cfg.raw['cost_cap_usd']:.2f}")
    if cfg.raw.get("audit", {}).get("redact_payload"):
        extras.append("audit.redact_payload=true")
    if extras:
        console.print("  " + " · ".join(extras))

    raise typer.Exit(EXIT_OK)
