"""Bitemporal read-only Cypher coverage: qualifiers, version functions, fail-closed."""

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest
from gestaltdb.cypher import parse_ast
from gestaltdb.cypher_ast import (
    FunctionCall,
    MatchClause,
    Query,
    SubqueryClause,
    TemporalQualifier,
)
from gestaltdb.graphdb import IndexMaintenanceMode, Node
from gestaltdb.temporal import TemporalInstant, TemporalInterval

from langchain_gestaltdb import GestaltDBGraph, ReadOnlyQueryError
from langchain_gestaltdb._read_only import validate_read_only

VALID_LITERAL = 'datetime("1970-01-01T00:00:01Z")'
VALID_AT = datetime(1970, 1, 1, 0, 0, 1, tzinfo=timezone.utc)
QUALIFIED = f"MATCH (n:Person) FOR VALID_TIME AS OF {VALID_LITERAL} RETURN n"
QUALIFIED_NAME = f"MATCH (n:Person) FOR VALID_TIME AS OF {VALID_LITERAL} RETURN n.name AS name"


def _seed_versioned(path: Path, *, defer: bool = False) -> GestaltDBGraph:
    adapter = GestaltDBGraph.create(path, backend="lmdb")
    mode = IndexMaintenanceMode.DEFER if defer else IndexMaintenanceMode.MAINTAIN
    adapter.graph.put_node_version(
        Node("alice", labels=["Person"], properties={"name": "Alice"}),
        valid=TemporalInterval(TemporalInstant(0)),
        index_mode=mode,
    )
    return adapter


def test_valid_time_literal_executes_with_version_metadata(tmp_path: Path) -> None:
    with _seed_versioned(tmp_path / "g") as adapter:
        rows = adapter.query_read_only(
            f"MATCH (n:Person) FOR VALID_TIME AS OF {VALID_LITERAL} "
            "RETURN n.name AS name, versionId(n) AS vid, validFrom(n) AS vf, "
            "validTo(n) AS vt, systemFrom(n) AS sf"
        )
        assert [row["name"] for row in rows] == ["Alice"]
        assert isinstance(rows[0]["vid"], str)
        assert rows[0]["vf"] == {"$type": "TemporalInstant", "epoch_microseconds": 0}
        assert rows[0]["vt"] is None
        assert rows[0]["sf"]["$type"] == "TemporalInstant"


def test_valid_time_parameter_executes(tmp_path: Path) -> None:
    with _seed_versioned(tmp_path / "g") as adapter:
        rows = adapter.query_read_only(
            "MATCH (n:Person) FOR VALID_TIME AS OF $t RETURN n.name AS name", {"t": VALID_AT}
        )
        assert rows == [{"name": "Alice"}]


def test_system_time_with_valid_time_executes(tmp_path: Path) -> None:
    with _seed_versioned(tmp_path / "g") as adapter:
        rows = adapter.query_read_only(
            f"MATCH (n:Person) FOR VALID_TIME AS OF {VALID_LITERAL} "
            'FOR SYSTEM_TIME AS OF datetime("2100-01-01T00:00:00Z") '
            "RETURN n.name AS name"
        )
        assert rows == [{"name": "Alice"}]


def test_qualified_reads_in_subquery_and_union_execute(tmp_path: Path) -> None:
    with _seed_versioned(tmp_path / "g") as adapter:
        rows = adapter.query_read_only(
            f"CALL {{ {QUALIFIED} }} RETURN n.name AS name",
        )
        assert rows == [{"name": "Alice"}]
        rows = adapter.query_read_only(f"{QUALIFIED} UNION ALL {QUALIFIED}")
        assert [row["n"]["properties"]["name"] for row in rows] == ["Alice", "Alice"]


def test_version_functions_are_null_for_current_entities(tmp_path: Path) -> None:
    with GestaltDBGraph.create(tmp_path / "g", backend="lmdb") as adapter:
        adapter.graph.put_node(Node("a", labels=["Person"], properties={"name": "Alice"}))
        rows = adapter.query_read_only(
            "MATCH (n:Person) RETURN n.name AS name, versionId(n) AS vid, "
            "validFrom(n) AS vf, validTo(n) AS vt, systemFrom(n) AS sf"
        )
        assert rows == [{"name": "Alice", "vid": None, "vf": None, "vt": None, "sf": None}]


def test_deferred_temporal_index_fail_closed_then_rebuild(tmp_path: Path) -> None:
    with _seed_versioned(tmp_path / "g", defer=True) as adapter:
        assert "temporal" in adapter.graph.stale_indexes()
        with pytest.raises(RuntimeError, match="stale"):
            adapter.query_read_only(QUALIFIED_NAME)
        adapter.graph.rebuild_temporal_indexes()
        assert adapter.graph.stale_indexes() == ()
        rows = adapter.query_read_only(QUALIFIED_NAME)
        assert rows == [{"name": "Alice"}]


@pytest.mark.parametrize(
    "query",
    [
        f"{QUALIFIED} SET n.x = 1 RETURN n",
        f"{QUALIFIED} DELETE n RETURN n",
        f"{QUALIFIED} MERGE (m:Bad) RETURN m",
        f"CREATE (n:Bad) FOR VALID_TIME AS OF {VALID_LITERAL} RETURN n",
        f"MATCH (n:Person) FOR SYSTEM_TIME AS OF {VALID_LITERAL} RETURN n",
    ],
)
def test_qualified_writes_and_system_without_valid_rejected(tmp_path: Path, query: str) -> None:
    with _seed_versioned(tmp_path / "g") as adapter:
        before = adapter.query("MATCH (n) RETURN n")
        with pytest.raises(ReadOnlyQueryError):
            adapter.query_read_only(query)
        assert adapter.query("MATCH (n) RETURN n") == before


def test_system_without_valid_hand_built_ast_rejected() -> None:
    both = parse_ast(
        f"MATCH (n) FOR VALID_TIME AS OF {VALID_LITERAL} "
        f"FOR SYSTEM_TIME AS OF {VALID_LITERAL} RETURN n"
    )
    system_only = MatchClause(
        patterns=both.clauses[0].patterns,
        qualifiers=tuple(q for q in both.clauses[0].qualifiers if q.kind == "system"),
    )
    assert len(system_only.qualifiers) == 1
    combined = Query(clauses=(system_only, both.clauses[1]), source="")
    with patch("langchain_gestaltdb._read_only.parse_ast", return_value=combined):
        with pytest.raises(ReadOnlyQueryError, match="SYSTEM_TIME"):
            validate_read_only("MATCH (n) RETURN n")


def test_temporal_write_hand_built_ast_reports_cannot_write() -> None:
    write = parse_ast("MATCH (n) SET n.x = 1 RETURN n")
    qualified = parse_ast(QUALIFIED)
    combined = Query(clauses=(qualified.clauses[0], write.clauses[1]), source="")
    with patch("langchain_gestaltdb._read_only.parse_ast", return_value=combined):
        with pytest.raises(ReadOnlyQueryError, match="cannot write"):
            validate_read_only("MATCH (n) RETURN n")


@pytest.mark.parametrize(
    ("qualifiers", "match"),
    [
        ((TemporalQualifier(kind="frozen", expression=None),), "not read-only"),
        ((object(),), "Unsupported temporal qualifier"),
    ],
)
def test_unknown_temporal_qualifiers_fail_closed(qualifiers: object, match: str) -> None:
    clause = MatchClause(patterns=(), qualifiers=qualifiers)  # type: ignore[arg-type]
    with patch(
        "langchain_gestaltdb._read_only.parse_ast",
        return_value=Query(clauses=(clause,), source=""),
    ):
        with pytest.raises(ReadOnlyQueryError, match=match):
            validate_read_only("MATCH (n) RETURN n")


def test_deeply_nested_qualifier_expression_rejected() -> None:
    expression: object = "x"
    for _ in range(34):
        expression = FunctionCall(name="f", arguments=(expression,), distinct=False)
    clause = MatchClause(
        patterns=(), qualifiers=(TemporalQualifier(kind="valid", expression=expression),)
    )
    with patch(
        "langchain_gestaltdb._read_only.parse_ast",
        return_value=Query(clauses=(clause,), source=""),
    ):
        with pytest.raises(ReadOnlyQueryError, match="nesting"):
            validate_read_only("MATCH (n) RETURN n")


def test_clause_nesting_still_bound_when_collection_passes() -> None:
    root = Query(clauses=(), source="")
    for _ in range(34):
        root = Query(clauses=(SubqueryClause(root),), source="")
    with (
        patch("langchain_gestaltdb._read_only.parse_ast", return_value=root),
        patch("langchain_gestaltdb._read_only._collect_temporal_kinds", return_value=None),
    ):
        with pytest.raises(ReadOnlyQueryError, match="nesting"):
            validate_read_only("SHOW INDEXES")
