"""Layer-1 heuristic: validate output against a JSON schema."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import jsonschema

from evalguard_evaluators.base import EvalContext, Score


class JsonSchemaHeuristic:
    kind = "heuristic"
    layer = 1

    def __init__(self) -> None:
        self.id: str = "json_schema"
        self._schema: dict[str, Any] = {}

    def configure(self, cfg: dict[str, Any]) -> None:
        self.id = cfg.get("id", "json_schema")
        if "schema" in cfg:
            self._schema = cfg["schema"]
        elif "schema_file" in cfg:
            self._schema = json.loads(Path(cfg["schema_file"]).read_text())
        else:
            raise ValueError("json_schema heuristic needs 'schema' or 'schema_file'")

    async def evaluate(self, ctx: EvalContext) -> list[Score]:
        try:
            parsed = json.loads(ctx.output)
        except json.JSONDecodeError as e:
            return [Score(self.id, self.kind, self.layer, 0.0, False, {"error": f"invalid json: {e}"})]
        try:
            jsonschema.validate(parsed, self._schema)
        except jsonschema.ValidationError as e:
            return [Score(self.id, self.kind, self.layer, 0.0, False, {"error": e.message, "path": list(e.absolute_path)})]
        return [Score(self.id, self.kind, self.layer, 1.0, True, {})]
