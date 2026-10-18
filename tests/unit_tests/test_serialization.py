"""Serialization preserves supported fields and rejects unstable coercions."""

import json
from datetime import date, datetime, time

import pytest
from gestaltdb.graphdb import Node
from gestaltdb.temporal import (
    TemporalDate,
    TemporalDuration,
    TemporalInstant,
    TemporalInterval,
    TemporalLocalDateTime,
    TemporalLocalTime,
    TemporalTime,
)

from langchain_gestaltdb._serialization import normalize


@pytest.mark.parametrize(
    "value",
    [
        None,
        True,
        2,
        1.5,
        "text",
        b"utf8",
        date(2024, 1, 1),
        time(12),
        datetime(2024, 1, 1),
    ],
)
def test_scalars_are_json_safe(value: object) -> None:
    json.dumps(normalize(value), allow_nan=False)


@pytest.mark.parametrize(
    "value",
    [
        TemporalDate(date(2024, 1, 1)),
        TemporalDuration(123),
        TemporalInstant(0),
        TemporalInterval(TemporalInstant(0)),
        TemporalLocalDateTime(datetime(2024, 1, 1)),
        TemporalLocalTime(1),
        TemporalTime(1, 3600),
    ],
)
def test_temporal_fields_are_tagged_and_exact(value: object) -> None:
    result = normalize(value)
    assert result["$type"] == type(value).__name__
    json.dumps(result, allow_nan=False)


def test_aliases_are_allowed_but_cycles_are_not() -> None:
    node = Node("a", properties={"nested": [b"b"]})
    result = normalize([node, node])
    assert result[0] == result[1]
    result[0]["properties"]["nested"].append("new")
    assert node.properties == {"nested": [b"b"]}
    cyclic: list[object] = []
    cyclic.append(cyclic)
    with pytest.raises(ValueError, match="Cyclic"):
        normalize(cyclic)


@pytest.mark.parametrize(
    "value",
    [
        float("nan"),
        float("inf"),
        {1: "not a string key"},
        object(),
        b"\xff",
    ],
)
def test_unsupported_values_fail(value: object) -> None:
    with pytest.raises((ValueError, TypeError, UnicodeError)):
        normalize(value)


def test_depth_is_bounded() -> None:
    nested: object = 1
    for _ in range(70):
        nested = [nested]
    with pytest.raises(ValueError, match="depth"):
        normalize(nested)
