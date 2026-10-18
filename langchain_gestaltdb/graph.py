"""Thin lifecycle and parameterized-query adapter for an embedded GestaltDB."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from gestaltdb.graphdb import GraphDB

from langchain_gestaltdb._serialization import normalize


class GestaltDBGraph:
    """Wrap a database without taking ownership unless explicitly requested.

    ``create``/``open`` own their handles. Closing a borrowed adapter invalidates
    the adapter but leaves the caller's database open. This low-level adapter
    permits writes; use read-only tools for untrusted agent queries. Upstream
    errors propagate unchanged. Backend guarantees are not strengthened.
    """

    def __init__(self, graph: GraphDB, *, owns_graph: bool = False) -> None:
        if not isinstance(graph, GraphDB):
            raise TypeError("graph must be a GestaltDB GraphDB instance")
        self._graph = graph
        self._owns_graph = owns_graph
        self._closed = False

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

    def __enter__(self) -> GestaltDBGraph:
        _ = self.graph
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()
