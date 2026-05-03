"""Per-row provider/params override.

Dataset rows can carry ``provider`` (e.g. ``openai:gpt-4o``) and
``params`` (a dict shallow-merged onto the trial provider config) to
mix cheap/expensive across one trial. The executor resolves the
effective provider/model/params per row and audits what actually ran.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from evalguard_cli.local.local_executor import execute
from evalguard_cli.local.sqlite_store import SqliteStore
from evalguard_cli.local.yaml_loader import load_config


def _setup(tmp_path: Path, dataset_lines: list[str], extra_yaml: str = "") -> tuple[SqliteStore, "object"]:
    (tmp_path / "datasets").mkdir()
    (tmp_path / "datasets" / "g.jsonl").write_text("\n".join(dataset_lines) + "\n")
    cfg_path = tmp_path / "evalguard.yaml"
    cfg_path.write_text(
        "version: 1\nproject: t\n"
        "providers:\n"
        "  - id: 'mock:base-model'\n"
        "    config: { mode: fixed, output: 'TRIAL_OUTPUT', latency_ms: 0 }\n"
        "datasets: [{ id: g, file: datasets/g.jsonl }]\n"
        + extra_yaml
    )
    cfg = load_config(cfg_path)
    store = SqliteStore(tmp_path / "local.db")
    store.init_schema()
    record = asyncio.run(execute(cfg, store=store, quiet=True))
    return store, record


def test_row_params_override_changes_output(tmp_path: Path):
    """A row that sets ``params: {output: ...}`` swaps the mock's
    canned output for that row only."""
    store, record = _setup(tmp_path, [
        '{"id":"r1","input":"a"}',
        '{"id":"r2","input":"b","params":{"output":"ROW_OVERRIDE"}}',
    ])
    [trial] = store.list_trials(record.run_id)
    r1 = store.get_row(record.run_id, "r1", trial_id=trial["trial_id"])
    r2 = store.get_row(record.run_id, "r2", trial_id=trial["trial_id"])
    assert r1["output"] == "TRIAL_OUTPUT"
    assert r2["output"] == "ROW_OVERRIDE"


def test_row_provider_override_records_effective_provider(tmp_path: Path):
    """A row with ``provider: mock:other-model`` runs on that model;
    the row + audit event reflect the effective provider/model."""
    store, record = _setup(tmp_path, [
        '{"id":"r1","input":"a"}',
        '{"id":"r2","input":"b","provider":"mock:other-model","params":{"output":"OTHER","mode":"fixed"}}',
    ])
    rows = {r["row_id"]: r for r in store.list_rows(record.run_id)}
    assert rows["r1"]["model"] == "base-model"
    assert rows["r2"]["model"] == "other-model"
    # Audit reflects the actual call, with row_override marked True.
    events = store.list_events(record.run_id, kind="provider.called")
    by_row = {e["row_id"]: e for e in events}
    assert by_row["r1"]["payload"]["model"] == "base-model"
    assert by_row["r1"]["payload"]["row_override"] is False
    assert by_row["r2"]["payload"]["model"] == "other-model"
    assert by_row["r2"]["payload"]["row_override"] is True
    assert by_row["r2"]["payload"]["trial_provider_id"] == "mock:base-model"


def test_cache_does_not_collide_between_overridden_and_default_rows(tmp_path: Path):
    """Two rows with the same input but different overrides must not
    share a cache entry — otherwise the second row would inherit the
    first's output."""
    store, record = _setup(tmp_path, [
        '{"id":"r1","input":"same-input"}',
        '{"id":"r2","input":"same-input","params":{"output":"DIFFERENT"}}',
    ])
    [trial] = store.list_trials(record.run_id)
    r1 = store.get_row(record.run_id, "r1", trial_id=trial["trial_id"])
    r2 = store.get_row(record.run_id, "r2", trial_id=trial["trial_id"])
    assert r1["output"] == "TRIAL_OUTPUT"
    assert r2["output"] == "DIFFERENT"
