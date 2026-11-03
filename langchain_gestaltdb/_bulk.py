"""Trusted-app bulk ingestion over columnar Arrow/Polars loads (GEP-07).

This module wraps the upstream ``GraphDB.ingest_polars`` / ``GraphDB.ingest_arrow``
bulk paths for trusted application code. It is not an agent tool surface: agents
must never call it directly. Unlike :mod:`langchain_gestaltdb._ingestion`, which
validates and merges small object graphs incrementally, these helpers load whole
node/edge tables at once with chunked writes and a caller-chosen secondary-index
strategy.

No whole-import atomicity is promised: validation runs before the first write,
but backend I/O failures can still leave a partial import. ``DEFER`` mode leaves
secondary property indexes stale until the trusted app calls
``graph.rebuild_deferred_indexes()``; indexed lookups fail closed while stale.
``DEFER_REBUILD`` (the default) rebuilds before returning, and ``MAINTAIN``
updates indexes during ingest at a throughput cost.

CSV pitfalls (frames built via ``pl.read_csv`` or equivalent):

- Declare explicit numeric dtypes (for example
  ``pl.read_csv(path, schema_overrides={"score": pl.Float64})``). Inferred
  integer columns can fail or silently coerce against float properties.
- A ``labels`` column must contain one list of labels per row. CSV text arrives
  as a single string per row and must be re-wrapped into lists (for example
  ``pl.col("labels").str.split(",")``); passing bare strings shreds each label
  into per-character labels.
"""

from __future__ import annotations

from typing import Any

_INDEX_MODES = ("maintain", "defer", "defer_rebuild")


def normalize_index_mode(index_mode: Any) -> Any:
    """Accept an index-maintenance mode enum or a case-insensitive name.

    Returns the upstream ``gestaltdb.IndexMaintenanceMode`` member when that
    enum is importable, otherwise the lowercase mode string the installed
    backend accepts. Anything else raises ``ValueError``.
    """
    try:
        from gestaltdb import IndexMaintenanceMode
    except ImportError:  # pragma: no cover - pinned upstream always exposes it
        IndexMaintenanceMode = None
    if IndexMaintenanceMode is not None and isinstance(index_mode, IndexMaintenanceMode):
        return index_mode
    if isinstance(index_mode, str) and index_mode.lower() in _INDEX_MODES:
        lowered = index_mode.lower()
        if IndexMaintenanceMode is not None:
            return IndexMaintenanceMode(lowered)
        return lowered
    raise ValueError(
        f"index_mode must be one of MAINTAIN, DEFER, DEFER_REBUILD (got {index_mode!r})"
    )


def require_chunk_size(chunk_size: Any) -> int:
    """Reject non-positive or non-integer chunk sizes before any I/O."""
    if type(chunk_size) is not int or chunk_size < 1:
        raise ValueError("chunk_size must be a positive integer")
    return chunk_size


def _require_column_names(value: Any, name: str) -> list[str] | tuple[str, ...] | None:
    if value is None:
        return None
    if (
        not isinstance(value, (list, tuple))
        or not value
        or any(type(item) is not str or not item for item in value)
    ):
        raise ValueError(f"{name} must be a non-empty list of column names or None")
    return value


def _require_present(columns: list[str], required: list[str], kind: str) -> None:
    missing = [column for column in required if column not in columns]
    if missing:
        raise ValueError(f"{kind} frame is missing required columns: {', '.join(missing)}")


def _frame_columns(pl: Any, frame: Any, kind: str) -> list[str]:
    # LazyFrame.columns resolves (and warns about resolving) the schema, so use
    # collect_schema() for lazy frames and .columns only for eager ones.
    if isinstance(frame, pl.LazyFrame):
        return list(frame.collect_schema().names())
    if isinstance(frame, pl.DataFrame):
        return list(frame.columns)
    raise TypeError(f"{kind}_df must be a polars.DataFrame or polars.LazyFrame")


def validate_polars_frames(
    *,
    node_df: Any,
    edge_df: Any,
    node_id: str,
    labels: Any,
    node_property_columns: Any,
    edge_id: str,
    source: str,
    target: str,
    edge_type: str,
    edge_property_columns: Any,
) -> Any:
    """Validate Polars frames before any write; return the imported polars module.

    Raises ``ImportError`` with an install hint when polars is not installed,
    ``TypeError`` for non-frame inputs, and ``ValueError`` for missing columns,
    bad column-name arguments, or a string-typed ``labels`` column (the CSV
    pitfall: bare strings would be shredded into per-character labels).
    """
    try:
        import polars as pl  # type: ignore[import-not-found]
    except ImportError as exc:
        raise ImportError(
            "Bulk Polars ingestion needs the optional 'polars' dependency; "
            "install it with `pip install polars` (or `uv add polars`)"
        ) from exc
    if not isinstance(node_df, (pl.DataFrame, pl.LazyFrame)):
        raise TypeError("node_df must be a polars.DataFrame or polars.LazyFrame")
    if not isinstance(edge_df, (pl.DataFrame, pl.LazyFrame)):
        raise TypeError("edge_df must be a polars.DataFrame or polars.LazyFrame")
    for name in (node_id, edge_id, source, target, edge_type):
        if type(name) is not str or not name:
            raise ValueError("Frame column names must be non-empty strings")
    _require_column_names(node_property_columns, "node_property_columns")
    _require_column_names(edge_property_columns, "edge_property_columns")
    node_columns = _frame_columns(pl, node_df, "node")
    edge_columns = _frame_columns(pl, edge_df, "edge")
    _require_present(node_columns, [node_id], "node")
    _require_present(edge_columns, [edge_id, source, target, edge_type], "edge")
    if isinstance(labels, str):
        _require_present(node_columns, [labels], "node")
        schema = node_df.schema if isinstance(node_df, pl.DataFrame) else node_df.collect_schema()
        if schema.get(labels) == pl.String:
            raise ValueError(
                f"labels column {labels!r} holds strings; re-wrap each row into a "
                "list of labels (CSV text is not a label list)"
            )
    elif labels is not None and (
        not isinstance(labels, (list, tuple))
        or not labels
        or any(type(item) is not str or not item for item in labels)
    ):
        raise ValueError("labels must be a column name, a non-empty list of labels, or None")
    for column in list(node_property_columns or []):
        _require_present(node_columns, [column], "node property")
    for column in list(edge_property_columns or []):
        _require_present(edge_columns, [column], "edge property")
    return pl


def _sized_length(value: Any, name: str) -> int:
    try:
        return len(value)
    except TypeError as exc:
        raise TypeError(f"{name} must be a sized Arrow-like column") from exc


def validate_arrow_columns(
    *,
    node_ids: Any,
    edge_ids: Any,
    sources: Any,
    targets: Any,
    edge_types: Any,
    labels: Any,
    node_properties: Any,
    edge_properties: Any,
) -> None:
    """Validate Arrow-like columns structurally (no pyarrow import required).

    Plain Python sequences work without pyarrow installed; ``pa.Array`` inputs
    require the caller to have installed it. Raises ``TypeError`` for unsized
    columns or non-mapping property bundles and ``ValueError`` for length
    mismatches or bad property names.
    """
    node_count = _sized_length(node_ids, "node_ids")
    edge_count = _sized_length(edge_ids, "edge_ids")
    for value, name in ((sources, "sources"), (targets, "targets"), (edge_types, "edge_types")):
        if _sized_length(value, name) != edge_count:
            raise ValueError(
                f"edge_ids, sources, targets and edge_types must have equal "
                f"lengths (edge_ids has {edge_count})"
            )
    if labels is not None and _sized_length(labels, "labels") != node_count:
        raise ValueError(f"labels must match node_ids in length ({node_count} nodes)")
    for bundle, name in (
        (node_properties, "node_properties"),
        (edge_properties, "edge_properties"),
    ):
        if bundle is None:
            continue
        if not isinstance(bundle, dict):
            raise TypeError(f"{name} must be a mapping of property name to column or None")
        expected = node_count if name == "node_properties" else edge_count
        for key, column in bundle.items():
            if type(key) is not str or not key:
                raise ValueError(f"{name} keys must be non-empty strings")
            if _sized_length(column, f"{name}[{key!r}]") != expected:
                raise ValueError(
                    f"{name}[{key!r}] must match "
                    f"{'node_ids' if name == 'node_properties' else 'edge_ids'} "
                    f"in length ({expected})"
                )


def ingest_polars_frames(
    graph: Any,
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
    """Validate Polars frames, bulk-load them, and return upstream counts.

    Returns the upstream result mapping with ``nodes``, ``edges``,
    ``rebuilt_indexes`` and ``stale_indexes`` entries. See the module docstring
    for atomicity, index-mode, and CSV-dtype guidance.
    """
    validate_polars_frames(
        node_df=node_df,
        edge_df=edge_df,
        node_id=node_id,
        labels=labels,
        node_property_columns=node_property_columns,
        edge_id=edge_id,
        source=source,
        target=target,
        edge_type=edge_type,
        edge_property_columns=edge_property_columns,
    )
    return dict(
        graph.ingest_polars(
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
            chunk_size=require_chunk_size(chunk_size),
            index_mode=normalize_index_mode(index_mode),
        )
    )


def ingest_arrow_columns(
    graph: Any,
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
    """Validate Arrow-like columns, bulk-load them, and return upstream counts.

    Returns the upstream result mapping with ``nodes``, ``edges``,
    ``rebuilt_indexes`` and ``stale_indexes`` entries. See the module docstring
    for atomicity, index-mode, and CSV-dtype guidance.
    """
    validate_arrow_columns(
        node_ids=node_ids,
        edge_ids=edge_ids,
        sources=sources,
        targets=targets,
        edge_types=edge_types,
        labels=labels,
        node_properties=node_properties,
        edge_properties=edge_properties,
    )
    return dict(
        graph.ingest_arrow(
            node_ids,
            edge_ids,
            sources,
            targets,
            edge_types,
            labels=labels,
            node_properties=node_properties,
            edge_properties=edge_properties,
            chunk_size=require_chunk_size(chunk_size),
            index_mode=normalize_index_mode(index_mode),
        )
    )
