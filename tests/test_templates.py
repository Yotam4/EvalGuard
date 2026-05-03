"""Smoke tests for the shipped templates (text_gen, rag, text_to_sql).

These tests guard the contract between ``evalguard init`` and the rest
of the pipeline: a freshly scaffolded project must validate, and (for
templates whose mock provider returns parseable output) it must run
end-to-end without hitting a real provider.
"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

import pytest

from evalguard_cli.local.local_executor import execute
from evalguard_cli.local.sqlite_store import SqliteStore
from evalguard_cli.local.yaml_loader import load_config


_TEMPLATES = Path(__file__).resolve().parents[1] / "packages" / "templates"


def _copy_template(template: str, dest: Path) -> None:
    src = _TEMPLATES / template
    assert src.exists(), f"template not found: {src}"
    shutil.copytree(src, dest)


def _validate(dest: Path):
    """``load_config`` is what ``evalguard validate`` calls under the hood."""
    return load_config(dest / "evalguard.yaml")


def _run(dest: Path):
    cfg = _validate(dest)
    store = SqliteStore(dest / "local.db")
    store.init_schema()
    return store, asyncio.run(execute(cfg, store=store, quiet=True))


# ---------------------------------------------------------------------------
# Each template's directory must mirror the layout init copies in.


@pytest.mark.parametrize("template", ["text_gen", "rag", "text_to_sql"])
def test_template_directory_layout(template: str, tmp_path: Path):
    dest = tmp_path / template
    _copy_template(template, dest)
    assert (dest / "evalguard.yaml").exists()
    assert (dest / "datasets").is_dir()
    assert any((dest / "datasets").iterdir())
    assert (dest / "prompts").is_dir()
    assert any((dest / "prompts").iterdir())


# ---------------------------------------------------------------------------
# Each template validates as a config.


@pytest.mark.parametrize("template", ["text_gen", "rag", "text_to_sql"])
def test_template_validates(template: str, tmp_path: Path):
    dest = tmp_path / template
    _copy_template(template, dest)
    loaded = _validate(dest)
    assert loaded.project
    # Every template ships at least one dataset; loader should resolve it.
    assert loaded.dataset_rows
    # Asset hashes are populated.
    assert loaded.assets


# ---------------------------------------------------------------------------
# Templates that should run end-to-end on the mock provider with no API key.
# Exclude rag here because the lexical-proxy metrics produce per-row failures
# on echoed prompts; gates still pass but row_status reflects mixed scores.


@pytest.mark.parametrize("template", ["text_gen", "text_to_sql"])
def test_template_runs_to_completion(template: str, tmp_path: Path):
    dest = tmp_path / template
    _copy_template(template, dest)
    store, record = _run(dest)
    assert record.row_count > 0
    # No infra error should have come back as a 'failed' run-status
    # without our knowing why.
    assert record.row_status in {"passed", "failed", "cost_capped"}


def test_text_to_sql_template_smoke_passes_gates(tmp_path: Path):
    """text_to_sql ships per-row ``params: {output: <SQL>}`` overrides
    so the mock provider returns valid SQL for each question and the
    pipeline reaches a fully-passing run end-to-end."""
    dest = tmp_path / "text_to_sql"
    _copy_template("text_to_sql", dest)
    store, record = _run(dest)
    assert record.row_count == 3
    assert record.row_pass_count == 3, (
        f"expected all rows to pass; got {record.row_pass_count}/{record.row_count}"
    )
