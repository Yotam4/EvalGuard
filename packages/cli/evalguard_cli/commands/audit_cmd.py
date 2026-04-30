"""``evalguard audit`` — inspect, verify, and export the per-run audit log.

Subcommands:

    evalguard audit show <run_id> [--kind ...] [--trial ...]
    evalguard audit verify <run_id>
    evalguard audit export <run_id> [--format jsonl|prov-json|otel-json]

The audit log is the append-only source of truth for the run; the
``runs`` / ``trials`` / ``scores`` / ``gate_results`` tables are
queryable views built by the executor as it emits events.
"""

from __future__ import annotations

import json
from pathlib import Path

import typer
from rich.table import Table

from evalguard_cli.console import console
from evalguard_cli.local.audit import verify_chain
from evalguard_cli.local.sqlite_store import SqliteStore


audit_app = typer.Typer(help="Inspect, verify, and export the audit log.")


_DEFAULT_DB = Path(".evalguard/local.db")


@audit_app.command("show")
def show(
    run_id: str = typer.Argument(..., help="Run ID (or prefix)."),
    kind: str | None = typer.Option(None, "--kind", help="Filter by event kind."),
    trial: str | None = typer.Option(None, "--trial", help="Filter by trial id."),
    limit: int = typer.Option(0, "--limit", "-n",
                              help="Cap event count (0 = no cap)."),
    db_path: Path = typer.Option(_DEFAULT_DB, "--db"),
) -> None:
    """Render the run's event timeline as a table."""
    store, full = _resolve(run_id, db_path)
    events = store.list_events(
        full["run_id"],
        kind=kind, trial_id=trial,
        limit=limit if limit > 0 else None,
    )
    if not events:
        console.print("[yellow]no events for that filter[/yellow]")
        return

    table = Table(title=f"Audit timeline · {full['run_id']} ({len(events)} events)")
    table.add_column("at", style="dim", no_wrap=True)
    table.add_column("kind", style="cyan", no_wrap=True)
    table.add_column("subject", style="dim")
    table.add_column("trial", style="dim", no_wrap=True)
    table.add_column("row", style="dim", no_wrap=True)
    table.add_column("dur_ms", justify="right")
    table.add_column("cost", justify="right")
    table.add_column("hash", style="dim")
    for ev in events:
        cost = f"${(ev.get('cost_usd') or 0):.4f}" if ev.get("cost_usd") else ""
        dur  = str(ev["duration_ms"]) if ev.get("duration_ms") is not None else ""
        subject = ev.get("subject_id") or ""
        if ev.get("subject_kind"):
            subject = f"{ev['subject_kind']}:{subject}"
        table.add_row(
            ev["started_at"][11:23],  # HH:MM:SS.mmm
            ev["kind"],
            subject,
            (ev.get("trial_id") or "")[6:14] if ev.get("trial_id") else "",
            ev.get("row_id") or "",
            dur, cost,
            ev["event_hash"][:8],
        )
    console.print(table)
    console.print(
        f"[dim]actor:[/dim] [cyan]{events[0]['actor_id']}[/cyan]  "
        f"[dim]({events[0]['actor_type']})[/dim]"
    )


@audit_app.command("verify")
def verify(
    run_id: str = typer.Argument(..., help="Run ID (or prefix)."),
    db_path: Path = typer.Option(_DEFAULT_DB, "--db"),
) -> None:
    """Walk the per-run hash chain end-to-end and report any tampering."""
    store, full = _resolve(run_id, db_path)
    result = verify_chain(store, full["run_id"])
    if result["ok"]:
        console.print(
            f"[green]✓ chain intact[/green] · {result['events']} events · "
            f"run [cyan]{full['run_id']}[/cyan]"
        )
        raise typer.Exit(0)
    console.print(
        f"[red]✗ chain BROKEN[/red] at event "
        f"[yellow]{result['broken_at']}[/yellow]\n  reason: {result['reason']}"
    )
    raise typer.Exit(2)


@audit_app.command("export")
def export(
    run_id: str = typer.Argument(..., help="Run ID (or prefix)."),
    format: str = typer.Option(
        "jsonl", "--format", "-f",
        help="jsonl (default) | prov-json (W3C PROV) | otel-json (OTel-flavoured spans).",
    ),
    db_path: Path = typer.Option(_DEFAULT_DB, "--db"),
) -> None:
    """Emit the audit log to stdout in the chosen archival format."""
    store, full = _resolve(run_id, db_path)
    events = store.list_events(full["run_id"])

    if format == "jsonl":
        for ev in events:
            print(json.dumps(ev, sort_keys=True, default=str))
        return

    if format == "prov-json":
        print(json.dumps(_to_prov_json(full, events), indent=2, default=str))
        return

    if format == "otel-json":
        print(json.dumps(_to_otel_spans(events), indent=2, default=str))
        return

    console.print(f"[red]unknown format:[/red] {format}")
    raise typer.Exit(2)


# ---------------------------------------------------------------------------


def _resolve(run_id: str, db_path: Path) -> tuple[SqliteStore, dict]:
    if not db_path.exists():
        console.print(f"[yellow]no run history at {db_path}[/yellow]")
        raise typer.Exit(1)
    store = SqliteStore(db_path)
    runs = store.list_runs(limit=1000)
    matching = [r for r in runs if r["run_id"].startswith(run_id)]
    if not matching:
        console.print(f"[red]no run found matching {run_id}[/red]")
        raise typer.Exit(1)
    return store, matching[0]


# Vocabulary mapping used by the export formats. The intent is "format
# converters" — they translate the EvalGuard event shape into widely-read
# vocabularies so external tools (compliance archives, observability
# stacks, lineage UIs) can ingest without bespoke glue.

_PROV_AGENT_ATTRS = {
    "prov:type": "evalguard:Actor",
}


def _to_prov_json(run: dict, events: list[dict]) -> dict:
    """W3C PROV-JSON serialization (https://www.w3.org/TR/prov-json/).

    Each event becomes a PROV ``activity``; the actor is one ``agent``;
    the run, trials, evaluators, and gates are ``entities``. Relations
    are emitted with ``wasGeneratedBy`` and ``wasAttributedTo``.
    """
    if not events:
        return {"prefix": {"evalguard": "https://evalguard.dev/ns#"},
                "activity": {}, "entity": {}, "agent": {}}

    actor = events[0]
    activities: dict[str, dict] = {}
    entities: dict[str, dict] = {f"evalguard:run/{run['run_id']}": {
        "prov:type": "evalguard:Run", "evalguard:project": run["project"],
        "evalguard:config_hash": run.get("config_hash"),
    }}
    agents: dict[str, dict] = {f"evalguard:actor/{actor['actor_id']}": {
        **_PROV_AGENT_ATTRS,
        "evalguard:actor_type": actor["actor_type"],
        "evalguard:actor_meta": actor["actor_meta"],
    }}
    rels_attr: dict[str, dict] = {}
    rels_gen: dict[str, dict] = {}

    for i, ev in enumerate(events):
        act_id = f"evalguard:event/{ev['event_id']}"
        activities[act_id] = {
            "prov:type": ev["kind"],
            "prov:startTime": ev["started_at"],
            "prov:endTime":   ev.get("finished_at") or ev["started_at"],
            "evalguard:event_hash": ev["event_hash"],
            "evalguard:prev_event_hash": ev.get("prev_event_hash"),
            "evalguard:subject_kind": ev.get("subject_kind"),
            "evalguard:subject_id":   ev.get("subject_id"),
            "evalguard:cost_usd":     ev.get("cost_usd") or 0.0,
        }
        rels_attr[f"_:wAt{i}"] = {
            "prov:activity": act_id,
            "prov:agent": f"evalguard:actor/{ev['actor_id']}",
        }
        if ev.get("subject_id"):
            ent_id = f"evalguard:{ev['subject_kind']}/{ev['subject_id']}"
            entities.setdefault(ent_id, {
                "prov:type": f"evalguard:{ev['subject_kind']}",
                "evalguard:version_id": ev.get("subject_version"),
            })
            rels_gen[f"_:wGB{i}"] = {
                "prov:entity": ent_id,
                "prov:activity": act_id,
            }

    return {
        "prefix": {
            "evalguard": "https://evalguard.dev/ns#",
            "prov":      "http://www.w3.org/ns/prov#",
        },
        "activity":         activities,
        "entity":           entities,
        "agent":            agents,
        "wasAttributedTo":  rels_attr,
        "wasGeneratedBy":   rels_gen,
    }


def _to_otel_spans(events: list[dict]) -> dict:
    """OTLP-flavoured JSON (OpenTelemetry collector input shape).

    One ``ResourceSpans`` per run; each event becomes one span. LLM
    calls land with ``gen_ai.*`` attribute names so an OTel collector
    can route them to any GenAI-aware backend.
    """
    if not events:
        return {"resourceSpans": []}

    actor = events[0]
    trace_id = events[0]["trace_id"]
    spans = []
    for ev in events:
        attrs: dict[str, object] = {
            "evalguard.event_id":      ev["event_id"],
            "evalguard.kind":          ev["kind"],
            "evalguard.run_id":        ev["run_id"],
            "evalguard.subject_kind":  ev.get("subject_kind"),
            "evalguard.subject_id":    ev.get("subject_id"),
            "evalguard.event_hash":    ev["event_hash"],
            "evalguard.prev_hash":     ev.get("prev_event_hash"),
        }
        if ev.get("trial_id"):
            attrs["evalguard.trial_id"] = ev["trial_id"]
        if ev.get("row_id"):
            attrs["evalguard.row_id"] = ev["row_id"]
        if ev.get("cost_usd") is not None:
            attrs["evalguard.cost_usd"] = ev["cost_usd"]

        # gen_ai.* mapping for LLM calls.
        if ev["kind"] == "provider.called":
            p = ev.get("payload") or {}
            attrs["gen_ai.system"] = p.get("provider")
            attrs["gen_ai.request.model"] = p.get("model")
            params = p.get("model_params") or {}
            for k in ("temperature", "top_p", "max_tokens"):
                if k in params:
                    attrs[f"gen_ai.request.{k}"] = params[k]
            tokens = p.get("tokens") or {}
            if "prompt" in tokens:
                attrs["gen_ai.usage.input_tokens"] = tokens["prompt"]
            if "completion" in tokens:
                attrs["gen_ai.usage.output_tokens"] = tokens["completion"]

        spans.append({
            "traceId":           trace_id,
            "spanId":            ev.get("span_id"),
            "parentSpanId":      ev.get("parent_span_id"),
            "name":              ev["kind"],
            "kind":              "SPAN_KIND_INTERNAL",
            "startTimeUnixNano": _iso_to_ns(ev["started_at"]),
            "endTimeUnixNano":   _iso_to_ns(ev.get("finished_at") or ev["started_at"]),
            "attributes":        [{"key": k, "value": _otel_value(v)}
                                  for k, v in attrs.items() if v is not None],
        })

    return {
        "resourceSpans": [{
            "resource": {
                "attributes": [
                    {"key": "service.name", "value": {"stringValue": "evalguard"}},
                    {"key": "evalguard.actor_id", "value": {"stringValue": actor["actor_id"]}},
                    {"key": "evalguard.actor_type", "value": {"stringValue": actor["actor_type"]}},
                ],
            },
            "scopeSpans": [{
                "scope": {"name": "evalguard.audit", "version": "1.0.0"},
                "spans": spans,
            }],
        }],
    }


def _otel_value(v: object) -> dict:
    if isinstance(v, bool):
        return {"boolValue": v}
    if isinstance(v, int):
        return {"intValue": str(v)}
    if isinstance(v, float):
        return {"doubleValue": v}
    return {"stringValue": str(v)}


def _iso_to_ns(iso: str) -> str:
    from datetime import datetime
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return "0"
    return str(int(dt.timestamp() * 1_000_000_000))
