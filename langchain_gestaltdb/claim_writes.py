"""Explicitly enabled claim-write tools with typed, validated inputs.

Writing claims is opt-in: these tools are constructed directly by a trusted
application and are never part of the default read-only claim toolkit. Agent
authority, provenance and confidence are never inferred; the caller must supply
them explicitly. Every write appends an immutable, bitemporal version, so fact
validity (``valid``) stays distinct from system knowledge time (``system_time``)
and prior evidence remains retrievable. There is no delete path.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Literal

from gestaltdb.versioning import ClaimVersion
from langchain_core.callbacks import CallbackManagerForToolRun
from langchain_core.tools import BaseTool, ToolException
from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator, model_validator

from langchain_gestaltdb._serialization import normalize
from langchain_gestaltdb.claims import _serialize_claim_version
from langchain_gestaltdb.graph import GestaltDBGraph

_CLAIM_WRITE_ERROR = (
    "GestaltDB claim write failed; check the claim identity, agent, provenance, "
    "times and database state."
)


class _ValidityInput(BaseModel):
    """Explicit timezone-aware, nonempty half-open fact-validity intervals."""

    model_config = ConfigDict(extra="forbid")
    valid_from: str | None = Field(
        default=None, max_length=64, description="ISO-8601 validity start with timezone"
    )
    valid_to: str | None = Field(
        default=None, max_length=64, description="ISO-8601 exclusive validity end with timezone"
    )

    @field_validator("valid_from", "valid_to")
    @classmethod
    def validate_instant(cls, value: str | None) -> str | None:
        if value is not None:
            instant = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if instant.tzinfo is None:
                raise ValueError("validity instants require an explicit timezone")
        return value

    @model_validator(mode="after")
    def validate_interval(self) -> _ValidityInput:
        if self.valid_to is not None:
            if self.valid_from is None:
                raise ValueError("valid_to requires valid_from")
            start = datetime.fromisoformat(self.valid_from.replace("Z", "+00:00"))
            end = datetime.fromisoformat(self.valid_to.replace("Z", "+00:00"))
            if end <= start:
                raise ValueError("valid_to must be after valid_from")
        return self


class ClaimAssertInput(_ValidityInput):
    """Assertion inputs; agent/provenance/polarity are explicit, never inferred."""

    subject: str = Field(min_length=1, max_length=1024, description="Claim subject entity ID")
    predicate: str = Field(min_length=1, max_length=1024, description="Asserted predicate")
    object: JsonValue = Field(
        description="Object entity ID, or a JSON literal when object_kind='literal'"
    )
    polarity: Literal["positive", "negative"] = Field(description="Assert or deny")
    agent: str = Field(min_length=1, max_length=1024, description="Asserting agent; required")
    object_kind: Literal["entity", "literal"] = Field(default="entity")
    source: str | None = Field(default=None, max_length=1024, description="Evidence source")
    confidence: float | None = Field(default=None, ge=0, le=1, description="Explicit confidence")
    world: str = Field(default="default", min_length=1, max_length=1024)
    provenance: dict[str, JsonValue] = Field(
        description="Explicit JSON provenance (use {} if unavailable); never inferred"
    )
    valid_from: str = Field(
        min_length=1, max_length=64, description="ISO-8601 fact-validity start (required)"
    )

    @field_validator("object", "provenance")
    @classmethod
    def validate_json(cls, value: Any) -> Any:
        normalize(value)  # Reject non-finite numbers and unsafe nested values before writing.
        return value

    @model_validator(mode="after")
    def validate_entity_object(self) -> ClaimAssertInput:
        if self.object_kind == "entity" and (not isinstance(self.object, str) or not self.object):
            raise ValueError("entity objects require a nonempty string ID")
        return self


class ClaimCorrectInput(_ValidityInput):
    """Correction inputs; a change must be explicit and never inferred."""

    claim_id: str = Field(min_length=1, max_length=256, description="claim:... identity to correct")
    supersedes_version_id: str = Field(
        min_length=1, max_length=64, description="Exact historical version being superseded"
    )
    confidence: float | None = Field(default=None, ge=0, le=1)
    provenance: dict[str, JsonValue] | None = Field(
        default=None, description="Replacement provenance"
    )

    @field_validator("provenance")
    @classmethod
    def validate_json(cls, value: Any) -> Any:
        normalize(value)
        return value

    @model_validator(mode="after")
    def require_change(self) -> ClaimCorrectInput:
        if self.confidence is None and self.provenance is None and self.valid_from is None:
            raise ValueError("a correction must change confidence, provenance or validity")
        return self


class ClaimRetractInput(_ValidityInput):
    """Retraction inputs; history is preserved, never deleted."""

    claim_id: str = Field(min_length=1, max_length=256, description="claim:... identity to retract")
    supersedes_version_id: str = Field(
        min_length=1,
        max_length=64,
        description="Exact historical version being withdrawn (required)",
    )
    reason: str | None = Field(default=None, max_length=1024, description="Explicit reason")


class _ClaimWriteTool(BaseTool):
    """Shared graph handle, output cap and redacted failure for claim writes."""

    graph: GestaltDBGraph = Field(exclude=True)
    max_chars: int = Field(default=16_000, ge=256, le=1_000_000, exclude=True)
    handle_tool_error: bool = True

    def _result(self, version: ClaimVersion) -> str:
        # A commit already happened. Never turn a formatting limit into an
        # apparent write failure that could prompt an unsafe retry.
        receipt = {
            "written": True,
            "version_id": version.version_id,
            "logical_id": version.logical_id,
            "truncated": True,
        }
        try:
            result = _serialize_claim_version(version)
            result.update(written=True, truncated=False)
            output = json.dumps(result, ensure_ascii=False)
            if len(output) <= self.max_chars:
                return output
        except (TypeError, ValueError):
            pass  # Preserve the successful receipt even if full serialization fails.
        return json.dumps(receipt, ensure_ascii=False)


class GestaltDBClaimAssertTool(_ClaimWriteTool):
    """Append a sourced assertion or denial with explicit provenance and validity."""

    name: str = "gestaltdb_claim_assert"
    description: str = (
        "Append a sourced positive/negative claim with explicit agent, provenance and fact "
        "validity. Writes are opt-in and append-only; nothing is inferred. Returns the new "
        "claim version with its system knowledge time."
    )
    args_schema: type[BaseModel] = ClaimAssertInput

    def _run(
        self,
        subject: str,
        predicate: str,
        object: Any,
        polarity: Literal["positive", "negative"],
        agent: str,
        valid_from: str,
        provenance: dict[str, Any],
        object_kind: Literal["entity", "literal"] = "entity",
        source: str | None = None,
        confidence: float | None = None,
        world: str = "default",
        valid_to: str | None = None,
        run_manager: CallbackManagerForToolRun | None = None,
    ) -> str:
        try:
            version = self.graph.graph.assert_claim(
                subject=subject,
                predicate=predicate,
                object=object,
                polarity=polarity,
                agent=agent,
                source=source,
                confidence=confidence,
                world=world,
                provenance=provenance,
                object_kind=object_kind,
                valid_from=valid_from,
                valid_to=valid_to,
            )
        except Exception:
            raise ToolException(_CLAIM_WRITE_ERROR) from None
        return self._result(version)


class GestaltDBClaimCorrectTool(_ClaimWriteTool):
    """Supersede one claim version with corrected confidence, provenance or validity."""

    name: str = "gestaltdb_claim_correct"
    description: str = (
        "Append a correction to an existing claim version, superseding an exact version id "
        "while preserving history. Confidence, provenance or fact validity must change "
        "explicitly. Writes are opt-in and append-only."
    )
    args_schema: type[BaseModel] = ClaimCorrectInput

    def _run(
        self,
        claim_id: str,
        supersedes_version_id: str,
        confidence: float | None = None,
        provenance: dict[str, Any] | None = None,
        valid_from: str | None = None,
        valid_to: str | None = None,
        run_manager: CallbackManagerForToolRun | None = None,
    ) -> str:
        try:
            kwargs: dict[str, Any] = {"supersedes_version_id": supersedes_version_id}
            if confidence is not None:
                kwargs["confidence"] = confidence
            if provenance is not None:
                kwargs["provenance"] = provenance
            if valid_from is not None:
                kwargs["valid_from"] = valid_from
            if valid_to is not None:
                kwargs["valid_to"] = valid_to
            # Use the logical ID: the Claim-object overload ignores explicit
            # confidence/provenance on GestaltDB 0.12.0 (upstream issue #110).
            version = self.graph.graph.correct_claim(claim_id, **kwargs)
        except Exception:
            raise ToolException(_CLAIM_WRITE_ERROR) from None
        return self._result(version)


class GestaltDBClaimRetractTool(_ClaimWriteTool):
    """Withdraw a claim over a validity interval without deleting its history."""

    name: str = "gestaltdb_claim_retract"
    description: str = (
        "Append a bitemporal retraction superseding an exact historical version, optionally "
        "over an explicit validity interval, preserving history. Writes are opt-in; nothing "
        "is deleted and nothing is inferred."
    )
    args_schema: type[BaseModel] = ClaimRetractInput

    def _run(
        self,
        claim_id: str,
        supersedes_version_id: str,
        reason: str | None = None,
        valid_from: str | None = None,
        valid_to: str | None = None,
        run_manager: CallbackManagerForToolRun | None = None,
    ) -> str:
        try:
            # Exact-version targeting avoids orphan interval-only retractions
            # accepted upstream in 0.12.0 (issue #111).
            kwargs: dict[str, Any] = {"supersedes_version_id": supersedes_version_id}
            if reason is not None:
                kwargs["reason"] = reason
            if valid_from is not None:
                kwargs["valid_from"] = valid_from
            if valid_to is not None:
                kwargs["valid_to"] = valid_to
            version = self.graph.graph.retract_claim(claim_id, **kwargs)
        except Exception:
            raise ToolException(_CLAIM_WRITE_ERROR) from None
        return self._result(version)
