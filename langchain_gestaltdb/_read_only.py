"""Version-isolated, fail-closed Cypher write-prevention policy (TD-03)."""

from __future__ import annotations

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


class ReadOnlyQueryError(ValueError):
    """The query could not be proven read-only under the supported AST policy."""


def validate_read_only(query: str, allowed_procedures: Collection[str] = ()) -> None:
    """Reject unknown syntax, mutations, DDL and non-allowlisted procedures.

    Recursively inspect every UNION branch and CALL subquery. Clause types are
    positively allowlisted, not matched by text. Procedures default to none and
    only known read procedures can be enabled. This is not an execution budget,
    a filesystem read-only handle, or protection against expensive reads.
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

    def inspect(node: Any, depth: int = 0) -> None:
        if depth > 32:
            raise ReadOnlyQueryError("Read-only query nesting exceeds 32 levels")
        kind = type(node)
        if kind is Query:
            for clause in node.clauses:
                if type(clause) is SubqueryClause:
                    inspect(clause.query, depth + 1)
                elif type(clause) not in _READ_CLAUSES:
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
