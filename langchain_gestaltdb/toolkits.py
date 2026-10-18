"""Factories for consistently configured graph-query and claim tools."""

from __future__ import annotations

from langchain_core.tools import BaseTool, BaseToolkit
from pydantic import ConfigDict, Field, StrictBool

from langchain_gestaltdb.claims import (
    GestaltDBClaimExplanationTool,
    GestaltDBClaimSearchTool,
    GestaltDBClaimStatusTool,
)
from langchain_gestaltdb.graph import GestaltDBGraph
from langchain_gestaltdb.tools import GestaltDBQueryTool, GestaltDBSchemaTool


class GestaltDBToolkit(BaseToolkit):
    """Default read-only toolkit; writes require trusted application opt-in."""

    model_config = ConfigDict(arbitrary_types_allowed=True)
    graph: GestaltDBGraph = Field(exclude=True)
    allow_writes: StrictBool = Field(default=False, exclude=True)
    allowed_procedures: tuple[str, ...] = Field(default=(), exclude=True)
    max_rows: int = Field(default=100, ge=1, le=1000, exclude=True)
    max_chars: int = Field(default=16_000, ge=256, le=1_000_000, exclude=True)

    def get_tools(self) -> list[BaseTool]:
        """Construct tools without transferring ownership of the graph handle."""
        return [
            GestaltDBSchemaTool(graph=self.graph, max_chars=self.max_chars),
            GestaltDBQueryTool(
                graph=self.graph,
                allow_writes=self.allow_writes,
                allowed_procedures=self.allowed_procedures,
                max_rows=self.max_rows,
                max_chars=self.max_chars,
            ),
        ]


class GestaltDBClaimToolkit(BaseToolkit):
    """Read-only epistemic claim toolkit; never writes to the graph.

    Claims are separate from mutable graph edges, so these tools read the
    bitemporal epistemic API directly rather than executing Cypher. Construction
    bounds are application-controlled and cannot be changed by an agent.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)
    graph: GestaltDBGraph = Field(exclude=True)
    max_evidence: int = Field(default=50, ge=1, le=1000, exclude=True)
    max_claims: int = Field(default=50, ge=1, le=1000, exclude=True)
    max_depth: int = Field(default=64, ge=0, le=1000, exclude=True)
    max_nodes: int = Field(default=1000, ge=1, le=100_000, exclude=True)
    max_edges: int = Field(default=2000, ge=0, le=200_000, exclude=True)
    max_chars: int = Field(default=16_000, ge=256, le=1_000_000, exclude=True)

    def get_tools(self) -> list[BaseTool]:
        """Construct tools without transferring ownership of the graph handle."""
        return [
            GestaltDBClaimStatusTool(
                graph=self.graph, max_evidence=self.max_evidence, max_chars=self.max_chars
            ),
            GestaltDBClaimSearchTool(
                graph=self.graph, max_claims=self.max_claims, max_chars=self.max_chars
            ),
            GestaltDBClaimExplanationTool(
                graph=self.graph,
                max_depth=self.max_depth,
                max_nodes=self.max_nodes,
                max_edges=self.max_edges,
                max_chars=self.max_chars,
            ),
        ]
