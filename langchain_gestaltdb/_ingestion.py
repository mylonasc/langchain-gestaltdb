"""Validated incremental ingestion with stable identity and source links."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from typing import Any

from gestaltdb.graphdb import Edge, GraphDB, Node

from langchain_gestaltdb._serialization import normalize
from langchain_gestaltdb.graph_document import GraphDocument, GraphNode


def _digest(value: Any) -> str:
    encoded = json.dumps(
        normalize(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def entity_id(value: str | int) -> str:
    """Keep ordinary strings readable; reserve a collision-free internal namespace."""
    if type(value) is int:
        return f"__lc_int__:{value}"
    if isinstance(value, str) and value:
        return f"__lc_str__:{_digest(value)}" if value.startswith("__lc_") else value
    raise ValueError("Entity IDs must be nonempty strings or integers (not booleans)")


def ingest(graph: GraphDB, values: Iterable[Any], include_source: bool) -> None:
    """Prevalidate a whole import, then write incrementally (not atomically).

    Duplicate nodes merge labels/properties; incoming properties win. Default
    edge identity includes endpoints, type and properties; exact duplicates
    collapse, distinct properties retain parallel edges. Explicit relationship
    IDs support property updates but cannot change endpoints/type. Source IDs
    use Document.id when supplied, otherwise content plus metadata.
    """
    documents = [GraphDocument.from_external(value) for value in values]
    nodes: dict[str, Node] = {}
    edges: dict[str, Edge] = {}

    def add_node(node: GraphNode) -> str:
        key = entity_id(node.id)
        properties = normalize(node.properties)
        previous = nodes.get(key)
        if previous is not None:
            properties = {**previous.properties, **properties}
        labels = sorted(set((previous.labels if previous else ()) + (node.type,)))
        nodes[key] = Node(key, labels=labels, properties=properties)
        return key

    def add_edge(edge: Edge) -> None:
        previous = edges.get(edge.get_id) or graph.get_edge(edge.get_id_bytes)
        if previous is not None:
            if (previous.source, previous.target, previous.get_type) != (
                edge.source,
                edge.target,
                edge.get_type,
            ):
                raise ValueError("An explicit relationship ID cannot change endpoints or type")
            edge.properties = {**previous.properties, **edge.properties}
        edges[edge.get_id] = edge

    for document in documents:
        mentioned: set[str] = set()
        for relationship in document.relationships:
            source, target = add_node(relationship.source), add_node(relationship.target)
            mentioned.update((source, target))
            properties = normalize(relationship.properties)
            if "type" in properties and properties["type"] != relationship.type:
                raise ValueError("Relationship properties['type'] conflicts with its type")
            properties["type"] = relationship.type
            identity = (
                ["explicit", relationship.id]
                if relationship.id is not None
                else ["fact", source, target, properties]
            )
            add_edge(Edge(f"__lc_edge__:{_digest(identity)}", source, target, properties))
        for node in document.nodes:
            mentioned.add(add_node(node))
        if include_source:
            if document.source is None:
                raise ValueError(
                    "include_source=True requires every graph document to have a source"
                )
            source_doc = document.source
            metadata = normalize(source_doc.metadata)
            identity = (
                ["id", source_doc.id]
                if source_doc.id is not None
                else ["content", source_doc.page_content, metadata]
            )
            source_id = f"__lc_source__:{_digest(identity)}"
            nodes[source_id] = Node(
                source_id,
                labels=["__LangChainSource__"],
                properties={
                    "page_content": source_doc.page_content,
                    "metadata": metadata,
                    "document_id": source_doc.id,
                },
            )
            for target in sorted(mentioned):
                add_edge(
                    Edge(
                        f"__lc_mention__:{_digest([source_id, target])}",
                        source_id,
                        target,
                        {"type": "MENTIONS"},
                    )
                )
    # Finish validation/merges before the first write. Backend I/O failures can
    # still leave a partial import; no cross-backend atomicity is implied.
    for key, node in nodes.items():
        existing = graph.get_node(node.get_id_bytes)
        if existing is not None:
            nodes[key] = Node(
                key,
                labels=sorted(set(existing.labels) | set(node.labels)),
                properties={**existing.properties, **node.properties},
            )
    for node in nodes.values():
        graph.put_node(node)
    for edge in edges.values():
        graph.put_edge(edge)  # TD-02: upstream JSON bulk adjacency is not reliable.
