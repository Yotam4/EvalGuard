"""``evalguard init`` — scaffold a project from a template."""

from __future__ import annotations

import shutil
from pathlib import Path

import typer

from evalguard_cli.console import console


def init(
    template: str = typer.Option("text_gen", "--template", "-t", help="Project template to scaffold"),
    path: Path = typer.Option(Path("."), "--path", "-p", help="Target directory"),
    force: bool = typer.Option(False, "--force", help="Overwrite existing files"),
) -> None:
    """Copy a template into the target directory."""
    src = _template_root() / template
    if not src.exists():
        console.print(f"[red]Unknown template '{template}'.[/red]")
        avail = ", ".join(sorted(p.name for p in _template_root().iterdir() if p.is_dir()))
        console.print(f"Available: {avail}")
        raise typer.Exit(2)

    path.mkdir(parents=True, exist_ok=True)
    for entry in src.rglob("*"):
        rel = entry.relative_to(src)
        target = path / rel
        if entry.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        if target.exists() and not force:
            console.print(f"[yellow]skip[/yellow] {rel} (exists, use --force)")
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(entry, target)
        console.print(f"[green]wrote[/green] {rel}")
    console.print(f"\n[bold green]template '{template}' ready in {path.resolve()}[/bold green]")
    console.print("Next: [cyan]evalguard run[/cyan]")


def _template_root() -> Path:
    """Locate ``packages/templates`` whether installed or run from source."""
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "packages" / "templates"
        if candidate.exists():
            return candidate
    raise FileNotFoundError("packages/templates not found relative to evalguard_cli")
