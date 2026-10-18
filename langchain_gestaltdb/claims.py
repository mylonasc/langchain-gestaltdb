"""Read-only epistemic claim tools with bounded, valid JSON output.

GestaltDB claims are sourced assertions or denials stored in a bitemporal
epistemic layer. They are separate from mutable graph edges: they do not create
current-state edges and never participate in ordinary Cypher matching, so these
tools call the epistemic API directly. They only read; no write path is exposed.

Structural guarantees:

- Supported, refuted, both and unknown stay four distinct ``status`` values.
- Agent, source, world and polarity filters plus valid/system time are forwarded.
- Explanations keep historical ids and explicit truncation metadata.
- Output is bounded; caps limit output, not execution time or memory.
"""

from __future__ import annotations

import json
from typing import Any, Literal

from gestaltdb import claim_statement_id
from langchain_core.callbacks import CallbackManagerForToolRun
from langchain_core.tools import BaseTool, ToolException
from pydantic import BaseModel, ConfigDict, Field

from langchain_gestaltdb._serialization import normalize
from langchain_gestaltdb.graph import GestaltDBGraph

_CLAIM_ERROR = (
    "GestaltDB claim operation failed; check the claim identity, filters, times and database state."
)


def _enum_value(value: Any) -> Any:
    """Return a str-enum's value, leaving plain strings/None unchanged."""
    return value.value if hasattr(value, "value") else value


def _serialize_claim(claim: Any) -> dict[str, Any]:
    """Serialize a claim payload with its deterministic identities."""
    return {
        "statement_id": claim.statement_id,
        "claim_id": claim.claim_id,
        "subject": claim.subject,
        "predicate": claim.predicate,
        "object": normalize(claim.object),
        "object_kind": _enum_value(claim.object_kind),
        "polarity": _enum_value(claim.polarity),
        "agent": claim.agent,
        "source": claim.source,
        "confidence": claim.confidence,
        "world": claim.world,
        "provenance": normalize(dict(claim.provenance)),
    }


def _serialize_claim_version(version: Any) -> dict[str, Any]:
    """Serialize one historical claim version, preserving version metadata."""
    claim = version.claim
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
        "claim": _serialize_claim(claim) if claim is not None else None,
    }


def _serialize_rule_version(version: Any) -> dict[str, Any]:
    """Serialize a Horn-rule version node without losing rule identity."""
    return {
        "rule_id": version.rule_id,
        "version_id": version.version_id,
        "system_time": normalize(version.system_time),
        "catalog_ordinal": version.catalog_ordinal,
        "when": [
            {"subject": atom.subject, "predicate": atom.predicate, "object": atom.object}
            for atom in version.when
        ],
        "then": {
            "subject": version.then.subject,
            "predicate": version.then.predicate,
            "object": version.then.object,
        },
    }


def _serialize_node_value(value: Any) -> Any:
    """Serialize an explanation node payload: claim version, rule or raw value."""
    if hasattr(value, "logical_id") and hasattr(value, "version_id"):
        return _serialize_claim_version(value)
    if hasattr(value, "rule_id") and hasattr(value, "when"):
        return _serialize_rule_version(value)
    return normalize(value)


def _bounded_records(
    records: list[dict[str, Any]], max_records: int, max_chars: int
) -> tuple[list[dict[str, Any]], bool]:
    """Keep whole records until the count or character cap is reached."""
    kept: list[dict[str, Any]] = []
    truncated = len(records) > max_records
    for record in records[:max_records]:
        kept.append(record)
        if len(json.dumps(kept, ensure_ascii=False)) > max_chars:
            kept.pop()
            truncated = True
            break
    return kept, truncated


class ClaimStatusInput(BaseModel):
    """Filters and bitemporal horizon; provider/bounds are not agent-controlled."""

    model_config = ConfigDict(extra="forbid")
    subject: str = Field(min_length=1, max_length=1024, description="Claim subject entity ID")
    predicate: str = Field(min_length=1, max_length=1024, description="Asserted predicate")
    object: Any = Field(
        description="Object entity ID, or a JSON literal when object_kind='literal'"
    )
    object_kind: Literal["entity", "literal"] = Field(
        default="entity", description="Interpretation of the object"
    )
    agent: str | None = Field(default=None, description="Restrict to one asserting agent")
    source: str | None = Field(default=None, description="Restrict to one source; omit to ignore")
    world: str | None = Field(default=None, description="Restrict to one world")
    valid_time: str = Field(min_length=1, max_length=64, description="ISO-8601 valid-time instant")
    system_time: str | None = Field(default=None, description="ISO-8601 system-time horizon")
    through_commit: int | None = Field(
        default=None, ge=0, description="System-time horizon as a commit ordinal"
    )


class ClaimSearchInput(BaseModel):
    """Indexed claim dimensions plus bitemporal horizon; bounds are fixed."""

    model_config = ConfigDict(extra="forbid")
    statement_id: str | None = Field(
        default=None, max_length=80, description="Deterministic statement:... identity"
    )
    subject: str | None = Field(default=None, max_length=1024)
    predicate: str | None = Field(default=None, max_length=1024)
    object: Any = Field(default=None, description="Object filter; omit (null) for any object")
    object_kind: Literal["entity", "literal"] | None = Field(default=None)
    agent: str | None = Field(default=None, max_length=1024)
    source: str | None = Field(default=None, max_length=1024)
    world: str | None = Field(default=None, max_length=1024)
    polarity: Literal["positive", "negative"] | None = Field(default=None)
    valid_time: str = Field(min_length=1, max_length=64, description="ISO-8601 valid-time instant")
    system_time: str | None = Field(default=None, description="ISO-8601 system-time horizon")
    through_commit: int | None = Field(default=None, ge=0)


class ClaimExplanationInput(BaseModel):
    """Claim identity and bitemporal horizon; traversal bounds are fixed."""

    model_config = ConfigDict(extra="forbid")
    claim_id: str = Field(min_length=1, max_length=256, description="claim:... identity to explain")
    valid_time: str = Field(min_length=1, max_length=64, description="ISO-8601 valid-time instant")
    system_time: str | None = Field(default=None, description="ISO-8601 system-time horizon")


class _ClaimTool(BaseTool):
    """Shared graph handle, output cap and redacted failure for claim tools."""

    graph: GestaltDBGraph = Field(exclude=True)
    max_chars: int = Field(default=16_000, ge=256, le=1_000_000, exclude=True)
    handle_tool_error: bool = True


class GestaltDBClaimStatusTool(_ClaimTool):
    """Report four-valued claim status with the supporting/refuting evidence.

    Status is one of supported/refuted/both/unknown under open-world semantics;
    the evidence list preserves the historical version and claim identities.
    """

    name: str = "gestaltdb_claim_status"
    description: str = (
        "Look up a sourced claim's status (supported, refuted, both or unknown) at a "
        "valid/system time, with bounded supporting and refuting evidence. Claims are "
        "epistemic assertions, not graph edges, and are never returned by Cypher."
    )
    args_schema: type[BaseModel] = ClaimStatusInput
    max_evidence: int = Field(default=50, ge=1, le=1000, exclude=True)

    def _run(
        self,
        subject: str,
        predicate: str,
        object: Any,
        valid_time: str,
        object_kind: Literal["entity", "literal"] = "entity",
        agent: str | None = None,
        source: str | None = None,
        world: str | None = None,
        system_time: str | None = None,
        through_commit: int | None = None,
        run_manager: CallbackManagerForToolRun | None = None,
    ) -> str:
        try:
            graph = self.graph.graph
            filters: dict[str, Any] = {}
            if agent is not None:
                filters["agent"] = agent
            if source is not None:
                filters["source"] = source
            if world is not None:
                filters["world"] = world
            if system_time is not None:
                filters["system_time"] = system_time
            if through_commit is not None:
                filters["through_commit"] = through_commit
            status = graph.claim_status(
                subject,
                predicate,
                object,
                valid_time=valid_time,
                object_kind=object_kind,
                **filters,
            )
            statement_id = claim_statement_id(subject, predicate, object, object_kind=object_kind)
            versions = list(
                graph.iter_claims_as_of(valid_time=valid_time, statement_id=statement_id, **filters)
            )
            evidence, truncated = _bounded_records(
                [_serialize_claim_version(version) for version in versions],
                self.max_evidence,
                self.max_chars,
            )
        except Exception:
            raise ToolException(_CLAIM_ERROR) from None
        result: dict[str, Any] = {
            "status": _enum_value(status),
            "statement_id": statement_id,
            "subject": subject,
            "predicate": predicate,
            "object": normalize(object),
            "object_kind": object_kind,
            "valid_time": valid_time,
            "system_time": system_time,
            "through_commit": through_commit,
            "filters": {"agent": agent, "source": source, "world": world},
            "evidence": evidence,
            "returned_evidence": len(evidence),
            "truncated": truncated,
        }
        return json.dumps(result, ensure_ascii=False)


class GestaltDBClaimSearchTool(_ClaimTool):
    """List indexed claims matching dimensions at a bitemporal horizon."""

    name: str = "gestaltdb_claim_search"
    description: str = (
        "Search sourced claims by statement, subject, predicate, object, agent, source, "
        "world or polarity at a valid/system time. Returns bounded historical claim "
        "versions. Claims are epistemic assertions, not graph edges."
    )
    args_schema: type[BaseModel] = ClaimSearchInput
    max_claims: int = Field(default=50, ge=1, le=1000, exclude=True)

    def _run(
        self,
        valid_time: str,
        statement_id: str | None = None,
        subject: str | None = None,
        predicate: str | None = None,
        object: Any = None,
        object_kind: Literal["entity", "literal"] | None = None,
        agent: str | None = None,
        source: str | None = None,
        world: str | None = None,
        polarity: Literal["positive", "negative"] | None = None,
        system_time: str | None = None,
        through_commit: int | None = None,
        run_manager: CallbackManagerForToolRun | None = None,
    ) -> str:
        try:
            graph = self.graph.graph
            kwargs: dict[str, Any] = {"valid_time": valid_time}
            if statement_id is not None:
                kwargs["statement_id"] = statement_id
            if subject is not None:
                kwargs["subject"] = subject
            if predicate is not None:
                kwargs["predicate"] = predicate
            if object is not None:
                kwargs["object"] = object
            if object_kind is not None:
                kwargs["object_kind"] = object_kind
            if agent is not None:
                kwargs["agent"] = agent
            if source is not None:
                kwargs["source"] = source
            if world is not None:
                kwargs["world"] = world
            if polarity is not None:
                kwargs["polarity"] = polarity
            if system_time is not None:
                kwargs["system_time"] = system_time
            if through_commit is not None:
                kwargs["through_commit"] = through_commit
            versions = list(graph.iter_claims_as_of(**kwargs))
            claims, truncated = _bounded_records(
                [_serialize_claim_version(version) for version in versions],
                self.max_claims,
                self.max_chars,
            )
        except Exception:
            raise ToolException(_CLAIM_ERROR) from None
        result: dict[str, Any] = {
            "claims": claims,
            "returned_claims": len(claims),
            "truncated": truncated,
            "valid_time": valid_time,
            "system_time": system_time,
            "through_commit": through_commit,
            "filters": {
                "statement_id": statement_id,
                "subject": subject,
                "predicate": predicate,
                "object_kind": object_kind,
                "agent": agent,
                "source": source,
                "world": world,
                "polarity": polarity,
            },
        }
        return json.dumps(result, ensure_ascii=False)


class GestaltDBClaimExplanationTool(_ClaimTool):
    """Return a bounded derivation graph with historical ids and truncation flags."""

    name: str = "gestaltdb_claim_explanation"
    description: str = (
        "Explain how a visible historical claim is derived: bounded claim/rule nodes and "
        "premise/derivation edges. Node values keep historical version ids and identities; "
        "truncation is explicit. The explanation is not a relevance ranking."
    )
    args_schema: type[BaseModel] = ClaimExplanationInput
    max_depth: int = Field(default=64, ge=0, le=1000, exclude=True)
    max_nodes: int = Field(default=1000, ge=1, le=100_000, exclude=True)
    max_edges: int = Field(default=2000, ge=0, le=200_000, exclude=True)

    def _run(
        self,
        claim_id: str,
        valid_time: str,
        system_time: str | None = None,
        run_manager: CallbackManagerForToolRun | None = None,
    ) -> str:
        try:
            explanation = self.graph.graph.explain_claim(
                claim_id,
                valid_time=valid_time,
                system_time=system_time,
                max_depth=self.max_depth,
                max_nodes=self.max_nodes,
            )
            if explanation is None:
                return json.dumps(
                    {
                        "found": False,
                        "claim_id": claim_id,
                        "valid_time": valid_time,
                        "system_time": system_time,
                        "truncated": False,
                    },
                    ensure_ascii=False,
                )
            nodes = [
                {
                    "node_id": node.node_id,
                    "kind": node.kind,
                    "value": _serialize_node_value(node.value),
                }
                for node in explanation.nodes
            ]
            edges = [
                {
                    "source_id": edge.source_id,
                    "target_id": edge.target_id,
                    "relationship": edge.relationship,
                    "premise_ordinal": edge.premise_ordinal,
                }
                for edge in explanation.edges
            ]
            truncated = bool(explanation.truncated)
            if len(edges) > self.max_edges:
                edges = edges[: self.max_edges]
                truncated = True
        except Exception:
            raise ToolException(_CLAIM_ERROR) from None

        def envelope() -> dict[str, Any]:
            return {
                "found": True,
                "claim_id": claim_id,
                "root_version_id": explanation.root_version_id,
                "logical_id": explanation.claim_id,
                "valid_time": valid_time,
                "system_time": system_time,
                "nodes": nodes,
                "returned_nodes": len(nodes),
                "edges": edges,
                "returned_edges": len(edges),
                "truncated": truncated,
            }

        while len(json.dumps(envelope(), ensure_ascii=False)) > self.max_chars and edges:
            edges.pop()
            truncated = True
        while len(json.dumps(envelope(), ensure_ascii=False)) > self.max_chars and nodes:
            nodes.pop()
            truncated = True
        return json.dumps(envelope(), ensure_ascii=False)
