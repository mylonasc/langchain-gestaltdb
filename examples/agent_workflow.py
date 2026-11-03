"""Offline agent-workflow demo: schema -> query/retrieve -> claim check -> explain.

Run: uv run --locked python examples/agent_workflow.py
Scripted tool sequence following docs/AGENT_WORKFLOW.md, not an LLM reasoning
benchmark. No model, API key, database server, or network service is required.
"""

from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from langchain_core.documents import Document

from langchain_gestaltdb import (
    GestaltDBClaimToolkit,
    GestaltDBGraph,
    GestaltDBRetriever,
    GestaltDBToolkit,
    GraphDocument,
    GraphNode,
    GraphRelationship,
    RetrievalHop,
)


def run_example(backend: str = "pyrex") -> dict[str, Any]:
    """Run one schema/query/retrieve/claim/explain pass and return a summary."""
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
            # Trusted setup (not agent input): index for exact-match seeding and
            # one sourced claim for the status/search/explanation tools.
            graph.graph.create_node_property_index("name")
            graph.graph.assert_claim(
                subject="alice",
                predicate="WORKS_FOR",
                object="acme",
                polarity="positive",
                agent="source:hr",
                source="hr.csv",
                confidence=0.9,
                world="reported",
                valid_from="2024-01-01T00:00:00Z",
            )

            # 1. Schema, then index check via the schema metadata.
            schema_tool, query_tool = GestaltDBToolkit(graph=graph).get_tools()
            schema = json.loads(schema_tool.invoke({}))
            assert "Person" in schema["node_props"]
            assert "name" in schema["metadata"]["indexes"]["indexed_node_properties"]

            # 2. Parameterized Cypher for the projection.
            rows = json.loads(
                query_tool.invoke(
                    {
                        "query": (
                            "MATCH (p:Person)-[:WORKS_FOR]->(c:Company) "
                            "WHERE p.name = $name "
                            "RETURN p.name AS person, c.name AS employer"
                        ),
                        "params": {"name": "Alice"},
                    }
                )
            )
            assert rows["returned_rows"] == 1

            # 3. Retriever for the seeded entity document with source evidence.
            retriever = GestaltDBRetriever(
                graph=graph,
                seed_label="Person",
                seed_property="name",
                hops=(RetrievalHop(edge_type="WORKS_FOR", direction="out"),),
            )
            retrieved = retriever.invoke("Alice")
            assert [doc.metadata["id"] for doc in retrieved if doc.metadata.get("is_seed")] == [
                "alice"
            ]

            # 4. Claim check, then explanation of the supporting version.
            status_tool, search_tool, explanation_tool = GestaltDBClaimToolkit(
                graph=graph
            ).get_tools()
            status = json.loads(
                status_tool.invoke(
                    {
                        "subject": "alice",
                        "predicate": "WORKS_FOR",
                        "object": "acme",
                        "world": "reported",
                        "valid_time": "2024-06-01T00:00:00Z",
                    }
                )
            )
            found = json.loads(
                search_tool.invoke(
                    {
                        "subject": "alice",
                        "predicate": "WORKS_FOR",
                        "world": "reported",
                        "valid_time": "2024-06-01T00:00:00Z",
                    }
                )
            )
            claim_id = found["claims"][0]["logical_id"]
            explanation = json.loads(
                explanation_tool.invoke(
                    {
                        "claim_id": claim_id,
                        "valid_time": "2024-06-01T00:00:00Z",
                    }
                )
            )
            return {
                "schema_labels": sorted(schema["node_props"]),
                "query_rows": rows["records"],
                "query_truncated": rows["truncated"],
                "retrieved_ids": sorted(doc.metadata["id"] for doc in retrieved),
                "claim_status": status["status"],
                "claims_found": found["returned_claims"],
                "explanation_truncated": explanation["truncated"],
            }


if __name__ == "__main__":
    print(json.dumps(run_example(), indent=2))
