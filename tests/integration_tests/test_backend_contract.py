"""The same public contract runs against each explicitly installed CI backend."""

import json
import os
from pathlib import Path

import pytest

from langchain_gestaltdb import (
    GestaltDBClaimStatusTool,
    GestaltDBClaimToolkit,
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


def test_backend_claim_status_and_evidence(tmp_path: Path) -> None:
    backend = os.environ.get("GESTALTDB_TEST_BACKEND", "lmdb")
    with GestaltDBGraph.create(tmp_path / "claims", backend=backend) as graph:
        graph.graph.assert_claim(
            subject="alice",
            predicate="WORKS_FOR",
            object="acme",
            polarity="positive",
            agent="hr",
            source="hr.csv",
            world="reported",
            valid_from="2024-01-01T00:00:00Z",
        )
        output = json.loads(
            GestaltDBClaimStatusTool(graph=graph).invoke(
                {
                    "subject": "alice",
                    "predicate": "WORKS_FOR",
                    "object": "acme",
                    "world": "reported",
                    "valid_time": "2024-06-01T00:00:00Z",
                }
            )
        )
        assert output["status"] == "supported"
        assert output["evidence"][0]["claim"]["source"] == "hr.csv"
        assert graph.query_read_only("MATCH (n) RETURN n") == []


def test_backend_claim_writes_are_append_only(tmp_path: Path) -> None:
    backend = os.environ.get("GESTALTDB_TEST_BACKEND", "lmdb")
    with GestaltDBGraph.create(tmp_path / "claims-write", backend=backend) as graph:
        write_tools = {
            tool.name: tool
            for tool in GestaltDBClaimToolkit(graph=graph, allow_writes=True).get_tools()
        }
        assert len(GestaltDBClaimToolkit(graph=graph).get_tools()) == 3
        asserted = json.loads(
            write_tools["gestaltdb_claim_assert"].invoke(
                {
                    "subject": "alice",
                    "predicate": "WORKS_FOR",
                    "object": "acme",
                    "polarity": "positive",
                    "agent": "hr",
                    "provenance": {"document": "hr.csv"},
                    "valid_from": "2024-01-01T00:00:00Z",
                }
            )
        )
        corrected = json.loads(
            write_tools["gestaltdb_claim_correct"].invoke(
                {
                    "claim_id": asserted["logical_id"],
                    "supersedes_version_id": asserted["version_id"],
                    "confidence": 0.9,
                }
            )
        )
        assert corrected["logical_id"] == asserted["logical_id"]
        assert corrected["supersedes_version_id"] == asserted["version_id"]
        assert corrected["claim"]["provenance"] == {"document": "hr.csv"}
