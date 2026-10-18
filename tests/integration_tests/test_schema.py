from pathlib import Path
from unittest.mock import patch

import pytest
from gestaltdb.graphdb import Edge, Node

from langchain_gestaltdb import GestaltDBGraph


def test_schema_empty_cache_refresh_and_defensive_copy(tmp_path: Path) -> None:
    with GestaltDBGraph.create(tmp_path / "g", backend="lmdb") as adapter:
        assert adapter.get_structured_schema["node_props"] == {}
        adapter.graph.put_nodes(
            [
                Node("a", labels=["Person", "Employee"], properties={"age": 2}),
                Node("b", labels=["Company"], properties={"active": True}),
            ]
        )
        adapter.graph.put_edge(Edge("e", "a", "b", properties={"type": "WORKS_FOR", "since": 2020}))
        assert adapter.get_structured_schema["node_props"] == {}
        adapter.refresh_schema()
        schema = adapter.get_structured_schema
        assert schema["node_props"]["Person"] == [{"property": "age", "type": "integer"}]
        assert {r["start"] for r in schema["relationships"]} == {"Person", "Employee"}
        assert schema["rel_props"]["WORKS_FOR"] == [{"property": "since", "type": "integer"}]
        schema["node_props"].clear()
        assert adapter.get_structured_schema["node_props"]
        assert "not all Neo4j" in adapter.get_schema


def test_limits_are_real_entity_read_bounds_and_override_bypasses_scan(tmp_path: Path) -> None:
    with GestaltDBGraph.create(tmp_path / "g", backend="lmdb") as owned:
        owned.graph.put_nodes([Node(str(i), labels=["N"]) for i in range(10)])
        limited = GestaltDBGraph(owned.graph, schema_node_limit=2, schema_edge_limit=1)
        with patch.object(owned.graph, "get_node", wraps=owned.graph.get_node) as get:
            schema = limited.get_structured_schema
            assert get.call_count == 2
        assert schema["metadata"]["truncated"] is True
        override = {"node_props": {"Trusted": []}}
        supplied = GestaltDBGraph(owned.graph, schema_override=override)
        override.clear()
        with patch.object(owned.graph, "get_node_keys_generator", side_effect=AssertionError):
            assert supplied.get_structured_schema == {"node_props": {"Trusted": []}}
        exhaustive = GestaltDBGraph(owned.graph, schema_node_limit=None)
        assert exhaustive.get_structured_schema["metadata"]["inspected_nodes"] == 10


def test_canonical_schema_does_not_use_stale_label_indexes(tmp_path: Path) -> None:
    with GestaltDBGraph.create(tmp_path / "g", backend="lmdb") as adapter:
        adapter.graph.put_node(Node("a", labels=["Person"]))
        with patch.object(adapter.graph, "iter_node_ids_by_label", side_effect=AssertionError):
            assert "Person" in adapter.get_structured_schema["node_props"]


@pytest.mark.parametrize("limit", [0, -1, True, 1.5])
def test_invalid_limits(tmp_path: Path, limit: object) -> None:
    with GestaltDBGraph.create(tmp_path / "g", backend="lmdb") as adapter:
        with pytest.raises(ValueError, match="Schema limits"):
            GestaltDBGraph(adapter.graph, schema_node_limit=limit)


def test_mixed_properties_unlabeled_nodes_and_bounded_endpoints(tmp_path: Path) -> None:
    with GestaltDBGraph.create(tmp_path / "g", backend="lmdb") as owned:
        owned.graph.put_nodes(
            [
                Node("a", labels=["N"], properties={"value": 1}),
                Node("b", labels=["N"], properties={"value": "mixed"}),
                Node("c"),
            ]
        )
        owned.graph.put_edge(Edge("e", "b", "c", properties={"type": "T"}))
        limited = GestaltDBGraph(owned.graph, schema_node_limit=1, schema_edge_limit=1)
        with patch.object(owned.graph, "get_node", wraps=owned.graph.get_node) as get:
            schema = limited.get_structured_schema
            assert get.call_count <= 3
        assert schema["node_props"]["N"] == [{"property": "value", "type": "integer | string"}]
        assert "__unlabeled__" in schema["node_props"]
        assert schema["rel_props"]["T"] == []
