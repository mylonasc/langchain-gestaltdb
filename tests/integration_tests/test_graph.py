"""Real persistence, ownership, parameters, and native result conversion."""

import json
from pathlib import Path
from unittest.mock import patch

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
        assert reopened.graph.manifest["backend"]["name"] == "lmdb"
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


def test_errors_propagate(tmp_path: Path) -> None:
    with pytest.raises(TypeError, match="GraphDB"):
        GestaltDBGraph(object())
    with GestaltDBGraph.create(tmp_path / "errors", backend="lmdb") as adapter:
        with pytest.raises(ValueError):
            adapter.query("NOT CYPHER")


def test_default_pyrex_creation_and_manifest_reopen(tmp_path: Path) -> None:
    path = tmp_path / "default"
    with GestaltDBGraph.create(path) as graph:
        assert graph.graph.manifest["backend"]["name"] == "pyrex"
        graph.query("CREATE (n:Person {id: 'alice'}) RETURN n")
        assert graph.query_read_only("MATCH (n:Person) RETURN n.id AS id") == [{"id": "alice"}]
    with GestaltDBGraph.open(path) as reopened:
        assert reopened.graph.manifest["backend"]["name"] == "pyrex"
        assert reopened.query_read_only("MATCH (n:Person) RETURN n.id AS id") == [{"id": "alice"}]


def test_missing_default_backend_does_not_fall_back(tmp_path: Path) -> None:
    with patch.object(
        GraphDB, "create", side_effect=ImportError("pyrex-rocksdb missing")
    ) as create:
        with pytest.raises(ImportError, match="pyrex-rocksdb missing"):
            GestaltDBGraph.create(tmp_path / "g")
    create.assert_called_once_with(tmp_path / "g", backend="pyrex", serializer="json")
