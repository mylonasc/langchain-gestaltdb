"""Version-isolated, fail-closed Cypher write-prevention policy (TD-03)."""

from __future__ import annotations

import dataclasses
from collections.abc import Collection
from typing import Any

from gestaltdb.cypher import parse_ast
from gestaltdb.cypher_ast import (
    EntailsCall,
    MatchClause,
    OptionalMatchClause,
    Query,
    ReturnClause,
    SampleTypedPathsCall,
    ShowConstraints,
    ShowIndexes,
    SubqueryClause,
    TemporalQualifier,
    UnionQuery,
    UnwindClause,
    WhereClause,
    WithClause,
)

_READ_CLAUSES = frozenset(
    {
        MatchClause,
        OptionalMatchClause,
        ReturnClause,
        UnwindClause,
        WhereClause,
        WithClause,
    }
)
_PROCEDURES = {SampleTypedPathsCall: "pg.sample_typed_paths", EntailsCall: "kg.entails"}
_TEMPORAL_QUALIFIER_KINDS = frozenset({"valid", "system"})


class ReadOnlyQueryError(ValueError):
    """The query could not be proven read-only under the supported AST policy."""


def _collect_temporal_kinds(node: Any, kinds: set[str], depth: int = 0) -> None:
    """Collect valid/system qualifier kinds across unions and subqueries.

    Unknown qualifier nodes or kinds fail closed. Qualifier expressions keep the
    same nesting budget as clauses; expression forms are otherwise unrestricted,
    so temporal version functions need no separate allowlist.
    """
    if depth > 32:
        raise ReadOnlyQueryError("Read-only query nesting exceeds 32 levels")
    node_type = type(node)
    if node_type is Query:
        for clause in node.clauses:
            if type(clause) is SubqueryClause:
                _collect_temporal_kinds(clause.query, kinds, depth + 1)
            else:
                for qualifier in getattr(clause, "qualifiers", ()):
                    if type(qualifier) is not TemporalQualifier:
                        raise ReadOnlyQueryError("Unsupported temporal qualifier")
                    if qualifier.kind not in _TEMPORAL_QUALIFIER_KINDS:
                        raise ReadOnlyQueryError(
                            f"Temporal qualifier {qualifier.kind!r} is not read-only"
                        )
                    kinds.add(qualifier.kind)
                    _inspect_expression(qualifier.expression, depth + 1)
    elif node_type is UnionQuery:
        for branch in node.branches:
            _collect_temporal_kinds(branch, kinds, depth + 1)
    elif node_type in (ShowIndexes, ShowConstraints) or node_type in _PROCEDURES:
        return
    # Any other top-level node is reported by the read-only clause inspection.


def _inspect_expression(node: Any, depth: int) -> None:
    """Bound qualifier-expression nesting without restricting expression forms."""
    if depth > 32:
        raise ReadOnlyQueryError("Read-only query nesting exceeds 32 levels")
    if node is None or isinstance(node, (str, bytes, bool, int, float)):
        return
    if isinstance(node, (tuple, list)):
        for item in node:
            _inspect_expression(item, depth + 1)
        return
    if dataclasses.is_dataclass(node) and not isinstance(node, type):
        for field in dataclasses.fields(node):
            if field.name in ("span", "source"):
                continue
            _inspect_expression(getattr(node, field.name), depth + 1)


def validate_read_only(query: str, allowed_procedures: Collection[str] = ()) -> None:
    """Reject unknown syntax, mutations, DDL and non-allowlisted procedures.

    Recursively inspect every UNION branch and CALL subquery. Clause types are
    positively allowlisted, not matched by text. Procedures default to none and
    only known read procedures can be enabled. Bitemporal
    ``FOR VALID_TIME AS OF`` (plus optional ``FOR SYSTEM_TIME AS OF``) qualifiers
    are permitted on ``MATCH``/``OPTIONAL MATCH``; the version metadata functions
    ``versionId``/``validFrom``/``validTo``/``systemFrom`` are ordinary read
    projections. System time requires valid time in the same query, and any
    temporal qualifier makes the whole query write-restricted. This is not an
    execution budget, a filesystem read-only handle, or protection against
    expensive reads.
    """
    allowed = frozenset(allowed_procedures)
    if allowed - set(_PROCEDURES.values()):
        raise ReadOnlyQueryError("Only known read-only procedures may be allowlisted")
    if not isinstance(query, str) or not query.strip() or len(query) > 10_000:
        raise ReadOnlyQueryError("Query must contain 1–10000 characters")
    try:
        parsed = parse_ast(query)
    except Exception as exc:
        raise ReadOnlyQueryError("Query is not accepted by the supported Cypher parser") from exc

    kinds: set[str] = set()
    _collect_temporal_kinds(parsed, kinds)
    if "system" in kinds and "valid" not in kinds:
        raise ReadOnlyQueryError("FOR SYSTEM_TIME requires FOR VALID_TIME in the same query")
    temporal = bool(kinds)

    def inspect(node: Any, depth: int = 0) -> None:
        if depth > 32:
            raise ReadOnlyQueryError("Read-only query nesting exceeds 32 levels")
        kind = type(node)
        if kind is Query:
            for clause in node.clauses:
                if type(clause) is SubqueryClause:
                    inspect(clause.query, depth + 1)
                elif type(clause) not in _READ_CLAUSES:
                    if temporal:
                        raise ReadOnlyQueryError("Temporal-qualified queries cannot write")
                    raise ReadOnlyQueryError(f"Clause {type(clause).__name__} is not read-only")
        elif kind is UnionQuery:
            for branch in node.branches:
                inspect(branch, depth + 1)
        elif kind in (ShowIndexes, ShowConstraints):
            return
        elif kind in _PROCEDURES:
            if _PROCEDURES[kind] not in allowed:
                raise ReadOnlyQueryError("Procedure is not allowlisted for read-only execution")
        else:
            raise ReadOnlyQueryError(f"Unsupported read-only query node: {kind.__name__}")

    inspect(parsed)
