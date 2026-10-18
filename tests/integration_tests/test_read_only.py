from pathlib import Path
from unittest.mock import patch

import pytest
from gestaltdb.cypher_ast import Query, SubqueryClause
from gestaltdb.graphdb import Node

from langchain_gestaltdb import GestaltDBGraph, ReadOnlyQueryError
from langchain_gestaltdb._read_only import validate_read_only

MUTATIONS = [
    "CREATE (n:Bad) RETURN n",
    "MATCH (n) SET n.x = 1 RETURN n",
    "MATCH (n) REMOVE n.name RETURN n",
    "MATCH (n) DELETE n RETURN n",
    "MATCH (n) DETACH DELETE n RETURN n",
    "MERGE (n:Bad {id:'x'}) RETURN n",
    "UNWIND [1] AS x FOREACH (y IN [x] | CREATE (n:Bad)) RETURN x",
    "CREATE CONSTRAINT unique_name FOR (n:Person) REQUIRE n.name IS UNIQUE",
    "DROP CONSTRAINT unique_name",
    "CALL { MATCH (n) SET n.x = 1 RETURN n } RETURN n",
    "CALL { CALL { MATCH (n) DELETE n RETURN n } RETURN n } RETURN n",
    "MATCH (n) RETURN n UNION MATCH (n) SET n.x=1 RETURN n",
    "MATCH (n) RETURN n; CREATE (m:Bad)",
    "/* looks harmless */ MATCH (n) SET n.x=1 RETURN n",
]


@pytest.mark.parametrize("query", MUTATIONS)
def test_rejected_queries_never_execute_or_change_state(tmp_path: Path, query: str) -> None:
    path = tmp_path / "g"
    with GestaltDBGraph.create(path, backend="lmdb") as adapter:
        adapter.graph.put_node(Node("a", labels=["Person"], properties={"name": "Alice"}))
        before = adapter.query("MATCH (n) RETURN n")
        constraints = adapter.query("SHOW CONSTRAINTS")
        with patch.object(adapter.graph, "query", side_effect=AssertionError("must not execute")):
            with pytest.raises(ReadOnlyQueryError):
                adapter.query_read_only(query)
        assert adapter.query("MATCH (n) RETURN n") == before
        assert adapter.query("SHOW CONSTRAINTS") == constraints
    with GestaltDBGraph.open(path) as reopened:
        assert reopened.query("MATCH (n) RETURN n") == before


@pytest.mark.parametrize(
    "query",
    [
        "MATCH (n) RETURN n",
        "MATCH (n) WHERE n.name = 'DELETE SET CREATE' RETURN n",
        "MATCH (n) WITH n WHERE n.name IS NOT NULL RETURN n.name AS name",
        "CALL { MATCH (n) RETURN n } RETURN n",
        "MATCH (n) RETURN n UNION ALL MATCH (n) RETURN n",
        "UNWIND [1,2] AS x RETURN x",
        "SHOW INDEXES",
        "SHOW CONSTRAINTS",
    ],
)
def test_read_queries_are_accepted(tmp_path: Path, query: str) -> None:
    with GestaltDBGraph.create(tmp_path / "g", backend="lmdb") as adapter:
        adapter.graph.put_node(Node("a", properties={"name": "DELETE SET CREATE"}))
        assert isinstance(adapter.query_read_only(query), list)


def test_parameters_are_not_interpreted_as_cypher(tmp_path: Path) -> None:
    with GestaltDBGraph.create(tmp_path / "g", backend="lmdb") as adapter:
        value = "'; DETACH DELETE n; //"
        adapter.graph.put_node(Node("a", properties={"name": value}))
        assert adapter.query_read_only(
            "MATCH (n) WHERE n.name=$name RETURN n.id AS id", {"name": value}
        ) == [{"id": "a"}]


def test_procedure_allowlist_and_unknown_ast_are_fail_closed() -> None:
    query = "CALL pg.sample_typed_paths($seeds,$pattern) YIELD path RETURN path"
    with pytest.raises(ReadOnlyQueryError, match="allowlisted"):
        validate_read_only(query)
    validate_read_only(query, ["pg.sample_typed_paths"])
    with pytest.raises(ReadOnlyQueryError):
        validate_read_only("CALL apoc.cypher.run('CREATE (n)', {}) YIELD value RETURN value")
    with pytest.raises(ReadOnlyQueryError, match="known"):
        validate_read_only("SHOW INDEXES", ["apoc.cypher.run"])
    with patch("langchain_gestaltdb._read_only.parse_ast", return_value=object()):
        with pytest.raises(ReadOnlyQueryError, match="Unsupported"):
            validate_read_only("SHOW INDEXES")


@pytest.mark.parametrize("query", ["", " ", "MATCH " + "x" * 10000])
def test_text_limits(query: str) -> None:
    with pytest.raises(ReadOnlyQueryError):
        validate_read_only(query)


def test_ast_nesting_and_unknown_clauses_fail_closed() -> None:
    root = Query(clauses=(), source="")
    for _ in range(34):
        root = Query(clauses=(SubqueryClause(root),), source="")
    with patch("langchain_gestaltdb._read_only.parse_ast", return_value=root):
        with pytest.raises(ReadOnlyQueryError, match="nesting"):
            validate_read_only("SHOW INDEXES")
    with patch("langchain_gestaltdb._read_only.parse_ast", return_value=Query((object(),), "")):
        with pytest.raises(ReadOnlyQueryError, match="Clause"):
            validate_read_only("SHOW INDEXES")
