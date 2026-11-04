"""Thin lifecycle and parameterized-query adapter for an embedded GestaltDB."""

from __future__ import annotations

import json
from collections.abc import Collection, Iterable
from contextlib import AbstractContextManager
from copy import deepcopy
from pathlib import Path
from typing import Any, cast

from gestaltdb.graphdb import GraphDB

from langchain_gestaltdb import _bulk, _compat
from langchain_gestaltdb._ingestion import ingest
from langchain_gestaltdb._read_only import validate_read_only
from langchain_gestaltdb._schema import discover_schema
from langchain_gestaltdb._serialization import normalize


class GestaltDBGraph:
    """Wrap a database without taking ownership unless explicitly requested.

    ``create``/``open`` own their handles. Closing a borrowed adapter invalidates
    the adapter but leaves the caller's database open. This low-level adapter
    permits writes; use read-only tools for untrusted agent queries. Upstream
    errors propagate unchanged. Backend guarantees are not strengthened.

    Read-only inspection (``manifest``, ``database_id``, ``index_statistics``,
    ``stale_indexes``) passes through to the underlying handle and fails closed
    once this adapter is closed. No raw write transaction is exposed to agents:
    writes go through validated ingestion, claim tools, or explicit trusted use
    of ``graph``. To pin several reads to one visible state, use
    ``adapter.read_view(...)`` (temporal horizon) or
    ``adapter.graph.current_read_view()`` (backend snapshot); both are backend
    dependent (see :meth:`query`).
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
        cls, path: str | Path, *, backend: str = "pyrex", serializer: str = "json", **options: Any
    ) -> GestaltDBGraph:
        """Create an owned database; default to PyRex/RocksDB, never silently fall back.

        Explicit ``backend="lmdb"``/``"leveldb"`` overrides require their extras.
        ``open`` always follows the persisted manifest, not this creation default.
        """
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

    @property
    def manifest(self) -> dict[str, Any]:
        """Return a copy of the persisted backend/serializer manifest.

        Read-only passthrough to ``GraphDB.manifest``; fails closed once this
        adapter is closed. ``manifest["backend"]["name"]`` is the persisted
        backend (``"pyrex"``, ``"lmdb"``, or ``"leveldb"``); ``open`` always
        follows it, never the creation default. The copy is detached, so
        mutating it cannot affect the database.
        """
        return cast("dict[str, Any]", self.graph.manifest)

    @property
    def database_id(self) -> str:
        """Return the stable UUID persisted with this database.

        Read-only passthrough to ``GraphDB.database_id``; fails closed once
        this adapter is closed. The value is stable across close/reopen of the
        same database directory and identifies the source in read-view
        provenance. It is not a snapshot, ordering, or secrecy guarantee.
        """
        return cast("str", self.graph.database_id)

    def index_statistics(self) -> dict[str, object]:
        """Return persisted property-index definitions visible to the planner.

        Read-only passthrough to ``GraphDB.index_statistics``; fails closed
        once this adapter is closed. Keys are ``"indexed_node_properties"`` and
        ``"indexed_edge_properties"`` with sorted tuple values. Reports
        definitions, not freshness; consult :meth:`stale_indexes` for deferred
        families still requiring a rebuild.
        """
        return cast("dict[str, object]", self.graph.index_statistics())

    def stale_indexes(self) -> tuple[str, ...]:
        """Return sorted index families requiring a rebuild.

        Read-only passthrough to ``GraphDB.stale_indexes``; fails closed once
        this adapter is closed. A fresh database reports ``()``; deferred
        ingestion (``index_mode="defer"``) marks families such as
        ``"node_label"``/``"edge_type"`` stale until
        ``graph.rebuild_deferred_indexes()`` runs. Canonical entity reads bypass
        stale secondary indexes, but indexed lookups must not be trusted while
        their family is listed here.
        """
        return cast("tuple[str, ...]", self.graph.stale_indexes())

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

        ``read_snapshot`` selects backend snapshot behavior and otherwise
        propagates to ``GraphDB.query`` unchanged: ``None`` (default) uses a
        unified read snapshot automatically where the backend supports one;
        ``True`` requires one and fails where it is unavailable; ``False`` opts
        out. LMDB always supports snapshots and exposes verifiable MVCC
        provenance via ``graph.current_read_view()``. Transactional PyRex
        (``backend_options={"transactional": True}``) can pin reads but its
        provenance is not verifiable. LevelDB and default (non-transactional)
        PyRex reject explicit snapshot requests with ``NotImplementedError``;
        reads there are best-effort, not pinned across the query.

        To pin several reads to one visible state, do not rely on repeated
        ``read_snapshot`` queries. Instead share one horizon explicitly::

            with adapter.read_view(valid_time=...) as view:
                ...
            with adapter.graph.current_read_view() as view:
                ...

        ``read_view`` pins a temporal commit horizon on either supported
        GestaltDB layout (0.12 facade or 0.17 ``temporal.open_read_view``);
        ``current_read_view`` pins mutable entities, adjacency, and indexes to
        one backend snapshot where supported. Both are context managers, must
        be closed via the ``with`` block, and inherit the backend limits above.
        Lookup and traversal order is never guaranteed; compare ID sets, and
        decode typed traversal byte IDs with ``graph.key_to_string(...)``
        before comparing with string IDs. There is no whole-graph enumeration
        API.
        """
        result = self.graph.query(query, parameters=params, read_snapshot=read_snapshot)
        return [normalize(record) for record in result.records]

    def read_view(
        self,
        *,
        valid_time: Any,
        system_time: Any = None,
        through_commit: int | None = None,
    ) -> AbstractContextManager[Any]:
        """Pin repeated reads to one temporal horizon across supported layouts.

        Yields a ``GraphReadView`` exposing point-in-time entity reads
        (``get_node_as_of``/``get_edge_as_of``/iteration) at ``valid_time`` and
        an optional system horizon (``system_time`` or ``through_commit``).
        On 0.12 this is ``GraphDB.read_view``; on 0.17 it is
        ``temporal.open_read_view`` authenticated with this database's
        manifest and ID. The view must be used as a context manager; it is
        backend dependent and adds no snapshot guarantee beyond upstream.
        """
        return _compat.open_read_view(
            self.graph,
            valid_time=valid_time,
            system_time=system_time,
            through_commit=through_commit,
        )

    def close(self) -> None:
        """Idempotently close the adapter and, only when owned, its database."""
        if not self._closed:
            if self._owns_graph:
                self._graph.close()
            self._closed = True

    def query_read_only(
        self,
        query: str,
        params: dict[str, Any] | None = None,
        *,
        allowed_procedures: Collection[str] = (),
        read_snapshot: bool | None = None,
    ) -> list[dict[str, Any]]:
        """Validate parsed Cypher recursively before executing any database code.

        Read-only means write prevention, not a computational budget. Only
        pg.sample_typed_paths/kg.entails can be explicitly allowlisted; both
        may be expensive. Query text is limited to 10000 characters.

        ``read_snapshot`` has the same backend-dependent semantics as
        :meth:`query`: ``None`` snapshots automatically where supported,
        ``True`` requires a snapshot (LMDB or transactional PyRex; LevelDB and
        default PyRex raise ``NotImplementedError``), ``False`` opts out.
        """
        _ = self.graph
        validate_read_only(query, allowed_procedures)
        return self.query(query, params, read_snapshot=read_snapshot)

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

    def add_graph_documents(
        self, graph_documents: Iterable[Any], include_source: bool = False
    ) -> None:
        """Merge validated graph documents using incremental writes.

        Accepts local GraphDocument or equivalent mappings/Pydantic models.
        This operation is not whole-import atomic. Sources are ordinary graph
        provenance, not epistemic claims. The schema cache is invalidated.
        """
        self._schema = None
        ingest(self.graph, graph_documents, include_source)

    def ingest_polars(
        self,
        node_df: Any,
        edge_df: Any,
        *,
        node_id: str = "node_id",
        labels: Any = "labels",
        node_property_columns: list[str] | tuple[str, ...] | None = None,
        edge_id: str = "edge_id",
        source: str = "source",
        target: str = "target",
        edge_type: str = "edge_type",
        edge_property_columns: list[str] | tuple[str, ...] | None = None,
        chunk_size: int = 100_000,
        index_mode: Any = "defer_rebuild",
    ) -> dict[str, Any]:
        """Bulk-load Polars node/edge frames for trusted application code.

        This is not an agent tool: agents must never call it directly. Frames
        are validated before the first write and the schema cache is
        invalidated, but no whole-import atomicity is promised: backend I/O
        failures can leave a partial import.

        ``index_mode`` is ``MAINTAIN`` (update indexes during ingest, slower),
        ``DEFER`` (leave secondary indexes stale until the trusted app calls
        ``graph.rebuild_deferred_indexes()``; indexed lookups fail closed while
        stale), or ``DEFER_REBUILD`` (rebuild before returning; the default).

        CSV pitfalls: declare explicit numeric dtypes when reading CSVs (for
        example ``schema_overrides={"score": pl.Float64}``), and re-wrap a
        string ``labels`` column into one list per row (bare strings would be
        shredded into per-character labels). Returns the upstream counts with
        ``nodes``, ``edges``, ``rebuilt_indexes`` and ``stale_indexes``.
        """
        self._schema = None
        try:
            return _bulk.ingest_polars_frames(
                self.graph,
                node_df,
                edge_df,
                node_id=node_id,
                labels=labels,
                node_property_columns=node_property_columns,
                edge_id=edge_id,
                source=source,
                target=target,
                edge_type=edge_type,
                edge_property_columns=edge_property_columns,
                chunk_size=chunk_size,
                index_mode=index_mode,
            )
        finally:
            self._schema = None

    def ingest_arrow(
        self,
        node_ids: Any,
        edge_ids: Any,
        sources: Any,
        targets: Any,
        edge_types: Any,
        *,
        labels: Any = None,
        node_properties: dict[str, Any] | None = None,
        edge_properties: dict[str, Any] | None = None,
        chunk_size: int = 100_000,
        index_mode: Any = "defer_rebuild",
    ) -> dict[str, Any]:
        """Bulk-load Arrow-like node/edge columns for trusted application code.

        This is not an agent tool: agents must never call it directly. Columns
        are validated before the first write and the schema cache is
        invalidated, but no whole-import atomicity is promised: backend I/O
        failures can leave a partial import. Plain Python sequences work
        without pyarrow installed; ``pa.Array`` inputs require it.

        ``index_mode`` is ``MAINTAIN`` (update indexes during ingest, slower),
        ``DEFER`` (leave secondary indexes stale until the trusted app calls
        ``graph.rebuild_deferred_indexes()``; indexed lookups fail closed while
        stale), or ``DEFER_REBUILD`` (rebuild before returning; the default).
        Numeric columns need explicit dtypes (CSV text defaults to strings).
        Returns the upstream counts with ``nodes``, ``edges``,
        ``rebuilt_indexes`` and ``stale_indexes``.
        """
        self._schema = None
        try:
            return _bulk.ingest_arrow_columns(
                self.graph,
                node_ids,
                edge_ids,
                sources,
                targets,
                edge_types,
                labels=labels,
                node_properties=node_properties,
                edge_properties=edge_properties,
                chunk_size=chunk_size,
                index_mode=index_mode,
            )
        finally:
            self._schema = None

    def __enter__(self) -> GestaltDBGraph:
        _ = self.graph
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()
