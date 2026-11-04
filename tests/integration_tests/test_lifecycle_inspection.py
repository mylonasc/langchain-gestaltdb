"""Read-only lifecycle inspection passthroughs (G17-02, GEP-03)."""

from tempfile import TemporaryDirectory

import pytest
from gestaltdb.graphdb import GraphDB, Node

from langchain_gestaltdb import GestaltDBGraph


def test_owned_inspection_manifest_id_and_index_round_trip() -> None:
    with TemporaryDirectory() as tmpdir:
        path = f"{tmpdir}/graph"
        adapter = GestaltDBGraph.create(path)
        try:
            # PyRex/RocksDB creation default; no silent fallback.
            assert adapter.manifest["backend"]["name"] == "pyrex"
            assert adapter.database_id == adapter.graph.database_id
            database_id = adapter.database_id
            assert adapter.stale_indexes() == ()
            assert adapter.index_statistics() == {
                "indexed_node_properties": (),
                "indexed_edge_properties": (),
            }
            adapter.graph.put_nodes([Node("a", labels=["Person"], properties={"kind": "staff"})])
            adapter.graph.create_node_property_index("kind")
            assert adapter.index_statistics() == {
                "indexed_node_properties": ("kind",),
                "indexed_edge_properties": (),
            }
            # Manifest copies are detached from the database.
            manifest = adapter.manifest
            manifest["backend"]["name"] = "mutated"
            assert adapter.manifest["backend"]["name"] == "pyrex"
        finally:
            adapter.close()
        adapter.close()  # Idempotent; owned handle already released.
        with pytest.raises(RuntimeError, match="closed"):
            _ = adapter.manifest
        with pytest.raises(RuntimeError, match="closed"):
            _ = adapter.database_id
        with pytest.raises(RuntimeError, match="closed"):
            adapter.index_statistics()
        with pytest.raises(RuntimeError, match="closed"):
            adapter.stale_indexes()

        with GestaltDBGraph.open(path) as reopened:
            assert reopened.manifest["backend"]["name"] == "pyrex"
            assert reopened.database_id == database_id
            assert reopened.stale_indexes() == ()
            assert reopened.index_statistics() == {
                "indexed_node_properties": ("kind",),
                "indexed_edge_properties": (),
            }


def test_borrowed_wrapper_inspection_leaves_owner_open() -> None:
    with TemporaryDirectory() as tmpdir:
        raw = GraphDB.create(f"{tmpdir}/borrowed", backend="pyrex")
        try:
            adapter = GestaltDBGraph(raw)
            try:
                assert adapter.manifest["backend"]["name"] == "pyrex"
                assert adapter.database_id == raw.database_id
                assert adapter.stale_indexes() == ()
                assert adapter.index_statistics() == {
                    "indexed_node_properties": (),
                    "indexed_edge_properties": (),
                }
            finally:
                adapter.close()
            # Closing the borrowed adapter leaves the owner's handle usable.
            assert raw.query("MATCH (n) RETURN n").records == []
            with pytest.raises(RuntimeError, match="closed"):
                _ = adapter.database_id
        finally:
            raw.close()


def test_read_snapshot_semantics_default_pyrex() -> None:
    """Default PyRex reads directly; explicit snapshots are rejected.

    LMDB snapshots everything verifiably (MVCC) and transactional PyRex pins
    reads, while LevelDB and default PyRex raise ``NotImplementedError`` for
    ``read_snapshot=True`` and ``current_read_view``. This pins the default
    side of that matrix; the LMDB/LevelDB sides run in the backend-contract
    CI jobs with their extras installed.
    """
    with TemporaryDirectory() as tmpdir:
        with GestaltDBGraph.create(f"{tmpdir}/graph") as adapter:
            adapter.graph.put_nodes([Node("a", labels=["Person"])])
            expected = [{"id": "a"}]
            assert adapter.query("MATCH (n) RETURN n.id AS id") == expected
            assert adapter.query("MATCH (n) RETURN n.id AS id", read_snapshot=False) == expected
            with pytest.raises(NotImplementedError):
                adapter.query("MATCH (n) RETURN n.id AS id", read_snapshot=True)
            with pytest.raises(NotImplementedError):
                with adapter.graph.current_read_view():
                    pass


def test_transactional_pyrex_pins_explicit_snapshots() -> None:
    with TemporaryDirectory() as tmpdir:
        options = {"backend_options": {"transactional": True}}
        with GestaltDBGraph.create(f"{tmpdir}/graph", **options) as adapter:
            assert adapter.manifest["backend"]["name"] == "pyrex"
            adapter.graph.put_nodes([Node("a", labels=["Person"])])
            assert adapter.query("MATCH (n) RETURN n.id AS id", read_snapshot=True) == [{"id": "a"}]
            with adapter.graph.current_read_view():
                assert adapter.query("MATCH (n) RETURN n.id AS id") == [{"id": "a"}]
