"""Validate and load ``evalguard.yaml`` plus the assets it references."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import jsonschema
import yaml


@dataclass
class LoadedConfig:
    """Resolved config — file paths replaced with content, hashes computed."""

    project: str
    raw: dict[str, Any]
    config_path: Path
    base_dir: Path
    config_hash: str
    prompts: dict[str, str] = field(default_factory=dict)            # id -> template body
    dataset_rows: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    schemas: dict[str, dict[str, Any]] = field(default_factory=dict)


def load_config(path: Path) -> LoadedConfig:
    path = path.resolve()
    if not path.exists():
        raise FileNotFoundError(f"config not found: {path}")
    text = path.read_text()
    data = yaml.safe_load(text)
    if not isinstance(data, dict):
        raise ValueError("evalguard.yaml must be a mapping at the top level")

    schema = json.loads(_schema_path().read_text())
    jsonschema.validate(data, schema)

    base = path.parent
    prompts = _load_prompts(data.get("prompts", []), base)
    datasets = _load_datasets(data.get("datasets", []), base)

    config_hash = hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()
    return LoadedConfig(
        project=data["project"],
        raw=data,
        config_path=path,
        base_dir=base,
        config_hash=config_hash,
        prompts=prompts,
        dataset_rows=datasets,
    )


def _load_prompts(specs: list[dict[str, Any]], base: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for s in specs:
        if "template" in s:
            out[s["id"]] = s["template"]
        else:
            out[s["id"]] = (base / s["file"]).read_text()
    return out


def _load_datasets(specs: list[dict[str, Any]], base: Path) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    for s in specs:
        rows = []
        with (base / s["file"]).open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                rows.append(json.loads(line))
        if "limit" in s:
            rows = rows[: int(s["limit"])]
        out[s["id"]] = rows
    return out


def _schema_path() -> Path:
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "packages" / "schemas" / "evalguard.schema.json"
        if candidate.exists():
            return candidate
    raise FileNotFoundError("evalguard.schema.json not found")
