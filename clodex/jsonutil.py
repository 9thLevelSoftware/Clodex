from __future__ import annotations

import json
import re
from typing import Any


class AgentEnvelopeError(ValueError):
    """The agent CLI reported an error inside its JSON result envelope."""


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
        if value.get("type") == "result":
            return unwrap_claude_envelope(value)
        if "result" in value and isinstance(value["result"], dict):
            return value["result"]
        if "content" in value and isinstance(value["content"], str):
            return extract_json_object(value["content"])
        if "message" in value and isinstance(value["message"], str):
            return extract_json_object(value["message"])
        return value
    raise ValueError("model output was not a JSON object")


def unwrap_claude_envelope(envelope: dict[str, Any]) -> dict[str, Any]:
    """Unwrap the `claude -p --output-format json` result envelope."""
    if envelope.get("is_error"):
        raise AgentEnvelopeError(f"agent returned an error result: {envelope.get('result')}")
    structured = envelope.get("structured_output")
    if isinstance(structured, dict):
        return structured
    result = envelope.get("result")
    if isinstance(result, dict):
        return result
    if isinstance(result, str):
        return extract_json_object(result)
    raise ValueError("agent result envelope had no usable result")
