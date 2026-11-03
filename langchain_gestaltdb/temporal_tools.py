"""Read-only bitemporal graph-history tools with bounded, valid JSON output.

Temporal node/edge versions are immutable assertions separate from mutable
current-state records. Valid intervals are half-open ``[start, end)``: a
version is visible when ``start <= valid_time < end`` (an ``end`` of ``None``
means still open). System horizons are inclusive visible commit prefixes:
pass either ``system_time`` or ``through_commit``, never both semantics at
once; ``through_commit`` pins history to commits ``<=`` the given ordinal.

These tools only read. They never write, migrate, reclaim, or rebuild:

- ``migrate_time_indexed_edges``, ``list_temporal_orphans`` and
  ``reclaim_temporal_orphans`` are trusted-application runbook operations,
  not agent tools. Back up the complete database and stop writers before
  migration; run migration with ``delete_legacy=False``, validate
  representative as-of reads, then rerun with ``delete_legacy=True``.
  List orphans before any bounded ``reclaim_temporal_orphans(through_commit=...)``
  call; malformed artifacts fail closed and commit IDs are never reused.
- Deferred temporal writes make indexed reads fail closed until the trusted
  application runs ``rebuild_temporal_indexes()`` (or
  ``rebuild_deferred_indexes()``). These tools surface that state as an
  explicit error with a rebuild hint and never rebuild automatically.
"""

from __future__ import annotations

from typing import Any, Literal

from langchain_core.callbacks import CallbackManagerForToolRun
from langchain_core.tools import BaseTool, ToolException
from pydantic import BaseModel, ConfigDict, Field

from langchain_gestaltdb._compat import call_upstream
from langchain_gestaltdb._serialization import normalize
from langchain_gestaltdb._tool_output import bounded_json
from langchain_gestaltdb.graph import GestaltDBGraph

_TEMPORAL_ERROR = (
    "GestaltDB temporal read failed; check the identity, times, endpoint anchor and database state."
)
_STALE_TEMPORAL_ERROR = (
    "Temporal indexes are stale; ask the trusted application to run "
    "rebuild_temporal_indexes() or rebuild_deferred_indexes() before retrying. "
    "Reads fail closed and never rebuild automatically."
)


def _enum_value(value: Any) -> Any:
    """Return a str-enum's value, leaving plain values unchanged."""
    return value.value if hasattr(value, "value") else value


def _is_stale_error(exc: Exception) -> bool:
    """Detect fail-closed stale-index failures without echoing backend detail."""
    message = str(exc).lower()
    return "stale" in message or "rebuild" in message


def _serialize_version(version: Any, payload_key: str) -> dict[str, Any]:
    """Serialize a node/edge version with its bitemporal coordinates."""
    payload = getattr(version, "node", None)
    if payload is None:
        payload = getattr(version, "edge", None)
    return {
        "version_id": version.version_id,
        "logical_id": version.logical_id,
        "commit_id": version.commit_id,
        "commit_ordinal": version.commit_ordinal,
        "system_time": normalize(version.system_time),
        "valid": normalize(version.valid),
        "operation": _enum_value(version.operation),
        "payload_hash": version.payload_hash,
        "supersedes_version_id": version.supersedes_version_id,
        "reason": version.reason,
        payload_key: normalize(payload),
    }


def _horizon_kwargs(system_time: str | None, through_commit: int | None) -> dict[str, Any]:
    """Forward only the horizons the caller supplied."""
    kwargs: dict[str, Any] = {}
    if system_time is not None:
        kwargs["system_time"] = system_time
    if through_commit is not None:
        kwargs["through_commit"] = through_commit
    return kwargs


class AsOfInput(BaseModel):
    """Logical identity plus a bitemporal horizon; bounds are not agent-controlled."""

    model_config = ConfigDict(extra="forbid")
    logical_id: str = Field(min_length=1, max_length=1024, description="Logical node/edge ID")
    valid_time: str = Field(min_length=1, max_length=64, description="ISO-8601 valid-time instant")
    system_time: str | None = Field(
        default=None, description="ISO-8601 system-time horizon (inclusive)"
    )
    through_commit: int | None = Field(
        default=None, ge=0, description="System horizon as an inclusive commit ordinal"
    )


class TemporalScanInput(BaseModel):
    """Catalog scan at one horizon; edge scans need exactly one endpoint anchor."""

    model_config = ConfigDict(extra="forbid")
    kind: Literal["node", "edge"] = Field(description="Version catalog to scan")
    valid_time: str = Field(min_length=1, max_length=64, description="ISO-8601 valid-time instant")
    system_time: str | None = Field(
        default=None, description="ISO-8601 system-time horizon (inclusive)"
    )
    through_commit: int | None = Field(
        default=None, ge=0, description="System horizon as an inclusive commit ordinal"
    )
    source: str | None = Field(
        default=None, max_length=1024, description="Outgoing edge anchor; rejects target"
    )
    target: str | None = Field(
        default=None, max_length=1024, description="Incoming edge anchor; rejects source"
    )
    edge_type: str | None = Field(default=None, max_length=1024)


class _TemporalTool(BaseTool):
    """Shared graph handle, output cap and redacted failure for temporal tools."""

    graph: GestaltDBGraph = Field(exclude=True)
    max_chars: int = Field(default=16_000, ge=256, le=1_000_000, exclude=True)
    handle_tool_error: bool = True


class GestaltDBNodeAsOfTool(_TemporalTool):
    """Resolve one logical node at a valid time and optional system horizon.

    Valid intervals are half-open ``[start, end)``; system horizons are
    inclusive visible commit prefixes. Returns ``found: false`` when no
    version covers the instant instead of failing.
    """

    name: str = "gestaltdb_node_as_of"
    description: str = (
        "Read-only lookup of a versioned node's properties, labels and bitemporal "
        "coordinates at a valid time with an optional system horizon. Half-open "
        "valid intervals; commit-prefix system horizons."
    )
    args_schema: type[BaseModel] = AsOfInput

    def _run(
        self,
        logical_id: str,
        valid_time: str,
        system_time: str | None = None,
        through_commit: int | None = None,
        run_manager: CallbackManagerForToolRun | None = None,
    ) -> str:
        try:
            version = call_upstream(
                self.graph.graph,
                "get_node_as_of",
                logical_id,
                valid_time=valid_time,
                **_horizon_kwargs(system_time, through_commit),
            )
        except Exception as exc:
            if _is_stale_error(exc):
                raise ToolException(_STALE_TEMPORAL_ERROR) from None
            raise ToolException(_TEMPORAL_ERROR) from None
        envelope: dict[str, Any] = {
            "logical_id": logical_id,
            "valid_time": valid_time,
            "system_time": system_time,
            "through_commit": through_commit,
        }
        if version is None:
            envelope.update({"found": False, "version": None})
        else:
            envelope.update({"found": True, "version": _serialize_version(version, "node")})
        return bounded_json(envelope, (), self.max_chars)


class GestaltDBEdgeAsOfTool(_TemporalTool):
    """Resolve one logical edge at a valid time and optional system horizon.

    Valid intervals are half-open ``[start, end)``; system horizons are
    inclusive visible commit prefixes. Returns ``found: false`` when no
    version covers the instant instead of failing.
    """

    name: str = "gestaltdb_edge_as_of"
    description: str = (
        "Read-only lookup of a versioned edge's endpoints, properties and bitemporal "
        "coordinates at a valid time with an optional system horizon. Half-open "
        "valid intervals; commit-prefix system horizons."
    )
    args_schema: type[BaseModel] = AsOfInput

    def _run(
        self,
        logical_id: str,
        valid_time: str,
        system_time: str | None = None,
        through_commit: int | None = None,
        run_manager: CallbackManagerForToolRun | None = None,
    ) -> str:
        try:
            version = call_upstream(
                self.graph.graph,
                "get_edge_as_of",
                logical_id,
                valid_time=valid_time,
                **_horizon_kwargs(system_time, through_commit),
            )
        except Exception as exc:
            if _is_stale_error(exc):
                raise ToolException(_STALE_TEMPORAL_ERROR) from None
            raise ToolException(_TEMPORAL_ERROR) from None
        envelope: dict[str, Any] = {
            "logical_id": logical_id,
            "valid_time": valid_time,
            "system_time": system_time,
            "through_commit": through_commit,
        }
        if version is None:
            envelope.update({"found": False, "version": None})
        else:
            envelope.update({"found": True, "version": _serialize_version(version, "edge")})
        return bounded_json(envelope, (), self.max_chars)


class GestaltDBTemporalScanTool(_TemporalTool):
    """Iteratively scan one historical catalog, bounded by entity and char caps.

    Node scans walk the whole node catalog at the horizon. Edge scans are
    endpoint-anchored upstream: exactly one of ``source`` (outgoing) or
    ``target`` (incoming) is required. Output caps bound output only, not
    database work or intermediate materialization.
    """

    name: str = "gestaltdb_temporal_scan"
    description: str = (
        "Read-only iterative scan of versioned nodes, or of endpoint-anchored "
        "versioned edges, visible at a valid time with an optional system horizon. "
        "Edge scans require exactly one of source or target. Bounded output."
    )
    args_schema: type[BaseModel] = TemporalScanInput
    max_entities: int = Field(default=50, ge=1, le=1000, exclude=True)

    def _run(
        self,
        kind: Literal["node", "edge"],
        valid_time: str,
        system_time: str | None = None,
        through_commit: int | None = None,
        source: str | None = None,
        target: str | None = None,
        edge_type: str | None = None,
        run_manager: CallbackManagerForToolRun | None = None,
    ) -> str:
        try:
            horizon = _horizon_kwargs(system_time, through_commit)
            if kind == "node":
                versions = list(
                    call_upstream(
                        self.graph.graph, "iter_nodes_as_of", valid_time=valid_time, **horizon
                    )
                )
                payload_key = "node"
            else:
                if (source is None) == (target is None):
                    raise ToolException(
                        "Edge scans require exactly one endpoint anchor: "
                        "source for outgoing or target for incoming edges."
                    )
                kwargs: dict[str, Any] = {"valid_time": valid_time, **horizon}
                if source is not None:
                    kwargs.update({"direction": "out", "source": source})
                else:
                    kwargs.update({"direction": "in", "target": target})
                if edge_type is not None:
                    kwargs["edge_type"] = edge_type
                versions = list(call_upstream(self.graph.graph, "iter_edges_as_of", **kwargs))
                payload_key = "edge"
            entities = [_serialize_version(version, payload_key) for version in versions]
        except ToolException:
            raise
        except Exception as exc:
            if _is_stale_error(exc):
                raise ToolException(_STALE_TEMPORAL_ERROR) from None
            raise ToolException(_TEMPORAL_ERROR) from None
        result: dict[str, Any] = {
            "kind": kind,
            "valid_time": valid_time,
            "system_time": system_time,
            "through_commit": through_commit,
            "filters": {"source": source, "target": target, "edge_type": edge_type},
            "entities": entities,
            "returned_entities": len(entities),
            "truncated": False,
        }
        return bounded_json(
            result, (("entities", "returned_entities", self.max_entities),), self.max_chars
        )
