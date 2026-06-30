from __future__ import annotations

import json
import re
from typing import Any


def extract_json_object(text: str) -> dict[str, Any]:
    stripped = text.strip()
    if not stripped:
        raise ValueError("empty model output")

    try:
        value = json.loads(stripped)
        return normalize_json_value(value)
    except json.JSONDecodeError:
        pass

    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", stripped, re.DOTALL)
    if fenced:
        return normalize_json_value(json.loads(fenced.group(1)))

    start = stripped.find("{")
    end = stripped.rfind("}")
    if start >= 0 and end > start:
        return normalize_json_value(json.loads(stripped[start : end + 1]))

    raise ValueError("no JSON object found in model output")


def normalize_json_value(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        if "result" in value and isinstance(value["result"], dict):
            return value["result"]
        if "content" in value and isinstance(value["content"], str):
            return extract_json_object(value["content"])
        if "message" in value and isinstance(value["message"], str):
            return extract_json_object(value["message"])
        return value
    raise ValueError("model output was not a JSON object")
