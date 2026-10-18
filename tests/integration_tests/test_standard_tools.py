from collections.abc import Iterator
from pathlib import Path

import pytest
from langchain_core.tools import BaseTool
from langchain_tests.unit_tests.tools import ToolsUnitTests

from langchain_gestaltdb import (
    GestaltDBClaimExplanationTool,
    GestaltDBClaimSearchTool,
    GestaltDBClaimStatusTool,
    GestaltDBGraph,
    GestaltDBQueryTool,
    GestaltDBSchemaTool,
)


class TestQueryToolStandard(ToolsUnitTests):
    @pytest.fixture(autouse=True)
    def graph_fixture(self, tmp_path: Path) -> Iterator[None]:
        with GestaltDBGraph.create(tmp_path / "g", backend="lmdb") as graph:
            self.graph = graph
            yield

    @property
    def tool_constructor(self) -> type[BaseTool]:
        return GestaltDBQueryTool

    @property
    def tool_constructor_params(self) -> dict:
        return {"graph": self.graph}

    @property
    def tool_invoke_params_example(self) -> dict:
        return {"query": "MATCH (n) RETURN n", "params": {}}


class TestSchemaToolStandard(TestQueryToolStandard):
    @property
    def tool_constructor(self) -> type[BaseTool]:
        return GestaltDBSchemaTool

    @property
    def tool_invoke_params_example(self) -> dict:
        return {}


class TestClaimStatusToolStandard(TestQueryToolStandard):
    @property
    def tool_constructor(self) -> type[BaseTool]:
        return GestaltDBClaimStatusTool

    @property
    def tool_invoke_params_example(self) -> dict:
        return {
            "subject": "alice",
            "predicate": "WORKS_FOR",
            "object": "acme",
            "valid_time": "2024-06-01T00:00:00Z",
        }


class TestClaimSearchToolStandard(TestQueryToolStandard):
    @property
    def tool_constructor(self) -> type[BaseTool]:
        return GestaltDBClaimSearchTool

    @property
    def tool_invoke_params_example(self) -> dict:
        return {"subject": "alice", "valid_time": "2024-06-01T00:00:00Z"}


class TestClaimExplanationToolStandard(TestQueryToolStandard):
    @property
    def tool_constructor(self) -> type[BaseTool]:
        return GestaltDBClaimExplanationTool

    @property
    def tool_invoke_params_example(self) -> dict:
        return {"claim_id": "claim:" + "0" * 64, "valid_time": "2024-06-01T00:00:00Z"}
