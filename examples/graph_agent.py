"""Offline end-to-end ingestion, schema, tool-call dispatch and source evidence.

Run: uv run --locked python examples/graph_agent.py
This is a scripted agent-style tool call, not an LLM reasoning benchmark.
"""

from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from langchain_core.documents import Document
from langchain_core.messages import ToolMessage

from langchain_gestaltdb import (
    GestaltDBGraph,
    GestaltDBToolkit,
    GraphDocument,
    GraphNode,
    GraphRelationship,
)


def run_example(backend: str = "lmdb") -> dict[str, Any]:
    """Return an answer with source evidence, without a model or external service."""
    with TemporaryDirectory() as directory:
        person = GraphNode(id="alice", type="Person", properties={"name": "Alice"})
        company = GraphNode(id="acme", type="Company", properties={"name": "Acme"})
        document = GraphDocument(
            nodes=[person, company],
            relationships=[GraphRelationship(source=person, target=company, type="WORKS_FOR")],
            source=Document(
                id="hr-record-1",
                page_content="Alice works for Acme.",
                metadata={"source": "example:hr-record"},
            ),
        )
        with GestaltDBGraph.create(Path(directory) / "graph", backend=backend) as graph:
            graph.add_graph_documents([document], include_source=True)
            schema_tool, query_tool = GestaltDBToolkit(graph=graph).get_tools()
            schema = json.loads(schema_tool.invoke({}))
            assert "Person" in schema["node_props"]
            response = query_tool.invoke(
                {
                    "type": "tool_call",
                    "id": "example-call",
                    "name": query_tool.name,
                    "args": {
                        "query": (
                            "MATCH (s:__LangChainSource__)-[:MENTIONS]->(p:Person) "
                            "MATCH (p)-[:WORKS_FOR]->(c:Company) WHERE p.name=$name "
                            "RETURN p.name AS person, c.name AS employer, "
                            "s.page_content AS evidence, s.metadata AS provenance"
                        ),
                        "params": {"name": "Alice"},
                    },
                }
            )
            assert isinstance(response, ToolMessage)
            result: dict[str, Any] = json.loads(str(response.content))
            assert result["records"][0]["employer"] == "Acme"
            assert not result["truncated"]
            return result


if __name__ == "__main__":
    print(json.dumps(run_example(), indent=2))
