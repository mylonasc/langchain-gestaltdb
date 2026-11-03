"""G17-05: schema index metadata and the read-only index advisor."""

import json
import os
from tempfile import TemporaryDirectory

from gestaltdb import IndexMaintenanceMode
from gestaltdb.graphdb import Edge, Node

from langchain_gestaltdb import (
    GestaltDBGraph,
    GestaltDBIndexAdvisorTool,
    GestaltDBToolkit,
    advise_index,
)


def _backend() -> str:
    return os.environ.get("GESTALTDB_TEST_BACKEND", "pyrex")


def test_schema_metadata_reports_indexes_staleness_and_constraints() -> None:
    with TemporaryDirectory() as tmpdir:
        with GestaltDBGraph.create(f"{tmpdir}/g", backend=_backend()) as graph:
            graph.graph.put_node(Node("a", labels=["Person"], properties={"name": "Alice"}))
            graph.graph.put_edge(Edge("e", "a", "a", properties={"type": "KNOWS", "since": 2020}))
            graph.graph.create_node_property_index("name")
            graph.query("CREATE CONSTRAINT FOR (n:Person) REQUIRE n.email IS UNIQUE")
            schema = graph.get_structured_schema
            metadata = schema["metadata"]
            assert "name" in metadata["index_definitions"]["indexed_node_properties"]
            assert "name" in metadata["indexes"]["indexed_node_properties"]
            assert metadata["stale_indexes"] == []
            assert metadata["temporal_indexes_current"] is True
            assert {
                "name": "Person_email_unique",
                "type": "UNIQUE",
                "label": "Person",
                "property": "email",
            } in metadata["constraints"]
            assert "gestaltdb_index_advisor" in metadata["dialect"]


def test_schema_signals_deferred_staleness_without_hiding_entities() -> None:
    with TemporaryDirectory() as tmpdir:
        with GestaltDBGraph.create(f"{tmpdir}/g", backend=_backend()) as graph:
            graph.graph.create_node_property_index("name")
            node = Node("a", labels=["Deferred"], properties={"name": "Alice"})
            graph.graph.ingest_nodes_arrow(
                ["a"],
                [graph.graph.entity_serializer.serialize(node, "Node")],
                native=False,
                index_mode=IndexMaintenanceMode.DEFER,
            )
            metadata = graph.get_structured_schema["metadata"]
            assert "node_property" in metadata["stale_indexes"]
            assert metadata["temporal_indexes_current"] is True
            # Canonical reads bypass secondary indexes, so sampling still sees the label.
            assert "Deferred" in graph.get_structured_schema["node_props"]
            graph.graph.rebuild_deferred_indexes()
            graph.refresh_schema()
            assert graph.get_structured_schema["metadata"]["stale_indexes"] == []


def test_advisor_node_missing_exists_stale_transitions() -> None:
    with TemporaryDirectory() as tmpdir:
        with GestaltDBGraph.create(f"{tmpdir}/g", backend=_backend()) as graph:
            missing = advise_index(graph, node_label="Person", node_property="email")
            assert missing["index_exists"] is False
            assert missing["action"] == "create_index"
            assert missing["create_call"] == "graph.create_node_property_index('email')"
            assert missing["rebuild_call"] == "graph.rebuild_deferred_indexes()"

            graph.graph.create_node_property_index("email")
            current = advise_index(graph, node_label="Person", node_property="email")
            assert current["index_exists"] is True
            assert current["stale"] is False
            assert current["action"] == "current"

            node = Node("a", labels=["Person"], properties={"email": "a@example.com"})
            graph.graph.ingest_nodes_arrow(
                ["a"],
                [graph.graph.entity_serializer.serialize(node, "Node")],
                native=False,
                index_mode=IndexMaintenanceMode.DEFER,
            )
            stale = advise_index(graph, node_label="Person", node_property="email")
            assert stale["index_exists"] is True
            assert stale["stale"] is True
            assert stale["action"] == "rebuild_indexes"

            graph.graph.rebuild_deferred_indexes()
            recovered = advise_index(graph, node_label="Person", node_property="email")
            assert recovered["stale"] is False
            assert recovered["action"] == "current"


def test_advisor_edge_predicate_transitions() -> None:
    with TemporaryDirectory() as tmpdir:
        with GestaltDBGraph.create(f"{tmpdir}/g", backend=_backend()) as graph:
            missing = advise_index(graph, edge_type="KNOWS", edge_property="since")
            assert missing["target"] == "edge"
            assert missing["action"] == "create_index"
            assert missing["create_call"] == "graph.create_edge_property_index('since')"

            graph.graph.create_edge_property_index("since")
            graph.graph.put_nodes([Node("a"), Node("b")])
            edge = Edge("e", "a", "b", properties={"type": "KNOWS", "since": 2020})
            graph.graph.ingest_edges_arrow(
                ["e"],
                ["a"],
                ["b"],
                ["KNOWS"],
                [graph.graph.entity_serializer.serialize(edge, "Edge")],
                native=False,
                index_mode=IndexMaintenanceMode.DEFER,
            )
            stale = advise_index(graph, edge_type="KNOWS", edge_property="since")
            assert stale["stale"] is True
            assert stale["action"] == "rebuild_indexes"
            graph.graph.rebuild_deferred_indexes()
            assert (
                advise_index(graph, edge_type="KNOWS", edge_property="since")["action"] == "current"
            )


def test_advisor_tool_is_read_only_bounded_and_toolkit_opt_in() -> None:
    with TemporaryDirectory() as tmpdir:
        with GestaltDBGraph.create(f"{tmpdir}/g", backend=_backend()) as graph:
            assert len(GestaltDBToolkit(graph=graph).get_tools()) == 2
            tools = GestaltDBToolkit(graph=graph, include_index_advisor=True).get_tools()
            assert [tool.name for tool in tools].count("gestaltdb_index_advisor") == 1

            tool = GestaltDBIndexAdvisorTool(graph=graph)
            before = set(graph.graph.stale_indexes())
            output = json.loads(tool.invoke({"node_label": "Person", "node_property": "email"}))
            assert output["action"] == "create_index"
            assert set(graph.graph.stale_indexes()) == before
            assert graph.graph.index_statistics()["indexed_node_properties"] == ()
