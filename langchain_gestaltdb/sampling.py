"""Multi-hop typed-path/subgraph sampling as an agent tool.

Use :class:`GestaltDBRetriever` when the agent needs seeded entity documents
with source-linked text (one document per entity, ``MENTIONS`` provenance).
Use :class:`GestaltDBSamplingTool` when the agent needs raw multi-hop typed
paths or a materialized subgraph (node/edge records following an ordered
``SamplingPattern``), e.g. drug -> protein -> disease traversals.

The tool is read-only: it never writes and never rebuilds indexes. Typed
adjacency stays usable during deferred ingestion, so sampling does not touch
``stale_indexes``. Output is bounded (caps limit output, not database work)
and byte IDs are decoded with ``key_to_string``.
"""

from __future__ import annotations

import json
import random
from typing import Any, Literal

from gestaltdb.sampling import SamplingHop, SamplingPattern
from langchain_core.callbacks import CallbackManagerForToolRun
from langchain_core.tools import BaseTool, ToolException
from pydantic import BaseModel, ConfigDict, Field

from langchain_gestaltdb._serialization import normalize
from langchain_gestaltdb._tool_output import bounded_json
from langchain_gestaltdb.graph import GestaltDBGraph
from langchain_gestaltdb.retrievers import MAX_FANOUT, MAX_HOPS

_SAMPLING_ERROR = "GestaltDB sampling failed; check the seed IDs, pattern, and database state."


class SamplingHopInput(BaseModel):
    """One typed traversal step; bounds are enforced at validation."""

    model_config = ConfigDict(extra="forbid")

    edge_type: str = Field(min_length=1, max_length=256)
    direction: Literal["out", "in", "any"] = "out"
    sample_size: int = Field(default=10, ge=1, le=MAX_FANOUT)


class SamplingInput(BaseModel):
    """Seeds and ordered pattern are agent-controllable; caps are not."""

    model_config = ConfigDict(extra="forbid")

    seed_ids: list[str] = Field(min_length=1, max_length=1000)
    hops: list[SamplingHopInput] = Field(min_length=1, max_length=MAX_HOPS)
    mode: Literal["paths", "subgraph"] = Field(
        default="paths", description="Return sampled paths or a materialized subgraph"
    )


def _field(hop: Any, name: str, default: Any = None) -> Any:
    if isinstance(hop, dict):
        return hop.get(name, default)
    return getattr(hop, name, default)


def _decode_step(db: Any, record: dict[str, Any]) -> dict[str, Any]:
    return {
        "edge_id": db.key_to_string(record["edge_id"]),
        "source_id": db.key_to_string(record["source_id"]),
        "target_id": db.key_to_string(record["target_id"]),
        "neighbor_id": db.key_to_string(record["neighbor_id"]),
        "edge_type": record["edge_type"],
        "direction": record["direction"],
    }


def _path_sort_key(path: dict[str, Any]) -> str:
    return json.dumps(path, sort_keys=True, ensure_ascii=False)


class GestaltDBSamplingTool(BaseTool):
    """Sample multi-hop typed paths or a typed subgraph around seed nodes.

    Seeds that do not exist are skipped, never fabricated. Output order is
    deterministic (paths sorted by seed/steps, nodes/edges sorted by ID) and
    is **not** a relevance ranking. ``rng_seed`` is application-controlled for
    reproducibility. Caps bound returned output, not database work or
    intermediate materialization. Read-only: never writes, never rebuilds
    indexes.
    """

    name: str = "gestaltdb_sample_paths"
    description: str = (
        "Sample multi-hop typed paths (or a materialized subgraph) following an ordered "
        "edge-type pattern from seed node IDs. Use for multi-hop traversals such as "
        "drug->protein->disease; use GestaltDBRetriever for seeded entity documents "
        "with source text. Returns decoded IDs, deterministic ordering, and truncation flags."
    )
    args_schema: type[BaseModel] = SamplingInput
    graph: GestaltDBGraph = Field(exclude=True)
    max_paths: int = Field(default=50, ge=1, le=1000, exclude=True)
    max_nodes: int = Field(default=1000, ge=1, le=100_000, exclude=True)
    max_edges: int = Field(default=2000, ge=0, le=200_000, exclude=True)
    max_chars: int = Field(default=16_000, ge=256, le=1_000_000, exclude=True)
    rng_seed: int = Field(default=0, exclude=True)
    handle_tool_error: bool = True

    def _run(
        self,
        seed_ids: list[str] | list[Any],
        hops: list[dict[str, Any]] | list[Any],
        mode: Literal["paths", "subgraph"] = "paths",
        run_manager: CallbackManagerForToolRun | None = None,
    ) -> str:
        try:
            db = self.graph.graph
            seeds = self._valid_seeds(db, seed_ids)
            if not seeds:
                return self._empty_envelope(mode)
            pattern = SamplingPattern(
                [
                    SamplingHop(
                        _field(hop, "edge_type"),
                        direction=_field(hop, "direction", "out"),
                        sample_size=int(_field(hop, "sample_size", 10)),
                    )
                    for hop in hops
                ]
            )
            rng = random.Random(self.rng_seed)
            if mode == "subgraph":
                return self._subgraph_envelope(db, seeds, pattern)
            return self._paths_envelope(db, seeds, pattern, rng)
        except ToolException:
            raise
        except Exception:
            raise ToolException(_SAMPLING_ERROR) from None

    def _valid_seeds(self, db: Any, seed_ids: list[str]) -> list[str]:
        seeds: list[str] = []
        seen: set[str] = set()
        for seed in seed_ids:
            if not seed or seed in seen:
                continue
            seen.add(seed)
            if db.get_node(db.node_key_to_bytes(seed)) is None:
                continue  # Never fabricate entities for absent seeds.
            seeds.append(seed)
        return seeds

    def _decode_paths(self, db: Any, raw_paths: list[dict[str, Any]]) -> list[dict[str, Any]]:
        decoded = [
            {
                "seed": db.key_to_string(entry["seed"]),
                "path": [_decode_step(db, record) for record in entry["path"]],
            }
            for entry in raw_paths
        ]
        decoded.sort(key=_path_sort_key)
        return decoded

    def _paths_envelope(
        self, db: Any, seeds: list[str], pattern: SamplingPattern, rng: random.Random
    ) -> str:
        raw = db.sample_typed_paths(seeds, pattern, rng=rng)
        paths = self._decode_paths(db, list(raw))
        result: dict[str, Any] = {
            "mode": "paths",
            "seeds": seeds,
            "paths": normalize(paths),
            "returned_paths": len(paths),
            "truncated": False,
        }
        return bounded_json(result, (("paths", "returned_paths", self.max_paths),), self.max_chars)

    def _subgraph_envelope(self, db: Any, seeds: list[str], pattern: SamplingPattern) -> str:
        rng = random.Random(self.rng_seed)
        raw = db.sample_typed_subgraph(seeds, pattern, rng=rng)
        paths = self._decode_paths(db, list(raw.get("paths", [])))
        nodes: list[dict[str, Any]] = []
        for key, node in raw.get("nodes", {}).items():
            if node is None:
                continue
            node_id = db.key_to_string(key)
            nodes.append(
                {
                    "id": node_id,
                    "labels": list(node.labels),
                    "properties": normalize(node.properties or {}),
                }
            )
        nodes.sort(key=lambda item: item["id"])
        edges: list[dict[str, Any]] = []
        for key, edge in raw.get("edges", {}).items():
            if edge is None:
                continue
            record = edge.to_dict()
            properties = normalize(record.get("properties") or {})
            edge_type = properties.get("type")
            if hasattr(edge, "get_type"):
                try:
                    edge_type = edge.get_type()
                except Exception:
                    pass
            edges.append(
                {
                    "id": db.key_to_string(key),
                    "source": str(record.get("source")),
                    "target": str(record.get("target")),
                    "type": edge_type,
                    "properties": properties,
                }
            )
        edges.sort(key=lambda item: item["id"])
        result = {
            "mode": "subgraph",
            "seeds": seeds,
            "nodes": nodes,
            "returned_nodes": len(nodes),
            "edges": edges,
            "returned_edges": len(edges),
            "paths": paths,
            "returned_paths": len(paths),
            "truncated": False,
        }
        return bounded_json(
            result,
            (
                ("paths", "returned_paths", self.max_paths),
                ("nodes", "returned_nodes", self.max_nodes),
                ("edges", "returned_edges", self.max_edges),
            ),
            self.max_chars,
        )

    def _empty_envelope(self, mode: str) -> str:
        if mode == "subgraph":
            result: dict[str, Any] = {
                "mode": "subgraph",
                "seeds": [],
                "nodes": [],
                "returned_nodes": 0,
                "edges": [],
                "returned_edges": 0,
                "paths": [],
                "returned_paths": 0,
                "truncated": False,
            }
            return bounded_json(
                result,
                (
                    ("paths", "returned_paths", self.max_paths),
                    ("nodes", "returned_nodes", self.max_nodes),
                    ("edges", "returned_edges", self.max_edges),
                ),
                self.max_chars,
            )
        result = {
            "mode": "paths",
            "seeds": [],
            "paths": [],
            "returned_paths": 0,
            "truncated": False,
        }
        return bounded_json(result, (("paths", "returned_paths", self.max_paths),), self.max_chars)
