"""Loader: validation, line-numbered JSONL errors, per-asset version_ids."""

from __future__ import annotations

from pathlib import Path

import pytest

from evalguard_cli.local.yaml_loader import load_config


def _write_minimal(base: Path) -> Path:
    (base / "datasets").mkdir()
    (base / "prompts").mkdir()
    (base / "datasets" / "g.jsonl").write_text(
        '{"id":"a","input":"hi","expected":"hello"}\n'
        '{"id":"b","input":"x","expected":"y"}\n'
    )
    (base / "prompts" / "p.md").write_text("Say {input}")
    cfg = base / "evalguard.yaml"
    cfg.write_text(
        "version: 1\n"
        "project: test\n"
        "providers: [{ id: mock:m }]\n"
        "prompts: [{ id: p1, file: prompts/p.md }]\n"
        "datasets: [{ id: g, file: datasets/g.jsonl }]\n"
    )
    return cfg


def test_load_config_resolves_assets_and_hashes(tmp_project: Path) -> None:
    cfg_path = _write_minimal(tmp_project)
    cfg = load_config(cfg_path)

    assert cfg.project == "test"
    assert cfg.config_hash and len(cfg.config_hash) == 64
    assert "p1" in cfg.prompts
    assert cfg.prompts["p1"] == "Say {input}"
    assert cfg.dataset_rows["g"][0]["id"] == "a"

    kinds = {a.kind for a in cfg.assets}
    assert {"prompt", "dataset"} <= kinds
    # Per-asset version_ids are 64-char hex hashes
    for a in cfg.assets:
        assert len(a.version_id) == 64


def test_load_config_is_deterministic_for_same_inputs(tmp_project: Path, tmp_path_factory) -> None:
    cfg_path = _write_minimal(tmp_project)
    other = tmp_path_factory.mktemp("dup")
    cfg_path2 = _write_minimal(other)
    a = load_config(cfg_path)
    b = load_config(cfg_path2)
    a_versions = sorted((x.kind, x.asset_id, x.version_id) for x in a.assets)
    b_versions = sorted((x.kind, x.asset_id, x.version_id) for x in b.assets)
    assert a_versions == b_versions


def test_jsonl_error_includes_line_number(tmp_project: Path) -> None:
    (tmp_project / "datasets").mkdir()
    (tmp_project / "datasets" / "g.jsonl").write_text(
        '{"id":"a","input":"ok"}\n'
        'this is not json\n'
    )
    cfg = tmp_project / "evalguard.yaml"
    cfg.write_text(
        "version: 1\nproject: t\nproviders: [{id: mock:m}]\n"
        "datasets: [{id: g, file: datasets/g.jsonl}]\n"
    )
    with pytest.raises(ValueError) as exc:
        load_config(cfg)
    assert "datasets/g.jsonl:2" in str(exc.value)


def test_schema_validation_catches_missing_provider(tmp_project: Path) -> None:
    cfg = tmp_project / "evalguard.yaml"
    cfg.write_text("version: 1\nproject: t\ndatasets: []\n")
    with pytest.raises(Exception):
        load_config(cfg)
