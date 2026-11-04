import json
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from langchain_gestaltdb import (
    GestaltDBEntailsTool,
    GestaltDBGraph,
    GestaltDBModalToolkit,
    GestaltDBWorldAccessibilityTool,
)
from langchain_gestaltdb._compat import call_upstream

VALID_TIME = "2024-06-01T00:00:00Z"
ATOM_POS = {"subject": "s-pos", "predicate": "WORKS_FOR", "object": "acme"}
ATOM_NEG = {"subject": "s-neg", "predicate": "WORKS_FOR", "object": "acme"}
ATOM_BOTH = {"subject": "s-both", "predicate": "WORKS_FOR", "object": "acme"}


@pytest.fixture
def modal_graph(tmp_path: Path) -> Iterator[GestaltDBGraph]:
    with GestaltDBGraph.create(tmp_path / "g", backend="lmdb") as graph:
        db = graph.graph
        call_upstream(
            db,
            "assert_claim",
            subject="s-pos",
            predicate="WORKS_FOR",
            object="acme",
            polarity="positive",
            agent="hr",
            world="wpos",
            valid_from="2024-01-01T00:00:00Z",
        )
        call_upstream(
            db,
            "assert_claim",
            subject="s-neg",
            predicate="WORKS_FOR",
            object="acme",
            polarity="negative",
            agent="audit",
            world="wneg",
            valid_from="2024-01-01T00:00:00Z",
        )
        for polarity, agent in (("positive", "hr"), ("negative", "audit")):
            call_upstream(
                db,
                "assert_claim",
                subject="s-both",
                predicate="WORKS_FOR",
                object="acme",
                polarity=polarity,
                agent=agent,
                world="wboth",
                valid_from="2024-01-01T00:00:00Z",
            )
        links = (
            ("qpos", "wpos", "belief"),
            ("qneg", "wneg", "belief"),
            ("qboth", "wboth", "belief"),
            ("qempty", "wempty", "belief"),
            ("qk", "wpos", "knowledge"),
            ("qm", "wpos", "modal"),
        )
        for from_world, to_world, kind in links:
            call_upstream(
                db,
                "assert_world_accessibility",
                agent="hr",
                from_world=from_world,
                to_world=to_world,
                kind=kind,
                valid_from="2024-01-01T00:00:00Z",
            )
        yield graph


def _entails(graph: GestaltDBGraph, **overrides) -> dict:
    args = {
        "agent": "hr",
        "proposition": ATOM_POS,
        "mode": "BELIEVES",
        "world": "qpos",
        "valid_time": VALID_TIME,
    }
    args.update(overrides)
    return json.loads(GestaltDBEntailsTool(graph=graph).invoke(args))


def test_entails_reports_all_four_statuses(modal_graph: GestaltDBGraph) -> None:
    assert _entails(modal_graph)["status"] == "supported"
    assert _entails(modal_graph, proposition=ATOM_NEG, world="qneg")["status"] == "refuted"
    assert _entails(modal_graph, proposition=ATOM_BOTH, world="qboth")["status"] == "both"
    assert _entails(modal_graph, world="qempty")["status"] == "unknown"
    assert _entails(modal_graph, world="qnowhere")["status"] == "unknown"

    supported = _entails(modal_graph)
    assert supported["agent"] == "hr"
    assert supported["mode"] == "BELIEVES"
    assert supported["world"] == "qpos"
    assert supported["max_depth"] == 4
    assert supported["max_states"] == 1000
    assert supported["truncated"] is False
    assert "confidence" in supported
    assert supported["explanation"]["status"] == "supported"
    json.dumps(supported, allow_nan=False)


def test_entails_modes_use_independent_frames(modal_graph: GestaltDBGraph) -> None:
    # qk links to wpos only in the knowledge frame: KNOWS sees it, BELIEVES does not.
    assert _entails(modal_graph, world="qk")["status"] == "unknown"
    assert _entails(modal_graph, mode="KNOWS", world="qk")["status"] == "supported"
    # qneg links to wneg only in the belief frame: BELIEVES sees it, KNOWS does not.
    assert _entails(modal_graph, proposition=ATOM_NEG, world="qneg")["status"] == "refuted"
    assert (
        _entails(modal_graph, proposition=ATOM_NEG, mode="KNOWS", world="qneg")["status"]
        == "unknown"
    )
    # POSSIBLE/NECESSARY use the modal frame: visible from qm, unknown from qpos.
    assert _entails(modal_graph, mode="POSSIBLE", world="qm")["status"] == "supported"
    assert _entails(modal_graph, mode="NECESSARY", world="qm")["status"] == "supported"
    assert _entails(modal_graph, mode="POSSIBLE", world="qpos")["status"] == "unknown"
    assert _entails(modal_graph, mode="NECESSARY", world="qpos")["status"] == "unknown"


def test_entails_boolean_and_nested_formulas(modal_graph: GestaltDBGraph) -> None:
    negated = _entails(modal_graph, proposition={"operator": "NOT", "formula": ATOM_POS})
    assert negated["status"] == "refuted"
    nested = _entails(
        modal_graph,
        proposition={"operator": "BELIEVES", "agent": "hr", "formula": ATOM_POS},
    )
    # The inner BELIEVES is evaluated at wpos, which has no outgoing links.
    assert nested["status"] == "unknown"


def test_entails_bounds_are_application_controlled(modal_graph: GestaltDBGraph) -> None:
    tool = GestaltDBEntailsTool(graph=modal_graph)
    assert "graph" not in tool.model_dump()
    base = {
        "agent": "hr",
        "proposition": ATOM_POS,
        "mode": "BELIEVES",
        "world": "qpos",
        "valid_time": VALID_TIME,
    }
    with pytest.raises(ValidationError):
        tool.invoke({**base, "max_depth": 1})
    with pytest.raises(ValidationError):
        tool.invoke({**base, "max_states": 1})
    with pytest.raises(ValidationError):
        tool.invoke({**base, "mode": 123})
    with pytest.raises(ValidationError):
        GestaltDBEntailsTool(graph=modal_graph, max_depth=0)
    with pytest.raises(ValidationError):
        GestaltDBEntailsTool(graph=modal_graph, max_states=0)

    exhausted = GestaltDBEntailsTool(graph=modal_graph, max_states=1)
    assert "modal entailment failed" in exhausted.invoke(base)


def test_entails_matches_direct_api_and_kg_entails(modal_graph: GestaltDBGraph) -> None:
    tool = GestaltDBEntailsTool(graph=modal_graph)
    direct = call_upstream(
        modal_graph.graph,
        "entails",
        "hr",
        ATOM_POS,
        "BELIEVES",
        world="qpos",
        valid_time=VALID_TIME,
    )
    via_tool = json.loads(
        tool.invoke(
            {
                "agent": "hr",
                "proposition": ATOM_POS,
                "mode": "BELIEVES",
                "world": "qpos",
                "valid_time": VALID_TIME,
            }
        )
    )
    assert via_tool["status"] == direct.status.value
    assert via_tool["confidence"] == direct.confidence
    rows = modal_graph.query(
        "CALL kg.entails('hr', {subject: 's-pos', predicate: 'WORKS_FOR', object: 'acme'}, "
        "'BELIEVES', {world: 'qpos', validTime: '2024-06-01T00:00:00Z'}) "
        "YIELD status, confidence, explanation RETURN status, confidence"
    )
    assert rows[0]["status"] == via_tool["status"]
    assert rows[0]["confidence"] == via_tool["confidence"]


def test_entails_honors_system_time_horizons(modal_graph: GestaltDBGraph) -> None:
    # Commits 1-4 hold the claims; the accessibility links land later, so an
    # early commit horizon sees the evidence but not the path to it.
    assert _entails(modal_graph, through_commit=4)["status"] == "unknown"
    assert _entails(modal_graph)["status"] == "supported"


def test_accessibility_write_requires_opt_in(modal_graph: GestaltDBGraph) -> None:
    read_only = GestaltDBModalToolkit(graph=modal_graph).get_tools()
    assert [tool.name for tool in read_only] == ["gestaltdb_entails"]
    tools = {
        tool.name: tool
        for tool in GestaltDBModalToolkit(graph=modal_graph, allow_writes=True).get_tools()
    }
    assert [tool.name for tool in tools.values()] == [
        "gestaltdb_entails",
        "gestaltdb_world_accessibility_assert",
    ]
    write = tools["gestaltdb_world_accessibility_assert"]
    assert "graph" not in write.model_dump()
    with pytest.raises(ValidationError):
        write.invoke(
            {
                "agent": "hr",
                "from_world": "w0",
                "to_world": "wpos",
                "kind": "belief",
                "valid_from": VALID_TIME,
                "allow_writes": True,
            }
        )
    with pytest.raises(ValidationError):
        write.invoke(
            {
                "agent": "hr",
                "from_world": "w0",
                "to_world": "wpos",
                "kind": "sesquipedalian",
                "valid_from": VALID_TIME,
            }
        )

    assert _entails(modal_graph, world="w0")["status"] == "unknown"
    receipt = json.loads(
        write.invoke(
            {
                "agent": "hr",
                "from_world": "w0",
                "to_world": "wpos",
                "kind": "belief",
                "valid_from": "2024-01-01T00:00:00Z",
                "provenance": {"ticket": "G17-10"},
            }
        )
    )
    assert receipt["written"] is True
    assert receipt["claim"]["subject"] == "w0"
    assert receipt["claim"]["world"] == "w0"
    assert _entails(modal_graph, world="w0")["status"] == "supported"


def test_modal_errors_are_redacted(modal_graph: GestaltDBGraph) -> None:
    tool = GestaltDBEntailsTool(graph=modal_graph)
    invalid = tool.invoke(
        {
            "agent": "hr",
            "proposition": {"operator": "ATOM"},
            "mode": "BELIEVES",
            "world": "qpos",
            "valid_time": VALID_TIME,
        }
    )
    assert "modal entailment failed" in invalid
    with patch.object(
        modal_graph.graph.epistemic, "entails", side_effect=RuntimeError("/secret/path")
    ):
        output = tool.invoke(
            {
                "agent": "hr",
                "proposition": ATOM_POS,
                "mode": "BELIEVES",
                "world": "qpos",
                "valid_time": VALID_TIME,
            }
        )
    assert "modal entailment failed" in output
    assert "secret" not in output

    write = GestaltDBWorldAccessibilityTool(graph=modal_graph)
    with patch.object(
        modal_graph.graph.epistemic,
        "assert_world_accessibility",
        side_effect=RuntimeError("secret"),
    ):
        failed = write.invoke(
            {
                "agent": "hr",
                "from_world": "w0",
                "to_world": "wpos",
                "kind": "belief",
                "valid_from": VALID_TIME,
            }
        )
    assert "claim write failed" in failed
    assert "secret" not in failed


def test_toolkit_bounds_cannot_change_from_agent_input(modal_graph: GestaltDBGraph) -> None:
    entails, accessibility = GestaltDBModalToolkit(graph=modal_graph, allow_writes=True).get_tools()
    assert set(entails.get_input_schema().model_fields) == {
        "agent",
        "proposition",
        "mode",
        "world",
        "valid_time",
        "system_time",
        "through_commit",
    }
    assert set(accessibility.get_input_schema().model_fields) == {
        "agent",
        "from_world",
        "to_world",
        "kind",
        "valid_from",
        "valid_to",
        "source",
        "provenance",
    }
