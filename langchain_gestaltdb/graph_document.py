"""Dependency-light graph documents, with structural external-model conversion."""

from __future__ import annotations

from typing import Any

from langchain_core.documents import Document
from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, field_validator


class GraphNode(BaseModel):
    """A stable string/integer entity ID, one native label, and properties."""

    model_config = ConfigDict(extra="forbid")
    id: StrictStr | StrictInt
    type: str = Field(default="Node", min_length=1)
    properties: dict[str, Any] = Field(default_factory=dict)

    @field_validator("id")
    @classmethod
    def nonempty_id(cls, value: str | int) -> str | int:
        if value == "":
            raise ValueError("Entity IDs cannot be empty")
        return value


class GraphRelationship(BaseModel):
    """A directed fact; optional explicit ID preserves identical parallel edges."""

    model_config = ConfigDict(extra="forbid")
    source: GraphNode
    target: GraphNode
    type: str = Field(min_length=1)
    properties: dict[str, Any] = Field(default_factory=dict)
    id: StrictStr | StrictInt | None = None


class GraphDocument(BaseModel):
    """Extracted graph plus an optional source Document.

    ``from_external`` accepts models with equivalent fields without importing
    Community or Neo4j. Compatibility is structural, not inheritance-based.
    """

    model_config = ConfigDict(extra="forbid")
    nodes: list[GraphNode] = Field(default_factory=list)
    relationships: list[GraphRelationship] = Field(default_factory=list)
    source: Document | None = None

    @classmethod
    def from_external(cls, value: Any) -> GraphDocument:
        """Validate a local document, mapping, or external Pydantic graph model."""
        if isinstance(value, cls):
            return value.model_copy(deep=True)
        if callable(getattr(value, "model_dump", None)):
            value = value.model_dump()
        return cls.model_validate(value)
