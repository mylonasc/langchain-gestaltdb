"""Real persistence, ownership, parameters, and native result conversion."""

import json
from pathlib import Path

import pytest
from gestaltdb.graphdb import Edge, GraphDB, Node

from langchain_gestaltdb import GestaltDBGraph


def test_owned_lifecycle_and_native_results(tmp_path: Path) -> None:
    path = tmp_path / "graph"
    with GestaltDBGraph.create(path, backend="lmdb") as adapter:
        adapter.graph.put_nodes([Node("a", labels=["Person"]), Node("b")])
        adapter.graph.put_edge(Edge("e", "a", "b", properties={"type": "KNOWS"}))
        row = adapter.query("MATCH p=(a:Person)-[r]->(b) RETURN p,a,r")[0]
        assert row["a"] == {"id": "a", "labels": ["Person"], "properties": {}}
        assert row["r"]["source"] == "a"
        assert row["p"]["nodes"][1]["id"] == "b"
        json.dumps(row, allow_nan=False)
    adapter.close()
    with pytest.raises(RuntimeError, match="closed"):
        adapter.query("MATCH (n) RETURN n")
    with GestaltDBGraph.open(path) as reopened:
        query = "MATCH (n) WHERE n.id = $id RETURN n.id AS id"
        assert reopened.query(query, {"id": "a"}) == [{"id": "a"}]
        assert reopened.query(query, {"id": "' OR 1=1"}) == []
        assert reopened.query("MATCH (n) RETURN n.id AS id", read_snapshot=True)


def test_borrowed_handle_remains_open_after_context(tmp_path: Path) -> None:
    raw = GraphDB.create(tmp_path / "borrowed", backend="lmdb")
    try:
        with GestaltDBGraph(raw) as adapter:
            assert adapter.graph is raw
        assert raw.query("MATCH (n) RETURN n").records == []
        with pytest.raises(RuntimeError, match="closed"):
            adapter.__enter__()
    finally:
        raw.close()


def test_errors_propagate_and_backend_is_explicit(tmp_path: Path) -> None:
    with pytest.raises(TypeError, match="backend"):
        GestaltDBGraph.create(tmp_path)  # type: ignore[call-arg]
    with pytest.raises(TypeError, match="GraphDB"):
        GestaltDBGraph(object())
    with GestaltDBGraph.create(tmp_path / "errors", backend="lmdb") as adapter:
        with pytest.raises(ValueError):
            adapter.query("NOT CYPHER")
