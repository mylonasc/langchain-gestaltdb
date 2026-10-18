import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from gestaltdb.graphdb import Node

from langchain_gestaltdb import GestaltDBGraph, GestaltDBSchemaTool


def test_executable_offline_example() -> None:
    example = Path(__file__).resolve().parents[2] / "examples" / "graph_agent.py"
    result = subprocess.run(
        [sys.executable, str(example)], check=True, capture_output=True, text=True
    )
    answer = json.loads(result.stdout)
    assert answer["records"] == [
        {
            "person": "Alice",
            "employer": "Acme",
            "evidence": "Alice works for Acme.",
            "provenance": {"source": "example:hr-record"},
        }
    ]


def test_real_deferred_indexes_schema_bypass_and_query_rebuild(tmp_path: Path) -> None:
    with GestaltDBGraph.create(tmp_path / "g", backend="lmdb") as graph:
        node = Node("a", labels=["Deferred"], properties={"name": "Alice"})
        payload = graph.graph.entity_serializer.serialize(node, "Node")
        graph.graph.ingest_nodes_arrow(["a"], [payload], native=False, index_mode="defer")
        assert "node_label" in graph.graph.stale_indexes()
        assert "Deferred" in graph.get_structured_schema["node_props"]
        with pytest.raises(RuntimeError, match="stale"):
            graph.query_read_only("MATCH (n:Deferred) RETURN n")
        graph.graph.rebuild_deferred_indexes()
        assert graph.query_read_only("MATCH (n:Deferred) RETURN n.id AS id") == [{"id": "a"}]


def test_schema_tool_refresh_and_redacted_failure(tmp_path: Path) -> None:
    with GestaltDBGraph.create(tmp_path / "g", backend="lmdb") as graph:
        tool = GestaltDBSchemaTool(graph=graph)
        assert json.loads(tool.invoke({}))["node_props"] == {}
        graph.graph.put_node(Node("a", labels=["New"]))
        assert json.loads(tool.invoke({}))["node_props"] == {}
        assert "New" in json.loads(tool.invoke({"refresh": True}))["node_props"]
        with patch.object(graph, "refresh_schema", side_effect=RuntimeError("secret path")):
            output = tool.invoke({"refresh": True})
            assert "failed" in output and "secret path" not in output


def test_native_temporal_results_and_snapshot_write_rejection(tmp_path: Path) -> None:
    with GestaltDBGraph.create(tmp_path / "g", backend="lmdb") as graph:
        graph.graph.put_node(Node("a"))
        result = graph.query_read_only(
            "MATCH (n) RETURN datetime('2024-01-01T00:00:00Z') AS instant, "
            "date('2024-01-01') AS date, duration('P1D') AS duration"
        )[0]
        assert result["instant"]["$type"] == "TemporalInstant"
        assert result["date"] == {"$type": "TemporalDate", "value": "2024-01-01"}
        assert result["duration"]["total_microseconds"] == 86_400_000_000
        json.dumps(result, allow_nan=False)
        with pytest.raises(ValueError, match="writes"):
            graph.query("CREATE (n:Bad) RETURN n", read_snapshot=True)
        assert graph.query("MATCH (n:Bad) RETURN n") == []


def test_invalid_backend_does_not_silently_fall_back(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        GestaltDBGraph.create(tmp_path / "g", backend="not-a-backend")
