"""``evalguard comment`` — render a sticky PR-comment markdown body.

Consumes the same data the JSON contract exposes (``run_to_dict``) plus
an optional baseline file for Δ-vs-baseline reporting. Output is a
self-contained markdown body the Phase-1 GitHub Action posts to PRs.

The first line is an HTML comment marker so the Action can find an
existing comment on subsequent runs and PATCH it instead of posting a
new one ("sticky" comment pattern). The marker can be customized via
``--marker`` so users can host multiple stickies on one PR.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import typer

from evalguard_cli.console import console
from evalguard_cli.local.baseline_io import load_baseline
from evalguard_cli.local.serializer import run_to_dict
from evalguard_cli.local.sqlite_store import SqliteStore

DEFAULT_MARKER = "<!-- evalguard:pr-comment -->"


def comment(
    run_id: str | None = typer.Argument(None, help="Run ID (or prefix); omit with --last."),
    last: bool = typer.Option(False, "--last", help="Use the most recent run."),
    baseline: Path | None = typer.Option(None, "--baseline", help="Baseline JSON for Δ-vs-baseline section."),
    marker: str = typer.Option(DEFAULT_MARKER, "--marker", help="HTML comment marker for sticky updates."),
    out: Path | None = typer.Option(None, "--out", "-o", help="Write to file (otherwise stdout)."),
    db_path: Path = typer.Option(Path(".evalguard/local.db"), "--db", help="SQLite path"),
) -> None:
    """Render a sticky PR-comment markdown body for a run."""
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

    payload = run_to_dict(store, full["run_id"], include_rows=False)
    base = None
    if baseline is not None:
        base = load_baseline(baseline)

    body = render_comment(payload, baseline=base.metrics if base else None,
                           baseline_run_id=base.run_id if base else None,
                           marker=marker)
    if out is None:
        # ``console.print`` would coerce angle brackets / pipes — print
        # directly so the markdown is byte-for-byte clean.
        print(body)
    else:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(body)
        console.print(f"[green]wrote[/green] {out}")


# ---------------------------------------------------------------------------
# Renderer (importable for tests)


# Status verdict → emoji + status word for the comment header.
_HEADER_STATUS = {
    "passed":      ("✅", "passed"),
    "warned":      ("⚠️", "warned"),
    "row_failed":  ("❌", "row_failed"),
    "gate_failed": ("❌", "gate_failed"),
    "cost_capped": ("⚠️", "cost_capped"),
    "error":       ("❌", "error"),
}


def render_comment(
    payload: dict[str, Any],
    *,
    baseline: dict[str, Any] | None = None,
    baseline_run_id: str | None = None,
    marker: str = DEFAULT_MARKER,
) -> str:
    """Render the markdown body. Pure function — no I/O."""
    parts: list[str] = [marker, ""]
    parts.append(_header(payload))
    parts.append("")
    parts.append(_summary_line(payload))
    parts.append("")

    trials = payload.get("trials") or []
    if len(trials) >= 2:
        parts.append(_trials_table(trials))
        parts.append("")

    parts.append(_gates_section(payload))
    parts.append("")

    if baseline is not None:
        delta_md = _delta_table(payload, baseline, baseline_run_id)
        if delta_md:
            parts.append(delta_md)
            parts.append("")

    parts.append(_provenance_line(payload))
    return "\n".join(parts).rstrip() + "\n"


# ---------------------------------------------------------------------------
# section builders


def _header(payload: dict[str, Any]) -> str:
    status = payload.get("status") or "unknown"
    emoji, word = _HEADER_STATUS.get(status, ("·", status))
    project = payload.get("project") or "(unnamed)"
    return f"## EvalGuard · `{project}` · {emoji} {word}"


def _summary_line(payload: dict[str, Any]) -> str:
    pass_count = payload.get("row_pass_count") or 0
    total = payload.get("row_count") or 0
    cost = payload.get("cost_usd") or 0.0
    n_trials = len(payload.get("trials") or [])
    trial_word = "trial" if n_trials == 1 else "trials"
    return f"**{pass_count}/{total}** row-evaluations passed · cost **${cost:.4f}** · {n_trials} {trial_word}"


def _trials_table(trials: list[dict[str, Any]]) -> str:
    rows = [
        "### Trials",
        "",
        "| Trial | Provider | Status | Gate | Rows | Cost |",
        "|---|---|:-:|:-:|---:|---:|",
    ]
    for t in trials:
        status = t.get("status") or "-"
        gate = t.get("gate_status") or "-"
        rows.append(
            f"| `{(t['trial_id'] or '')[:14]}…` | `{t['provider_id']}` | "
            f"{_emoji_for(status)} {status} | "
            f"{_emoji_for(gate)} {gate} | "
            f"{t['row_pass_count']}/{t['row_count']} | "
            f"${t['cost_usd']:.4f} |"
        )
    return "\n".join(rows)


def _gates_section(payload: dict[str, Any]) -> str:
    trials = payload.get("trials") or []
    rows = [
        "### Gates",
        "",
        "| Gate | Trial | Severity | Verdict |",
        "|---|---|---|:-:|",
    ]
    any_gate = False
    for t in trials:
        for g in t.get("gates") or []:
            any_gate = True
            verdict = "PASS" if g.get("passed") else (
                "FAIL" if g.get("severity") == "block" else g.get("severity", "warn").upper()
            )
            verdict_md = (
                "✅ PASS" if g.get("passed")
                else ("❌ FAIL" if g.get("severity") == "block" else f"⚠️ {verdict}")
            )
            rows.append(
                f"| `{g['gate_name']}` | `{(t['trial_id'] or '')[:14]}…` | "
                f"`{g.get('severity', 'block')}` | {verdict_md} |"
            )
    if not any_gate:
        return "_No gates configured._"
    return "\n".join(rows)


def _delta_table(
    payload: dict[str, Any],
    baseline: dict[str, Any],
    baseline_run_id: str | None,
) -> str:
    """Δ-vs-baseline table for the metrics that exist in both runs."""
    aggregate = (payload.get("aggregate") or {}).get("metrics") or {}
    keys = sorted(_scalar_keys(aggregate) & _scalar_keys(baseline))
    if not keys:
        return ""
    header = (
        "### Δ vs baseline" + (f" (`{baseline_run_id[:14]}…`)" if baseline_run_id else "")
    )
    rows = [
        header,
        "",
        "| Metric | base | PR | Δ |",
        "|---|---:|---:|---:|",
    ]
    any_row = False
    for key in keys:
        va = _scalar(baseline, key)
        vb = _scalar(aggregate, key)
        if va is None or vb is None:
            continue
        delta = vb - va
        if abs(delta) < 1e-9:
            arrow = ""
        else:
            improved = (delta < 0) if _lower_is_better(key) else (delta > 0)
            arrow = " ✅" if improved else " ✗"
        rows.append(f"| `{key}` | {va:.4f} | {vb:.4f} | {delta:+.4f}{arrow} |")
        any_row = True
    return "\n".join(rows) if any_row else ""


def _provenance_line(payload: dict[str, Any]) -> str:
    bits: list[str] = []
    bits.append(f"run_id: `{payload.get('run_id', '')}`")
    cfg_hash = payload.get("config_hash") or ""
    if cfg_hash:
        bits.append(f"config_hash: `{cfg_hash[:12]}…`")
    schema_v = payload.get("schema_version")
    if schema_v:
        bits.append(f"schema: `{schema_v}`")
    return "  ·  ".join(bits)


# ---------------------------------------------------------------------------
# helpers shared with diff_cmd (kept local to avoid cross-module coupling)


def _emoji_for(status: str | None) -> str:
    return {
        "passed": "✅", "warned": "⚠️", "failed": "❌",
        "row_failed": "❌", "gate_failed": "❌", "cost_capped": "⚠️",
        "running": "·", "none": "·",
    }.get(status or "", "·")


def _lower_is_better(name: str) -> bool:
    lo = name.lower()
    return ("cost" in lo) or ("latency" in lo) or lo.endswith("fail_count")


def _scalar_keys(metrics: dict[str, Any]) -> set[str]:
    out: set[str] = set()
    for k, v in metrics.items():
        if isinstance(v, (int, float)) and k not in {"row_count"}:
            out.add(k)
    for ev_id, agg in (metrics.get("by_evaluator") or {}).items():
        if isinstance(agg, dict):
            for inner in ("mean", "pass_rate"):
                if inner in agg:
                    out.add(f"{ev_id}.{inner}")
    return out


def _scalar(metrics: dict[str, Any], dotted: str) -> float | None:
    if dotted in metrics:
        v = metrics[dotted]
        return float(v) if isinstance(v, (int, float)) else None
    if "." in dotted:
        head, tail = dotted.split(".", 1)
        agg = (metrics.get("by_evaluator") or {}).get(head)
        if isinstance(agg, dict) and tail in agg:
            v = agg[tail]
            return float(v) if isinstance(v, (int, float)) else None
    return None
