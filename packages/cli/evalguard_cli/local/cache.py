"""Content-addressable cache for provider calls.

Cache key = sha256(provider || model || prompt || params || input_repr).
Stored as JSON files under ``.evalguard/cache/<sha[:2]>/<sha>.json``.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any


class ContentCache:
    def __init__(self, root: Path, enabled: bool = True) -> None:
        self.root = root
        self.enabled = enabled
        if enabled:
            self.root.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def key(provider: str, model: str, prompt: str, params: dict[str, Any] | None, payload: Any) -> str:
        h = hashlib.sha256()
        h.update(provider.encode())
        h.update(b"\x00")
        h.update(model.encode())
        h.update(b"\x00")
        h.update(prompt.encode())
        h.update(b"\x00")
        h.update(json.dumps(params or {}, sort_keys=True).encode())
        h.update(b"\x00")
        h.update(json.dumps(payload, sort_keys=True, default=str).encode())
        return h.hexdigest()

    def _path(self, key: str) -> Path:
        return self.root / key[:2] / f"{key}.json"

    def get(self, key: str) -> dict[str, Any] | None:
        if not self.enabled:
            return None
        p = self._path(key)
        if not p.exists():
            return None
        return json.loads(p.read_text())

    def put(self, key: str, value: dict[str, Any]) -> None:
        if not self.enabled:
            return
        p = self._path(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        # Atomic write: never expose a partial file to a concurrent
        # reader. The tmpfile carries os.getpid() so two writers racing
        # on the same key don't clobber each other's tmp.
        tmp = p.with_name(f"{p.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(value))
        tmp.replace(p)
