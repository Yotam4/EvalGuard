"""Local executor: end-to-end mock run, cost cap, prompt rendering."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from evalguard_cli.local.local_executor import _render_prompt, execute
from evalguard_cli.local.sqlite_store import SqliteStore
from evalguard_cli.local.yaml_loader import load_config


def _project(base: Path, *, gate_threshold: float = 4.0, score: float = 4.5,
             rows: int = 3, cost_cap: float | None = None) -> Path:
    (base / "datasets").mkdir(parents=True, exist_ok=True)
    (base / "prompts").mkdir(parents=True, exist_ok=True)
    lines = [f'{{"id":"r{i}","input":"hello {i}","expected":"echo","tags":["normal"]}}'
             for i in range(rows)]
    (base / "datasets" / "g.jsonl").write_text("\n".join(lines) + "\n")
    (base / "prompts" / "p.md").write_text("Say back: {input}")
    cap = f"\ncost_cap_usd: {cost_cap}\n" if cost_cap is not None else ""
    cfg = base / "evalguard.yaml"
    cfg.write_text(
        "version: 1\n"
        "project: t\n"
        "providers: [{ id: 'mock:m', config: { mode: echo } }]\n"
        "prompts: [{ id: p1, file: prompts/p.md }]\n"
        "datasets: [{ id: g, file: datasets/g.jsonl }]\n"
        "judges:\n"
        f"  - {{ id: q, type: mock_pointwise, score: {score}, threshold: {gate_threshold} }}\n"
        f"{cap}"
    )
    return cfg


def test_executor_runs_mock_pipeline_end_to_end(tmp_path: Path) -> None:
    cfg = load_config(_project(tmp_path))
    store = SqliteStore(tmp_path / "local.db")
    store.init_schema()
    record = asyncio.run(execute(cfg, store=store, quiet=True))
    assert record.row_count == 3
    assert record.row_status == "passed"
    metrics = store.compute_metrics(record.run_id)
    assert metrics["pass_rate"] == 1.0
    assert metrics["q.mean"] == pytest.approx(4.5)
    assert "by_layer" in metrics and 3 in metrics["by_layer"]
    assert metrics["by_tag"]["normal"]["pass_rate"] == 1.0


def test_executor_marks_failed_when_judge_below_threshold(tmp_path: Path) -> None:
    cfg = load_config(_project(tmp_path, gate_threshold=4.9, score=4.0))
    store = SqliteStore(tmp_path / "local.db")
    store.init_schema()
    record = asyncio.run(execute(cfg, store=store, quiet=True))
    assert record.row_status == "failed"
    assert record.row_fail_count == 3


def test_render_prompt_substitutes_only_identifier_braces() -> None:
    template = 'Output JSON: {"summary":"..."}\n\nText: {input}'
    rendered = _render_prompt(template, {"input": "hello"})
    assert '"summary"' in rendered
    assert "Text: hello" in rendered


def test_render_prompt_raises_on_unknown_field() -> None:
    with pytest.raises(ValueError) as exc:
        _render_prompt("Use {missing}", {"input": "x"})
    assert "missing" in str(exc.value)
