"""Validate and load ``evalguard.yaml`` plus the assets it references.

Each asset (prompt, dataset, judge, heuristic, schema, rubric) is given a
``version_id = sha256(content)``. The top-level ``config_hash`` covers the
canonical YAML; individual asset hashes flow into ``LoadedConfig.assets``
so per-asset regression diffs are possible later.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import jsonschema
import yaml

from evalguard_cli.local.resolver import Resolver


@dataclass
class AssetVersion:
    kind: str          # "prompt" | "dataset" | "judge" | "heuristic" | "schema" | "rubric"
    asset_id: str      # config-declared id (or synthesized for inline assets)
    version_id: str    # sha256 of canonicalised content
    source: str        # "file:relative/path" | "inline" | "ref:registry://..."


@dataclass
class LoadedConfig:
    """Resolved config — file paths replaced with content, hashes computed."""

    project: str
    raw: dict[str, Any]                 # original parsed YAML (post-validation)
    resolved: dict[str, Any]            # YAML with all file refs inlined
    config_path: Path
    base_dir: Path
    config_hash: str
    prompts: dict[str, str] = field(default_factory=dict)
    dataset_rows: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    assets: list[AssetVersion] = field(default_factory=list)


def load_config(path: Path) -> LoadedConfig:
    path = path.resolve()
    if not path.exists():
        raise FileNotFoundError(f"config not found: {path}")
    text = path.read_text()
    data = yaml.safe_load(text)
    if not isinstance(data, dict):
        raise ValueError("evalguard.yaml must be a mapping at the top level")

    # Substitute ${ENV_VAR} (and ${VAR:-default}) before schema validation so
    # secrets, base URLs, model ids, anything can live in the environment.
    data = _substitute_env(data)

    schema = json.loads(_schema_path().read_text())
    jsonschema.validate(data, schema)

    base = path.parent
    resolver = Resolver(base)
    resolved = copy.deepcopy(data)
    assets: list[AssetVersion] = []

    prompts = _load_prompts(resolved.get("prompts", []), resolver, assets)
    datasets = _load_datasets(resolved.get("datasets", []), resolver, base, assets)
    systems = resolved.get("systems") or {}
    _inline_evaluator_files(resolved.get("heuristics", []), resolver, base, assets,
                            kind="heuristic", systems=systems)
    _inline_evaluator_files(resolved.get("metrics", []), resolver, base, assets,
                            kind="metric", systems=systems)
    _inline_evaluator_files(resolved.get("judges", []), resolver, base, assets,
                            kind="judge", systems=systems)

    config_hash = _sha256_canonical(resolved)
    return LoadedConfig(
        project=resolved["project"],
        raw=resolved,
        resolved=resolved,
        config_path=path,
        base_dir=base,
        config_hash=config_hash,
        prompts=prompts,
        dataset_rows=datasets,
        assets=assets,
    )


def _load_prompts(specs: list[dict[str, Any]], resolver: Resolver, assets: list[AssetVersion]) -> dict[str, str]:
    out: dict[str, str] = {}
    for spec in specs:
        body = resolver.resolve_text({k: spec[k] for k in spec if k in {"file", "content", "ref", "template"}}
                                     if "template" not in spec
                                     else {"content": spec["template"]})
        version_id = _sha256_text(body)
        out[spec["id"]] = body
        source = _source_label(spec)
        assets.append(AssetVersion("prompt", spec["id"], version_id, source))
        spec["template"] = body          # inline so the rest of the system never re-reads
        spec["version_id"] = version_id
        spec.pop("file", None)
    return out


def _load_datasets(
    specs: list[dict[str, Any]], resolver: Resolver, base: Path, assets: list[AssetVersion]
) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    for spec in specs:
        rel = spec["file"]
        rows: list[dict[str, Any]] = []
        path = (base / rel).resolve()
        with path.open() as f:
            for lineno, line in enumerate(f, start=1):
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    rows.append(json.loads(stripped))
                except json.JSONDecodeError as e:
                    raise ValueError(f"{rel}:{lineno}: invalid JSON ({e.msg}) at col {e.colno}") from e
        if "limit" in spec:
            rows = rows[: int(spec["limit"])]
        version_id = _sha256_canonical(rows)
        out[spec["id"]] = rows
        assets.append(AssetVersion("dataset", spec["id"], version_id, f"file:{rel}"))
        spec["row_count"] = len(rows)
        spec["version_id"] = version_id
    return out


def _inline_evaluator_files(
    specs: list[dict[str, Any]],
    resolver: Resolver,
    base: Path,
    assets: list[AssetVersion],
    *,
    kind: str,
    systems: dict[str, Any] | None = None,
) -> None:
    """Replace ``schema_file`` / ``rubric_file`` with inline ``schema`` / ``rubric``.

    Evaluators receive content directly and never touch the filesystem; this
    is the seam that lets us swap to ``ref://`` refs without changing any
    evaluator code.

    When an evaluator declares ``system: <name>``, the matching entry from
    the top-level ``systems:`` block is inlined as ``_system`` so the
    evaluator can read it from its own ``configure(cfg)`` call without
    needing global state. The reference is resolved at YAML-load time so
    the spec's ``version_id`` covers the system binding.
    """
    systems = systems or {}
    for spec in specs:
        ev_id = spec.get("id", spec.get("type", kind))
        if "schema_file" in spec:
            blob = resolver.resolve_text({"file": spec["schema_file"]})
            schema_obj = json.loads(blob)
            v = _sha256_canonical(schema_obj)
            assets.append(AssetVersion("schema", ev_id, v, f"file:{spec['schema_file']}"))
            spec["schema"] = schema_obj
            spec["schema_version_id"] = v
            spec.pop("schema_file")
        if "rubric_file" in spec:
            blob = resolver.resolve_text({"file": spec["rubric_file"]})
            v = _sha256_text(blob)
            assets.append(AssetVersion("rubric", ev_id, v, f"file:{spec['rubric_file']}"))
            spec["rubric"] = blob
            spec["rubric_version_id"] = v
            spec.pop("rubric_file")
        if "system" in spec:
            sys_name = spec["system"]
            if sys_name not in systems:
                raise ValueError(
                    f"evaluator {ev_id!r} references system {sys_name!r} but no "
                    f"such entry exists under top-level ``systems:``. "
                    f"Available: {sorted(systems)}"
                )
            sys_cfg = dict(systems[sys_name])
            # Resolve a ``schema:`` field that points at a file (e.g.
            # ``schema: schemas/db.sql``) into inline content so the
            # evaluator never touches the filesystem.
            if isinstance(sys_cfg.get("schema"), str) and sys_cfg["schema"].endswith(
                (".sql", ".ddl", ".json")
            ):
                blob = resolver.resolve_text({"file": sys_cfg["schema"]})
                sv = _sha256_text(blob)
                assets.append(AssetVersion(
                    "schema", f"{ev_id}.system.{sys_name}", sv,
                    f"file:{sys_cfg['schema']}",
                ))
                sys_cfg["schema_source"] = sys_cfg["schema"]
                sys_cfg["schema"] = blob
                sys_cfg["schema_version_id"] = sv
            spec["_system"] = {"name": sys_name, **sys_cfg}
        # Hash the final spec (post-inlining) so evaluator versions track config + assets.
        spec["version_id"] = _sha256_canonical({k: v for k, v in spec.items() if k != "version_id"})
        assets.append(AssetVersion(kind, ev_id, spec["version_id"], "inline"))


def _source_label(spec: dict[str, Any]) -> str:
    if "file" in spec:
        return f"file:{spec['file']}"
    if "ref" in spec:
        return f"ref:{spec['ref']}"
    return "inline"


def _sha256_text(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _sha256_canonical(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def _schema_path() -> Path:
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "packages" / "schemas" / "evalguard.schema.json"
        if candidate.exists():
            return candidate
    raise FileNotFoundError("evalguard.schema.json not found")


# ``${VAR}`` or ``${VAR:-default}`` — names match POSIX-style identifiers.
_ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def _substitute_env(value: Any) -> Any:
    """Recursively substitute ``${ENV}`` references in any string value.

    Missing variables raise a clear error; ``${VAR:-default}`` provides a
    fallback. ``$$`` is left alone so users can write literal ``$`` if
    needed (rare).
    """
    if isinstance(value, str):
        def _sub(m: "re.Match[str]") -> str:
            name = m.group(1)
            default = m.group(2)
            v = os.environ.get(name)
            if v is None:
                if default is None:
                    raise ValueError(
                        f"environment variable ${{{name}}} is not set "
                        "(use ${" + name + ":-default} to provide a fallback)"
                    )
                return default
            return v
        return _ENV_RE.sub(_sub, value)
    if isinstance(value, dict):
        return {k: _substitute_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_substitute_env(v) for v in value]
    return value
