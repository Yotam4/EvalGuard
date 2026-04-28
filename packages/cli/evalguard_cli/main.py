"""Entrypoint for the ``evalguard`` CLI."""

from __future__ import annotations

import typer

from evalguard_cli.commands import init_cmd, run_cmd, view_cmd

app = typer.Typer(
    name="evalguard",
    help="Declarative LLM evaluation. Local-first; the same config runs in CI and on a server.",
    no_args_is_help=True,
    add_completion=False,
)

app.command("init")(init_cmd.init)
app.command("run")(run_cmd.run)
app.command("view")(view_cmd.view)


if __name__ == "__main__":
    app()
