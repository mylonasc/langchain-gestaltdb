"""Read-only property-index advisor for trusted-application index management.

The advisor never creates, rebuilds, or otherwise mutates indexes: agents have
no DDL path. It reports which ``create_node_property_index`` or
``create_edge_property_index`` call the trusted application should run for a
planned predicate, whether that property index already exists (via
``index_statistics()``), whether it is stale after deferred ingestion (via
``stale_indexes()``), and the exact ``rebuild_deferred_indexes()`` call that
restores currency. Index lookups fail closed while stale, so the application
must rebuild before relying on the index.
"""

from __future__ import annotations

from typing import Any

from langchain_core.callbacks import CallbackManagerForToolRun
from langchain_core.tools import BaseTool
from pydantic import BaseModel, ConfigDict, Field, model_validator

from langchain_gestaltdb._tool_output import bounded_json
from langchain_gestaltdb.graph import GestaltDBGraph

_NODE_FAMILY = "node_property"
_EDGE_FAMILY = "edge_property"


def _statistics(graph: Any) -> dict[str, Any]:
    try:
        return dict(graph.index_statistics())
    except Exception:
        return {}


def _stale_families(graph: Any) -> tuple[str, ...]:
    try:
        return tuple(graph.stale_indexes())
    except Exception:
        return ()


def advise_index(
    graph: GestaltDBGraph,
    *,
    node_label: str | None = None,
    node_property: str | None = None,
    edge_type: str | None = None,
    edge_property: str | None = None,
) -> dict[str, Any]:
    """Advise which property index would serve a planned predicate.

    Exactly one predicate must be given: ``node_label`` with ``node_property``,
    or ``edge_type`` with ``edge_property``. The wrapped database handle is only
    read via ``index_statistics()`` and ``stale_indexes()``; no index is
    created or rebuilt. The returned ``create_call``/``rebuild_call`` strings
    are instructions for the trusted application, not executed actions.
    """
    _ = graph.graph
    node_given = node_label is not None or node_property is not None
    edge_given = edge_type is not None or edge_property is not None
    if node_given == edge_given:
        raise ValueError(
            "Advise exactly one predicate: node_label with node_property, "
            "or edge_type with edge_property."
        )
    if node_given and (not node_label or not node_property):
        raise ValueError("Node advice requires both node_label and node_property.")
    if edge_given and (not edge_type or not edge_property):
        raise ValueError("Edge advice requires both edge_type and edge_property.")

    statistics = _statistics(graph.graph)
    stale = _stale_families(graph.graph)
    if node_given:
        assert node_label is not None and node_property is not None
        indexed = tuple(statistics.get("indexed_node_properties", ()))
        exists = node_property in indexed
        is_stale = _NODE_FAMILY in stale
        create_call = f"graph.create_node_property_index({node_property!r})"
        advice: dict[str, Any] = {
            "target": "node",
            "node_label": node_label,
            "node_property": node_property,
        }
    else:
        assert edge_type is not None and edge_property is not None
        indexed = tuple(statistics.get("indexed_edge_properties", ()))
        exists = edge_property in indexed
        is_stale = _EDGE_FAMILY in stale
        create_call = f"graph.create_edge_property_index({edge_property!r})"
        advice = {
            "target": "edge",
            "edge_type": edge_type,
            "edge_property": edge_property,
        }
    if not exists:
        action = "create_index"
    elif is_stale:
        action = "rebuild_indexes"
    else:
        action = "current"
    advice.update(
        {
            "index_exists": exists,
            "stale": is_stale,
            "stale_index_families": sorted(stale),
            "action": action,
            "create_call": create_call,
            "rebuild_call": "graph.rebuild_deferred_indexes()",
            "note": (
                "Read-only advice only; the agent must not run DDL. "
                "The trusted application creates the index, then rebuilds "
                "deferred ingests before relying on index lookups."
            ),
        }
    )
    return advice


class IndexAdvisorInput(BaseModel):
    """Exactly one predicate: a node (label, property) or an edge (type, property)."""

    model_config = ConfigDict(extra="forbid")
    node_label: str | None = Field(default=None, min_length=1, max_length=256)
    node_property: str | None = Field(default=None, min_length=1, max_length=256)
    edge_type: str | None = Field(default=None, min_length=1, max_length=256)
    edge_property: str | None = Field(default=None, min_length=1, max_length=256)

    @model_validator(mode="after")
    def check_predicate(self) -> IndexAdvisorInput:
        node_given = self.node_label is not None or self.node_property is not None
        edge_given = self.edge_type is not None or self.edge_property is not None
        if node_given == edge_given:
            raise ValueError(
                "Provide node_label with node_property, or edge_type with edge_property."
            )
        if node_given and (not self.node_label or not self.node_property):
            raise ValueError("Node advice requires both node_label and node_property.")
        if edge_given and (not self.edge_type or not self.edge_property):
            raise ValueError("Edge advice requires both edge_type and edge_property.")
        return self


class GestaltDBIndexAdvisorTool(BaseTool):
    """Read-only index advice: existence, staleness, and the trusted-app rebuild call."""

    name: str = "gestaltdb_index_advisor"
    description: str = (
        "Read-only property-index advisor. Given a planned node predicate "
        "(node_label, node_property) or edge predicate (edge_type, edge_property), "
        "report whether the property index exists, whether it is stale after "
        "deferred ingestion, and which create/rebuild call the trusted application "
        "should run. Never creates or rebuilds indexes itself."
    )
    args_schema: type[BaseModel] = IndexAdvisorInput
    graph: GestaltDBGraph = Field(exclude=True)
    max_chars: int = Field(default=16_000, ge=256, le=1_000_000, exclude=True)
    handle_tool_error: bool = True

    def _run(
        self,
        node_label: str | None = None,
        node_property: str | None = None,
        edge_type: str | None = None,
        edge_property: str | None = None,
        run_manager: CallbackManagerForToolRun | None = None,
    ) -> str:
        advice = advise_index(
            self.graph,
            node_label=node_label,
            node_property=node_property,
            edge_type=edge_type,
            edge_property=edge_property,
        )
        return bounded_json(advice, (), self.max_chars)
