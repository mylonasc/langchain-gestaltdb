import json
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from langchain_gestaltdb import (
    GestaltDBClaimExplanationTool,
    GestaltDBClaimSearchTool,
    GestaltDBClaimStatusTool,
    GestaltDBClaimToolkit,
    GestaltDBGraph,
)

VALID_TIME = "2024-06-01T00:00:00Z"


@pytest.fixture
def claim_graph(tmp_path: Path) -> Iterator[tuple[GestaltDBGraph, object]]:
    with GestaltDBGraph.create(tmp_path / "g", backend="lmdb") as graph:
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
        derived = database.run_rules(as_of=VALID_TIME).versions[0]
        yield graph, derived


def test_status_distinguishes_four_valued_results(claim_graph) -> None:
    graph, _ = claim_graph
    tool = GestaltDBClaimStatusTool(graph=graph)
    both = json.loads(
        tool.invoke(
            {
                "subject": "alice",
                "predicate": "WORKS_FOR",
                "object": "acme",
                "valid_time": VALID_TIME,
                "world": "reported",
            }
        )
    )
    assert both["status"] == "both"
    assert both["returned_evidence"] == 2
    assert {item["claim"]["polarity"] for item in both["evidence"]} == {"positive", "negative"}
    assert both["truncated"] is False

    supported = json.loads(
        tool.invoke(
            {
                "subject": "alice",
                "predicate": "WORKS_FOR",
                "object": "acme",
                "valid_time": VALID_TIME,
                "source": "hr.csv",
            }
        )
    )
    by_agent = json.loads(
        tool.invoke(
            {
                "subject": "alice",
                "predicate": "WORKS_FOR",
                "object": "acme",
                "valid_time": VALID_TIME,
                "agent": "source:hr",
            }
        )
    )
    at_first_commit = json.loads(
        tool.invoke(
            {
                "subject": "alice",
                "predicate": "WORKS_FOR",
                "object": "acme",
                "valid_time": VALID_TIME,
                "through_commit": 1,
            }
        )
    )
    refuted = json.loads(
        tool.invoke(
            {
                "subject": "alice",
                "predicate": "WORKS_FOR",
                "object": "acme",
                "valid_time": VALID_TIME,
                "source": "audit.csv",
            }
        )
    )
    unknown = json.loads(
        tool.invoke(
            {
                "subject": "nobody",
                "predicate": "WORKS_FOR",
                "object": "nowhere",
                "valid_time": VALID_TIME,
            }
        )
    )
    assert supported["status"] == "supported"
    assert refuted["status"] == "refuted"
    assert unknown["status"] == "unknown"
    assert unknown["evidence"] == []
    assert by_agent["status"] == "supported"
    assert by_agent["filters"]["agent"] == "source:hr"
    assert at_first_commit["status"] == "supported"
    assert at_first_commit["returned_evidence"] == 1


def test_search_filters_and_bounded_output(claim_graph) -> None:
    graph, _ = claim_graph
    tool = GestaltDBClaimSearchTool(graph=graph)
    everything = json.loads(
        tool.invoke({"subject": "alice", "predicate": "WORKS_FOR", "valid_time": VALID_TIME})
    )
    assert everything["returned_claims"] == 2
    negative = json.loads(
        tool.invoke(
            {
                "subject": "alice",
                "predicate": "WORKS_FOR",
                "valid_time": VALID_TIME,
                "polarity": "negative",
            }
        )
    )
    assert [item["claim"]["agent"] for item in negative["claims"]] == ["source:audit"]
    by_agent = json.loads(
        tool.invoke({"agent": "source:hr", "valid_time": VALID_TIME, "world": "reported"})
    )
    assert by_agent["returned_claims"] == 1
    by_world = json.loads(tool.invoke({"world": "missing", "valid_time": VALID_TIME}))
    assert by_world["returned_claims"] == 0
    by_source = json.loads(tool.invoke({"source": "audit.csv", "valid_time": VALID_TIME}))
    assert by_source["returned_claims"] == 1
    assert by_source["filters"]["source"] == "audit.csv"

    small = GestaltDBClaimSearchTool(graph=graph, max_claims=1)
    capped = json.loads(small.invoke({"subject": "alice", "valid_time": VALID_TIME}))
    assert capped["returned_claims"] == 1
    assert capped["truncated"] is True
    json.dumps(capped, allow_nan=False)


def test_bitemporal_horizons_and_historical_versions(tmp_path: Path) -> None:
    with GestaltDBGraph.create(tmp_path / "g", backend="lmdb") as graph:
        database = graph.graph
        first = database.assert_claim(
            subject="acme",
            predicate="MEMBER_OF",
            object="industry",
            polarity="positive",
            agent="source:registry",
            confidence=0.4,
            world="reported",
            valid_from="2024-03-01T00:00:00Z",
        )
        database.correct_claim(
            first.logical_id,
            supersedes_version_id=first.version_id,
            confidence=0.9,
            provenance={"reviewed": True},
        )
        status = GestaltDBClaimStatusTool(graph=graph)
        search = GestaltDBClaimSearchTool(graph=graph)
        assert (
            json.loads(
                status.invoke(
                    {
                        "subject": "acme",
                        "predicate": "MEMBER_OF",
                        "object": "industry",
                        "valid_time": "2024-01-15T00:00:00Z",
                    }
                )
            )["status"]
            == "unknown"
        )
        latest = json.loads(search.invoke({"subject": "acme", "valid_time": VALID_TIME}))
        assert latest["claims"][0]["operation"] == "correct"
        assert latest["claims"][0]["claim"]["confidence"] == 0.9
        assert latest["claims"][0]["claim"]["provenance"] == {"reviewed": True}
        historical = json.loads(
            search.invoke({"subject": "acme", "valid_time": VALID_TIME, "through_commit": 1})
        )
        assert historical["claims"][0]["operation"] == "assert"
        assert historical["claims"][0]["claim"]["confidence"] == 0.4
        by_system_time = json.loads(
            search.invoke(
                {
                    "subject": "acme",
                    "valid_time": VALID_TIME,
                    "system_time": first.system_time.to_datetime().isoformat(),
                }
            )
        )
        assert by_system_time["claims"][0]["operation"] == "assert"
        historical_status = json.loads(
            status.invoke(
                {
                    "subject": "acme",
                    "predicate": "MEMBER_OF",
                    "object": "industry",
                    "valid_time": VALID_TIME,
                    "system_time": first.system_time.to_datetime().isoformat(),
                }
            )
        )
        assert historical_status["status"] == "supported"


def test_explanation_preserves_historical_ids_and_truncation(claim_graph) -> None:
    graph, derived = claim_graph
    tool = GestaltDBClaimExplanationTool(graph=graph)
    explanation = json.loads(
        tool.invoke({"claim_id": derived.logical_id, "valid_time": VALID_TIME})
    )
    assert explanation["found"] is True
    assert explanation["root_version_id"] == derived.version_id
    assert explanation["logical_id"] == derived.logical_id
    assert explanation["truncated"] is False
    root = next(node for node in explanation["nodes"] if node["node_id"] == derived.version_id)
    assert root["kind"] == "claim"
    assert root["value"]["version_id"] == derived.version_id
    assert root["value"]["claim"]["claim_id"] == derived.logical_id
    assert {edge["relationship"] for edge in explanation["edges"]} == {"derived_by", "premise"}
    assert sorted(
        edge["premise_ordinal"]
        for edge in explanation["edges"]
        if edge["relationship"] == "premise"
    ) == [0, 1]

    bounded = GestaltDBClaimExplanationTool(graph=graph, max_nodes=1)
    capped = json.loads(bounded.invoke({"claim_id": derived.logical_id, "valid_time": VALID_TIME}))
    assert capped["truncated"] is True
    assert capped["returned_nodes"] == 1

    missing = json.loads(
        GestaltDBClaimExplanationTool(graph=graph).invoke(
            {"claim_id": "claim:" + "0" * 64, "valid_time": VALID_TIME}
        )
    )
    assert missing == {
        "found": False,
        "claim_id": "claim:" + "0" * 64,
        "valid_time": VALID_TIME,
        "system_time": None,
        "truncated": False,
    }


def test_claims_are_not_graph_edges_or_cypher_visible(claim_graph) -> None:
    graph, _ = claim_graph
    assert graph.query_read_only("MATCH (n) RETURN n") == []
    assert graph.query_read_only("MATCH ()-[r]->() RETURN r") == []


def test_literal_objects_are_distinct_from_entities(tmp_path: Path) -> None:
    with GestaltDBGraph.create(tmp_path / "g", backend="lmdb") as graph:
        graph.graph.assert_claim(
            subject="metric",
            predicate="VALUE",
            object=42,
            object_kind="literal",
            polarity="positive",
            agent="sensor",
            world="reported",
            valid_from="2024-01-01T00:00:00Z",
        )
        tool = GestaltDBClaimStatusTool(graph=graph)
        literal = json.loads(
            tool.invoke(
                {
                    "subject": "metric",
                    "predicate": "VALUE",
                    "object": 42,
                    "object_kind": "literal",
                    "valid_time": VALID_TIME,
                }
            )
        )
        found = json.loads(
            GestaltDBClaimSearchTool(graph=graph).invoke(
                {
                    "object": 42,
                    "object_kind": "literal",
                    "valid_time": VALID_TIME,
                }
            )
        )
        entity = json.loads(
            tool.invoke(
                {
                    "subject": "metric",
                    "predicate": "VALUE",
                    "object": "42",
                    "object_kind": "entity",
                    "valid_time": VALID_TIME,
                }
            )
        )
        assert literal["status"] == "supported"
        assert literal["object_kind"] == "literal"
        assert found["returned_claims"] == 1
        assert found["claims"][0]["claim"]["object"] == 42
        assert entity["status"] == "unknown"


def test_character_caps_bound_records(claim_graph) -> None:
    graph, derived = claim_graph
    status = GestaltDBClaimStatusTool(graph=graph, max_chars=256)
    capped_status = json.loads(
        status.invoke(
            {
                "subject": "alice",
                "predicate": "WORKS_FOR",
                "object": "acme",
                "valid_time": VALID_TIME,
            }
        )
    )
    assert capped_status["evidence"] == []
    assert capped_status["truncated"] is True

    search = GestaltDBClaimSearchTool(graph=graph, max_chars=256)
    capped_search = json.loads(search.invoke({"subject": "alice", "valid_time": VALID_TIME}))
    assert capped_search["claims"] == []
    assert capped_search["truncated"] is True

    explanation_tool = GestaltDBClaimExplanationTool(graph=graph, max_chars=256)
    capped_explanation = json.loads(
        explanation_tool.invoke({"claim_id": derived.logical_id, "valid_time": VALID_TIME})
    )
    assert capped_explanation["nodes"] == []
    assert capped_explanation["edges"] == []
    assert capped_explanation["truncated"] is True

    no_edges = GestaltDBClaimExplanationTool(graph=graph, max_edges=0)
    bounded_edges = json.loads(
        no_edges.invoke({"claim_id": derived.logical_id, "valid_time": VALID_TIME})
    )
    assert bounded_edges["returned_edges"] == 0
    assert bounded_edges["truncated"] is True


def test_unknown_node_values_are_normalized(claim_graph) -> None:
    from types import SimpleNamespace

    graph, _ = claim_graph
    stub = SimpleNamespace(
        root_version_id="v1",
        claim_id="claim:x",
        nodes=[SimpleNamespace(node_id="n1", kind="other", value={"key": "value"})],
        edges=[],
        truncated=False,
    )
    tool = GestaltDBClaimExplanationTool(graph=graph)
    with patch.object(graph.graph, "explain_claim", return_value=stub):
        output = json.loads(tool.invoke({"claim_id": "claim:x", "valid_time": VALID_TIME}))
    assert output["nodes"] == [{"node_id": "n1", "kind": "other", "value": {"key": "value"}}]


def test_toolkit_and_agent_cannot_change_bounds(claim_graph) -> None:
    graph, _ = claim_graph
    status, search, explanation = GestaltDBClaimToolkit(graph=graph).get_tools()
    assert [tool.name for tool in (status, search, explanation)] == [
        "gestaltdb_claim_status",
        "gestaltdb_claim_search",
        "gestaltdb_claim_explanation",
    ]
    for tool in (status, search, explanation):
        assert "graph" not in tool.model_dump()
    with pytest.raises(ValidationError):
        status.invoke(
            {
                "subject": "alice",
                "predicate": "WORKS_FOR",
                "object": "acme",
                "valid_time": VALID_TIME,
                "max_evidence": 1000,
            }
        )
    assert set(GestaltDBClaimSearchTool(graph=graph).get_input_schema().model_fields) == {
        "statement_id",
        "subject",
        "predicate",
        "object",
        "object_kind",
        "agent",
        "source",
        "world",
        "polarity",
        "valid_time",
        "system_time",
        "through_commit",
    }


def test_errors_are_redacted(claim_graph) -> None:
    graph, _ = claim_graph
    search = GestaltDBClaimSearchTool(graph=graph)
    invalid = search.invoke({"statement_id": "not-a-statement", "valid_time": VALID_TIME})
    assert "claim operation failed" in invalid
    assert "statement_id" not in invalid
    with patch.object(graph.graph, "claim_status", side_effect=RuntimeError("/secret/path")):
        output = GestaltDBClaimStatusTool(graph=graph).invoke(
            {
                "subject": "alice",
                "predicate": "WORKS_FOR",
                "object": "acme",
                "valid_time": VALID_TIME,
            }
        )
    assert "claim operation failed" in output
    assert "secret" not in output
    with patch.object(graph.graph, "explain_claim", side_effect=RuntimeError("/secret/path")):
        explanation = GestaltDBClaimExplanationTool(graph=graph).invoke(
            {"claim_id": "claim:x", "valid_time": VALID_TIME}
        )
    assert "claim operation failed" in explanation
    assert "secret" not in explanation


def test_executable_offline_claim_example() -> None:
    example = Path(__file__).resolve().parents[2] / "examples" / "claim_agent.py"
    result = subprocess.run(
        [sys.executable, str(example)], check=True, capture_output=True, text=True
    )
    answer = json.loads(result.stdout)
    assert answer["status"] == "both"
    assert answer["supporting_and_refuting"] == 2
    assert answer["explanation_nodes"] >= 3
    assert answer["explanation_truncated"] is False
