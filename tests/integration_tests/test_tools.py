import asyncio
import json
from pathlib import Path
from unittest.mock import patch

import pytest
from gestaltdb.graphdb import Node
from langchain_core.messages import ToolMessage
from pydantic import ValidationError

from langchain_gestaltdb import (
    GestaltDBGraph,
    GestaltDBQueryTool,
    GestaltDBSchemaTool,
    GestaltDBToolkit,
)


def test_default_toolkit_invocation_and_agent_tool_call(tmp_path: Path) -> None:
    with GestaltDBGraph.create(tmp_path / "g", backend="lmdb") as graph:
        graph.graph.put_node(Node("a", labels=["Person"], properties={"name": "Alice"}))
        schema, query = GestaltDBToolkit(graph=graph).get_tools()
        assert "Person" in json.loads(schema.invoke({}))["node_props"]
        args = {
            "query": "MATCH (n) WHERE n.name=$name RETURN n.name AS name",
            "params": {"name": "Alice"},
        }
        assert json.loads(query.invoke(args))["records"] == [{"name": "Alice"}]
        result = query.invoke(
            {"type": "tool_call", "id": "call-1", "name": query.name, "args": args}
        )
        assert isinstance(result, ToolMessage)
        assert result.tool_call_id == "call-1"
        assert json.loads(result.content)["records"] == [{"name": "Alice"}]
        assert "not read-only" in query.invoke({"query": "MATCH (n) SET n.secret=1 RETURN n"})
        assert graph.graph.get_node(b"a").properties == {"name": "Alice"}
        assert "graph" not in query.model_dump()


def test_permissions_and_limits_cannot_be_passed_by_agent(tmp_path: Path) -> None:
    with GestaltDBGraph.create(tmp_path / "g", backend="lmdb") as graph:
        tool = GestaltDBQueryTool(graph=graph)
        for field in ["allow_writes", "allowed_procedures", "max_rows", "max_chars", "graph"]:
            with pytest.raises(ValidationError):
                tool.invoke({"query": "MATCH (n) RETURN n", field: True})
        assert set(tool.get_input_schema().model_fields) == {"query", "params"}
        with pytest.raises(ValidationError):
            GestaltDBSchemaTool(graph=graph).invoke({"query": "CREATE (n)"})


def test_trusted_write_opt_in_and_error_redaction(tmp_path: Path) -> None:
    with GestaltDBGraph.create(tmp_path / "g", backend="lmdb") as graph:
        tool = GestaltDBQueryTool(graph=graph, allow_writes=True)
        assert "Write-enabled" in tool.description
        tool.invoke({"query": "CREATE (n:Person {id:'a'}) RETURN n"})
        assert graph.graph.get_node(b"a") is not None
        with patch.object(
            graph.graph, "query", side_effect=RuntimeError("/private/path secret-token")
        ):
            output = tool.invoke({"query": "MATCH (n) RETURN n"})
            assert "secret-token" not in output
            assert "failed" in output


def test_output_caps_preserve_valid_json_and_report_truncation(tmp_path: Path) -> None:
    with GestaltDBGraph.create(tmp_path / "g", backend="lmdb") as graph:
        graph.graph.put_nodes([Node(str(i), properties={"huge": "x" * 1000}) for i in range(5)])
        rows = GestaltDBQueryTool(graph=graph, max_rows=2).invoke(
            {"query": "MATCH (n) RETURN n.id AS id"}
        )
        assert json.loads(rows)["returned_rows"] == 2
        assert json.loads(rows)["truncated"] is True
        output = GestaltDBQueryTool(graph=graph, max_chars=256).invoke(
            {"query": "MATCH (n) RETURN n"}
        )
        assert len(output) <= 256
        assert json.loads(output) == {"records": [], "returned_rows": 0, "truncated": True}
        assert "output limit" in GestaltDBSchemaTool(graph=graph, max_chars=256).invoke({})


def test_base_tool_async_adapter_runs_without_native_async_claim(tmp_path: Path) -> None:
    with GestaltDBGraph.create(tmp_path / "g", backend="lmdb") as graph:
        result = asyncio.run(
            GestaltDBQueryTool(graph=graph).ainvoke({"query": "MATCH (n) RETURN n"})
        )
        assert json.loads(result)["records"] == []
