"""Opt-in offline visualization artifacts for agents (G17-11, GEP-13).

Trusted application code calls :func:`render_artifact` with an explicit output
path to write a self-contained offline ``.html`` artifact (the D3.js/React
front end is prebuilt and packaged; no JavaScript toolchain, network, or new
runtime dependency) or a dependency-free static ``.svg``. The agent receives a
bounded summary JSON object (path, node/edge counts, truncation accounting and
banner text) and never raw HTML/SVG markup.

Query mode enforces the same fail-closed read-only Cypher policy as the query
tool before delegating to ``gestaltdb.viz.api``. Caps default to ``VizOptions``
(2000 nodes / 5000 edges) with absolute ceilings at twice the caps; cap
truncation is deterministic, emits ``TruncationWarning`` (captured and
surfaced in the summary), and renders an on-canvas banner in the viewer.
Payloads beyond the absolute ceiling raise ``VizCapExceededError`` with a
sampling suggestion. Visualization imports stay lazy so ``import
langchain_gestaltdb`` never pays for them and environments without the
packaged ``gestaltdb.viz`` module fail with an actionable error.
"""

from __future__ import annotations

import importlib
import json
import warnings
from pathlib import Path
from typing import Any, Literal
from xml.sax.saxutils import escape as _xml_escape

from langchain_core.callbacks import CallbackManagerForToolRun
from langchain_core.tools import BaseTool, ToolException
from pydantic import BaseModel, ConfigDict, Field, StrictInt

from langchain_gestaltdb._read_only import ReadOnlyQueryError, validate_read_only
from langchain_gestaltdb.graph import GestaltDBGraph

MODES = ("nodes_edges", "query", "sample")
FORMATS = ("html", "svg")
DEFAULT_MAX_NODES = 2000
DEFAULT_MAX_EDGES = 5000
_CEILING_MULTIPLIER = 2
SAMPLING_SUGGESTION = (
    "sample a subgraph with sample mode (visualize_sample) or narrow the query with LIMIT instead."
)
_VIZ_MISSING_MESSAGE = (
    "Graph visualization requires the packaged gestaltdb.viz module "
    "(ships with supported gestaltdb releases); failed to import {target}: {error}. "
    "Upgrade gestaltdb to a release that packages gestaltdb.viz and retry."
)
_FORMAT_SUFFIX = {"html": ".html", "svg": ".svg"}


class VizDependencyError(ImportError):
    """The packaged ``gestaltdb.viz`` module is unavailable."""


def _require_viz_api() -> Any:
    """Import ``gestaltdb.viz.api`` lazily with an actionable error."""
    try:
        return importlib.import_module("gestaltdb.viz.api")
    except ImportError as exc:
        raise VizDependencyError(
            _VIZ_MISSING_MESSAGE.format(target="gestaltdb.viz.api", error=exc)
        ) from exc


def _banner_text(nodes_dropped: int, edges_dropped: int, max_nodes: int, max_edges: int) -> str:
    """Mirror the viewer's on-canvas capped-sample banner as plain text."""
    return (
        f"Showing a capped sample: {nodes_dropped} nodes and {edges_dropped} edges omitted "
        f"(caps {max_nodes} nodes/{max_edges} edges); {SAMPLING_SUGGESTION}"
    )


def render_artifact(
    graph: GestaltDBGraph | None,
    mode: str,
    output_path: str | Path,
    *,
    format: str = "html",
    nodes: Any = None,
    edges: Any = None,
    query: str | None = None,
    params: dict[str, Any] | None = None,
    seeds: Any = None,
    pattern: Any = None,
    rng: Any = None,
    allowed_procedures: Any = (),
    max_nodes: int = DEFAULT_MAX_NODES,
    max_edges: int = DEFAULT_MAX_EDGES,
    title: str = "GestaltDB graph",
    svg_layout: str = "circle",
    svg_width: int = 1200,
    svg_height: int = 800,
    max_chars: int = 16_000,
) -> str:
    """Render an offline visualization artifact and return bounded summary JSON.

    ``mode`` is ``"nodes_edges"`` (explicit collections), ``"query"`` (Cypher
    matches, read-only validated) or ``"sample"`` (typed sampling around
    seeds, the large-graph path). ``graph`` may be ``None`` only for
    ``nodes_edges``. The artifact is written to ``output_path`` (suffix must
    match ``format``); the returned JSON carries ``path``, ``format``,
    ``mode``, ``node_count``, ``edge_count``, ``truncated``,
    ``nodes_dropped``/``edges_dropped``, ``banner`` (the on-canvas banner
    text, or ``None``) and ``warning`` (the captured ``TruncationWarning``
    text, or ``None``). Raw HTML/SVG is never returned.
    """
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}, got {mode!r}")
    if format not in FORMATS:
        raise ValueError(f"format must be one of {FORMATS}, got {format!r}")
    if not isinstance(output_path, (str, Path)) or not str(output_path):
        raise ValueError("output_path must be an explicit non-empty path")
    resolved = Path(output_path).expanduser().resolve()
    expected_suffix = _FORMAT_SUFFIX[format]
    if resolved.suffix.lower() != expected_suffix:
        raise ValueError(f"output_path suffix must be {expected_suffix!r} for format {format!r}")
    for name, value in (("max_nodes", max_nodes), ("max_edges", max_edges)):
        if type(value) is not int or value < 1:
            raise ValueError(f"{name} must be a positive integer")
    if type(max_chars) is not int or max_chars < 256:
        raise ValueError("max_chars must be an integer >= 256")
    if not isinstance(title, str) or not title:
        raise ValueError("title must be a non-empty string")

    api = _require_viz_api()
    try:
        viz_api_options = api.VizOptions
    except AttributeError as exc:
        raise VizDependencyError(
            _VIZ_MISSING_MESSAGE.format(target="gestaltdb.viz.api.VizOptions", error=exc)
        ) from exc
    options = viz_api_options(max_nodes=max_nodes, max_edges=max_edges, title=title)

    figure: Any
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        try:
            if mode == "nodes_edges":
                if nodes is None and edges is None:
                    raise ValueError("nodes_edges mode requires nodes and/or edges")
                figure = api.visualize_nodes_edges(nodes or (), edges or (), options=options)
            elif mode == "query":
                if not isinstance(query, str) or not query:
                    raise ValueError("query mode requires a non-empty query string")
                if graph is None:
                    raise ValueError("query mode requires a graph")
                validate_read_only(query, tuple(allowed_procedures))
                figure = api.visualize_query(
                    graph.graph, query, dict(params) if params else None, options=options
                )
            else:  # mode == "sample"
                if seeds is None or pattern is None:
                    raise ValueError("sample mode requires seeds and pattern")
                if graph is None:
                    raise ValueError("sample mode requires a graph")
                figure = api.visualize_sample(graph.graph, seeds, pattern, rng=rng, options=options)
        except ReadOnlyQueryError:
            raise
        except Exception as exc:
            cap_error = getattr(api, "VizCapExceededError", ())
            if isinstance(cap_error, type) and isinstance(exc, cap_error):
                raise ValueError(
                    f"Visualization exceeds the absolute ceiling "
                    f"({_CEILING_MULTIPLIER}x caps: "
                    f"{max_nodes * _CEILING_MULTIPLIER} nodes/"
                    f"{max_edges * _CEILING_MULTIPLIER} edges); {SAMPLING_SUGGESTION}"
                ) from exc
            raise
    warning_text: str | None = None
    for caught_warning in caught:
        if issubclass(caught_warning.category, Warning):
            warning_text = str(caught_warning.message)
            break

    viz = figure.viz
    node_count = len(getattr(viz, "nodes", ()) or ())
    edge_count = len(getattr(viz, "edges", ()) or ())
    truncation = getattr(viz, "truncation", None)
    nodes_dropped = int(getattr(truncation, "nodes_dropped", 0) or 0)
    edges_dropped = int(getattr(truncation, "edges_dropped", 0) or 0)
    truncated = bool(getattr(truncation, "truncated", False))
    banner = _banner_text(nodes_dropped, edges_dropped, max_nodes, max_edges) if truncated else None

    resolved.parent.mkdir(parents=True, exist_ok=True)
    if format == "html":
        figure.save(resolved)
    else:
        document = figure.as_svg(layout=svg_layout, width=svg_width, height=svg_height)
        if banner is not None:
            banner_element = (
                f'<text x="12" y="24" font-family="sans-serif" font-size="14" '
                f'fill="currentColor" role="alert">{_xml_escape(banner)}</text>'
            )
            document = document.replace("</svg>", f"{banner_element}</svg>")
        resolved.write_text(document, encoding="utf-8")

    summary: dict[str, Any] = {
        "path": str(resolved),
        "format": format,
        "mode": mode,
        "node_count": node_count,
        "edge_count": edge_count,
        "truncated": truncated,
        "nodes_dropped": nodes_dropped,
        "edges_dropped": edges_dropped,
        "banner": banner,
        "warning": warning_text,
    }
    encoded = json.dumps(summary, ensure_ascii=False)
    if len(encoded) > max_chars:
        return json.dumps(
            {
                "error": "Visualization summary exceeds max_chars; increase the configured cap.",
                "path": str(resolved),
                "truncated": True,
            }
        )
    return encoded


class VizInput(BaseModel):
    """Agent-controllable visualization request; paths and bounds are app-controlled."""

    model_config = ConfigDict(extra="forbid")
    mode: Literal["nodes_edges", "query", "sample"] = Field(description="Artifact source mode")
    format: Literal["html", "svg"] = Field(default="html", description="Artifact format")
    query: str | None = Field(
        default=None, min_length=1, max_length=10_000, description="Read-only Cypher for query mode"
    )
    params: dict[str, Any] = Field(default_factory=dict, description="Values for $parameters")
    nodes: list[dict[str, Any]] = Field(
        default_factory=list, max_length=10_000, description="Node dicts for nodes_edges mode"
    )
    edges: list[dict[str, Any]] = Field(
        default_factory=list, max_length=10_000, description="Edge dicts for nodes_edges mode"
    )
    filename: str = Field(
        default="gestaltdb_viz",
        min_length=1,
        max_length=128,
        description="Artifact basename without suffix; directory is app-controlled",
    )


class GestaltDBVizTool(BaseTool):
    """Render an offline visualization artifact and return bounded summary JSON.

    Offline only: artifacts inline the packaged viewer or dependency-free SVG,
    with no network or JavaScript-toolchain dependency. The artifact directory
    is application-controlled via ``output_dir``; the agent supplies only a
    basename. Query mode is read-only validated. Raw HTML/SVG is never
    returned to the agent.
    """

    name: str = "gestaltdb_viz"
    description: str = (
        "Render an offline GestaltDB visualization artifact (HTML viewer or static SVG) "
        "from explicit nodes/edges or a read-only Cypher query. "
        "Returns JSON with the artifact path, node/edge counts and truncation banner. "
        "Large graphs are capped; sample or narrow the query when truncated."
    )
    args_schema: type[BaseModel] = VizInput
    graph: GestaltDBGraph = Field(exclude=True)
    output_dir: str = Field(default=".", exclude=True)
    allowed_procedures: tuple[str, ...] = Field(default=(), exclude=True)
    max_nodes: StrictInt = Field(default=DEFAULT_MAX_NODES, ge=1, le=100_000, exclude=True)
    max_edges: StrictInt = Field(default=DEFAULT_MAX_EDGES, ge=1, le=200_000, exclude=True)
    max_chars: StrictInt = Field(default=16_000, ge=256, le=1_000_000, exclude=True)
    handle_tool_error: bool = True

    def _run(
        self,
        mode: str,
        format: str = "html",
        query: str | None = None,
        params: dict[str, Any] | None = None,
        nodes: list[dict[str, Any]] | None = None,
        edges: list[dict[str, Any]] | None = None,
        filename: str = "gestaltdb_viz",
        run_manager: CallbackManagerForToolRun | None = None,
    ) -> str:
        try:
            if not isinstance(filename, str) or not filename or Path(filename).name != filename:
                raise ToolException(
                    "Invalid filename; supply a plain basename without directories."
                )
            output_path = Path(self.output_dir).expanduser() / f"{filename}{_FORMAT_SUFFIX[format]}"
            if mode == "sample":
                raise ToolException(
                    "Sample mode needs caller-side seeds/pattern; use render_artifact directly."
                )
            return render_artifact(
                self.graph,
                mode,
                output_path,
                format=format,
                nodes=nodes or (),
                edges=edges or (),
                query=query,
                params=params,
                allowed_procedures=self.allowed_procedures,
                max_nodes=int(self.max_nodes),
                max_edges=int(self.max_edges),
                max_chars=int(self.max_chars),
            )
        except ToolException:
            raise
        except ReadOnlyQueryError as exc:
            raise ToolException(str(exc)) from None
        except VizDependencyError as exc:
            raise ToolException(str(exc)) from None
        except (ValueError, OSError) as exc:
            raise ToolException(str(exc)) from None
        except Exception:
            raise ToolException(
                "GestaltDB visualization failed; check the mode inputs and output directory."
            ) from None
