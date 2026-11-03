import json
from pathlib import Path
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from langchain_gestaltdb import (
    GestaltDBClaimExplanationTool,
    GestaltDBGraph,
    GestaltDBRuleGetTool,
    GestaltDBRuleListTool,
    GestaltDBRuleRunTool,
    GestaltDBRuleToolkit,
)

VALID_TIME = "2024-06-01T00:00:00Z"
WHEN = [
    {"subject": "?p", "predicate": "WORKS_FOR", "object": "?c"},
    {"subject": "?c", "predicate": "MEMBER_OF", "object": "?g"},
]
THEN = {"subject": "?p", "predicate": "AFFILIATED_WITH", "object": "?g"}
THEN_ONE_HOP = {"subject": "?p", "predicate": "AFFILIATED_WITH", "object": "?c"}


def _seed(graph: GestaltDBGraph, world: str = "reported") -> None:
    database = graph.graph
    database.assert_claim(
        subject="alice",
        predicate="WORKS_FOR",
        object="acme",
        polarity="positive",
        agent="source:hr",
        world=world,
        valid_from="2024-01-01T00:00:00Z",
    )
    database.assert_claim(
        subject="acme",
        predicate="MEMBER_OF",
        object="industry",
        polarity="positive",
        agent="source:registry",
        world=world,
        valid_from="2024-03-01T00:00:00Z",
    )


def _write_tools(graph: GestaltDBGraph) -> dict:
    return {
        tool.name: tool for tool in GestaltDBRuleToolkit(graph=graph, allow_writes=True).get_tools()
    }


def test_rule_lifecycle_create_run_explain_maintain(tmp_path: Path) -> None:
    with GestaltDBGraph.create(tmp_path / "g", backend="lmdb") as graph:
        tools = _write_tools(graph)
        created = json.loads(
            tools["gestaltdb_rule_create"].invoke(
                {"rule_id": "employment-implies-affiliation", "when": WHEN, "then": THEN}
            )
        )
        assert created["written"] is True
        assert created["rule_id"] == "employment-implies-affiliation"
        assert created["when"] == WHEN
        assert created["then"] == THEN

        # A second version of the same rule is append-only history, not a mutation.
        assert created["version_id"] is not None

        _seed(graph)
        first = json.loads(tools["gestaltdb_rule_run"].invoke({"as_of": VALID_TIME}))
        assert first["derived_count"] >= 1
        assert first["commit_id"] is not None
        assert first["returned_versions"] == first["derived_count"]
        assert first["truncated"] is False
        derived = first["versions"][0]
        assert derived["claim"]["agent"] == "gestaltdb:rules"
        assert derived["claim"]["world"] == "reported"

        # An unchanged repeat run is idempotent: nothing new derived, no new commit.
        rerun = json.loads(tools["gestaltdb_rule_run"].invoke({"as_of": VALID_TIME}))
        assert rerun["derived_count"] == 0
        assert rerun["commit_id"] is None
        assert rerun["returned_versions"] == 0

        explanation = json.loads(
            GestaltDBClaimExplanationTool(graph=graph).invoke(
                {"claim_id": derived["logical_id"], "valid_time": VALID_TIME}
            )
        )
        assert explanation["found"] is True
        assert explanation["root_version_id"] == derived["version_id"]
        assert {edge["relationship"] for edge in explanation["edges"]} == {
            "derived_by",
            "premise",
        }

        maintained = json.loads(tools["gestaltdb_rule_maintain"].invoke({"as_of": VALID_TIME}))
        assert maintained["support_count"] >= 1
        assert maintained["retracted_count"] == 0


def test_maintain_retracts_only_after_last_support(tmp_path: Path) -> None:
    with GestaltDBGraph.create(tmp_path / "g", backend="lmdb") as graph:
        tools = _write_tools(graph)
        tools["gestaltdb_rule_create"].invoke(
            {
                "rule_id": "affiliation",
                "when": [{"subject": "?p", "predicate": "WORKS_FOR", "object": "?c"}],
                "then": {"subject": "?p", "predicate": "AFFILIATED_WITH", "object": "?c"},
            }
        )
        first = graph.graph.assert_claim(
            subject="alice",
            predicate="WORKS_FOR",
            object="acme",
            polarity="positive",
            agent="source:hr",
            world="reported",
            valid_from="2024-01-01T00:00:00Z",
        )
        second = graph.graph.assert_claim(
            subject="alice",
            predicate="WORKS_FOR",
            object="acme",
            polarity="positive",
            agent="source:audit",
            world="reported",
            valid_from="2024-01-01T00:00:00Z",
        )
        run = json.loads(tools["gestaltdb_rule_run"].invoke({"as_of": VALID_TIME}))
        assert run["derived_count"] == 1
        assert run["justification_count"] == 2
        logical_id = run["versions"][0]["logical_id"]

        graph.graph.retract_claim(
            first.logical_id,
            supersedes_version_id=first.version_id,
            valid_from="2024-01-01T00:00:00Z",
        )
        kept = json.loads(tools["gestaltdb_rule_maintain"].invoke({"as_of": VALID_TIME}))
        assert kept["retracted_count"] == 0
        assert kept["support_count"] == 1
        assert graph.graph.get_claim_as_of(logical_id, valid_time=VALID_TIME) is not None

        graph.graph.retract_claim(
            second.logical_id,
            supersedes_version_id=second.version_id,
            valid_from="2024-01-01T00:00:00Z",
        )
        withdrawn = json.loads(tools["gestaltdb_rule_maintain"].invoke({"as_of": VALID_TIME}))
        assert withdrawn["retracted_count"] == 1
        assert withdrawn["support_count"] == 0
        assert graph.graph.get_claim_as_of(logical_id, valid_time=VALID_TIME) is None


def test_bound_exhaustion_persists_no_partial_conclusions(tmp_path: Path) -> None:
    with GestaltDBGraph.create(tmp_path / "g", backend="lmdb") as graph:
        tools = {
            tool.name: tool
            for tool in GestaltDBRuleToolkit(
                graph=graph, allow_writes=True, max_iterations=1
            ).get_tools()
        }
        tools["gestaltdb_rule_create"].invoke(
            {"rule_id": "employment-implies-affiliation", "when": WHEN, "then": THEN}
        )
        _seed(graph)
        before = len(list(graph.graph.iter_claims_as_of(valid_time=VALID_TIME)))
        output = tools["gestaltdb_rule_run"].invoke({"as_of": VALID_TIME})
        assert "rule operation failed" in output
        assert len(list(graph.graph.iter_claims_as_of(valid_time=VALID_TIME))) == before

        # The failed run left nothing behind: a full-budget rerun derives cleanly.
        full = {
            tool.name: tool
            for tool in GestaltDBRuleToolkit(graph=graph, allow_writes=True).get_tools()
        }
        recovered = json.loads(full["gestaltdb_rule_run"].invoke({"as_of": VALID_TIME}))
        assert recovered["derived_count"] >= 1
        assert recovered["commit_id"] is not None


def test_rules_join_within_one_world(tmp_path: Path) -> None:
    with GestaltDBGraph.create(tmp_path / "g", backend="lmdb") as graph:
        tools = _write_tools(graph)
        tools["gestaltdb_rule_create"].invoke(
            {
                "rule_id": "affiliation",
                "when": [{"subject": "?p", "predicate": "WORKS_FOR", "object": "?c"}],
                "then": {"subject": "?p", "predicate": "AFFILIATED_WITH", "object": "?c"},
            }
        )
        _seed(graph, world="reported")
        isolated = json.loads(
            tools["gestaltdb_rule_run"].invoke({"as_of": VALID_TIME, "world": "missing"})
        )
        assert isolated["derived_count"] == 0
        assert isolated["commit_id"] is None
        scoped = json.loads(
            tools["gestaltdb_rule_run"].invoke({"as_of": VALID_TIME, "world": "reported"})
        )
        assert scoped["derived_count"] == 1
        assert scoped["versions"][0]["claim"]["world"] == "reported"


def test_unsafe_rule_never_reaches_backend(tmp_path: Path) -> None:
    with GestaltDBGraph.create(tmp_path / "g", backend="lmdb") as graph:
        tools = _write_tools(graph)
        with patch.object(graph.graph, "create_rule") as create:
            output = tools["gestaltdb_rule_create"].invoke(
                {
                    "rule_id": "unsafe",
                    "when": [{"subject": "?p", "predicate": "WORKS_FOR", "object": "?c"}],
                    "then": {"subject": "?x", "predicate": "AFFILIATED_WITH", "object": "?c"},
                }
            )
            create.assert_not_called()
        assert "rule operation failed" in output
        assert json.loads(GestaltDBRuleGetTool(graph=graph).invoke({"rule_id": "unsafe"})) == {
            "found": False,
            "rule_id": "unsafe",
            "system_time": None,
            "truncated": False,
            "rule": None,
        }


def test_nonconstant_predicates_are_rejected(tmp_path: Path) -> None:
    with GestaltDBGraph.create(tmp_path / "g", backend="lmdb") as graph:
        tools = _write_tools(graph)
        with patch.object(graph.graph, "create_rule") as create:
            output = tools["gestaltdb_rule_create"].invoke(
                {
                    "rule_id": "meta",
                    "when": [{"subject": "?p", "predicate": "?rel", "object": "?c"}],
                    "then": {"subject": "?p", "predicate": "AFFILIATED_WITH", "object": "?c"},
                }
            )
            create.assert_not_called()
        assert "rule operation failed" in output


def test_read_tools_get_list_and_bounds(tmp_path: Path) -> None:
    with GestaltDBGraph.create(tmp_path / "g", backend="lmdb") as graph:
        tools = _write_tools(graph)
        created = json.loads(
            tools["gestaltdb_rule_create"].invoke(
                {"rule_id": "affiliation", "when": WHEN[:1], "then": THEN_ONE_HOP}
            )
        )
        fetched = json.loads(GestaltDBRuleGetTool(graph=graph).invoke({"rule_id": "affiliation"}))
        assert fetched["found"] is True
        assert fetched["rule"]["rule_id"] == "affiliation"
        assert fetched["rule"]["version_id"] == created["version_id"]
        assert fetched["rule"]["then"] == THEN_ONE_HOP

        listed = json.loads(GestaltDBRuleListTool(graph=graph).invoke({}))
        assert listed["returned_rules"] == 1
        assert listed["rules"][0]["rule_id"] == "affiliation"
        assert listed["truncated"] is False

        capped = json.loads(GestaltDBRuleListTool(graph=graph, max_rules=1).invoke({}))
        assert capped["returned_rules"] == 1

        tiny = GestaltDBRuleListTool(graph=graph, max_rules=1)
        assert tiny.max_rules == 1
        missing = json.loads(GestaltDBRuleGetTool(graph=graph).invoke({"rule_id": "no-such-rule"}))
        assert missing["found"] is False
        assert missing["rule"] is None


def test_toolkit_opt_in_and_bounds_are_app_controlled(tmp_path: Path) -> None:
    with GestaltDBGraph.create(tmp_path / "g", backend="lmdb") as graph:
        read_only = GestaltDBRuleToolkit(graph=graph).get_tools()
        assert [tool.name for tool in read_only] == [
            "gestaltdb_rule_get",
            "gestaltdb_rule_list",
        ]
        full = GestaltDBRuleToolkit(graph=graph, allow_writes=True).get_tools()
        assert [tool.name for tool in full] == [
            "gestaltdb_rule_get",
            "gestaltdb_rule_list",
            "gestaltdb_rule_create",
            "gestaltdb_rule_run",
            "gestaltdb_rule_maintain",
        ]
        for tool in full:
            assert "graph" not in tool.model_dump()
        run = {tool.name: tool for tool in full}["gestaltdb_rule_run"]
        with pytest.raises(ValidationError):
            run.invoke({"as_of": VALID_TIME, "max_iterations": 1})
        with pytest.raises(ValidationError):
            GestaltDBRuleToolkit(graph=graph, allow_writes="true")
        with pytest.raises(ValidationError):
            GestaltDBRuleRunTool(graph=graph).invoke({"system_time": None})


def test_errors_are_redacted(tmp_path: Path) -> None:
    with GestaltDBGraph.create(tmp_path / "g", backend="lmdb") as graph:
        tools = _write_tools(graph)
        with patch.object(graph.graph, "create_rule", side_effect=RuntimeError("/secret/path")):
            created = tools["gestaltdb_rule_create"].invoke(
                {"rule_id": "r", "when": WHEN[:1], "then": THEN_ONE_HOP}
            )
        assert "rule operation failed" in created
        assert "secret" not in created
        with patch.object(graph.graph, "get_rule", side_effect=RuntimeError("/secret/path")):
            fetched = GestaltDBRuleGetTool(graph=graph).invoke({"rule_id": "r"})
        assert "rule operation failed" in fetched
        assert "secret" not in fetched
        with patch.object(
            graph.graph, "iter_rule_versions", side_effect=RuntimeError("/secret/path")
        ):
            listed = GestaltDBRuleListTool(graph=graph).invoke({})
        assert "rule operation failed" in listed
        assert "secret" not in listed
        with patch.object(graph.graph, "run_rules", side_effect=RuntimeError("/secret/path")):
            ran = tools["gestaltdb_rule_run"].invoke({"as_of": VALID_TIME})
        assert "rule operation failed" in ran
        assert "secret" not in ran
        with patch.object(graph.graph, "maintain_truth", side_effect=RuntimeError("/secret/path")):
            maintained = tools["gestaltdb_rule_maintain"].invoke({"as_of": VALID_TIME})
        assert "rule operation failed" in maintained
        assert "secret" not in maintained


def test_character_caps_bound_records(tmp_path: Path) -> None:
    with GestaltDBGraph.create(tmp_path / "g", backend="lmdb") as graph:
        tools = _write_tools(graph)
        tools["gestaltdb_rule_create"].invoke(
            {"rule_id": "affiliation", "when": WHEN[:1], "then": THEN_ONE_HOP}
        )
        _seed(graph)
        run_capped = json.loads(
            GestaltDBRuleRunTool(graph=graph, max_chars=320).invoke({"as_of": VALID_TIME})
        )
        assert len(json.dumps(run_capped)) <= 320
        assert run_capped["truncated"] is True
        assert run_capped["versions"] == []
        assert run_capped["derived_count"] >= 1
        listed = json.loads(
            GestaltDBRuleListTool(graph=graph, max_chars=256).invoke({"rule_id": "affiliation"})
        )
        assert len(json.dumps(listed)) <= 256
        assert listed["truncated"] is True
        assert listed["rules"] == []
        get_capped = json.loads(
            GestaltDBRuleGetTool(graph=graph, max_chars=256).invoke({"rule_id": "affiliation"})
        )
        assert len(json.dumps(get_capped)) <= 256
