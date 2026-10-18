import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from langchain_gestaltdb import (
    GestaltDBClaimToolkit,
    GestaltDBGraph,
)

VALID_TIME = "2024-06-01T00:00:00Z"
READ_ONLY_NAMES = [
    "gestaltdb_claim_status",
    "gestaltdb_claim_search",
    "gestaltdb_claim_explanation",
]


def _write_tools(graph: GestaltDBGraph) -> dict:
    return {
        tool.name: tool
        for tool in GestaltDBClaimToolkit(graph=graph, allow_writes=True).get_tools()
    }


def _assert(tools: dict, **overrides) -> dict:
    args = {
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
    args.update(overrides)
    return json.loads(tools["gestaltdb_claim_assert"].invoke(args))


def test_writes_require_opt_in_and_record_explicit_inputs(tmp_path: Path) -> None:
    with GestaltDBGraph.create(tmp_path / "g", backend="lmdb") as graph:
        read_only = GestaltDBClaimToolkit(graph=graph).get_tools()
        assert [tool.name for tool in read_only] == READ_ONLY_NAMES
        assert not any("assert" in tool.name or "correct" in tool.name for tool in read_only)
        assert not any("retract" in tool.name for tool in read_only)

        written = _assert(_write_tools(graph))
        assert written["operation"] == "assert"
        assert written["supersedes_version_id"] is None
        assert written["claim"]["agent"] == "source:hr"
        assert written["claim"]["source"] == "hr.csv"
        assert written["claim"]["confidence"] == 0.4
        assert written["claim"]["provenance"] == {"document": "hr.csv"}
        assert written["valid"]["$type"] == "TemporalInterval"
        assert written["system_time"]["$type"] == "TemporalInstant"


def test_write_inputs_are_typed_and_validated(tmp_path: Path) -> None:
    with GestaltDBGraph.create(tmp_path / "g", backend="lmdb") as graph:
        tools = _write_tools(graph)
        assert "graph" not in tools["gestaltdb_claim_assert"].model_dump()
        with pytest.raises(ValidationError):
            tools["gestaltdb_claim_assert"].invoke(
                {
                    "subject": "a",
                    "predicate": "P",
                    "object": "b",
                    "polarity": "positive",
                    "valid_from": "2024-01-01T00:00:00Z",
                }
            )
        with pytest.raises(ValidationError):
            tools["gestaltdb_claim_assert"].invoke(
                {
                    "subject": "a",
                    "predicate": "P",
                    "object": "b",
                    "polarity": "positive",
                    "agent": "x",
                }
            )
        with pytest.raises(ValidationError):
            tools["gestaltdb_claim_assert"].invoke(
                {
                    "subject": "a",
                    "predicate": "P",
                    "object": "b",
                    "polarity": "positive",
                    "agent": "x",
                    "confidence": 1.5,
                }
            )
        with pytest.raises(ValidationError):
            tools["gestaltdb_claim_assert"].invoke(
                {
                    "subject": "a",
                    "predicate": "P",
                    "object": "b",
                    "polarity": "positive",
                    "agent": "x",
                    "allow_writes": True,
                }
            )
        with pytest.raises(ValidationError):
            tools["gestaltdb_claim_correct"].invoke(
                {"claim_id": "claim:x", "supersedes_version_id": "v"}
            )
        with pytest.raises(ValidationError):
            tools["gestaltdb_claim_retract"].invoke({"claim_id": "claim:x"})


def test_correction_preserves_history_and_identity(tmp_path: Path) -> None:
    with GestaltDBGraph.create(tmp_path / "g", backend="lmdb") as graph:
        tools = _write_tools(graph)
        asserted = _assert(tools)
        corrected = json.loads(
            tools["gestaltdb_claim_correct"].invoke(
                {
                    "claim_id": asserted["logical_id"],
                    "supersedes_version_id": asserted["version_id"],
                    "confidence": 0.9,
                    "provenance": {"reviewed": True},
                }
            )
        )
        assert corrected["operation"] == "correct"
        assert corrected["logical_id"] == asserted["logical_id"]
        assert corrected["supersedes_version_id"] == asserted["version_id"]
        assert corrected["claim"]["statement_id"] == asserted["claim"]["statement_id"]
        assert corrected["claim"]["claim_id"] == asserted["claim"]["claim_id"]
        assert corrected["claim"]["confidence"] == 0.9
        assert corrected["claim"]["provenance"] == {"reviewed": True}

        search = {tool.name: tool for tool in GestaltDBClaimToolkit(graph=graph).get_tools()}[
            "gestaltdb_claim_search"
        ]
        historical = json.loads(
            search.invoke(
                {
                    "statement_id": asserted["claim"]["statement_id"],
                    "valid_time": VALID_TIME,
                    "through_commit": asserted["commit_id"],
                }
            )
        )
        assert historical["claims"][0]["operation"] == "assert"
        assert historical["claims"][0]["claim"]["confidence"] == 0.4
        latest = json.loads(
            search.invoke(
                {
                    "statement_id": asserted["claim"]["statement_id"],
                    "valid_time": VALID_TIME,
                }
            )
        )
        assert latest["claims"][0]["operation"] == "correct"


def test_correction_rejects_mismatched_identity(tmp_path: Path) -> None:
    with GestaltDBGraph.create(tmp_path / "g", backend="lmdb") as graph:
        tools = _write_tools(graph)
        asserted = _assert(tools)
        other = _assert(tools, subject="bob", object="globex")
        output = tools["gestaltdb_claim_correct"].invoke(
            {
                "claim_id": other["logical_id"],
                "supersedes_version_id": asserted["version_id"],
                "confidence": 0.5,
            }
        )
        assert "claim write failed" in output
        search = {tool.name: tool for tool in GestaltDBClaimToolkit(graph=graph).get_tools()}[
            "gestaltdb_claim_search"
        ]
        latest = json.loads(
            search.invoke(
                {"statement_id": asserted["claim"]["statement_id"], "valid_time": VALID_TIME}
            )
        )
        assert [item["operation"] for item in latest["claims"]] == ["assert"]


def test_retraction_withdraws_but_preserves_history(tmp_path: Path) -> None:
    with GestaltDBGraph.create(tmp_path / "g", backend="lmdb") as graph:
        tools = _write_tools(graph)
        asserted = _assert(tools)
        retracted = json.loads(
            tools["gestaltdb_claim_retract"].invoke(
                {
                    "claim_id": asserted["logical_id"],
                    "supersedes_version_id": asserted["version_id"],
                    "reason": "employment ended",
                    "valid_from": "2024-05-01T00:00:00Z",
                }
            )
        )
        assert retracted["operation"] == "retract"
        assert retracted["claim"] is None
        assert retracted["supersedes_version_id"] == asserted["version_id"]
        assert retracted["reason"] == "employment ended"
        assert retracted["logical_id"] == asserted["logical_id"]

        status = {tool.name: tool for tool in GestaltDBClaimToolkit(graph=graph).get_tools()}[
            "gestaltdb_claim_status"
        ]
        args = {
            "subject": "alice",
            "predicate": "WORKS_FOR",
            "object": "acme",
        }
        assert (
            json.loads(status.invoke({**args, "valid_time": "2024-03-01T00:00:00Z"}))["status"]
            == "supported"
        )
        assert json.loads(status.invoke({**args, "valid_time": VALID_TIME}))["status"] == "unknown"


def test_validity_changes_and_output_bounds(tmp_path: Path) -> None:
    with GestaltDBGraph.create(tmp_path / "g", backend="lmdb") as graph:
        tools = _write_tools(graph)
        asserted = _assert(tools)
        corrected = json.loads(
            tools["gestaltdb_claim_correct"].invoke(
                {
                    "claim_id": asserted["logical_id"],
                    "supersedes_version_id": asserted["version_id"],
                    "valid_from": "2024-01-01T00:00:00Z",
                    "valid_to": "2024-12-31T00:00:00Z",
                }
            )
        )
        assert corrected["valid"]["end"] is not None
        retracted = json.loads(
            tools["gestaltdb_claim_retract"].invoke(
                {
                    "claim_id": asserted["logical_id"],
                    "valid_from": "2024-05-01T00:00:00Z",
                    "valid_to": "2024-06-01T00:00:00Z",
                }
            )
        )
        assert retracted["operation"] == "retract"
        with pytest.raises(ValidationError):
            tools["gestaltdb_claim_correct"].invoke(
                {
                    "claim_id": "claim:x",
                    "supersedes_version_id": "v",
                    "valid_to": "2024-12-31T00:00:00Z",
                }
            )
        with pytest.raises(ValidationError):
            tools["gestaltdb_claim_retract"].invoke(
                {"claim_id": "claim:x", "valid_to": "2024-12-31T00:00:00Z"}
            )

        limited = {
            tool.name: tool
            for tool in GestaltDBClaimToolkit(
                graph=graph, allow_writes=True, max_chars=256
            ).get_tools()
        }
        output = limited["gestaltdb_claim_assert"].invoke(
            {
                "subject": "a",
                "predicate": "P",
                "object": "b",
                "polarity": "positive",
                "agent": "x",
                "valid_from": "2024-01-01T00:00:00Z",
            }
        )
        assert "output limit" in output


def test_write_failures_are_redacted(tmp_path: Path) -> None:
    with GestaltDBGraph.create(tmp_path / "g", backend="lmdb") as graph:
        tools = _write_tools(graph)
        with patch.object(
            graph.graph, "assert_claim", side_effect=RuntimeError("/private/path secret-token")
        ):
            output = tools["gestaltdb_claim_assert"].invoke(
                {
                    "subject": "a",
                    "predicate": "P",
                    "object": "b",
                    "polarity": "positive",
                    "agent": "x",
                    "valid_from": "2024-01-01T00:00:00Z",
                }
            )
        assert "claim write failed" in output
        assert "secret" not in output
        with patch.object(graph.graph, "retract_claim", side_effect=RuntimeError("/private/path")):
            retracted = tools["gestaltdb_claim_retract"].invoke(
                {"claim_id": "claim:x", "supersedes_version_id": "v"}
            )
        assert "claim write failed" in retracted


def test_executable_offline_write_example() -> None:
    example = Path(__file__).resolve().parents[2] / "examples" / "claim_writes.py"
    result = subprocess.run(
        [sys.executable, str(example)], check=True, capture_output=True, text=True
    )
    answer = json.loads(result.stdout)
    assert answer["validity_differs_from_knowledge_time"] is True
    assert answer["correction_supersedes_assert"] is True
    assert answer["identity_preserved"] is True
    assert answer["confidence_known_at_assert"] == [0.4]
    assert answer["confidence_known_at_correction"] == [0.9]
    assert answer["confidence_after_retraction"] == []
    assert answer["read_only_tool_count"] == 3
