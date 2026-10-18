"""Bounded typed-graph retrieval over native labels, types, and typed traversal.

This is deliberately not a vector store. It resolves a small set of seed
entities (through an index, explicit IDs, or a caller-supplied resolver such as
an external vector store) and follows a bounded, typed traversal to produce
source-linked ``Document`` records.
"""

from __future__ import annotations

import json
import random
from collections.abc import Callable, Sequence
from typing import Any, Literal

from langchain_core.callbacks import CallbackManagerForRetrieverRun
from langchain_core.documents import Document
from langchain_core.retrievers import BaseRetriever
from pydantic import BaseModel, ConfigDict, Field, model_validator

from langchain_gestaltdb._serialization import normalize
from langchain_gestaltdb.graph import GestaltDBGraph

MAX_HOPS = 8
"""Hard cap on typed traversal depth (number of hops)."""

MAX_FANOUT = 100
"""Hard cap on sampled neighbors per hop, per frontier entry."""

SOURCE_LABEL = "__LangChainSource__"
MENTIONS = "MENTIONS"


class RetrievalHop(BaseModel):
    """One typed traversal step with an explicit, bounded fanout.

    ``edge_type`` matches GestaltDB ``Edge.properties['type']``. ``sample_size``
    bounds how many neighbors this hop may sample for each incoming path.
    """

    model_config = ConfigDict(extra="forbid")

    edge_type: str = Field(min_length=1, max_length=256)
    direction: Literal["out", "in", "any"] = "out"
    sample_size: int = Field(default=10, ge=1, le=MAX_FANOUT)


class GestaltDBRetriever(BaseRetriever):
    """Resolve seeds, traverse typed relationships, and return bounded documents.

    Configure exactly one seed strategy: ``seed_ids``, ``seed_resolver``, or the
    indexed ``seed_label``/``seed_property`` pair. Traversal is bounded by the
    pattern length (<=8 hops), per-hop ``sample_size`` (<=100), ``max_paths``,
    ``max_entities`` and ``max_content_chars``.

    Output order is deterministic (seeds first, then IDs sorted) and is **not** a
    relevance ranking. Sampling itself is seeded for reproducibility but carries
    no database ordering guarantee. The retriever never writes; seeding by label
    requires the caller to have created the matching property index already.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    graph: GestaltDBGraph = Field(exclude=True)
    hops: tuple[RetrievalHop, ...] = ()
    seed_ids: tuple[str, ...] = ()
    seed_label: str | None = None
    seed_property: str | None = None
    seed_resolver: Callable[[str], Sequence[str]] | None = Field(default=None, exclude=True)
    max_seeds: int = Field(default=10, ge=1, le=1000)
    max_paths: int = Field(default=50, ge=1, le=1000)
    max_entities: int = Field(default=100, ge=1, le=1000)
    max_content_chars: int = Field(default=2000, ge=1, le=100_000)
    include_sources: bool = True
    max_sources_per_entity: int = Field(default=5, ge=0, le=100)
    rng_seed: int = 0

    @model_validator(mode="after")
    def _validate_config(self) -> GestaltDBRetriever:
        strategies = (
            bool(self.seed_ids),
            self.seed_resolver is not None,
            self.seed_label is not None,
        )
        if sum(strategies) != 1:
            raise ValueError(
                "Configure exactly one seed strategy: seed_ids, seed_resolver, or seed_label"
            )
        if self.seed_label is not None and not self.seed_property:
            raise ValueError("seed_label requires seed_property for indexed seed resolution")
        if any(not seed for seed in self.seed_ids):
            raise ValueError("seed_ids must be non-empty strings")
        if len(self.hops) > MAX_HOPS:
            raise ValueError(f"At most {MAX_HOPS} traversal hops are supported")
        return self

    def _get_relevant_documents(
        self, query: str, *, run_manager: CallbackManagerForRetrieverRun
    ) -> list[Document]:
        db = self.graph.graph
        if self.hops or self.include_sources:
            stale = set(db.stale_indexes())
            if "edge_type" in stale:
                raise RuntimeError(
                    "typed traversal requires current edge_type indexes; "
                    "call graph.graph.rebuild_deferred_indexes() first"
                )
        seeds = self._resolve_seeds(db, query)
        if not seeds:
            return []
        entity_paths, truncated = self._traverse(db, seeds)
        ordered = list(seeds) + [value for value in sorted(entity_paths) if value not in set(seeds)]
        if len(ordered) > self.max_entities:
            ordered = ordered[: self.max_entities]
            truncated = True
        seed_set = set(seeds)
        return [
            self._document(db, entity_id, entity_paths[entity_id], entity_id in seed_set, truncated)
            for entity_id in ordered
        ]

    def _resolve_seeds(self, db: Any, query: str) -> list[str]:
        if self.seed_ids:
            candidates: list[str] = list(self.seed_ids)
        elif self.seed_resolver is not None:
            candidates = [str(value) for value in self.seed_resolver(query)]
        else:
            candidates = [
                db.key_to_string(key)
                for key in db.iter_node_ids_by_label_property(
                    self.seed_label, self.seed_property, query
                )
            ]
        seeds: list[str] = []
        seen: set[str] = set()
        for candidate in candidates:
            if candidate in seen:
                continue
            seen.add(candidate)
            if db.get_node(db.node_key_to_bytes(candidate)) is None:
                continue  # Never fabricate documents for absent entities.
            seeds.append(candidate)
            if len(seeds) >= self.max_seeds:
                break
        return seeds

    def _traverse(self, db: Any, seeds: list[str]) -> tuple[dict[str, list[dict[str, Any]]], bool]:
        entity_paths: dict[str, list[dict[str, Any]]] = {
            seed: [{"seed": seed, "steps": []}] for seed in seeds
        }
        frontier: list[tuple[str, str, list[dict[str, Any]]]] = [(seed, seed, []) for seed in seeds]
        rng = random.Random(self.rng_seed)
        truncated = False
        for hop in self.hops:
            next_frontier: list[tuple[str, str, list[dict[str, Any]]]] = []
            for seed, current, steps in frontier:
                records = db.sample_neighbors(
                    current,
                    hop.edge_type,
                    direction=hop.direction,
                    sample_size=hop.sample_size,
                    rng=rng,
                )
                for record in records:
                    step = {
                        "edge_id": db.key_to_string(record["edge_id"]),
                        "source_id": db.key_to_string(record["source_id"]),
                        "target_id": db.key_to_string(record["target_id"]),
                        "neighbor_id": db.key_to_string(record["neighbor_id"]),
                        "edge_type": record["edge_type"],
                        "direction": record["direction"],
                    }
                    next_frontier.append((seed, step["neighbor_id"], [*steps, step]))
            if len(next_frontier) > self.max_paths:
                next_frontier = next_frontier[: self.max_paths]
                truncated = True
            if not next_frontier:
                break
            frontier = next_frontier
            for seed, current, steps in frontier:
                entity_paths.setdefault(current, []).append({"seed": seed, "steps": steps})
        return entity_paths, truncated

    def _document(
        self,
        db: Any,
        entity_id: str,
        paths: list[dict[str, Any]],
        is_seed: bool,
        truncated: bool,
    ) -> Document:
        node = db.get_node(db.node_key_to_bytes(entity_id))
        labels = list(node.labels) if node is not None else []
        properties = normalize(node.properties) if node is not None else {}
        relationship_types = sorted({step["edge_type"] for path in paths for step in path["steps"]})
        sources = self._sources(db, entity_id) if self.include_sources else []
        content = self._render(entity_id, labels, properties)
        if sources:
            content = (
                content + "\n\n" + "\n\n".join(str(source["page_content"]) for source in sources)
            )
        return Document(
            page_content=content[: self.max_content_chars],
            metadata={
                "id": entity_id,
                "labels": labels,
                "properties": properties,
                "relationship_types": relationship_types,
                "paths": paths,
                "sources": [
                    {
                        "id": source["id"],
                        "document_id": source["document_id"],
                        "metadata": source["metadata"],
                    }
                    for source in sources
                ],
                "hops": max((len(path["steps"]) for path in paths), default=0),
                "is_seed": is_seed,
                "truncated": truncated,
            },
        )

    def _sources(self, db: Any, entity_id: str) -> list[dict[str, Any]]:
        if self.max_sources_per_entity == 0:
            return []
        found: list[dict[str, Any]] = []
        for record in db.iter_typed_adjacency(entity_id, MENTIONS, direction="in"):
            source_id = db.key_to_string(record["source_id"])
            node = db.get_node(db.node_key_to_bytes(source_id))
            if node is None or SOURCE_LABEL not in node.labels:
                continue
            node_properties = node.properties or {}
            found.append(
                {
                    "id": source_id,
                    "page_content": node_properties.get("page_content", ""),
                    "document_id": node_properties.get("document_id"),
                    "metadata": normalize(node_properties.get("metadata") or {}),
                }
            )
            if len(found) >= self.max_sources_per_entity:
                break
        return found

    @staticmethod
    def _render(entity_id: str, labels: list[str], properties: dict[str, Any]) -> str:
        header = f"{'/'.join(labels) or 'Node'} {entity_id}"
        if properties:
            return f"{header} {json.dumps(properties, sort_keys=True, ensure_ascii=False)}"
        return header
