"""Wire format shared between the parent process and the sandbox bootstrap.

Submitted code can return *anything*.  This module defines how such values are
made safe to transport (strict JSON, no ``NaN``, no out-of-range integers) and
how they are turned back into something a human can read in the dashboard.
"""

from __future__ import annotations

import math
from typing import Any

SPECIAL_FLOAT = "__arena_float__"
BIG_INT = "__arena_bigint__"
UNSERIALIZABLE = "__arena_unserializable__"
BYTES = "__arena_bytes__"
SET = "__arena_set__"

_NAN = float("nan")
_POS_INF = float("inf")
_NEG_INF = float("-inf")


def decode(value: Any) -> Any:
    """Turn the wire format produced by ``bootstrap.to_jsonable`` back into
    plain Python values, so comparisons see real ``nan`` / ``inf`` / big ints."""
    if isinstance(value, dict):
        if len(value) == 1:
            if SPECIAL_FLOAT in value:
                name = value[SPECIAL_FLOAT]
                return {"nan": _NAN, "inf": _POS_INF, "-inf": _NEG_INF}.get(name, _NAN)
            if BIG_INT in value:
                return int(value[BIG_INT])
            if BYTES in value:
                try:
                    return bytes.fromhex(value[BYTES])
                except ValueError:
                    return value[BYTES]
            if UNSERIALIZABLE in value:
                return value.get("repr", "<unrepresentable>")
        if SET in value:
            return list(value[SET])
        return {key: decode(item) for key, item in value.items()}
    if isinstance(value, list):
        return [decode(item) for item in value]
    return value


def describe(value: Any, limit: int = 600) -> str:
    """Human-readable, single-line rendering used in mismatch reports."""
    text = _describe(value, 0)
    if len(text) > limit:
        text = text[: limit - 1] + "\u2026"
    return text


def _describe(value: Any, depth: int) -> str:
    if depth > 12:
        return "\u2026"
    if value is None:
        return "None"
    if isinstance(value, bool):
        return "True" if value else "False"
    if isinstance(value, float):
        if math.isnan(value):
            return "NaN"
        if math.isinf(value):
            return "Infinity" if value > 0 else "-Infinity"
        if value == int(value) and abs(value) < 1e16:
            return f"{value:.1f}"
        return repr(value)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        return _quote(value)
    if isinstance(value, bytes):
        preview = value[:64]
        suffix = "\u2026" if len(value) > 64 else ""
        return f"b{preview!r}{suffix}"
    if isinstance(value, dict):
        items = list(value.items())[:20]
        inner = ", ".join(f"{_describe(k, depth + 1)}: {_describe(v, depth + 1)}" for k, v in items)
        if len(value) > 20:
            inner += f", \u2026 {len(value) - 20} more"
        return "{" + inner + "}"
    if isinstance(value, (list, tuple)):
        open_b, close_b = ("[", "]") if isinstance(value, list) else ("(", ")")
        inner = ", ".join(_describe(item, depth + 1) for item in list(value)[:20])
        if len(value) > 20:
            inner += f", \u2026 {len(value) - 20} more"
        return open_b + inner + close_b
    with_repr = repr(value)
    return with_repr[:200]


def _quote(text: str) -> str:
    if len(text) > 120:
        text = text[:117] + "\u2026"
    escaped = text.replace("\\", "\\\\").replace("\n", "\\n").replace("\t", "\\t").replace('"', '\\"')
    return f'"{escaped}"'


def preview(value: Any, limit: int = 200_000) -> Any:
    """Shrink very large results so the API response stays a sane size."""
    if isinstance(value, list) and len(value) > 5000:
        return {
            "__arena_truncated_list__": True,
            "length": len(value),
            "head": preview(value[:200], limit),
        }
    if isinstance(value, dict) and len(value) > 5000:
        head = dict(list(value.items())[:200])
        return {
            "__arena_truncated_dict__": True,
            "length": len(value),
            "head": preview(head, limit),
        }
    if isinstance(value, str) and len(value) > limit:
        return value[:limit] + f"\u2026 [truncated, {len(value)} characters]"
    if isinstance(value, list):
        return [preview(item, limit) for item in value[:5000]]
    if isinstance(value, dict):
        return {key: preview(item, limit) for key, item in list(value.items())[:5000]}
    return value
