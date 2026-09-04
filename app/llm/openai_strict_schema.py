"""OpenAI structured-output schemas require additionalProperties: false on every object.

Pydantic unions (anyOf) and dict[str, Any] omit that flag and the API returns 400.
"""

from __future__ import annotations

from typing import Any


def apply_openai_strict_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Mutate a JSON Schema so OpenAI strict structured outputs accept it."""
    _strictify(schema)
    return schema


def _strictify(node: Any) -> None:
    if isinstance(node, list):
        for item in node:
            _strictify(item)
        return
    if not isinstance(node, dict):
        return
    if node.get("type") == "object" or "properties" in node:
        properties = node.get("properties")
        if isinstance(properties, dict) and properties:
            node["additionalProperties"] = False
            node["required"] = list(properties.keys())
            for child in properties.values():
                _strictify(child)
        elif node.get("type") == "object" and not properties:
            # Unconstrained dict → string so the schema stays closed.
            node.clear()
            node["type"] = "string"
    for key in ("$defs", "definitions"):
        defs = node.get(key)
        if isinstance(defs, dict):
            for child in defs.values():
                _strictify(child)
    for key in ("anyOf", "oneOf", "allOf"):
        branch = node.get(key)
        if isinstance(branch, list):
            for child in branch:
                _strictify(child)
    items = node.get("items")
    if items is not None:
        _strictify(items)
    prefix = node.get("prefixItems")
    if isinstance(prefix, list):
        for child in prefix:
            _strictify(child)
