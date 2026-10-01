"""JSON schemas for structured agent output, plus a small stdlib validator."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

SCHEMA_DIR = Path(__file__).resolve().parent / "schemas"
SCHEMA_NAMES = ("plan", "audit_verdict")


class SchemaValidationError(ValueError):
    """Agent output parsed as JSON but did not match the required schema."""


def schema_path(name: str) -> Path:
    """Path to a packaged schema file (Codex's --output-schema needs a real file)."""
    if name not in SCHEMA_NAMES:
        raise ValueError(f"Unknown schema: {name}")
    return SCHEMA_DIR / f"{name}.schema.json"


@lru_cache(maxsize=None)
def load_schema(name: str) -> dict[str, Any]:
    return json.loads(schema_path(name).read_text(encoding="utf-8"))


def compact_schema(name: str) -> str:
    """Single-line JSON for `claude --json-schema` (which only accepts inline JSON)."""
    return json.dumps(load_schema(name), separators=(",", ":"))


def validate(value: Any, schema: dict[str, Any], path: str = "$") -> list[str]:
    """Check type, enum, required and nested items/properties.

    Deliberately lenient about extra properties: the CLIs enforce the strict
    schema themselves, and an unexpected key is not worth a retry.
    """
    expected = schema.get("type")
    if expected is not None:
        types = expected if isinstance(expected, list) else [expected]
        if not any(_is_type(value, kind) for kind in types):
            return [f"{path}: expected {' or '.join(types)}, got {_type_name(value)}"]
    errors: list[str] = []
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: {value!r} is not one of {schema['enum']}")
    if isinstance(value, dict):
        properties = schema.get("properties", {})
        for key in schema.get("required", []):
            if key not in value:
                errors.append(f"{path}.{key}: missing required property")
        for key, subschema in properties.items():
            if key in value:
                errors.extend(validate(value[key], subschema, f"{path}.{key}"))
    elif isinstance(value, list) and "items" in schema:
        for index, item in enumerate(value):
            errors.extend(validate(item, schema["items"], f"{path}[{index}]"))
    return errors


def check(value: Any, name: str) -> None:
    errors = validate(value, load_schema(name))
    if errors:
        raise SchemaValidationError("; ".join(errors[:8]))


def _is_type(value: Any, kind: str) -> bool:
    if kind == "string":
        return isinstance(value, str)
    if kind == "boolean":
        return isinstance(value, bool)
    if kind == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if kind == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if kind == "array":
        return isinstance(value, list)
    if kind == "object":
        return isinstance(value, dict)
    if kind == "null":
        return value is None
    return True


def _type_name(value: Any) -> str:
    return "null" if value is None else type(value).__name__
