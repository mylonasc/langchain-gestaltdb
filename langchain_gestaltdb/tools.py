"""LangChain tools with read-only defaults and bounded, valid JSON output."""

from __future__ import annotations

import json
from typing import Any

from langchain_core.callbacks import CallbackManagerForToolRun
from langchain_core.tools import BaseTool, ToolException
from pydantic import BaseModel, ConfigDict, Field, StrictBool, model_validator

from langchain_gestaltdb._read_only import ReadOnlyQueryError, validate_read_only
from langchain_gestaltdb.graph import GestaltDBGraph


class QueryInput(BaseModel):
    """Only query/parameters are agent-controllable; permissions/bounds are not."""

    model_config = ConfigDict(extra="forbid")
    query: str = Field(min_length=1, max_length=10_000, description="Supported GestaltDB Cypher")
    params: dict[str, Any] = Field(default_factory=dict, description="Values for $parameters")


class SchemaInput(BaseModel):
    """Refresh is permitted; inspection bounds remain application-controlled."""

    model_config = ConfigDict(extra="forbid")
    refresh: StrictBool = Field(default=False, description="Refresh the bounded schema sample")


def _bounded_output(records: list[dict[str, Any]], max_rows: int, max_chars: int) -> str:
    result: dict[str, Any] = {
        "records": [],
        "returned_rows": 0,
        "truncated": len(records) > max_rows,
    }
    for row in records[:max_rows]:
        result["records"].append(row)
        result["returned_rows"] += 1
        if len(json.dumps(result, ensure_ascii=False)) > max_chars:
            result["records"].pop()
            result["returned_rows"] -= 1
            result["truncated"] = True
            break
    return json.dumps(result, ensure_ascii=False)


class GestaltDBQueryTool(BaseTool):
    """Write prevention by default; explicit trusted construction enables writes.

    Row/character limits apply to returned output, not execution or intermediate
    memory. Read queries can be expensive. BaseTool's default async adapter uses
    threads; this is not a native async database or a concurrency guarantee.
    """

    name: str = "gestaltdb_query"
    description: str = (
        "Read-only parameterized GestaltDB Cypher query. Inspect gestaltdb_schema first. "
        "Returns JSON records and truncation metadata. Output caps are not execution limits."
    )
    args_schema: type[BaseModel] = QueryInput
    graph: GestaltDBGraph = Field(exclude=True)
    allow_writes: StrictBool = Field(default=False, exclude=True)
    allowed_procedures: tuple[str, ...] = Field(default=(), exclude=True)
    max_rows: int = Field(default=100, ge=1, le=1000, exclude=True)
    max_chars: int = Field(default=16_000, ge=256, le=1_000_000, exclude=True)
    handle_tool_error: bool = True

    @model_validator(mode="after")
    def check_policy(self) -> GestaltDBQueryTool:
        validate_read_only("SHOW INDEXES", self.allowed_procedures)
        if self.allow_writes:
            self.description = self.description.replace("Read-only", "Write-enabled")
        return self

    def _run(
        self,
        query: str,
        params: dict[str, Any] | None = None,
        run_manager: CallbackManagerForToolRun | None = None,
    ) -> str:
        try:
            if self.allow_writes:
                records = self.graph.query(query, params)
            else:
                records = self.graph.query_read_only(
                    query, params, allowed_procedures=self.allowed_procedures
                )
            return _bounded_output(records, self.max_rows, self.max_chars)
        except ReadOnlyQueryError as exc:
            raise ToolException(str(exc)) from None
        except Exception:
            # Do not echo data, query text, paths, credentials, or raw backend errors.
            raise ToolException(
                "GestaltDB query failed; check the dialect, parameters and database state."
            ) from None


class GestaltDBSchemaTool(BaseTool):
    """Return bounded cached schema and dialect guidance without write access."""

    name: str = "gestaltdb_schema"
    description: str = (
        "Inspect cached, sampled GestaltDB schema, property types, relationship endpoints "
        "and Cypher guidance. Missing sampled properties may still exist. Optional refresh."
    )
    args_schema: type[BaseModel] = SchemaInput
    graph: GestaltDBGraph = Field(exclude=True)
    max_chars: int = Field(default=16_000, ge=256, le=1_000_000, exclude=True)
    handle_tool_error: bool = True

    def _run(
        self, refresh: bool = False, run_manager: CallbackManagerForToolRun | None = None
    ) -> str:
        try:
            if refresh:
                self.graph.refresh_schema()
            output = self.graph.get_schema
        except Exception:
            raise ToolException(
                "GestaltDB schema inspection failed; check the database state."
            ) from None
        if len(output) > self.max_chars:
            raise ToolException(
                "Schema exceeds output limit; use smaller samples or a trusted schema override."
            )
        return output
