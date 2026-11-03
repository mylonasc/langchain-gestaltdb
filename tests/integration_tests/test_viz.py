"""Database-backed visualization tests (G17-11, GEP-13).

Offline and fast: tiny temporary embedded databases, no network or JS toolchain.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from gestaltdb.graphdb import Edge, Node, SamplingPattern

from langchain_gestaltdb import GestaltDBGraph, GestaltDBVizTool
from langchain_gestaltdb._read_only import ReadOnlyQueryError
from langchain_gestaltdb.viz import render_artifact


def _seeded(tmp_path: Path) -> GestaltDBGraph:
    adapter = GestaltDBGraph.create(tmp_path / "viz")
    adapter.graph.put_nodes([Node("a", labels=["Person"]), Node("b", labels=["Person"])])
    adapter.graph.put_edge(Edge("e", "a", "b", properties={"type": "KNOWS"}))
    return adapter


def test_query_mode_artifact_counts(tmp_path: Path) -> None:
    adapter = _seeded(tmp_path)
    try:
        output = tmp_path / "query.html"
        summary = json.loads(
            render_artifact(adapter, "query", output, query="MATCH (a)-[r]->(b) RETURN a, r, b")
        )
        assert summary["mode"] == "query"
        assert summary["node_count"] == 2
        assert summary["edge_count"] == 1
        assert summary["truncated"] is False
        assert output.is_file()
    finally:
        adapter.close()


def test_query_mode_rejects_writes(tmp_path: Path) -> None:
    adapter = _seeded(tmp_path)
    try:
        with pytest.raises(ReadOnlyQueryError):
            render_artifact(
                adapter,
                "query",
                tmp_path / "write.html",
                query="CREATE (n:Person) RETURN n",
            )
    finally:
        adapter.close()


def test_sample_mode_artifact_counts(tmp_path: Path) -> None:
    adapter = _seeded(tmp_path)
    try:
        output = tmp_path / "sample.svg"
        pattern = SamplingPattern([{"edge_type": "KNOWS", "direction": "any", "sample_size": 10}])
        summary = json.loads(
            render_artifact(adapter, "sample", output, format="svg", seeds=["a"], pattern=pattern)
        )
        assert summary["mode"] == "sample"
        assert summary["node_count"] == 2
        assert summary["edge_count"] == 1
        assert output.is_file()
    finally:
        adapter.close()


def test_viz_tool_query_returns_bounded_summary(tmp_path: Path) -> None:
    adapter = _seeded(tmp_path)
    try:
        outdir = tmp_path / "artifacts"
        tool = GestaltDBVizTool(graph=adapter, output_dir=str(outdir))
        raw = tool.invoke(
            {"mode": "query", "query": "MATCH (a)-[r]->(b) RETURN a, r, b", "filename": "q1"}
        )
        assert isinstance(raw, str)
        summary = json.loads(raw)
        assert summary["node_count"] == 2
        assert summary["path"].startswith(str(outdir.resolve()))
        assert "<svg" not in raw and "<!doctype" not in raw.lower()
    finally:
        adapter.close()


def test_viz_tool_rejects_path_traversal(tmp_path: Path) -> None:
    adapter = _seeded(tmp_path)
    try:
        tool = GestaltDBVizTool(graph=adapter, output_dir=str(tmp_path))
        raw = tool.invoke({"mode": "query", "query": "MATCH (n) RETURN n", "filename": "../evil"})
        assert isinstance(raw, str)
        assert "basename" in raw
        assert not (tmp_path / "evil.html").exists()
    finally:
        adapter.close()
