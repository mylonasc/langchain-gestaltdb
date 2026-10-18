"""Thin lifecycle and parameterized-query adapter for an embedded GestaltDB."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

from gestaltdb.graphdb import GraphDB

from langchain_gestaltdb._schema import discover_schema
from langchain_gestaltdb._serialization import normalize


class GestaltDBGraph:
    """Wrap a database without taking ownership unless explicitly requested.

    ``create``/``open`` own their handles. Closing a borrowed adapter invalidates
    the adapter but leaves the caller's database open. This low-level adapter
    permits writes; use read-only tools for untrusted agent queries. Upstream
    errors propagate unchanged. Backend guarantees are not strengthened.
    """

    def __init__(
        self,
        graph: GraphDB,
        *,
        owns_graph: bool = False,
        schema_node_limit: int | None = 100,
        schema_edge_limit: int | None = 100,
        schema_override: dict[str, Any] | None = None,
    ) -> None:
        if not isinstance(graph, GraphDB):
            raise TypeError("graph must be a GestaltDB GraphDB instance")
        self._graph = graph
        self._owns_graph = owns_graph
        self._closed = False
        for limit in (schema_node_limit, schema_edge_limit):
            if limit is not None and (type(limit) is not int or limit < 1):
                raise ValueError("Schema limits must be positive integers or None (exhaustive)")
        self._schema_node_limit = schema_node_limit
        self._schema_edge_limit = schema_edge_limit
        self._schema_override = deepcopy(schema_override)
        self._schema: dict[str, Any] | None = None

    @classmethod
    def create(
        cls, path: str | Path, *, backend: str, serializer: str = "json", **options: Any
    ) -> GestaltDBGraph:
        """Create an owned manifest-backed database with explicit backend selection."""
        return cls(
            GraphDB.create(path, backend=backend, serializer=serializer, **options), owns_graph=True
        )

    @classmethod
    def open(cls, path: str | Path, **options: Any) -> GestaltDBGraph:
        """Open an owned database using its persisted backend/serializer manifest."""
        return cls(GraphDB.open(path, **options), owns_graph=True)

    @property
    def graph(self) -> GraphDB:
        """The underlying handle; accessing it after closing the adapter fails."""
        if self._closed:
            raise RuntimeError("GestaltDBGraph is closed")
        return self._graph

    def query(
        self,
        query: str,
        params: dict[str, Any] | None = None,
        *,
        read_snapshot: bool | None = None,
    ) -> list[dict[str, Any]]:
        """Execute supported Cypher and normalize records to fresh JSON values.

        Native entities become dictionaries, paths become nodes/edges maps,
        and temporal values retain exact fields with a ``$type`` tag. Invalid
        UTF-8, unknown objects, cycles and non-finite floats fail explicitly.
        No timeout, thread safety, or backend-independent atomicity is added.
        """
        result = self.graph.query(query, parameters=params, read_snapshot=read_snapshot)
        return [normalize(record) for record in result.records]

    def close(self) -> None:
        """Idempotently close the adapter and, only when owned, its database."""
        if not self._closed:
            if self._owns_graph:
                self._graph.close()
            self._closed = True

    @property
    def get_structured_schema(self) -> dict[str, Any]:
        """Return a defensive copy of the cached bounded schema (lazy on first access)."""
        _ = self.graph
        if self._schema is None:
            self.refresh_schema()
        return deepcopy(self._schema) if self._schema is not None else {}

    @property
    def get_schema(self) -> str:
        """Return stable JSON schema text suitable for tool/LLM context."""
        return json.dumps(self.get_structured_schema, sort_keys=True, ensure_ascii=False)

    def refresh_schema(self) -> None:
        """Refresh from canonical entities, or re-copy an explicit trusted override.

        Inspects at most the configured nodes/edges plus two endpoints per edge.
        Set a limit to None to explicitly permit exhaustive inspection. Sampling
        is backend-key ordered, not representative or a coherent snapshot.
        """
        graph = self.graph
        schema = self._schema_override
        if schema is None:
            schema = discover_schema(graph, self._schema_node_limit, self._schema_edge_limit)
        self._schema = normalize(schema)

    def __enter__(self) -> GestaltDBGraph:
        _ = self.graph
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()
