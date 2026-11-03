"""Offline visualization artifact tests (G17-11, GEP-13).

No database, network, or JavaScript toolchain: nodes_edges mode builds
payloads from explicit dicts, and the packaged viewer inlines its bundle.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from langchain_gestaltdb import VizDependencyError, render_artifact
from langchain_gestaltdb.viz import (
    DEFAULT_MAX_EDGES,
    DEFAULT_MAX_NODES,
    GestaltDBVizTool,
)

NODES = [
    {"id": "n1", "labels": ["Person"], "properties": {"name": "Ann"}},
    {"id": "n2", "labels": ["Person"], "properties": {"name": "Bo"}},
    {"id": "n3", "labels": ["Place"], "properties": {"name": "Café"}},
]
EDGES = [
    {"id": "e1", "source": "n1", "target": "n2", "type": "KNOWS"},
    {"id": "e2", "source": "n2", "target": "n3", "type": "VISITED"},
]


def test_defaults_match_viz_options_caps() -> None:
    assert (DEFAULT_MAX_NODES, DEFAULT_MAX_EDGES) == (2000, 5000)


def test_nodes_edges_html_artifact_offline(tmp_path: Path) -> None:
    output = tmp_path / "graph.html"
    summary = json.loads(render_artifact(None, "nodes_edges", output, nodes=NODES, edges=EDGES))
    assert summary["node_count"] == 3
    assert summary["edge_count"] == 2
    assert summary["truncated"] is False
    assert summary["banner"] is None
    assert summary["warning"] is None
    assert summary["path"] == str(output.resolve())
    assert summary["format"] == "html"
    assert "<!doctype" not in json.dumps(summary).lower().replace("graph.html", "")
    document = output.read_text(encoding="utf-8")
    assert document.startswith("<!doctype html>")
    assert '"truncated":false' in document
    assert 'src="http' not in document
    assert "connect-src 'none'" in document


def test_caps_truncate_with_banner_and_warning(tmp_path: Path) -> None:
    output = tmp_path / "capped.html"
    summary = json.loads(
        render_artifact(
            None, "nodes_edges", output, nodes=NODES, edges=EDGES, max_nodes=2, max_edges=1
        )
    )
    assert summary["truncated"] is True
    assert summary["node_count"] == 2
    assert summary["edge_count"] == 1
    assert summary["nodes_dropped"] == 1
    assert summary["edges_dropped"] == 1
    assert summary["banner"] is not None
    assert "capped sample" in summary["banner"]
    assert "LIMIT" in summary["banner"]
    assert summary["warning"] is not None
    assert "truncated" in summary["warning"]
    assert '"truncated":true' in output.read_text(encoding="utf-8")


def test_absolute_ceiling_suggests_sampling(tmp_path: Path) -> None:
    nodes = [{"id": f"n{i}"} for i in range(2 * DEFAULT_MAX_NODES + 1)]
    with pytest.raises(ValueError, match="sample.*LIMIT"):
        render_artifact(None, "nodes_edges", tmp_path / "too-big.html", nodes=nodes, edges=[])


def test_svg_circle_and_grid_layouts(tmp_path: Path) -> None:
    for layout in ("circle", "grid"):
        output = tmp_path / f"{layout}.svg"
        summary = json.loads(
            render_artifact(
                None,
                "nodes_edges",
                output,
                format="svg",
                nodes=NODES,
                edges=EDGES,
                svg_layout=layout,
            )
        )
        assert summary["format"] == "svg"
        assert summary["truncated"] is False
        document = output.read_text(encoding="utf-8")
        assert document.startswith("<svg")
        assert "<circle" in document
        assert "http" not in document.split("<svg")[0]


def test_svg_embeds_truncation_banner_on_canvas(tmp_path: Path) -> None:
    output = tmp_path / "capped.svg"
    summary = json.loads(
        render_artifact(
            None, "nodes_edges", output, format="svg", nodes=NODES, edges=EDGES, max_nodes=2
        )
    )
    assert summary["truncated"] is True
    document = output.read_text(encoding="utf-8")
    assert 'role="alert"' in document
    assert "capped sample" in document


def test_missing_viz_dep_is_actionable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "gestaltdb.viz.api", None)
    with pytest.raises(VizDependencyError, match="gestaltdb\\.viz"):
        render_artifact(None, "nodes_edges", tmp_path / "x.html", nodes=NODES, edges=EDGES)


def test_invalid_inputs_fail_closed(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="mode"):
        render_artifact(None, "bogus", tmp_path / "x.html", nodes=NODES, edges=EDGES)
    with pytest.raises(ValueError, match="format"):
        render_artifact(None, "nodes_edges", tmp_path / "x.png", nodes=NODES, edges=EDGES)
    with pytest.raises(ValueError, match="suffix"):
        render_artifact(None, "nodes_edges", tmp_path / "x.svg", nodes=NODES, edges=EDGES)
    with pytest.raises(ValueError, match="requires a graph"):
        render_artifact(None, "query", tmp_path / "x.html", query="MATCH (n) RETURN n")
    with pytest.raises(ValueError, match="non-empty query"):
        render_artifact(None, "query", tmp_path / "x.html")
    with pytest.raises(ValueError, match="seeds and pattern"):
        render_artifact(None, "sample", tmp_path / "x.html")
    with pytest.raises(ValueError, match="positive integer"):
        render_artifact(
            None, "nodes_edges", tmp_path / "x.html", nodes=NODES, edges=EDGES, max_nodes=0
        )


def test_summary_never_exceeds_max_chars(tmp_path: Path) -> None:
    output = tmp_path / "graph.html"
    summary = json.loads(
        render_artifact(None, "nodes_edges", output, nodes=NODES, edges=EDGES, max_chars=256)
    )
    encoded = json.dumps(summary)
    assert len(encoded) <= 256 or "error" in summary
    assert output.is_file()


def test_viz_tool_requires_graph_instance() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        GestaltDBVizTool(graph=None)  # type: ignore[arg-type]
