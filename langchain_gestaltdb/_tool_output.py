"""Whole-record JSON output bounds, including response metadata."""

from __future__ import annotations

import json
from typing import Any


def bounded_json(
    result: dict[str, Any],
    limits: tuple[tuple[str, str, int], ...],
    max_chars: int,
) -> str:
    """Cap lists, then trim them in priority order until the envelope fits.

    Each limit is (list key, returned-count key, row cap). Metadata is never
    silently clipped: if it alone exceeds the budget, return an explicit error.
    This bounds output only, not database work or intermediate materialization.
    """
    for key, count, cap in limits:
        if len(result[key]) > cap:
            result["truncated"] = True
        result[key] = result[key][:cap]
        result[count] = len(result[key])
    encoded = json.dumps(result, ensure_ascii=False)
    for key, count, _ in limits:
        while len(encoded) > max_chars and result[key]:
            result[key].pop()
            result[count] = len(result[key])
            result["truncated"] = True
            encoded = json.dumps(result, ensure_ascii=False)
    if len(encoded) > max_chars:
        return json.dumps(
            {
                "error": "Response metadata exceeds max_chars; increase the configured cap.",
                "truncated": True,
            }
        )
    return encoded
