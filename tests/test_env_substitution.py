"""${ENV} substitution in evalguard.yaml."""

from __future__ import annotations

from pathlib import Path

import pytest

from evalguard_cli.local.yaml_loader import load_config


def _project(base: Path, *, provider_id: str = "${PROVIDER_ID}",
             api_key: str = "${API_KEY}") -> Path:
    (base / "datasets").mkdir()
    (base / "datasets" / "g.jsonl").write_text('{"id":"a","input":"hi"}\n')
    cfg = base / "evalguard.yaml"
    cfg.write_text(
        "version: 1\n"
        "project: t\n"
        "providers:\n"
        f"  - id: '{provider_id}'\n"
        "    config:\n"
        f"      api_key: '{api_key}'\n"
        "datasets:\n"
        "  - { id: g, file: datasets/g.jsonl }\n"
    )
    return cfg


def test_env_var_is_substituted(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PROVIDER_ID", "openai:gpt-4o")
    monkeypatch.setenv("API_KEY", "sk-test")
    cfg = load_config(_project(tmp_path))
    assert cfg.raw["providers"][0]["id"] == "openai:gpt-4o"
    assert cfg.raw["providers"][0]["config"]["api_key"] == "sk-test"


def test_missing_env_var_raises(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("PROVIDER_ID", raising=False)
    monkeypatch.delenv("API_KEY", raising=False)
    with pytest.raises(ValueError) as exc:
        load_config(_project(tmp_path))
    msg = str(exc.value)
    assert "PROVIDER_ID" in msg or "API_KEY" in msg


def test_default_fallback(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("OLLAMA_BASE_URL", raising=False)
    (tmp_path / "datasets").mkdir()
    (tmp_path / "datasets" / "g.jsonl").write_text('{"id":"a","input":"hi"}\n')
    cfg = tmp_path / "evalguard.yaml"
    cfg.write_text(
        "version: 1\nproject: t\n"
        "providers:\n"
        "  - id: 'mock:m'\n"
        "    config:\n"
        "      base_url: '${OLLAMA_BASE_URL:-http://localhost:11434/v1}'\n"
        "datasets:\n"
        "  - { id: g, file: datasets/g.jsonl }\n"
    )
    loaded = load_config(cfg)
    assert loaded.raw["providers"][0]["config"]["base_url"] == "http://localhost:11434/v1"
