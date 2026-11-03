"""Horn-rule lifecycle tools with bounded, valid JSON output.

GestaltDB Horn rules derive new entity claims from positive entity claims
within one world. Rules are versioned and append-only: ``create_rule`` stores
one immutable rule version, ``run_rules`` evaluates the active rule set to a
bounded fixpoint, and ``maintain_truth`` incrementally reconciles derived
conclusions after premise or rule changes. Derived conclusions are stored
under the ``gestaltdb:rules`` agent with rule/premise version justifications
and can be inspected with the claim-explanation tool.

Read tools (``GestaltDBRuleGetTool``/``GestaltDBRuleListTool``) are safe for
untrusted agents. Create/run/maintain tools are opt-in trusted-application
tools mirroring the claim-write toolkit: bounds are application-controlled
and never agent-set, limit errors surface the redacted upstream failure with
no partial conclusions persisted, and an unchanged repeat run is idempotent
(``derived_count`` 0 with no new commit).
"""

from __future__ import annotations

import json
from typing import Any, Literal

from langchain_core.callbacks import CallbackManagerForToolRun
from langchain_core.tools import BaseTool, ToolException
from pydantic import BaseModel, ConfigDict, Field

from langchain_gestaltdb._compat import call_upstream
from langchain_gestaltdb._tool_output import bounded_json
from langchain_gestaltdb.claims import _serialize_claim_version, _serialize_rule_version
from langchain_gestaltdb.graph import GestaltDBGraph

_RULE_ERROR = (
    "GestaltDB rule operation failed; check the rule identity, bounds, times and database state."
)


def _head_variables(atom: dict[str, str]) -> set[str]:
    """Collect ``?``-prefixed variables from a rule atom's subject/object."""
    return {
        value
        for value in (atom.get("subject"), atom.get("object"))
        if isinstance(value, str) and value.startswith("?")
    }


class RuleAtomInput(BaseModel):
    """One rule atom; predicates are constants, subject/object may bind variables."""

    model_config = ConfigDict(extra="forbid")
    subject: str = Field(min_length=1, max_length=1024)
    predicate: str = Field(min_length=1, max_length=1024)
    object: str = Field(min_length=1, max_length=1024)


class RuleGetInput(BaseModel):
    """Rule identity with an optional system-time horizon; bounds are fixed."""

    model_config = ConfigDict(extra="forbid")
    rule_id: str = Field(min_length=1, max_length=1024, description="Rule identity to fetch")
    system_time: str | None = Field(
        default=None, description="ISO-8601 system-time horizon; omit for latest"
    )


class RuleListInput(BaseModel):
    """Optional rule filter with a system-time horizon; output bounds are fixed."""

    model_config = ConfigDict(extra="forbid")
    rule_id: str | None = Field(
        default=None, max_length=1024, description="Restrict to one rule; omit for all rules"
    )
    system_time: str | None = Field(
        default=None, description="ISO-8601 system-time horizon; omit for latest"
    )


class RuleCreateInput(BaseModel):
    """Safe positive Horn rule; every head variable must be bound in the body."""

    model_config = ConfigDict(extra="forbid")
    rule_id: str = Field(min_length=1, max_length=1024, description="Rule identity to version")
    when: list[RuleAtomInput] = Field(
        min_length=1, max_length=64, description="Body atoms; predicates must be constants"
    )
    then: RuleAtomInput = Field(description="Head atom; variables must be bound in the body")


class RuleRunInput(BaseModel):
    """Fixpoint evaluation horizon; workload bounds are application-controlled."""

    model_config = ConfigDict(extra="forbid")
    as_of: str = Field(min_length=1, max_length=64, description="ISO-8601 valid-time horizon")
    system_time: str | None = Field(
        default=None, description="ISO-8601 system-time horizon; omit for latest"
    )
    world: str | None = Field(default=None, max_length=1024)


class RuleMaintainInput(BaseModel):
    """Truth-maintenance horizon; later calls may omit ``as_of`` to resume."""

    model_config = ConfigDict(extra="forbid")
    as_of: str | None = Field(
        default=None, min_length=1, max_length=64, description="ISO-8601 valid-time horizon"
    )
    system_time: str | None = Field(
        default=None, description="ISO-8601 system-time horizon; omit for latest"
    )
    world: str | None = Field(default=None, max_length=1024)


class _RuleTool(BaseTool):
    """Shared graph handle, output cap and redacted failure for rule tools."""

    graph: GestaltDBGraph = Field(exclude=True)
    max_chars: int = Field(default=16_000, ge=256, le=1_000_000, exclude=True)
    handle_tool_error: bool = True


class GestaltDBRuleGetTool(_RuleTool):
    """Fetch one versioned Horn rule by identity at a system-time horizon."""

    name: str = "gestaltdb_rule_get"
    description: str = (
        "Fetch one versioned Horn rule by rule id, with its body atoms, head atom and "
        "version metadata. Rules derive entity claims within one world; use the claim "
        "explanation tool to inspect derived conclusions."
    )
    args_schema: type[BaseModel] = RuleGetInput

    def _run(
        self,
        rule_id: str,
        system_time: str | None = None,
        run_manager: CallbackManagerForToolRun | None = None,
    ) -> str:
        try:
            kwargs: dict[str, Any] = {}
            if system_time is not None:
                kwargs["system_time"] = system_time
            version = call_upstream(self.graph.graph, "get_rule", rule_id, **kwargs)
            if version is None:
                return bounded_json(
                    {
                        "found": False,
                        "rule_id": rule_id,
                        "system_time": system_time,
                        "truncated": False,
                        "rule": None,
                    },
                    (),
                    self.max_chars,
                )
            result: dict[str, Any] = {
                "found": True,
                "rule_id": rule_id,
                "system_time": system_time,
                "truncated": False,
                "rule": _serialize_rule_version(version),
            }
        except Exception:
            raise ToolException(_RULE_ERROR) from None
        return bounded_json(result, (), self.max_chars)


class GestaltDBRuleListTool(_RuleTool):
    """List versioned Horn rules, optionally restricted to one rule identity."""

    name: str = "gestaltdb_rule_list"
    description: str = (
        "List versioned Horn rules, optionally restricted to one rule id, at a system-time "
        "horizon. Returns bounded rule versions with body/head atoms. Output caps do not "
        "limit catalog work."
    )
    args_schema: type[BaseModel] = RuleListInput
    max_rules: int = Field(default=50, ge=1, le=1000, exclude=True)

    def _run(
        self,
        rule_id: str | None = None,
        system_time: str | None = None,
        run_manager: CallbackManagerForToolRun | None = None,
    ) -> str:
        try:
            kwargs = {}
            if system_time is not None:
                kwargs["system_time"] = system_time
            versions = list(
                call_upstream(self.graph.graph, "iter_rule_versions", rule_id, **kwargs)
            )
            rules = [_serialize_rule_version(version) for version in versions]
        except Exception:
            raise ToolException(_RULE_ERROR) from None
        result = {
            "rules": rules,
            "returned_rules": len(rules),
            "truncated": False,
            "rule_id": rule_id,
            "system_time": system_time,
        }
        return bounded_json(result, (("rules", "returned_rules", self.max_rules),), self.max_chars)


class _RuleWriteTool(BaseTool):
    """Shared graph handle, output cap and redacted failure for rule writes."""

    graph: GestaltDBGraph = Field(exclude=True)
    max_chars: int = Field(default=16_000, ge=256, le=1_000_000, exclude=True)
    handle_tool_error: bool = True


class GestaltDBRuleCreateTool(_RuleWriteTool):
    """Append one immutable version of a safe positive Horn rule.

    The body must be nonempty with constant predicates; every ``?``-prefixed
    head variable must be bound by a body subject/object. Writes are opt-in
    and append-only; nothing is inferred beyond the stored rule version.
    """

    name: str = "gestaltdb_rule_create"
    description: str = (
        "Append one immutable version of a safe positive Horn rule: constant body predicates, "
        "head variables bound in the body. Writes are opt-in and append-only."
    )
    args_schema: type[BaseModel] = RuleCreateInput

    def _run(
        self,
        rule_id: str,
        when: list[dict[str, str]] | list[RuleAtomInput],
        then: dict[str, str] | RuleAtomInput,
        run_manager: CallbackManagerForToolRun | None = None,
    ) -> str:
        try:
            body = [dict(atom) if isinstance(atom, dict) else atom.model_dump() for atom in when]
            head = dict(then) if isinstance(then, dict) else then.model_dump()
            self._check_safety(body, head)
            version = call_upstream(
                self.graph.graph,
                "create_rule",
                rule_id,
                when=[(atom["subject"], atom["predicate"], atom["object"]) for atom in body],
                then=(head["subject"], head["predicate"], head["object"]),
            )
        except ToolException:
            raise
        except Exception:
            raise ToolException(_RULE_ERROR) from None
        receipt = {
            "written": True,
            "rule_id": version.rule_id,
            "version_id": version.version_id,
            "truncated": True,
        }
        try:
            result = _serialize_rule_version(version)
            result.update(written=True, truncated=False)
            output = json.dumps(result, ensure_ascii=False)
            if len(output) <= self.max_chars:
                return output
        except (TypeError, ValueError):
            pass  # Preserve the successful receipt even if full serialization fails.
        return json.dumps(receipt, ensure_ascii=False)

    @staticmethod
    def _check_safety(body: list[dict[str, str]], head: dict[str, str]) -> None:
        """Reject non-constant predicates and unbound head variables before writing."""
        for atom in body:
            if atom["predicate"].startswith("?"):
                raise ToolException(_RULE_ERROR)
        if head["predicate"].startswith("?"):
            raise ToolException(_RULE_ERROR)
        bound = set()
        for atom in body:
            bound |= _head_variables(atom)
        unbound = _head_variables(head) - bound
        if unbound:
            raise ToolException(_RULE_ERROR)


class GestaltDBRuleRunTool(_RuleWriteTool):
    """Evaluate active rules to a bounded fixpoint, persisting derived conclusions.

    Workload bounds (``max_iterations``/``max_derivations``/``max_justifications``)
    are application-controlled construction parameters, never agent inputs. A
    limit error persists no partial conclusions and surfaces the redacted
    upstream failure. An unchanged repeat run is idempotent: ``derived_count``
    0 with no new commit.
    """

    name: str = "gestaltdb_rule_run"
    description: str = (
        "Evaluate active Horn rules to a bounded fixpoint at a valid time, persisting derived "
        "entity-claim conclusions with rule/premise justifications. Bounds are "
        "application-controlled; limit errors persist nothing. Reruns without changes derive "
        "nothing new."
    )
    args_schema: type[BaseModel] = RuleRunInput
    max_iterations: int = Field(default=100, ge=1, le=100_000, exclude=True)
    max_derivations: int = Field(default=10_000, ge=1, le=1_000_000, exclude=True)
    max_justifications: int = Field(default=100_000, ge=1, le=10_000_000, exclude=True)
    max_versions: int = Field(default=50, ge=1, le=1000, exclude=True)

    def _run(
        self,
        as_of: str,
        system_time: str | None = None,
        world: str | None = None,
        run_manager: CallbackManagerForToolRun | None = None,
    ) -> str:
        try:
            kwargs: dict[str, Any] = {
                "as_of": as_of,
                "max_iterations": self.max_iterations,
                "max_derivations": self.max_derivations,
                "max_justifications": self.max_justifications,
            }
            if system_time is not None:
                kwargs["system_time"] = system_time
            if world is not None:
                kwargs["world"] = world
            outcome = call_upstream(self.graph.graph, "run_rules", **kwargs)
            versions = [_serialize_claim_version(version) for version in outcome.versions]
        except Exception:
            raise ToolException(_RULE_ERROR) from None
        result = {
            "as_of": as_of,
            "system_time": system_time,
            "world": world,
            "iterations": outcome.iterations,
            "derived_count": outcome.derived_count,
            "justification_count": outcome.justification_count,
            "commit_id": outcome.commit_id,
            "versions": versions,
            "returned_versions": len(versions),
            "truncated": False,
        }
        return bounded_json(
            result, (("versions", "returned_versions", self.max_versions),), self.max_chars
        )


class GestaltDBRuleMaintainTool(_RuleWriteTool):
    """Reconcile derived conclusions with the current rule fixpoint.

    Keeps independent supports, corrects changed justifications, and retracts a
    conclusion only after its final support disappears. Later calls may omit
    ``as_of`` to resume the previous valid time. Bounds are
    application-controlled; writes are opt-in and append-only.
    """

    name: str = "gestaltdb_rule_maintain"
    description: str = (
        "Incrementally reconcile derived rule conclusions with the current fixpoint: keep "
        "independent supports, correct changed justifications, retract only after the final "
        "support disappears. Bounds are application-controlled."
    )
    args_schema: type[BaseModel] = RuleMaintainInput
    max_iterations: int = Field(default=100, ge=1, le=100_000, exclude=True)
    max_derivations: int = Field(default=10_000, ge=1, le=1_000_000, exclude=True)
    max_justifications: int = Field(default=100_000, ge=1, le=10_000_000, exclude=True)
    max_versions: int = Field(default=50, ge=1, le=1000, exclude=True)

    def _run(
        self,
        as_of: str | None = None,
        system_time: str | None = None,
        world: str | None = None,
        run_manager: CallbackManagerForToolRun | None = None,
    ) -> str:
        try:
            kwargs: dict[str, Any] = {
                "max_iterations": self.max_iterations,
                "max_derivations": self.max_derivations,
                "max_justifications": self.max_justifications,
            }
            if as_of is not None:
                kwargs["as_of"] = as_of
            if system_time is not None:
                kwargs["system_time"] = system_time
            if world is not None:
                kwargs["world"] = world
            outcome = call_upstream(self.graph.graph, "maintain_truth", **kwargs)
            versions = [_serialize_claim_version(version) for version in outcome.versions]
        except Exception:
            raise ToolException(_RULE_ERROR) from None
        result = {
            "as_of": as_of,
            "system_time": system_time,
            "world": world,
            "input_commit_id": outcome.input_commit_id,
            "commit_id": outcome.commit_id,
            "asserted_count": outcome.asserted_count,
            "corrected_count": outcome.corrected_count,
            "retracted_count": outcome.retracted_count,
            "support_count": outcome.support_count,
            "versions": versions,
            "returned_versions": len(versions),
            "truncated": False,
        }
        return bounded_json(
            result, (("versions", "returned_versions", self.max_versions),), self.max_chars
        )


RuleToolKind = Literal[
    "gestaltdb_rule_get",
    "gestaltdb_rule_list",
    "gestaltdb_rule_create",
    "gestaltdb_rule_run",
    "gestaltdb_rule_maintain",
]
