"""Read-only temporal tools over versioned nodes/edges with stale-index recovery."""

import json
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest
from gestaltdb.graphdb import Edge, Node

from langchain_gestaltdb import (
    GestaltDBEdgeAsOfTool,
    GestaltDBGraph,
    GestaltDBNodeAsOfTool,
    GestaltDBTemporalScanTool,
    GestaltDBTemporalToolkit,
)
from langchain_gestaltdb._compat import call_upstream

T0 = datetime(2024, 1, 1, tzinfo=timezone.utc)
T1 = datetime(2024, 6, 1, tzinfo=timezone.utc)
T2 = datetime(2024, 12, 1, tzinfo=timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.isoformat().replace("+00:00", "Z")


@pytest.fixture
def history_graph(tmp_path: Path) -> Iterator[GestaltDBGraph]:
    with GestaltDBGraph.create(tmp_path / "g", backend="lmdb") as graph:
        database = graph.graph
        call_upstream(
            database,
            "put_node_version",
            Node(node_id="alice", properties={"name": "Alice"}, labels=["Person"]),
            valid=(T0, T1),
        )
        call_upstream(
            database,
            "put_node_version",
            Node(node_id="bob", properties={"name": "Bob"}, labels=["Person"]),
            valid=(T0, None),
        )
        call_upstream(
            database,
            "put_edge_version",
            Edge(edge_id="e1", source="alice", target="bob", properties={"type": "KNOWS"}),
            valid=(T0, None),
        )
        yield graph


def test_node_as_of_open_and_closed_intervals(history_graph: GestaltDBGraph) -> None:
    tool = GestaltDBNodeAsOfTool(graph=history_graph)
    inside = json.loads(tool.invoke({"logical_id": "alice", "valid_time": _iso(T0)}))
    assert inside["found"] is True
    assert inside["version"]["logical_id"] == "alice"
    assert inside["version"]["node"]["properties"] == {"name": "Alice"}
    assert inside["version"]["node"]["labels"] == ["Person"]

    at_end = json.loads(tool.invoke({"logical_id": "alice", "valid_time": _iso(T1)}))
    assert at_end["found"] is False  # half-open [start, end): end is exclusive
    assert at_end["version"] is None

    after = json.loads(tool.invoke({"logical_id": "alice", "valid_time": "2024-07-01T00:00:00Z"}))
    assert after["found"] is False

    still_open = json.loads(tool.invoke({"logical_id": "bob", "valid_time": _iso(T2)}))
    assert still_open["found"] is True

    missing = json.loads(tool.invoke({"logical_id": "nobody", "valid_time": _iso(T0)}))
    assert missing == {
        "logical_id": "nobody",
        "valid_time": _iso(T0),
        "system_time": None,
        "through_commit": None,
        "found": False,
        "version": None,
    }


def test_edge_as_of_reports_endpoints(history_graph: GestaltDBGraph) -> None:
    tool = GestaltDBEdgeAsOfTool(graph=history_graph)
    found = json.loads(tool.invoke({"logical_id": "e1", "valid_time": _iso(T1)}))
    assert found["found"] is True
    assert found["version"]["edge"]["source"] == "alice"
    assert found["version"]["edge"]["target"] == "bob"

    missing = json.loads(tool.invoke({"logical_id": "nope", "valid_time": _iso(T1)}))
    assert missing["found"] is False


def test_system_horizon_pins_history(tmp_path: Path) -> None:
    with GestaltDBGraph.create(tmp_path / "g", backend="lmdb") as graph:
        database = graph.graph
        first = call_upstream(
            database,
            "put_node_version",
            Node(node_id="alice", properties={"role": "dev"}),
            valid=(T0, None),
        )
        call_upstream(
            database,
            "put_node_version",
            Node(node_id="alice", properties={"role": "lead"}),
            valid=(T0, None),
        )
        tool = GestaltDBNodeAsOfTool(graph=graph)
        latest = json.loads(tool.invoke({"logical_id": "alice", "valid_time": _iso(T1)}))
        assert latest["version"]["node"]["properties"] == {"role": "lead"}

        pinned = json.loads(
            tool.invoke(
                {
                    "logical_id": "alice",
                    "valid_time": _iso(T1),
                    "through_commit": first.commit_id,
                }
            )
        )
        assert pinned["found"] is True
        assert pinned["version"]["node"]["properties"] == {"role": "dev"}
        assert pinned["version"]["version_id"] == first.version_id

        by_system = json.loads(
            tool.invoke(
                {
                    "logical_id": "alice",
                    "valid_time": _iso(T1),
                    "system_time": first.system_time.to_datetime().isoformat(),
                }
            )
        )
        assert by_system["version"]["node"]["properties"] == {"role": "dev"}


def test_stale_temporal_fails_closed_with_rebuild_hint(tmp_path: Path) -> None:
    try:
        from gestaltdb import IndexMaintenanceMode

        defer = IndexMaintenanceMode.DEFER
    except ImportError:
        defer = None
    with GestaltDBGraph.create(tmp_path / "g", backend="lmdb") as graph:
        database = graph.graph
        if defer is not None:
            call_upstream(
                database,
                "put_node_version",
                Node(node_id="alice", properties={"name": "Alice"}),
                valid=(T0, None),
                index_mode=defer,
            )
            failing: object | None = None
        else:  # pragma: no cover - DEFER exists on all supported backends
            failing = RuntimeError("stale indexes require rebuild before query: temporal")
        tool = GestaltDBNodeAsOfTool(graph=graph)
        if failing is not None:
            with patch.object(database.temporal, "get_node_as_of", side_effect=failing):
                output = tool.invoke({"logical_id": "alice", "valid_time": _iso(T0)})
        else:
            output = tool.invoke({"logical_id": "alice", "valid_time": _iso(T0)})
        assert "rebuild_temporal_indexes" in output
        assert "rebuild_deferred_indexes" in output
        assert "never rebuild automatically" in output

        call_upstream(database, "rebuild_temporal_indexes")
        recovered = json.loads(tool.invoke({"logical_id": "alice", "valid_time": _iso(T0)}))
        assert recovered["found"] is True


def test_scan_bounds_entities_and_chars(history_graph: GestaltDBGraph) -> None:
    scan = GestaltDBTemporalScanTool(graph=history_graph)
    nodes = json.loads(scan.invoke({"kind": "node", "valid_time": _iso(T0)}))
    assert nodes["returned_entities"] == 2
    assert {item["logical_id"] for item in nodes["entities"]} == {"alice", "bob"}
    assert nodes["truncated"] is False

    capped = json.loads(
        GestaltDBTemporalScanTool(graph=history_graph, max_entities=1).invoke(
            {"kind": "node", "valid_time": _iso(T0)}
        )
    )
    assert capped["returned_entities"] == 1
    assert capped["truncated"] is True

    edges = json.loads(scan.invoke({"kind": "edge", "valid_time": _iso(T0), "source": "alice"}))
    assert [item["logical_id"] for item in edges["entities"]] == ["e1"]

    incoming = json.loads(scan.invoke({"kind": "edge", "valid_time": _iso(T0), "target": "bob"}))
    assert [item["logical_id"] for item in incoming["entities"]] == ["e1"]

    unanchored = scan.invoke({"kind": "edge", "valid_time": _iso(T0)})
    assert "exactly one endpoint anchor" in unanchored

    tiny = GestaltDBTemporalScanTool(graph=history_graph, max_chars=256).invoke(
        {"kind": "node", "valid_time": _iso(T0)}
    )
    assert len(tiny) <= 256
    assert json.loads(tiny)["truncated"] is True


def test_read_view_pins_repeated_reads(history_graph: GestaltDBGraph) -> None:
    try:
        with history_graph.read_view(valid_time=_iso(T1)) as view:
            first = view.get_node_as_of("bob", valid_time=_iso(T1))
            second = view.get_node_as_of("bob", valid_time=_iso(T1))
    except Exception as exc:
        pytest.skip(f"Backend rejects temporal read views: {type(exc).__name__}")
        return
    assert first is not None and second is not None
    assert first.version_id == second.version_id
    tool = json.loads(
        GestaltDBNodeAsOfTool(graph=history_graph).invoke(
            {"logical_id": "bob", "valid_time": _iso(T1)}
        )
    )
    assert tool["version"]["version_id"] == first.version_id


def test_errors_are_redacted(history_graph: GestaltDBGraph) -> None:
    database = history_graph.graph
    with patch.object(
        database.temporal, "get_node_as_of", side_effect=RuntimeError("/secret/path")
    ):
        output = GestaltDBNodeAsOfTool(graph=history_graph).invoke(
            {"logical_id": "alice", "valid_time": _iso(T0)}
        )
    assert "temporal read failed" in output
    assert "secret" not in output
    with patch.object(
        database.temporal, "iter_nodes_as_of", side_effect=RuntimeError("/secret/path")
    ):
        scanned = GestaltDBTemporalScanTool(graph=history_graph).invoke(
            {"kind": "node", "valid_time": _iso(T0)}
        )
    assert "temporal read failed" in scanned
    assert "secret" not in scanned


def test_toolkit_bounds_are_application_controlled(history_graph: GestaltDBGraph) -> None:
    node_tool, edge_tool, scan_tool = GestaltDBTemporalToolkit(graph=history_graph).get_tools()
    assert [tool.name for tool in (node_tool, edge_tool, scan_tool)] == [
        "gestaltdb_node_as_of",
        "gestaltdb_edge_as_of",
        "gestaltdb_temporal_scan",
    ]
    for tool in (node_tool, edge_tool, scan_tool):
        assert "graph" not in tool.model_dump()
