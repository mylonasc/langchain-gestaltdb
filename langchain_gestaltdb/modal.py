"""Bounded modal/belief entailment with opt-in world-accessibility writes.

``GestaltDBEntailsTool`` evaluates one modal formula (``BELIEVES``/``KNOWS``/
``POSSIBLE``/``NECESSARY``) in a single pinned bitemporal read view. It calls
the upstream bounded evaluator through ``_compat.call_upstream`` (facade on
0.12, epistemic service on 0.17+), so the same call backs
``CALL kg.entails(...) YIELD status, confidence, explanation``: the Cypher
procedure requires ``world`` and ``validTime`` options (``systemTime`` selects
historical knowledge) and returns the same four-valued ``status``.

Semantics the agent must respect:

- Four-valued and paraconsistent: ``status`` is one of ``supported``,
  ``refuted``, ``both`` or ``unknown``. Contradictory evidence yields ``both``;
  contradictions do not entail unrelated atoms.
- Missing accessibility is ``unknown``: a modal operator over a world with no
  outgoing accessibility link in the matching frame evaluates to ``unknown``,
  not to false. ``belief``, ``knowledge`` and ``modal`` frames are independent;
  a ``belief`` link never satisfies ``KNOWS``.
- Bounds are application-controlled: ``max_depth``/``max_states`` are fixed at
  construction and are never agent inputs. Upstream always evaluates with
  finite bounds; bound exhaustion fails closed (a redacted failure, never a
  partial verdict).
- Output caps bound returned JSON only, not evaluation work or state count.

World-accessibility links (``assert_world_accessibility``) are positive
temporal claims and therefore writes: ``GestaltDBWorldAccessibilityTool`` is
opt-in only, exposed through ``GestaltDBModalToolkit(allow_writes=True)``
exactly like the claim-write tools, and returns the same commit receipts.
"""

from __future__ import annotations

from typing import Any, Literal

from langchain_core.callbacks import CallbackManagerForToolRun
from langchain_core.tools import ToolException
from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator

from langchain_gestaltdb._compat import call_upstream
from langchain_gestaltdb._serialization import normalize
from langchain_gestaltdb._tool_output import bounded_json
from langchain_gestaltdb.claim_writes import _CLAIM_WRITE_ERROR, _ClaimWriteTool, _ValidityInput
from langchain_gestaltdb.claims import _ClaimTool, _enum_value

_MODAL_ERROR = (
    "GestaltDB modal entailment failed; check the agent, formula, mode, world, "
    "times and database state."
)

_ModalMode = Literal["BELIEVES", "KNOWS", "POSSIBLE", "NECESSARY"]
_AccessibilityKind = Literal["belief", "knowledge", "modal"]


class EntailsInput(BaseModel):
    """Agent inputs: identity, formula, mode, world and bitemporal horizon.

    Bounds (``max_depth``/``max_states``) are application-controlled and cannot
    be supplied here.
    """

    model_config = ConfigDict(extra="forbid")
    agent: str = Field(min_length=1, max_length=1024, description="Evaluating agent")
    proposition: JsonValue = Field(
        description="Modal formula: an atom {subject, predicate, object} or a "
        "NOT/AND/OR/nested-modal combination of atoms"
    )
    mode: _ModalMode = Field(description="Modal operator (case-insensitive)")
    world: str = Field(min_length=1, max_length=1024, description="World to evaluate in")
    valid_time: str = Field(min_length=1, max_length=64, description="ISO-8601 valid-time instant")
    system_time: str | None = Field(default=None, description="ISO-8601 system-time horizon")
    through_commit: int | None = Field(
        default=None, ge=0, description="System-time horizon as a commit ordinal"
    )

    @field_validator("mode", mode="before")
    @classmethod
    def normalize_mode(cls, value: Any) -> Any:
        if isinstance(value, str):
            return value.upper()
        return value


class WorldAccessibilityInput(_ValidityInput):
    """Accessibility-link inputs; the validity interval is required and explicit."""

    agent: str = Field(min_length=1, max_length=1024, description="Agent owning this frame")
    from_world: str = Field(min_length=1, max_length=1024)
    to_world: str = Field(min_length=1, max_length=1024)
    kind: _AccessibilityKind = Field(description="Independent frame: belief, knowledge or modal")
    valid_from: str = Field(
        min_length=1, max_length=64, description="ISO-8601 validity start (required)"
    )
    source: str | None = Field(default=None, max_length=1024, description="Evidence source")
    provenance: dict[str, JsonValue] | None = Field(
        default=None, description="Explicit JSON provenance (use {} if unavailable)"
    )

    @field_validator("provenance")
    @classmethod
    def validate_provenance(cls, value: Any) -> Any:
        normalize(value)
        return value


class GestaltDBEntailsTool(_ClaimTool):
    """Evaluate one bounded modal formula with a four-valued status.

    ``status`` is ``supported``/``refuted``/``both``/``unknown`` under
    paraconsistent semantics; missing accessibility in the evaluated frame is
    ``unknown``. This matches ``CALL kg.entails(...) YIELD status, confidence,
    explanation`` with ``world``/``validTime`` options (plus optional
    ``systemTime``): same evaluator, same status values, one pinned read view.
    ``max_depth``/``max_states`` are fixed at construction; exhaustion fails
    closed with a redacted error rather than a partial verdict.
    """

    name: str = "gestaltdb_entails"
    description: str = (
        "Evaluate a bounded modal formula (BELIEVES, KNOWS, POSSIBLE or NECESSARY) "
        "for one agent in one world at a valid/system time. Returns the four-valued "
        "status (supported, refuted, both or unknown), confidence and bounded "
        "evaluation evidence. Missing accessibility is unknown; frames are independent. "
        "Same bounded evaluator as CALL kg.entails. Bounds are application-controlled."
    )
    args_schema: type[BaseModel] = EntailsInput
    max_depth: int = Field(default=4, ge=1, le=1000, exclude=True)
    max_states: int = Field(default=1000, ge=1, le=1_000_000, exclude=True)

    def _run(
        self,
        agent: str,
        proposition: Any,
        mode: _ModalMode,
        world: str,
        valid_time: str,
        system_time: str | None = None,
        through_commit: int | None = None,
        run_manager: CallbackManagerForToolRun | None = None,
    ) -> str:
        try:
            formula = normalize(proposition)
            result = call_upstream(
                self.graph.graph,
                "entails",
                agent,
                formula,
                mode,
                world=world,
                valid_time=valid_time,
                system_time=system_time,
                through_commit=through_commit,
                max_depth=self.max_depth,
                max_states=self.max_states,
            )
            envelope: dict[str, Any] = {
                "status": _enum_value(result.status),
                "confidence": normalize(result.confidence),
                "explanation": normalize(result.explanation),
                "agent": agent,
                "mode": mode,
                "world": world,
                "valid_time": valid_time,
                "system_time": system_time,
                "through_commit": through_commit,
                "max_depth": self.max_depth,
                "max_states": self.max_states,
                "truncated": False,
            }
        except Exception:
            raise ToolException(_MODAL_ERROR) from None
        return bounded_json(envelope, (), self.max_chars)


class GestaltDBWorldAccessibilityTool(_ClaimWriteTool):
    """Append one positive temporal accessibility link in a single frame.

    Opt-in write: ``belief``, ``knowledge`` and ``modal`` frames are
    independent, so a link only enables entailment for its own frame's modes.
    Accessibility links are temporal claims with explicit validity; history is
    preserved and nothing is deleted. Returns the same commit receipt as the
    claim-write tools. Deferred external writes need a trusted rebuild before
    indexed reads (see the claim-write toolkit guidance).
    """

    name: str = "gestaltdb_world_accessibility_assert"
    description: str = (
        "Append a positive temporal accessibility link for one agent from one world "
        "to another in the belief, knowledge or modal frame. Opt-in write; frames "
        "are independent and history is preserved. Returns the appended version receipt."
    )
    args_schema: type[BaseModel] = WorldAccessibilityInput

    def _run(
        self,
        agent: str,
        from_world: str,
        to_world: str,
        kind: _AccessibilityKind,
        valid_from: str,
        valid_to: str | None = None,
        source: str | None = None,
        provenance: dict[str, Any] | None = None,
        run_manager: CallbackManagerForToolRun | None = None,
    ) -> str:
        try:
            version = call_upstream(
                self.graph.graph,
                "assert_world_accessibility",
                agent=agent,
                from_world=from_world,
                to_world=to_world,
                kind=kind,
                valid_from=valid_from,
                valid_to=valid_to,
                source=source,
                provenance={} if provenance is None else provenance,
            )
        except Exception:
            raise ToolException(_CLAIM_WRITE_ERROR) from None
        return self._result(version)
