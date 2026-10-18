"""Strict JSON-compatible normalization at the database boundary."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import fields
from datetime import date, datetime, time
from typing import Any

from gestaltdb.graphdb import Edge, Node
from gestaltdb.query_engine.cypher.ast import PathValue
from gestaltdb.temporal import (
    TemporalDate,
    TemporalDuration,
    TemporalInstant,
    TemporalInterval,
    TemporalLocalDateTime,
    TemporalLocalTime,
    TemporalTime,
)

_TEMPORAL = (
    TemporalDate,
    TemporalDuration,
    TemporalInstant,
    TemporalInterval,
    TemporalLocalDateTime,
    TemporalLocalTime,
    TemporalTime,
)


def normalize(value: Any, *, _active: set[int] | None = None, _depth: int = 0) -> Any:
    """Copy supported values to JSON values; reject lossy/unstable coercions.

    Temporal dataclasses use a ``$type`` tag plus their exact fields. UTF-8
    bytes decode strictly. Unknown values, non-string keys, cycles, non-finite
    floats and excessive depth raise rather than silently losing information.
    """
    if _depth > 64:
        raise ValueError("Query result exceeds the maximum nesting depth (64)")
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("Non-finite floats are not JSON-compatible")
        return value
    if isinstance(value, bytes):
        return value.decode("utf-8")
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    active = set() if _active is None else _active
    identity = id(value)
    if identity in active:
        raise ValueError("Cyclic query result")
    active.add(identity)
    try:
        if isinstance(value, (Node, Edge)):
            value = value.to_dict()
        elif isinstance(value, PathValue):
            value = {"nodes": value.nodes, "edges": value.edges}
        elif isinstance(value, _TEMPORAL):
            value = {
                "$type": type(value).__name__,
                **{f.name: getattr(value, f.name) for f in fields(value)},
            }
        if isinstance(value, Mapping):
            if not all(isinstance(key, str) for key in value):
                raise TypeError("Query result maps must have string keys")
            return {
                key: normalize(item, _active=active, _depth=_depth + 1)
                for key, item in value.items()
            }
        if isinstance(value, (tuple, list)):
            return [normalize(item, _active=active, _depth=_depth + 1) for item in value]
        raise TypeError(f"Unsupported query result type: {type(value).__name__}")
    finally:
        active.remove(identity)
