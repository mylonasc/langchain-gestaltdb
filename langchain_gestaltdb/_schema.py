"""Bounded schema inspection using canonical entities, not stale indexes."""

from __future__ import annotations

from collections import defaultdict
from itertools import islice
from typing import Any

from gestaltdb.graphdb import GraphDB

DIALECT_GUIDANCE = (
    "Use parameterized GestaltDB Cypher (not all Neo4j syntax is supported). "
    "Node types are native labels; relationship types are stored in properties['type']. "
    "Use MATCH/OPTIONAL MATCH, WHERE, WITH, UNWIND, RETURN, UNION and supported functions. "
    "Pattern comprehensions, exists(pattern), APOC and arbitrary procedures are unsupported. "
    "Schema is sampled unless explicitly configured otherwise; "
    "missing properties are not forbidden. "
    "Output limits are not execution timeouts. Claims are separate from ordinary graph edges."
)


def _type_name(value: Any) -> str:
    if value is None:
        return "null"
    return {
        bool: "boolean",
        int: "integer",
        float: "float",
        str: "string",
        bytes: "bytes",
        list: "list",
        tuple: "list",
        dict: "map",
    }.get(type(value), type(value).__name__)


def discover_schema(
    graph: GraphDB, node_limit: int | None, edge_limit: int | None
) -> dict[str, Any]:
    """Inspect at most N canonical nodes and E edges (+ bounded endpoint reads).

    One extra key is read to detect truncation, not one extra entity. ``None``
    explicitly opts into an exhaustive scan. Canonical reads bypass secondary
    indexes, so deferred index maintenance does not hide labels/properties.
    This is approximate discovery, not a concurrent database-wide snapshot.
    """
    node_keys = list(
        islice(graph.get_node_keys_generator(), None if node_limit is None else node_limit + 1)
    )
    edge_keys = list(islice(graph.iter_edge_ids(), None if edge_limit is None else edge_limit + 1))
    nodes_truncated = node_limit is not None and len(node_keys) > node_limit
    edges_truncated = edge_limit is not None and len(edge_keys) > edge_limit
    node_keys = node_keys[:node_limit]
    edge_keys = edge_keys[:edge_limit]
    nodes = {key: graph.get_node(key) for key in node_keys}
    edges = [graph.get_edge(key) for key in edge_keys]
    relationships: set[tuple[str, str, str]] = set()
    node_props: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    rel_props: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    for edge in edges:
        if edge is None:
            continue
        endpoints = []
        for endpoint in (edge.source, edge.target):
            key = endpoint.encode("utf-8") if isinstance(endpoint, str) else endpoint.get_id_bytes
            if key not in nodes:
                nodes[key] = graph.get_node(key)
            node = nodes[key]
            endpoints.append(
                tuple(node.labels) if node is not None and node.labels else ("__unlabeled__",)
            )
        edge_type = edge.properties.get("type") or "__untyped__"
        if not isinstance(edge_type, str):
            edge_type = str(edge_type)
        rel_props[edge_type]  # Retain relationship types with no other properties.
        for name, value in edge.properties.items():
            if name != "type":
                rel_props[edge_type][name].add(_type_name(value))
        for start in endpoints[0]:
            for end in endpoints[1]:
                relationships.add((start, edge_type, end))
    for node in nodes.values():
        if node is None:
            continue
        for label in node.labels or ("__unlabeled__",):
            node_props[label]
            for name, value in node.properties.items():
                node_props[label][name].add(_type_name(value))

    def properties(values: dict[str, dict[str, set[str]]]) -> dict[str, Any]:
        return {
            label: [
                {"property": name, "type": " | ".join(sorted(types))}
                for name, types in sorted(props.items())
            ]
            for label, props in sorted(values.items())
        }

    return {
        "node_props": properties(node_props),
        "rel_props": properties(rel_props),
        "relationships": [
            {"start": start, "type": kind, "end": end} for start, kind, end in sorted(relationships)
        ],
        "metadata": {
            "node_scan_limit": node_limit,
            "edge_scan_limit": edge_limit,
            "inspected_nodes": sum(node is not None for node in nodes.values()),
            "inspected_edges": sum(edge is not None for edge in edges),
            "truncated": nodes_truncated or edges_truncated,
            "indexes": graph.index_statistics(),
            "dialect": DIALECT_GUIDANCE,
        },
    }
