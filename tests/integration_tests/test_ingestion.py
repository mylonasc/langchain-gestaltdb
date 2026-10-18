from pathlib import Path
from unittest.mock import patch

import pytest
from langchain_core.documents import Document
from pydantic import BaseModel, ValidationError

from langchain_gestaltdb import GestaltDBGraph, GraphDocument, GraphNode, GraphRelationship
from langchain_gestaltdb._ingestion import entity_id


def fact(*, properties: dict | None = None, edge_id: str | None = None) -> GraphDocument:
    alice = GraphNode(id="alice", type="Person", properties={"name": "Alice"})
    bob = GraphNode(id="bob", type="Person")
    return GraphDocument(
        nodes=[alice, bob],
        relationships=[
            GraphRelationship(
                source=alice,
                target=bob,
                type="KNOWS",
                properties=properties or {},
                id=edge_id,
            )
        ],
        source=Document(page_content="Alice knows Bob", metadata={"url": "local.txt"}),
    )


def test_repeated_ingestion_sources_and_parallel_edges(tmp_path: Path) -> None:
    with GestaltDBGraph.create(tmp_path / "g", backend="lmdb") as adapter:
        document = fact(properties={"since": 2020})
        with patch.object(adapter.graph, "put_edges_bulk", side_effect=AssertionError):
            adapter.add_graph_documents([document], include_source=True)
            adapter.add_graph_documents([document], include_source=True)
        assert len(list(adapter.graph.iter_edge_ids())) == 3
        rows = adapter.query("MATCH (s:__LangChainSource__)-[:MENTIONS]->(n) RETURN s,n")
        assert {r["n"]["id"] for r in rows} == {"alice", "bob"}
        assert rows[0]["s"]["properties"]["metadata"] == {"url": "local.txt"}
        assert rows[0]["s"]["properties"]["page_content"] == "Alice knows Bob"
        adapter.add_graph_documents([fact(properties={"since": 2021})])
        assert len(adapter.query("MATCH ()-[r:KNOWS]->() RETURN r")) == 2
        assert document.relationships[0].properties == {"since": 2020}


def test_explicit_edge_updates_and_node_label_property_merges(tmp_path: Path) -> None:
    with GestaltDBGraph.create(tmp_path / "g", backend="lmdb") as adapter:
        adapter.add_graph_documents([fact(properties={"since": 2020}, edge_id="e")])
        assert "Person" in adapter.get_structured_schema["node_props"]
        updated = fact(properties={"since": 2022}, edge_id="e")
        updated.nodes[0] = GraphNode(id="alice", type="Employee", properties={"age": 30})
        adapter.add_graph_documents([updated])
        node = adapter.graph.get_node(b"alice")
        assert set(node.labels) == {"Person", "Employee"}
        assert node.properties == {"name": "Alice", "age": 30}
        assert "Employee" in adapter.get_structured_schema["node_props"]
        assert adapter.query("MATCH ()-[r:KNOWS]->() RETURN r.since AS since") == [{"since": 2022}]
        changed = fact(edge_id="e")
        changed.relationships[0].target = GraphNode(id="carol")
        with pytest.raises(ValueError, match="endpoints"):
            adapter.add_graph_documents([changed])
        assert adapter.graph.get_node(b"carol") is None


def test_id_collisions_and_identical_parallel_edges(tmp_path: Path) -> None:
    ids = [1, "1", "__lc_int__:1", "__lc_source__:x"]
    assert len({entity_id(value) for value in ids}) == len(ids)
    with GestaltDBGraph.create(tmp_path / "g", backend="lmdb") as adapter:
        adapter.add_graph_documents([GraphDocument(nodes=[GraphNode(id=i) for i in ids])])
        adapter.add_graph_documents([fact(edge_id="one"), fact(edge_id="two")])
        assert len(adapter.query("MATCH ()-[r:KNOWS]->() RETURN r")) == 2


def test_whole_input_validation_precedes_writes(tmp_path: Path) -> None:
    with GestaltDBGraph.create(tmp_path / "g", backend="lmdb") as adapter:
        with pytest.raises(ValueError, match="requires"):
            adapter.add_graph_documents([fact(), GraphDocument()], include_source=True)
        assert list(adapter.graph.get_node_keys_generator()) == []
        with pytest.raises(ValueError, match="conflicts"):
            adapter.add_graph_documents([fact(properties={"type": "OTHER"})])
        assert list(adapter.graph.iter_edge_ids()) == []


def test_external_model_structural_conversion_and_validation() -> None:
    class ExternalGraph(BaseModel):
        nodes: list[GraphNode]
        relationships: list[GraphRelationship]
        source: Document

    external = ExternalGraph(**fact().model_dump())
    converted = GraphDocument.from_external(external)
    assert converted.nodes[0].id == "alice"
    assert converted.source.page_content == "Alice knows Bob"
    with pytest.raises(ValidationError):
        GraphNode(id=True)
    with pytest.raises(ValidationError):
        GraphNode(id="")


def test_explicit_source_id_merges_content_updates(tmp_path: Path) -> None:
    with GestaltDBGraph.create(tmp_path / "g", backend="lmdb") as adapter:
        document = fact()
        document.source.id = "source-1"
        adapter.add_graph_documents([document], include_source=True)
        document.source.page_content = "Revised evidence"
        adapter.add_graph_documents([document], include_source=True)
        rows = adapter.query("MATCH (s:__LangChainSource__) RETURN s.page_content AS text")
        assert rows == [{"text": "Revised evidence"}]
