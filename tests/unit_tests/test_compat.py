"""Compatibility bridge dispatch tests (G17-01 review fixes).

These use small fakes rather than a database so both the 0.12 facade layout
and the 0.17 services layout are exercised in the standard suite.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest

from langchain_gestaltdb._compat import (
    call_upstream,
    evaluate_entailment,
    open_read_view,
    service_layout,
)


class _FacadeGraph:
    """Minimal 0.12-style facade: read_view and entails live on the graph."""

    def __init__(self) -> None:
        self.view_calls: list[dict[str, Any]] = []
        self.entails_calls: list[tuple[Any, ...]] = []

    @contextmanager
    def read_view(
        self, *, valid_time: Any, system_time: Any = None, through_commit: int | None = None
    ) -> Iterator[str]:
        self.view_calls.append(
            {
                "valid_time": valid_time,
                "system_time": system_time,
                "through_commit": through_commit,
            }
        )
        yield "facade-view"

    def entails(self, agent: str, proposition: Any, mode: str, **kwargs: Any) -> str:
        self.entails_calls.append((agent, proposition, mode, kwargs))
        return "facade-result"

    def iter_claims_as_of(self) -> None:
        return None

    def assert_claim(self) -> None:
        return None


class _TemporalService:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    @contextmanager
    def open_read_view(
        self,
        graph: Any,
        *,
        manifest: Any,
        database_id: str,
        valid_time: Any,
        system_time: Any = None,
        through_commit: int | None = None,
    ) -> Iterator[str]:
        self.calls.append(
            {
                "graph": graph,
                "manifest": manifest,
                "database_id": database_id,
                "valid_time": valid_time,
                "system_time": system_time,
                "through_commit": through_commit,
            }
        )
        yield "service-view"


class _EpistemicService:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []

    def entails(
        self,
        view: Any,
        agent: str,
        proposition: Any,
        mode: str,
        *,
        world: str,
        max_depth: int = 4,
        max_states: int = 1_000,
    ) -> str:
        self.calls.append((view, agent, proposition, mode, world, max_depth, max_states))
        return "service-result"


class _ServiceGraph:
    """Minimal 0.17-style graph exposing scope-owned services."""

    manifest = {"format_version": 1}
    database_id = "db-test"

    def __init__(self) -> None:
        self.temporal = _TemporalService()
        self.epistemic = _EpistemicService()
        self.rules = object()


def test_read_view_prefers_facade_when_present() -> None:
    graph = _FacadeGraph()
    with open_read_view(graph, valid_time="t1", through_commit=7) as view:
        assert view == "facade-view"
    assert graph.view_calls == [{"valid_time": "t1", "system_time": None, "through_commit": 7}]


def test_read_view_falls_back_to_temporal_service() -> None:
    graph = _ServiceGraph()
    with call_upstream(graph, "read_view", valid_time="t1", system_time="s1") as view:
        assert view == "service-view"
    call = graph.temporal.calls[0]
    assert call["graph"] is graph
    assert call["manifest"] == {"format_version": 1}
    assert call["database_id"] == "db-test"
    assert call["valid_time"] == "t1"
    assert call["system_time"] == "s1"
    assert call["through_commit"] is None


def test_entails_prefers_facade_when_present() -> None:
    graph = _FacadeGraph()
    result = evaluate_entailment(
        graph, "a1", {"subject": "s"}, "BELIEVES", world="w", valid_time="t1"
    )
    assert result == "facade-result"
    assert graph.entails_calls[0][3]["valid_time"] == "t1"


def test_entails_opens_pinned_view_for_epistemic_service() -> None:
    graph = _ServiceGraph()
    result = call_upstream(
        graph,
        "entails",
        "a1",
        {"subject": "s"},
        "BELIEVES",
        world="w",
        valid_time="t1",
        system_time="s1",
        max_depth=2,
        max_states=10,
    )
    assert result == "service-result"
    assert graph.epistemic.calls == [
        ("service-view", "a1", {"subject": "s"}, "BELIEVES", "w", 2, 10)
    ]
    assert graph.temporal.calls[0]["valid_time"] == "t1"
    assert graph.temporal.calls[0]["system_time"] == "s1"


def test_read_view_without_supported_layout_fails_closed() -> None:
    with pytest.raises(AttributeError, match="read_view"):
        with open_read_view(object(), valid_time="t1"):
            pass


def test_unsupported_operation_raises() -> None:
    with pytest.raises(AttributeError, match="Unsupported"):
        call_upstream(object(), "not_a_method")


def test_service_layout_reports_layout() -> None:
    assert service_layout(_FacadeGraph()) == "facade"
    assert service_layout(_ServiceGraph()) == "services"
    assert service_layout(object()) == "unknown"
