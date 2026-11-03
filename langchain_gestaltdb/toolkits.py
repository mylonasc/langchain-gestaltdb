"""Factories for consistently configured graph-query and claim tools."""

from __future__ import annotations

from langchain_core.tools import BaseTool, BaseToolkit
from pydantic import ConfigDict, Field, StrictBool

from langchain_gestaltdb.claim_writes import (
    GestaltDBClaimAssertTool,
    GestaltDBClaimCorrectTool,
    GestaltDBClaimRetractTool,
)
from langchain_gestaltdb.claims import (
    GestaltDBClaimExplanationTool,
    GestaltDBClaimSearchTool,
    GestaltDBClaimStatusTool,
)
from langchain_gestaltdb.graph import GestaltDBGraph
from langchain_gestaltdb.index_advisor import GestaltDBIndexAdvisorTool
from langchain_gestaltdb.modal import GestaltDBEntailsTool, GestaltDBWorldAccessibilityTool
from langchain_gestaltdb.rules import (
    GestaltDBRuleCreateTool,
    GestaltDBRuleGetTool,
    GestaltDBRuleListTool,
    GestaltDBRuleMaintainTool,
    GestaltDBRuleRunTool,
)
from langchain_gestaltdb.tools import GestaltDBQueryTool, GestaltDBSchemaTool


class GestaltDBToolkit(BaseToolkit):
    """Default read-only toolkit; writes require trusted application opt-in."""

    model_config = ConfigDict(arbitrary_types_allowed=True)
    graph: GestaltDBGraph = Field(exclude=True)
    allow_writes: StrictBool = Field(default=False, exclude=True)
    allowed_procedures: tuple[str, ...] = Field(default=(), exclude=True)
    max_rows: int = Field(default=100, ge=1, le=1000, exclude=True)
    max_chars: int = Field(default=16_000, ge=256, le=1_000_000, exclude=True)
    include_index_advisor: StrictBool = Field(default=False, exclude=True)

    def get_tools(self) -> list[BaseTool]:
        """Construct tools without transferring ownership of the graph handle."""
        tools: list[BaseTool] = [
            GestaltDBSchemaTool(graph=self.graph, max_chars=self.max_chars),
            GestaltDBQueryTool(
                graph=self.graph,
                allow_writes=self.allow_writes,
                allowed_procedures=self.allowed_procedures,
                max_rows=self.max_rows,
                max_chars=self.max_chars,
            ),
        ]
        if self.include_index_advisor:
            tools.append(GestaltDBIndexAdvisorTool(graph=self.graph, max_chars=self.max_chars))
        return tools


class GestaltDBClaimToolkit(BaseToolkit):
    """Epistemic claim toolkit; writes require explicit trusted opt-in.

    Claims are separate from mutable graph edges, so these tools read the
    bitemporal epistemic API directly rather than executing Cypher. Construction
    bounds are application-controlled and cannot be changed by an agent. The
    default toolkit is read-only; ``allow_writes=True`` additionally exposes
    append-only assertion, correction and retraction tools.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)
    graph: GestaltDBGraph = Field(exclude=True)
    allow_writes: StrictBool = Field(default=False, exclude=True)
    max_evidence: int = Field(default=50, ge=1, le=1000, exclude=True)
    max_claims: int = Field(default=50, ge=1, le=1000, exclude=True)
    max_depth: int = Field(default=64, ge=0, le=1000, exclude=True)
    max_nodes: int = Field(default=1000, ge=1, le=100_000, exclude=True)
    max_edges: int = Field(default=2000, ge=0, le=200_000, exclude=True)
    max_chars: int = Field(default=16_000, ge=256, le=1_000_000, exclude=True)

    def get_tools(self) -> list[BaseTool]:
        """Construct tools without transferring ownership of the graph handle."""
        tools: list[BaseTool] = [
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
        if self.allow_writes:
            tools.extend(
                [
                    GestaltDBClaimAssertTool(graph=self.graph, max_chars=self.max_chars),
                    GestaltDBClaimCorrectTool(graph=self.graph, max_chars=self.max_chars),
                    GestaltDBClaimRetractTool(graph=self.graph, max_chars=self.max_chars),
                ]
            )
        return tools


class GestaltDBModalToolkit(BaseToolkit):
    """Bounded modal entailment; accessibility writes require explicit opt-in.

    The entails tool is read-only: it evaluates one modal formula in a pinned
    bitemporal view with application-controlled ``max_depth``/``max_states``
    bounds that no agent input can change. ``allow_writes=True`` additionally
    exposes the append-only world-accessibility assertion tool, mirroring the
    claim-write tools (same commit receipts, same redacted failures).
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)
    graph: GestaltDBGraph = Field(exclude=True)
    allow_writes: StrictBool = Field(default=False, exclude=True)
    max_depth: int = Field(default=4, ge=1, le=1000, exclude=True)
    max_states: int = Field(default=1000, ge=1, le=1_000_000, exclude=True)
    max_chars: int = Field(default=16_000, ge=256, le=1_000_000, exclude=True)

    def get_tools(self) -> list[BaseTool]:
        """Construct tools without transferring ownership of the graph handle."""
        tools: list[BaseTool] = [
            GestaltDBEntailsTool(
                graph=self.graph,
                max_depth=self.max_depth,
                max_states=self.max_states,
                max_chars=self.max_chars,
            ),
        ]
        if self.allow_writes:
            tools.append(
                GestaltDBWorldAccessibilityTool(graph=self.graph, max_chars=self.max_chars)
            )
        return tools


class GestaltDBRuleToolkit(BaseToolkit):
    """Horn-rule toolkit; run/create/maintain require explicit trusted opt-in.

    Rules derive entity claims within one world, so the read tools (get/list)
    are safe by default. ``allow_writes=True`` additionally exposes append-only
    rule creation, bounded fixpoint evaluation and truth maintenance, mirroring
    the claim-write toolkit. Construction bounds are application-controlled and
    cannot be changed by an agent.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)
    graph: GestaltDBGraph = Field(exclude=True)
    allow_writes: StrictBool = Field(default=False, exclude=True)
    max_rules: int = Field(default=50, ge=1, le=1000, exclude=True)
    max_versions: int = Field(default=50, ge=1, le=1000, exclude=True)
    max_iterations: int = Field(default=100, ge=1, le=100_000, exclude=True)
    max_derivations: int = Field(default=10_000, ge=1, le=1_000_000, exclude=True)
    max_justifications: int = Field(default=100_000, ge=1, le=10_000_000, exclude=True)
    max_chars: int = Field(default=16_000, ge=256, le=1_000_000, exclude=True)

    def get_tools(self) -> list[BaseTool]:
        """Construct tools without transferring ownership of the graph handle."""
        tools: list[BaseTool] = [
            GestaltDBRuleGetTool(graph=self.graph, max_chars=self.max_chars),
            GestaltDBRuleListTool(
                graph=self.graph, max_rules=self.max_rules, max_chars=self.max_chars
            ),
        ]
        if self.allow_writes:
            tools.extend(
                [
                    GestaltDBRuleCreateTool(graph=self.graph, max_chars=self.max_chars),
                    GestaltDBRuleRunTool(
                        graph=self.graph,
                        max_iterations=self.max_iterations,
                        max_derivations=self.max_derivations,
                        max_justifications=self.max_justifications,
                        max_versions=self.max_versions,
                        max_chars=self.max_chars,
                    ),
                    GestaltDBRuleMaintainTool(
                        graph=self.graph,
                        max_iterations=self.max_iterations,
                        max_derivations=self.max_derivations,
                        max_justifications=self.max_justifications,
                        max_versions=self.max_versions,
                        max_chars=self.max_chars,
                    ),
                ]
            )
        return tools
