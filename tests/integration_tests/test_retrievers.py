"""Bounded typed traversal, seed strategies, caps, provenance, and determinism."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from gestaltdb.graphdb import Edge, Node
from langchain_core.documents import Document
from pydantic import ValidationError

from langchain_gestaltdb import (
    GestaltDBGraph,
    GestaltDBRetriever,
    GraphDocument,
    GraphNode,
    GraphRelationship,
    RetrievalHop,
)
from langchain_gestaltdb.retrievers import MENTIONS

NAME = "name"


def build_graph(graph: GestaltDBGraph) -> None:
    person = GraphNode(id="alice", type="Person", properties={NAME: "Alice"})
    company = GraphNode(id="acme", type="Company", properties={NAME: "Acme"})
    bob = GraphNode(id="bob", type="Person", properties={NAME: "Bob"})
    graph.add_graph_documents(
        [
            GraphDocument(
                nodes=[person, company],
                relationships=[GraphRelationship(source=person, target=company, type="WORKS_FOR")],
                source=Document(
                    id="hr-1",
                    page_content="Alice works for Acme.",
                    metadata={"path": "hr.txt"},
                ),
            )
        ],
        include_source=True,
    )
    graph.add_graph_documents(
        [
            GraphDocument(
                nodes=[bob],
                relationships=[GraphRelationship(source=bob, target=company, type="WORKS_FOR")],
            )
        ]
    )
    graph.graph.create_node_property_index(NAME)


def indexed_retriever(graph: GestaltDBGraph, **overrides: object) -> GestaltDBRetriever:
    params: dict[str, object] = {
        "graph": graph,
        "hops": (RetrievalHop(edge_type="WORKS_FOR", direction="out", sample_size=5),),
        "seed_label": "Person",
        "seed_property": NAME,
    }
    params.update(overrides)
    return GestaltDBRetriever(**params)  # type: ignore[arg-type]


def test_indexed_resolution_and_typed_traversal(tmp_path: Path) -> None:
    with GestaltDBGraph.create(tmp_path / "g", backend="lmdb") as graph:
        build_graph(graph)
        documents = indexed_retriever(graph).invoke("Alice")
        by_id = {document.metadata["id"]: document for document in documents}
        assert set(by_id) == {"alice", "acme"}
        seed = by_id["alice"]
        assert seed.metadata["is_seed"] is True
        assert seed.metadata["labels"] == ["Person"]
        assert seed.metadata["properties"] == {NAME: "Alice"}
        assert seed.metadata["paths"] == [{"seed": "alice", "steps": []}]
        assert "Person alice" in seed.page_content
        # Evidence text is attached because the graph was ingested with sources.
        assert "Alice works for Acme." in seed.page_content
        assert seed.metadata["sources"][0]["document_id"] == "hr-1"
        assert seed.metadata["sources"][0]["metadata"] == {"path": "hr.txt"}
        reached = by_id["acme"]
        assert reached.metadata["is_seed"] is False
        assert reached.metadata["relationship_types"] == ["WORKS_FOR"]
        assert reached.metadata["hops"] == 1
        (path,) = reached.metadata["paths"]
        assert path["seed"] == "alice"
        assert path["steps"][0]["edge_type"] == "WORKS_FOR"
        assert path["steps"][0]["source_id"] == "alice"
        assert path["steps"][0]["target_id"] == "acme"
        assert all(isinstance(value, str) for value in path["steps"][0].values())
        json.dumps([document.metadata for document in documents], allow_nan=False)
        assert documents[0].metadata["is_seed"] is True  # seeds are returned first


def test_unindexed_property_lookup_is_empty_not_guess(tmp_path: Path) -> None:
    with GestaltDBGraph.create(tmp_path / "g", backend="lmdb") as graph:
        person = GraphNode(id="alice", type="Person", properties={NAME: "Alice"})
        graph.add_graph_documents([GraphDocument(nodes=[person], relationships=[])])
        # No create_node_property_index call: indexed resolution finds nothing.
        assert indexed_retriever(graph).invoke("Alice") == []
        explicit = GestaltDBRetriever(graph=graph, hops=(), seed_ids=("alice",))
        assert explicit.invoke("ignored")[0].metadata["id"] == "alice"


def test_seed_ids_resolver_and_direction_variants(tmp_path: Path) -> None:
    with GestaltDBGraph.create(tmp_path / "g", backend="lmdb") as graph:
        build_graph(graph)
        explicit = GestaltDBRetriever(
            graph=graph,
            hops=(RetrievalHop(edge_type="WORKS_FOR", direction="in", sample_size=5),),
            seed_ids=("acme",),
        )
        by_id = {document.metadata["id"] for document in explicit.invoke("unused")}
        assert by_id == {"acme", "alice", "bob"}

        calls: list[str] = []

        def resolver(query: str) -> list[str]:
            calls.append(query)
            return ["alice", "alice", "ghost"]

        resolved = GestaltDBRetriever(
            graph=graph,
            hops=(RetrievalHop(edge_type="WORKS_FOR", direction="any", sample_size=5),),
            seed_resolver=resolver,
        )
        ids = {document.metadata["id"] for document in resolved.invoke("lookup")}
        assert calls == ["lookup"]
        assert ids == {"alice", "acme"}  # duplicates collapse and absent seeds are skipped


def test_caps_bound_fanout_paths_entities_seeds_and_content(tmp_path: Path) -> None:
    with GestaltDBGraph.create(tmp_path / "g", backend="lmdb") as graph:
        hub = GraphNode(id="hub", type="Hub")
        leaves = [GraphNode(id=f"leaf-{index:02d}", type="Leaf") for index in range(20)]
        graph.add_graph_documents(
            [
                GraphDocument(
                    nodes=[hub, *leaves],
                    relationships=[
                        GraphRelationship(source=hub, target=leaf, type="LINKS") for leaf in leaves
                    ],
                )
            ]
        )
        fanout = GestaltDBRetriever(
            graph=graph,
            hops=(RetrievalHop(edge_type="LINKS", direction="out", sample_size=4),),
            seed_ids=("hub",),
            max_entities=100,
        )
        documents = fanout.invoke("x")
        assert len(documents) == 5  # seed plus four sampled neighbors
        assert all(document.metadata["truncated"] is False for document in documents)

        capped = GestaltDBRetriever(
            graph=graph,
            hops=(RetrievalHop(edge_type="LINKS", direction="out", sample_size=4),),
            seed_ids=("hub",),
            max_entities=3,
        )
        capped_documents = capped.invoke("x")
        assert len(capped_documents) == 3
        assert all(document.metadata["truncated"] is True for document in capped_documents)
        assert capped_documents[0].metadata["id"] == "hub"  # seeds survive truncation

        content = GestaltDBRetriever(
            graph=graph,
            hops=(),
            seed_ids=("leaf-00",),
            max_content_chars=10,
        )
        assert len(content.invoke("x")[0].page_content) == 10
        assert content.invoke("x")[0].page_content == "Leaf leaf-"
        assert content.invoke("x")[0].metadata["content_truncated"] is True
        assert content.invoke("x")[0].metadata["truncated"] is True
        assert content.invoke("x")[0].metadata["result_truncated"] is False

        many = GestaltDBRetriever(
            graph=graph,
            hops=(),
            seed_resolver=lambda _query: [f"leaf-{index:02d}" for index in range(20)],
            max_seeds=3,
        )
        assert len(many.invoke("x")) == 3
        assert all(document.metadata["result_truncated"] for document in many.invoke("x"))
        exact = GestaltDBRetriever(graph=graph, seed_ids=("hub",), max_seeds=1).invoke("x")
        assert exact[0].metadata["result_truncated"] is False


def test_deterministic_sampling_with_fixed_seed(tmp_path: Path) -> None:
    with GestaltDBGraph.create(tmp_path / "g", backend="lmdb") as graph:
        hub = GraphNode(id="hub", type="Hub")
        leaves = [GraphNode(id=f"leaf-{index:02d}", type="Leaf") for index in range(50)]
        graph.add_graph_documents(
            [
                GraphDocument(
                    nodes=[hub, *leaves],
                    relationships=[
                        GraphRelationship(source=hub, target=leaf, type="LINKS") for leaf in leaves
                    ],
                )
            ]
        )

        def ids() -> list[str]:
            retriever = GestaltDBRetriever(
                graph=graph,
                hops=(RetrievalHop(edge_type="LINKS", direction="out", sample_size=6),),
                seed_ids=("hub",),
                rng_seed=1234,
            )
            return [document.metadata["id"] for document in retriever.invoke("x")]

        assert ids() == ids()
        assert len(ids()) == 7


def test_metadata_is_complete_and_reads_do_not_modify_graph(tmp_path: Path) -> None:
    with GestaltDBGraph.create(tmp_path / "g", backend="lmdb") as graph:
        build_graph(graph)
        before_nodes = {
            graph.graph.key_to_string(key) for key in graph.graph.get_node_keys_generator()
        }
        before_edges = {graph.graph.key_to_string(key) for key in graph.graph.iter_edge_ids()}
        documents = indexed_retriever(graph, include_sources=False).invoke("Alice")
        assert all(document.metadata["sources"] == [] for document in documents)
        after_nodes = {
            graph.graph.key_to_string(key) for key in graph.graph.get_node_keys_generator()
        }
        after_edges = {graph.graph.key_to_string(key) for key in graph.graph.iter_edge_ids()}
        assert before_nodes == after_nodes
        assert before_edges == after_edges
        assert {document.metadata["id"] for document in documents} == {"alice", "acme"}


def test_deferred_edge_ingest_keeps_traversal_and_sources_usable(tmp_path: Path) -> None:
    with GestaltDBGraph.create(tmp_path / "g", backend="lmdb") as graph:
        build_graph(graph)
        db = graph.graph
        edge = Edge("deferred", "alice", "bob", properties={"type": "KNOWS"})
        db.ingest_edges_arrow(
            ["deferred"],
            ["alice"],
            ["bob"],
            ["KNOWS"],
            [db.entity_serializer.serialize(edge, "Edge")],
            native=False,
            index_mode="defer",
        )
        assert "edge_type" in db.stale_indexes()
        with pytest.raises(RuntimeError, match="stale"):
            list(db.iter_edge_ids_by_type("KNOWS"))
        documents = GestaltDBRetriever(
            graph=graph,
            seed_ids=("alice",),
            hops=(RetrievalHop(edge_type="KNOWS"),),
        ).invoke("ignored")
        assert [document.metadata["id"] for document in documents] == ["alice", "bob"]
        assert documents[0].metadata["sources"]
        assert "edge_type" in db.stale_indexes()  # no implicit rebuild


def test_indexed_seed_resolution_rejects_stale_node_indexes(tmp_path: Path) -> None:
    with GestaltDBGraph.create(tmp_path / "g", backend="lmdb") as graph:
        graph.graph.create_node_property_index(NAME)
        node = Node("new", labels=["Person"], properties={NAME: "New"})
        graph.graph.ingest_nodes_arrow(
            ["new"],
            [graph.graph.entity_serializer.serialize(node, "Node")],
            native=False,
            index_mode="defer",
        )
        with pytest.raises(RuntimeError, match="stale"):
            indexed_retriever(graph).invoke("New")


def test_async_adapter_matches_sync(tmp_path: Path) -> None:
    with GestaltDBGraph.create(tmp_path / "g", backend="lmdb") as graph:
        build_graph(graph)
        retriever = indexed_retriever(graph)
        sync_ids = [document.metadata["id"] for document in retriever.invoke("Alice")]
        async_ids = [
            document.metadata["id"] for document in asyncio.run(retriever.ainvoke("Alice"))
        ]
        assert sync_ids == async_ids


def test_configuration_validation(tmp_path: Path) -> None:
    with GestaltDBGraph.create(tmp_path / "g", backend="lmdb") as graph:
        with pytest.raises(ValidationError, match="exactly one seed strategy"):
            GestaltDBRetriever(
                graph=graph, seed_ids=("a",), seed_label="Person", seed_property=NAME
            )
        with pytest.raises(ValidationError, match="seed_property"):
            GestaltDBRetriever(graph=graph, seed_label="Person")
        with pytest.raises(ValidationError, match="At most 8"):
            GestaltDBRetriever(
                graph=graph,
                seed_ids=("a",),
                hops=tuple(RetrievalHop(edge_type="X") for _ in range(9)),
            )
        with pytest.raises(ValidationError):
            RetrievalHop(edge_type="X", sample_size=101)
        with pytest.raises(ValidationError):
            RetrievalHop(edge_type="")
        with pytest.raises(ValidationError):
            RetrievalHop(edge_type="X", direction="sideways")  # type: ignore[arg-type]
        with pytest.raises(ValidationError, match="non-empty"):
            GestaltDBRetriever(graph=graph, seed_ids=("",))
        # An empty graph with a valid strategy returns no documents.
        assert indexed_retriever(graph).invoke("nobody") == []


def test_hops_empty_and_unrecognized_label_are_safe(tmp_path: Path) -> None:
    with GestaltDBGraph.create(tmp_path / "g", backend="lmdb") as graph:
        build_graph(graph)
        only_seeds = GestaltDBRetriever(
            graph=graph, hops=(), seed_ids=("alice", "acme", "ghost")
        ).invoke("x")
        assert [document.metadata["id"] for document in only_seeds] == ["alice", "acme"]
        assert all(
            document.metadata["paths"] == [{"seed": document.metadata["id"], "steps": []}]
            for document in only_seeds
        )
        assert indexed_retriever(graph, seed_label="Missing").invoke("Alice") == []


def test_source_linking_respects_caps_and_absent_sources(tmp_path: Path) -> None:
    with GestaltDBGraph.create(tmp_path / "g", backend="lmdb") as graph:
        alice = GraphNode(id="alice", type="Person", properties={NAME: "Alice"})
        graph.add_graph_documents([GraphDocument(nodes=[alice], relationships=[])])
        graph.graph.create_node_property_index(NAME)
        # No source documents were ingested: content is graph-only, sources empty.
        documents = indexed_retriever(graph).invoke("Alice")
        assert documents[0].metadata["sources"] == []
        assert documents[0].page_content.startswith("Person alice")

        sourced = GraphDocument(
            nodes=[alice],
            relationships=[],
            source=Document(id="s1", page_content="evidence", metadata={"kind": "test"}),
        )
        graph.add_graph_documents([sourced], include_source=True)
        limited = indexed_retriever(graph, max_sources_per_entity=0).invoke("Alice")
        assert limited[0].metadata["sources"] == []
        assert "evidence" not in limited[0].page_content
        with_source = indexed_retriever(graph).invoke("Alice")
        assert with_source[0].metadata["sources"][0]["document_id"] == "s1"
        assert "evidence" in with_source[0].page_content


def test_source_linking_skips_foreign_mentions_and_honors_cap(tmp_path: Path) -> None:
    with GestaltDBGraph.create(tmp_path / "g", backend="lmdb") as graph:
        alice = GraphNode(id="alice", type="Person", properties={NAME: "Alice"})
        graph.add_graph_documents(
            [
                GraphDocument(
                    nodes=[alice],
                    relationships=[],
                    source=Document(id="s1", page_content="first", metadata={"n": 1}),
                ),
                GraphDocument(
                    nodes=[alice],
                    relationships=[],
                    source=Document(id="s2", page_content="second", metadata={"n": 2}),
                ),
            ],
            include_source=True,
        )
        graph.graph.create_node_property_index(NAME)
        # A MENTIONS edge from a node that is not a LangChain source must be ignored.
        graph.graph.put_node(Node("imposter", labels=["Person"]))
        graph.graph.put_edge(Edge("bad", "imposter", "alice", properties={"type": MENTIONS}))
        documents = indexed_retriever(graph, max_sources_per_entity=5).invoke("Alice")
        sources = documents[0].metadata["sources"]
        assert {source["document_id"] for source in sources} == {"s1", "s2"}
        capped = indexed_retriever(graph, max_sources_per_entity=1).invoke("Alice")
        assert len(capped[0].metadata["sources"]) == 1
        assert capped[0].metadata["sources_truncated"] is True
        assert capped[0].metadata["result_truncated"] is False
        assert capped[0].metadata["truncated"] is True
        assert documents[0].metadata["sources_truncated"] is False


def test_max_paths_truncation_flag(tmp_path: Path) -> None:
    with GestaltDBGraph.create(tmp_path / "g", backend="lmdb") as graph:
        hub = GraphNode(id="hub", type="Hub")
        leaves = [GraphNode(id=f"leaf-{index}", type="Leaf") for index in range(10)]
        graph.add_graph_documents(
            [
                GraphDocument(
                    nodes=[hub, *leaves],
                    relationships=[
                        GraphRelationship(source=hub, target=leaf, type="LINKS") for leaf in leaves
                    ],
                )
            ]
        )
        retriever = GestaltDBRetriever(
            graph=graph,
            hops=(RetrievalHop(edge_type="LINKS", direction="out", sample_size=10),),
            seed_ids=("hub",),
            max_paths=2,
        )
        documents = retriever.invoke("x")
        assert all(document.metadata["truncated"] is True for document in documents)
        assert len(documents) == 3  # hub plus exactly two capped paths


def test_node_without_labels_renders_safely(tmp_path: Path) -> None:
    with GestaltDBGraph.create(tmp_path / "g", backend="lmdb") as graph:
        graph.graph.put_node(Node("plain"))
        documents = GestaltDBRetriever(graph=graph, hops=(), seed_ids=("plain",)).invoke("x")
        assert documents[0].page_content.startswith("Node plain")
