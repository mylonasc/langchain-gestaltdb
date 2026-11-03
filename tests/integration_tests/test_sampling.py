"""Multi-hop sampling tool: pattern paths/subgraph, caps, and read-only behavior."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from gestaltdb.graphdb import Edge
from pydantic import ValidationError

from langchain_gestaltdb import (
    GestaltDBGraph,
    GestaltDBSamplingTool,
    GraphDocument,
    GraphNode,
    GraphRelationship,
    SamplingInput,
)


def build_biopathway(graph: GestaltDBGraph) -> None:
    drug = GraphNode(id="drug", type="Drug", properties={"name": "Drug"})
    protein = GraphNode(id="protein", type="Protein", properties={"name": "Protein"})
    disease = GraphNode(id="disease", type="Disease", properties={"name": "Disease"})
    graph.add_graph_documents(
        [
            GraphDocument(
                nodes=[drug, protein, disease],
                relationships=[
                    GraphRelationship(source=drug, target=protein, type="TARGETS"),
                    GraphRelationship(source=protein, target=disease, type="ASSOCIATED_WITH"),
                ],
            )
        ]
    )


def make_tool(graph: GestaltDBGraph, **overrides: object) -> GestaltDBSamplingTool:
    params: dict[str, object] = {"graph": graph}
    params.update(overrides)
    return GestaltDBSamplingTool(**params)  # type: ignore[arg-type]


def invoke(tool: GestaltDBSamplingTool, **kwargs: object) -> dict[str, object]:
    return json.loads(tool.invoke(kwargs))  # type: ignore[return-value]


def test_multihop_paths_drug_protein_disease(tmp_path: Path) -> None:
    with GestaltDBGraph.create(tmp_path / "g") as graph:
        build_biopathway(graph)
        tool = make_tool(graph, rng_seed=7)
        payload = invoke(
            tool,
            seed_ids=["drug"],
            hops=[
                {"edge_type": "TARGETS", "direction": "out", "sample_size": 2},
                {"edge_type": "ASSOCIATED_WITH", "direction": "out", "sample_size": 2},
            ],
            mode="paths",
        )
        assert payload["mode"] == "paths"
        assert payload["seeds"] == ["drug"]
        assert payload["truncated"] is False
        assert payload["returned_paths"] == 1
        (entry,) = payload["paths"]
        assert entry["seed"] == "drug"
        assert [step["edge_type"] for step in entry["path"]] == [
            "TARGETS",
            "ASSOCIATED_WITH",
        ]
        assert entry["path"][0]["source_id"] == "drug"
        assert entry["path"][0]["target_id"] == "protein"
        assert entry["path"][0]["neighbor_id"] == "protein"
        assert entry["path"][1]["source_id"] == "protein"
        assert entry["path"][1]["target_id"] == "disease"
        assert all(
            isinstance(step[key], str)
            for step in entry["path"]
            for key in ("edge_id", "source_id", "target_id", "neighbor_id", "direction")
        )
        # Deterministic ordering and seeded sampling: repeated runs agree.
        again = invoke(
            tool,
            seed_ids=["drug"],
            hops=[
                {"edge_type": "TARGETS", "direction": "out", "sample_size": 2},
                {"edge_type": "ASSOCIATED_WITH", "direction": "out", "sample_size": 2},
            ],
            mode="paths",
        )
        assert again == payload


def test_subgraph_mode_materializes_nodes_and_edges(tmp_path: Path) -> None:
    with GestaltDBGraph.create(tmp_path / "g") as graph:
        build_biopathway(graph)
        payload = invoke(
            make_tool(graph, rng_seed=7),
            seed_ids=["drug"],
            hops=[
                {"edge_type": "TARGETS", "direction": "out", "sample_size": 2},
                {"edge_type": "ASSOCIATED_WITH", "direction": "out", "sample_size": 2},
            ],
            mode="subgraph",
        )
        assert payload["mode"] == "subgraph"
        assert {node["id"] for node in payload["nodes"]} == {"drug", "protein", "disease"}
        assert payload["returned_nodes"] == 3
        assert payload["returned_edges"] == 2
        assert {edge["type"] for edge in payload["edges"]} == {
            "TARGETS",
            "ASSOCIATED_WITH",
        }
        assert payload["returned_paths"] == 1
        assert payload["truncated"] is False
        json.dumps(payload, allow_nan=False)


def test_any_direction_reaches_both_sides(tmp_path: Path) -> None:
    with GestaltDBGraph.create(tmp_path / "g") as graph:
        build_biopathway(graph)
        payload = invoke(
            make_tool(graph, rng_seed=7),
            seed_ids=["protein"],
            hops=[{"edge_type": "TARGETS", "direction": "any", "sample_size": 10}],
            mode="paths",
        )
        neighbors = {step["neighbor_id"] for entry in payload["paths"] for step in entry["path"]}
        assert neighbors == {"drug"}
        both = invoke(
            make_tool(graph, rng_seed=7),
            seed_ids=["protein"],
            hops=[{"edge_type": "ASSOCIATED_WITH", "direction": "any", "sample_size": 10}],
            mode="paths",
        )
        reached = {step["neighbor_id"] for entry in both["paths"] for step in entry["path"]}
        assert "disease" in reached


def test_max_paths_truncation_flag(tmp_path: Path) -> None:
    with GestaltDBGraph.create(tmp_path / "g") as graph:
        hub = GraphNode(id="hub", type="Hub")
        leaves = [GraphNode(id=f"leaf-{index:02d}", type="Leaf") for index in range(10)]
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
        payload = invoke(
            make_tool(graph, max_paths=2),
            seed_ids=["hub"],
            hops=[{"edge_type": "LINKS", "direction": "out", "sample_size": 10}],
            mode="paths",
        )
        assert payload["returned_paths"] == 2
        assert payload["truncated"] is True


def test_absent_seeds_are_skipped_never_fabricated(tmp_path: Path) -> None:
    with GestaltDBGraph.create(tmp_path / "g") as graph:
        build_biopathway(graph)
        ghost = invoke(
            make_tool(graph),
            seed_ids=["ghost"],
            hops=[{"edge_type": "TARGETS", "direction": "out", "sample_size": 2}],
            mode="paths",
        )
        assert ghost["paths"] == []
        assert ghost["seeds"] == []
        assert "ghost" not in json.dumps(ghost)
        mixed = invoke(
            make_tool(graph),
            seed_ids=["ghost", "drug"],
            hops=[{"edge_type": "TARGETS", "direction": "out", "sample_size": 2}],
            mode="paths",
        )
        assert mixed["seeds"] == ["drug"]
        assert all(entry["seed"] == "drug" for entry in mixed["paths"])
        subgraph = invoke(
            make_tool(graph),
            seed_ids=["ghost"],
            hops=[{"edge_type": "TARGETS", "direction": "out", "sample_size": 2}],
            mode="subgraph",
        )
        assert subgraph["nodes"] == []
        assert subgraph["edges"] == []
        assert subgraph["paths"] == []


def test_sampling_is_read_only_and_keeps_stale_indexes(tmp_path: Path) -> None:
    with GestaltDBGraph.create(tmp_path / "g") as graph:
        build_biopathway(graph)
        db = graph.graph
        edge = Edge("deferred", "drug", "protein", properties={"type": "BINDS"})
        db.ingest_edges_arrow(
            ["deferred"],
            ["drug"],
            ["protein"],
            ["BINDS"],
            [db.entity_serializer.serialize(edge, "Edge")],
            native=False,
            index_mode="defer",
        )
        assert "edge_type" in db.stale_indexes()
        before_nodes = {db.key_to_string(key) for key in db.get_node_keys_generator()}
        before_edges = {db.key_to_string(key) for key in db.iter_edge_ids()}
        payload = invoke(
            make_tool(graph, rng_seed=3),
            seed_ids=["drug"],
            hops=[{"edge_type": "BINDS", "direction": "out", "sample_size": 5}],
            mode="paths",
        )
        assert payload["returned_paths"] == 1
        assert "edge_type" in db.stale_indexes()  # no implicit rebuild
        after_nodes = {db.key_to_string(key) for key in db.get_node_keys_generator()}
        after_edges = {db.key_to_string(key) for key in db.iter_edge_ids()}
        assert before_nodes == after_nodes
        assert before_edges == after_edges


def test_input_bounds_reject_oversized_pattern() -> None:
    with pytest.raises(ValidationError):
        SamplingInput(seed_ids=["a"], hops=[{"edge_type": "X"} for _ in range(9)])
    with pytest.raises(ValidationError):
        SamplingInput(seed_ids=["a"], hops=[{"edge_type": "X", "sample_size": 101}])
    with pytest.raises(ValidationError):
        SamplingInput(seed_ids=["a"], hops=[{"edge_type": ""}])
