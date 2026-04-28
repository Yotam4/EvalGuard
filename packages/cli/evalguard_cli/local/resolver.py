"""Resolve resource references to bytes.

Phase 0.5 only knows about local files (relative to the project's
``base_dir``). The same interface will later resolve ``registry://``
refs, signed URL refs, and inline content without any caller change.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass
class ResolverError(Exception):
    spec: dict[str, Any]
    reason: str

    def __str__(self) -> str:
        return f"cannot resolve resource {self.spec!r}: {self.reason}"


class Resolver:
    """Single entry point for fetching resource content.

    A *spec* is a mapping that may carry ``file`` (relative path),
    ``content`` (inline str), or ``ref`` (``registry://...`` — not yet
    supported). Missing keys raise; ambiguous specs raise.
    """

    def __init__(self, base_dir: Path) -> None:
        self.base_dir = base_dir

    def resolve_text(self, spec: dict[str, Any]) -> str:
        return self.resolve_bytes(spec).decode("utf-8")

    def resolve_bytes(self, spec: dict[str, Any]) -> bytes:
        keys = {k for k in ("file", "content", "ref") if k in spec}
        if not keys:
            raise ResolverError(spec, "missing one of 'file' | 'content' | 'ref'")
        if len(keys) > 1:
            raise ResolverError(spec, f"multiple of {sorted(keys)} set; choose one")
        if "content" in spec:
            value = spec["content"]
            return value.encode("utf-8") if isinstance(value, str) else bytes(value)
        if "file" in spec:
            path = (self.base_dir / spec["file"]).resolve()
            base = self.base_dir.resolve()
            try:
                path.relative_to(base)
            except ValueError as e:
                raise ResolverError(spec, f"path escapes project directory: {path}") from e
            try:
                return path.read_bytes()
            except OSError as e:
                raise ResolverError(spec, f"read failed: {e}") from e
        # ref is reserved for the registry phase
        raise ResolverError(spec, "ref:// resolution not implemented (planned: registry, signed URL)")
