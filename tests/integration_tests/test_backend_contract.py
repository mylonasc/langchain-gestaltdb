"""The same public contract runs against each explicitly installed CI backend."""

import json
import os
from pathlib import Path

import pytest

from langchain_gestaltdb import (
    GestaltDBGraph,
    GestaltDBToolkit,
    GraphDocument,
    GraphNode,
    GraphRelationship,
    ReadOnlyQueryError,
)


def test_backend_ingestion_schema_tool_and_reopen(tmp_path: Path) -> None:
    backend = os.environ.get("GESTALTDB_TEST_BACKEND", "lmdb")
    path = tmp_path / "graph"
    a, b = GraphNode(id="a", type="Person"), GraphNode(id="b", type="Company")
    document = GraphDocument(
        nodes=[a, b], relationships=[GraphRelationship(source=a, target=b, type="WORKS_FOR")]
    )
    with GestaltDBGraph.create(path, backend=backend) as graph:
        graph.add_graph_documents([document, document])
        assert len(list(graph.graph.iter_edge_ids())) == 1
        assert graph.get_structured_schema["relationships"] == [
            {"start": "Person", "type": "WORKS_FOR", "end": "Company"}
        ]
        _, tool = GestaltDBToolkit(graph=graph).get_tools()
        output = json.loads(tool.invoke({"query": "MATCH (a)-[:WORKS_FOR]->(b) RETURN a.id AS id"}))
        assert output["records"] == [{"id": "a"}]
        with pytest.raises(ReadOnlyQueryError):
            graph.query_read_only("MATCH (n) DETACH DELETE n")
    with GestaltDBGraph.open(path) as reopened:
        assert len(reopened.query_read_only("MATCH (n) RETURN n")) == 2
