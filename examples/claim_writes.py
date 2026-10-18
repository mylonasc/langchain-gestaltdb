"""Offline opt-in claim writes: fact validity vs system knowledge time.

Run: uv run --locked python examples/claim_writes.py
Scripted agent-style tool calls, not an LLM reasoning benchmark. No model or
network service is required. Writes are explicitly enabled for a trusted caller.
"""

from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from langchain_gestaltdb import GestaltDBClaimToolkit, GestaltDBGraph


def run_example(backend: str = "pyrex") -> dict[str, Any]:
    """Assert, correct and retract, distinguishing validity from knowledge time."""
    with TemporaryDirectory() as directory:
        with GestaltDBGraph.create(Path(directory) / "graph", backend=backend) as graph:
            tools = {
                tool.name: tool
                for tool in GestaltDBClaimToolkit(graph=graph, allow_writes=True).get_tools()
            }
            asserted = json.loads(
                tools["gestaltdb_claim_assert"].invoke(
                    {
                        "subject": "alice",
                        "predicate": "WORKS_FOR",
                        "object": "acme",
                        "polarity": "positive",
                        "agent": "source:hr",
                        "source": "hr.csv",
                        "confidence": 0.4,
                        "provenance": {"document": "hr.csv"},
                        "valid_from": "2024-01-01T00:00:00Z",
                    }
                )
            )
            corrected = json.loads(
                tools["gestaltdb_claim_correct"].invoke(
                    {
                        "claim_id": asserted["logical_id"],
                        "supersedes_version_id": asserted["version_id"],
                        "confidence": 0.9,
                        "provenance": {"document": "hr.csv", "reviewed": True},
                    }
                )
            )
            retracted = json.loads(
                tools["gestaltdb_claim_retract"].invoke(
                    {
                        "claim_id": asserted["logical_id"],
                        "supersedes_version_id": corrected["version_id"],
                        "reason": "employment ended",
                        "valid_from": "2024-05-01T00:00:00Z",
                    }
                )
            )

            def known_at(commit_id: int) -> list[float | None]:
                result = json.loads(
                    tools["gestaltdb_claim_search"].invoke(
                        {
                            "statement_id": asserted["claim"]["statement_id"],
                            "valid_time": "2024-06-01T00:00:00Z",
                            "through_commit": commit_id,
                        }
                    )
                )
                return [item["claim"]["confidence"] for item in result["claims"]]

            return {
                "writes_confirmed": all(
                    version["written"] for version in (asserted, corrected, retracted)
                ),
                "fact_validity": asserted["valid"],
                "knowledge_time": asserted["system_time"],
                "validity_differs_from_knowledge_time": asserted["valid"]
                != asserted["system_time"],
                "asserted_operation": asserted["operation"],
                "corrected_operation": corrected["operation"],
                "correction_supersedes_assert": corrected["supersedes_version_id"]
                == asserted["version_id"],
                "retracted_operation": retracted["operation"],
                "retraction_removes_current_view": retracted["claim"] is None,
                "identity_preserved": asserted["logical_id"]
                == corrected["logical_id"]
                == retracted["logical_id"],
                "confidence_known_at_assert": known_at(asserted["commit_id"]),
                "confidence_known_at_correction": known_at(corrected["commit_id"]),
                "confidence_after_retraction": known_at(retracted["commit_id"]),
                "read_only_tool_count": len(GestaltDBClaimToolkit(graph=graph).get_tools()),
            }


if __name__ == "__main__":
    print(json.dumps(run_example(), indent=2))
