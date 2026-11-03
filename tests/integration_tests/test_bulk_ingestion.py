"""Trusted-app bulk columnar ingestion (GEP-07, issue #43).

Conventions follow ``test_ingestion.py``: temporary database paths, owned
adapters closed via context managers, no paid model credentials, no
whole-graph enumeration. The pyarrow/polars integration tests skip with a
reason when the optional dependency is missing; validation and delegation are
also covered with stubbed frames so the suite still exercises this module on
backends and dependency sets without them.
"""

import os
import sys
import types
from pathlib import Path
from typing import Any

import pytest

from langchain_gestaltdb import GestaltDBGraph
from langchain_gestaltdb._bulk import (
    ingest_polars_frames,
    normalize_index_mode,
    require_chunk_size,
    validate_arrow_columns,
)


def _backend() -> str:
    return os.environ.get("GESTALTDB_TEST_BACKEND", "pyrex")


def _node_frame(pl: Any, **overrides: Any) -> Any:
    columns = {"node_id": ["alice", "bob"], "labels": [["Person"], ["Person"]]}
    return pl.DataFrame({**columns, **overrides})


def _edge_frame(pl: Any, **overrides: Any) -> Any:
    columns = {"edge_id": ["e1"], "source": ["alice"], "target": ["bob"], "edge_type": ["KNOWS"]}
    return pl.DataFrame({**columns, **overrides})


def test_normalize_index_mode_accepts_names_and_enum() -> None:
    from gestaltdb import IndexMaintenanceMode

    assert normalize_index_mode("MAINTAIN") == IndexMaintenanceMode.MAINTAIN
    assert normalize_index_mode("defer") == IndexMaintenanceMode.DEFER
    assert normalize_index_mode("Defer_Rebuild") == IndexMaintenanceMode.DEFER_REBUILD
    assert normalize_index_mode(IndexMaintenanceMode.DEFER) == IndexMaintenanceMode.DEFER


def test_normalize_index_mode_rejects_unknown() -> None:
    for bad in ("sometimes", "", None, 42, True):
        with pytest.raises(ValueError, match="index_mode"):
            normalize_index_mode(bad)


def test_normalize_index_mode_without_upstream_enum(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "gestaltdb", types.ModuleType("gestaltdb"))
    assert normalize_index_mode("DEFER") == "defer"
    with pytest.raises(ValueError, match="index_mode"):
        normalize_index_mode("sometimes")


def test_require_chunk_size() -> None:
    assert require_chunk_size(1) == 1
    for bad in (0, -100, "100", 1.5, True, None):
        with pytest.raises(ValueError, match="chunk_size"):
            require_chunk_size(bad)


def _stub_polars(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Install a minimal polars stand-in so validation is covered without the dep."""
    module = types.ModuleType("polars")
    module.String = "String"

    class DataFrame:
        def __init__(self, data: dict[str, list[Any]]) -> None:
            self._data = dict(data)

        @property
        def columns(self) -> list[str]:
            return list(self._data)

        @property
        def schema(self) -> dict[str, str]:
            return {key: "List" for key in self._data}

    class LazyFrame(DataFrame):
        def collect_schema(self) -> Any:
            names = self.columns
            return types.SimpleNamespace(names=lambda: list(names), get=lambda k: "List")

    module.DataFrame = DataFrame  # type: ignore[attr-defined]
    module.LazyFrame = LazyFrame  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "polars", module)
    return module


def _stub_graph(calls: dict[str, Any]) -> Any:
    def ingest_polars(node_df: Any, edge_df: Any, **kwargs: Any) -> dict[str, Any]:
        calls["polars"] = (node_df, edge_df, kwargs)
        return {"nodes": 2, "edges": 1, "rebuilt_indexes": {}, "stale_indexes": ()}

    def ingest_arrow(*columns: Any, **kwargs: Any) -> dict[str, Any]:
        calls["arrow"] = (columns, kwargs)
        return {"nodes": 2, "edges": 1, "rebuilt_indexes": {}, "stale_indexes": ()}

    return types.SimpleNamespace(
        ingest_polars=ingest_polars, ingest_arrow=ingest_arrow, close=lambda: None
    )


def test_polars_frames_validate_then_delegate(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    pl = _stub_polars(monkeypatch)
    calls: dict[str, Any] = {}
    graph = _stub_graph(calls)
    with GestaltDBGraph.create(tmp_path / "g", backend=_backend()) as adapter:
        monkeypatch.setattr(adapter, "_graph", graph)
        result = adapter.ingest_polars(
            pl.DataFrame({"node_id": ["a"], "labels": [["P"]], "name": ["A"]}),
            pl.DataFrame({"edge_id": ["e"], "source": ["a"], "target": ["a"], "edge_type": ["K"]}),
            node_property_columns=["name"],
            index_mode="MAINTAIN",
        )
    assert result["nodes"] == 2
    _, _, kwargs = calls["polars"]
    assert kwargs["node_property_columns"] == ["name"]
    assert kwargs["chunk_size"] == 100_000


def test_adapter_schema_cache_invalidated_by_bulk(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _stub_polars(monkeypatch)
    calls: dict[str, Any] = {}
    with GestaltDBGraph.create(tmp_path / "g", backend=_backend()) as adapter:
        _ = adapter.get_structured_schema  # populate the cache on the real graph
        assert adapter._schema is not None
        monkeypatch.setattr(adapter, "_graph", _stub_graph(calls))
        adapter.ingest_arrow(["a"], ["e"], ["a"], ["a"], ["K"])
        assert adapter._schema is None
        assert calls["arrow"][1]["index_mode"] is not None


def test_polars_import_error_is_actionable(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setitem(sys.modules, "polars", None)
    with GestaltDBGraph.create(tmp_path / "g", backend=_backend()) as adapter:
        with pytest.raises(ImportError, match="optional 'polars' dependency"):
            adapter.ingest_polars(object(), object())


def test_polars_frame_validation_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    pl = _stub_polars(monkeypatch)
    graph = _stub_graph({})
    nodes = pl.DataFrame({"node_id": ["a"], "labels": [["P"]]})
    edges = pl.DataFrame({"edge_id": ["e"], "source": ["a"], "target": ["a"], "edge_type": ["K"]})
    with pytest.raises(TypeError, match="node_df"):
        ingest_polars_frames(graph, object(), edges)
    with pytest.raises(TypeError, match="edge_df"):
        ingest_polars_frames(graph, nodes, object())
    with pytest.raises(ValueError, match="missing required columns: node_id"):
        ingest_polars_frames(graph, pl.DataFrame({"labels": [["P"]]}), edges)
    with pytest.raises(ValueError, match="missing required columns: source"):
        ingest_polars_frames(graph, nodes, pl.DataFrame({"edge_id": ["e"]}))
    with pytest.raises(ValueError, match="missing required columns: absent"):
        ingest_polars_frames(graph, nodes, edges, node_property_columns=["absent"])
    with pytest.raises(ValueError, match="missing required columns: absent"):
        ingest_polars_frames(graph, nodes, edges, edge_property_columns=["absent"])
    with pytest.raises(ValueError, match="chunk_size"):
        ingest_polars_frames(graph, nodes, edges, chunk_size=0)
    with pytest.raises(ValueError, match="index_mode"):
        ingest_polars_frames(graph, nodes, edges, index_mode="sometimes")
    with pytest.raises(ValueError, match="labels must be"):
        ingest_polars_frames(graph, nodes, edges, labels=42)
    with pytest.raises(ValueError, match="non-empty strings"):
        ingest_polars_frames(graph, nodes, edges, node_id="")
    for bad_columns in ("name", [], [123], [""]):
        with pytest.raises(ValueError, match="non-empty list of column names"):
            ingest_polars_frames(graph, nodes, edges, node_property_columns=bad_columns)
        with pytest.raises(ValueError, match="non-empty list of column names"):
            ingest_polars_frames(graph, nodes, edges, edge_property_columns=bad_columns)


def test_polars_string_labels_column_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    pl = _stub_polars(monkeypatch)

    class StringLabels(pl.DataFrame):
        @property
        def schema(self) -> dict[str, str]:
            return {key: "String" for key in self._data}

    graph = _stub_graph({})
    nodes = StringLabels({"node_id": ["a"], "labels": ["Person"]})
    edges = pl.DataFrame({"edge_id": ["e"], "source": ["a"], "target": ["a"], "edge_type": ["K"]})
    with pytest.raises(ValueError, match="re-wrap"):
        ingest_polars_frames(graph, nodes, edges)


def test_arrow_column_validation_errors(tmp_path: Path) -> None:
    with GestaltDBGraph.create(tmp_path / "g", backend=_backend()) as adapter:
        with pytest.raises(ValueError, match="equal lengths"):
            adapter.ingest_arrow(["a"], ["e"], ["a"], [], ["K"])
        with pytest.raises(ValueError, match="labels must match"):
            adapter.ingest_arrow(["a", "b"], ["e"], ["a"], ["b"], ["K"], labels=[["P"]])
        with pytest.raises(TypeError, match="node_properties"):
            adapter.ingest_arrow(["a"], ["e"], ["a"], ["a"], ["K"], node_properties=[("n", ["A"])])  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="in length"):
            adapter.ingest_arrow(
                ["a"], ["e"], ["a"], ["a"], ["K"], node_properties={"name": ["A", "B"]}
            )
        with pytest.raises(ValueError, match="non-empty strings"):
            adapter.ingest_arrow(["a"], ["e"], ["a"], ["a"], ["K"], node_properties={"": ["A"]})
        with pytest.raises(ValueError, match="chunk_size"):
            adapter.ingest_arrow(["a"], ["e"], ["a"], ["a"], ["K"], chunk_size=0)
        with pytest.raises(ValueError, match="index_mode"):
            adapter.ingest_arrow(["a"], ["e"], ["a"], ["a"], ["K"], index_mode="sometimes")
        with pytest.raises(TypeError, match="sized"):
            validate_arrow_columns(
                node_ids=(x for x in ["a"]),
                edge_ids=["e"],
                sources=["a"],
                targets=["a"],
                edge_types=["K"],
                labels=None,
                node_properties=None,
                edge_properties=None,
            )


def test_frame_columns_rejects_unknown_frame(monkeypatch: pytest.MonkeyPatch) -> None:
    from langchain_gestaltdb._bulk import _frame_columns

    pl = _stub_polars(monkeypatch)
    with pytest.raises(TypeError, match="node_df"):
        _frame_columns(pl, object(), "node")


def test_ingest_polars_defer_rebuild_lookup(tmp_path: Path) -> None:
    pl = pytest.importorskip("polars", reason="bulk polars test needs optional polars")
    with GestaltDBGraph.create(tmp_path / "g", backend=_backend()) as adapter:
        adapter.graph.create_node_property_index("name")
        result = adapter.ingest_polars(
            _node_frame(pl, name=["Alice", "Bob"]),
            _edge_frame(pl, since=[2020]),
            node_property_columns=["name"],
            edge_property_columns=["since"],
            chunk_size=1,
            index_mode="DEFER_REBUILD",
        )
        assert result["nodes"] == 2
        assert result["edges"] == 1
        assert result["stale_indexes"] == ()
        assert [node.get_id for node in adapter.graph.nodes_by_property("name", "Alice")] == [
            "alice"
        ]
        assert adapter.query("MATCH ()-[r:KNOWS]->() RETURN r.since AS since") == [{"since": 2020}]
        assert "Person" in adapter.get_structured_schema["node_props"]


def test_ingest_polars_defer_leaves_stale_until_rebuild(tmp_path: Path) -> None:
    pl = pytest.importorskip("polars", reason="bulk polars test needs optional polars")
    with GestaltDBGraph.create(tmp_path / "g", backend=_backend()) as adapter:
        adapter.graph.create_node_property_index("name")
        result = adapter.ingest_polars(
            _node_frame(pl, name=["Alice", "Bob"]),
            _edge_frame(pl),
            node_property_columns=["name"],
            index_mode="DEFER",
        )
        assert result["nodes"] == 2
        assert result["stale_indexes"] != ()
        adapter.graph.rebuild_deferred_indexes()
        assert [node.get_id for node in adapter.graph.nodes_by_property("name", "Bob")] == ["bob"]


def test_ingest_polars_rejects_csv_style_string_labels(tmp_path: Path) -> None:
    pl = pytest.importorskip("polars", reason="bulk polars test needs optional polars")
    with GestaltDBGraph.create(tmp_path / "g", backend=_backend()) as adapter:
        nodes = pl.DataFrame({"node_id": ["a"], "labels": ["Person"]})
        with pytest.raises(ValueError, match="re-wrap"):
            adapter.ingest_polars(nodes, _edge_frame(pl))
        assert list(adapter.graph.get_node_keys_generator()) == []


def test_ingest_polars_lazy_frames(tmp_path: Path) -> None:
    pl = pytest.importorskip("polars", reason="bulk polars test needs optional polars")
    with GestaltDBGraph.create(tmp_path / "g", backend=_backend()) as adapter:
        result = adapter.ingest_polars(
            _node_frame(pl).lazy(), _edge_frame(pl).lazy(), index_mode="maintain"
        )
        assert (result["nodes"], result["edges"]) == (2, 1)


def test_ingest_arrow_defer_rebuild_lookup(tmp_path: Path) -> None:
    pa = pytest.importorskip("pyarrow", reason="bulk arrow test needs optional pyarrow")
    with GestaltDBGraph.create(tmp_path / "g", backend=_backend()) as adapter:
        adapter.graph.create_node_property_index("name")
        result = adapter.ingest_arrow(
            pa.array(["alice", "bob"]),
            pa.array(["e1"]),
            pa.array(["alice"]),
            pa.array(["bob"]),
            pa.array(["KNOWS"]),
            labels=pa.array([["Person"], ["Person"]]),
            node_properties={"name": pa.array(["Alice", "Bob"])},
            edge_properties={"since": pa.array([2020])},
            index_mode="DEFER_REBUILD",
        )
        assert result["nodes"] == 2
        assert result["edges"] == 1
        assert [node.get_id for node in adapter.graph.nodes_by_property("name", "Alice")] == [
            "alice"
        ]
        assert adapter.query("MATCH (a:Person)-[:KNOWS]->(b) RETURN a.id AS a, b.id AS b") == [
            {"a": "alice", "b": "bob"}
        ]
