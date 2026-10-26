"""Code-owned prompt anchors; editable values never contain the output contracts."""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=1)
def _rows() -> tuple[dict, ...]:
    return tuple(json.loads(Path(__file__).with_suffix(".json").read_text(encoding="utf-8")))


def entries() -> list[dict]:
    """Public catalogue, independent of the model SDK and backend imports."""
    return [
        {key: row[key] for key in ("id", "name", "purpose", "default", "source", "protected")}
        for row in _rows()
    ]


def defaults() -> dict[str, str]:
    return {row["id"]: row["default"] for row in _rows()}


def _value(row: dict) -> str:
    from .context import current_values

    value = current_values().get(row["id"], row["default"])
    if not isinstance(value, str):
        raise ValueError("prompt value must be text: %s" % row["id"])
    return value


def apply_role(original: str) -> str:
    for row in _rows():
        if row["kind"] == "role" and row["original"] == original:
            return _value(row) + original[len(row["default"]):]
    return original


def apply_task(message: str, label: str) -> str:
    """Replace only the leading known anchor, never matching text inside user data."""
    row = next((r for r in _rows() if r.get("label") == label), None)
    if row is None or row["kind"] == "supplement":
        return message
    value = _value(row)
    if value == row["default"]:
        return message
    if not message.startswith(row["default"]):
        raise ValueError("prompt task anchor changed: %s" % label)
    return value + message[len(row["default"]):]


def task_supplement(label: str) -> str:
    row = next((r for r in _rows() if r.get("label") == label), None)
    return _value(row) if row and row["kind"] == "supplement" else ""


def trial_spec(prompt_id: str) -> dict:
    """Resolve one isolated call without exposing filesystem paths from real tasks."""
    row = next((r for r in _rows() if r["id"] == prompt_id), None)
    if row is None:
        raise ValueError("unknown prompt_id")
    role = next(r for r in _rows() if r["kind"] == "role" and r["role"] == row["role"])
    return {
        "system_prompt": apply_role(role["original"]),
        "label": row.get("label"),
        "message": row.get("template", ""),
        "supplement": task_supplement(row.get("label", "")),
    }
