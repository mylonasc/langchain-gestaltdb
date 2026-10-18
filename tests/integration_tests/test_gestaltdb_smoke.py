"""Exercise the installed database dependency, not a mock adapter."""

from pathlib import Path

import pytest
from gestaltdb import agent_docs
from gestaltdb.graphdb import Edge, GraphDB, Node


@pytest.mark.integration
def test_parameterized_query_survives_reopen(tmp_path: Path) -> None:
    path = tmp_path / "graph"
    graph = GraphDB.create(path, backend="lmdb", serializer="json")
    try:
        graph.put_nodes(
            [
                Node("alice", labels=["Person"], properties={"name": "Alice"}),
                Node("bob", labels=["Person"], properties={"name": "Bob"}),
            ]
        )
        graph.put_edge(Edge("knows-1", "alice", "bob", properties={"type": "KNOWS"}))
    finally:
        graph.close()

    reopened = GraphDB.open(path)
    try:
        result = reopened.query(
            "MATCH (a:Person {name: $name})-[:KNOWS]->(b) RETURN b.name AS name",
            parameters={"name": "Alice"},
        )
        assert result.records == [{"name": "Bob"}]
        assert tuple(result.columns) == ("name",)
    finally:
        reopened.close()


def test_installed_agent_guides_are_available() -> None:
    assert "GraphDB" in agent_docs.read_topic("quickstart")
    assert agent_docs.read_api_index()["methods"]
