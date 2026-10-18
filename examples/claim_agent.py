"""Offline claim status, contradiction and provenance tool calls.

Run: uv run --locked python examples/claim_agent.py
Scripted agent-style tool calls, not an LLM reasoning benchmark. No model or
network service is required; claims are epistemic assertions, not graph edges.
"""

from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from langchain_core.messages import ToolMessage

from langchain_gestaltdb import GestaltDBClaimToolkit, GestaltDBGraph


def run_example(backend: str = "lmdb") -> dict[str, Any]:
    """Return four-valued status and a bounded explanation without a model."""
    with TemporaryDirectory() as directory:
        with GestaltDBGraph.create(Path(directory) / "graph", backend=backend) as graph:
            database = graph.graph
            database.assert_claim(
                subject="alice",
                predicate="WORKS_FOR",
                object="acme",
                polarity="positive",
                agent="source:hr",
                source="hr.csv",
                confidence=0.97,
                world="reported",
                valid_from="2024-01-01T00:00:00Z",
            )
            database.assert_claim(
                subject="alice",
                predicate="WORKS_FOR",
                object="acme",
                polarity="negative",
                agent="source:audit",
                source="audit.csv",
                confidence=0.7,
                world="reported",
                valid_from="2024-01-01T00:00:00Z",
            )
            database.assert_claim(
                subject="acme",
                predicate="MEMBER_OF",
                object="industry",
                polarity="positive",
                agent="source:registry",
                world="reported",
                valid_from="2024-03-01T00:00:00Z",
            )
            database.create_rule(
                "employment-implies-affiliation",
                when=[("?p", "WORKS_FOR", "?c"), ("?c", "MEMBER_OF", "?g")],
                then=("?p", "AFFILIATED_WITH", "?g"),
            )
            derived = database.run_rules(as_of="2024-06-01T00:00:00Z").versions[0]

            status_tool, _search, explanation_tool = GestaltDBClaimToolkit(graph=graph).get_tools()
            status_call = status_tool.invoke(
                {
                    "type": "tool_call",
                    "id": "claim-status",
                    "name": status_tool.name,
                    "args": {
                        "subject": "alice",
                        "predicate": "WORKS_FOR",
                        "object": "acme",
                        "world": "reported",
                        "valid_time": "2024-06-01T00:00:00Z",
                    },
                }
            )
            assert isinstance(status_call, ToolMessage)
            status = json.loads(str(status_call.content))
            explanation = json.loads(
                explanation_tool.invoke(
                    {
                        "claim_id": derived.logical_id,
                        "valid_time": "2024-06-01T00:00:00Z",
                    }
                )
            )
            return {
                "status": status["status"],
                "supporting_and_refuting": status["returned_evidence"],
                "explanation_root": explanation["root_version_id"],
                "explanation_nodes": explanation["returned_nodes"],
                "explanation_truncated": explanation["truncated"],
            }


if __name__ == "__main__":
    print(json.dumps(run_example(), indent=2))
