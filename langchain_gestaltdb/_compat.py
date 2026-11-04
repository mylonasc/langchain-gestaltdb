"""Version compatibility for GestaltDB 0.12 facade vs 0.17+ services (G17-01).

0.12 exposes epistemic/temporal/rule operations directly on ``GraphDB``
(``graph.assert_claim``, ``graph.iter_claims_as_of``, ``graph.explain_claim``,
``graph.get_node_as_of``, ``graph.run_rules``, ...).

0.17 decomposes the facade into scope-owned services:
``graph.epistemic``, ``graph.temporal``, ``graph.rules`` (plus ``graph.nodes`` /
``graph.edges`` / ``graph.indexes`` / ``graph.traversal`` / ``graph.lookup``).
Direct facade methods were removed. ``gestaltdb.cypher_ast`` names used by the
read-only policy are unchanged (``EntailsCall``, ``SampleTypedPathsCall``,
``ShowConstraints``, ``ShowIndexes`` + ``parse_ast``).

This module prefers the facade when present (0.12) and falls back to services
(0.17+). Method renames in 0.17 are mapped:
- ``explain_claim`` -> ``rules.explain``
- ``run_rules`` -> ``rules.run``
- ``maintain_truth`` -> ``rules.maintain``
- ``read_view`` -> ``temporal.open_read_view`` (when facade is absent)
Everything else keeps its name under ``epistemic`` / ``temporal`` / ``rules``.

Two operations cannot be forwarded by simple method-name lookup:

- ``read_view`` is a context manager and, on 0.17, needs the owning graph plus
  ``manifest``/``database_id`` authentication. Use :func:`open_read_view`.
- ``entails`` is a plain method on 0.12 but takes an already-pinned
  ``GraphReadView`` on 0.17. Use :func:`evaluate_entailment`, which opens the
  view and passes it to ``epistemic.entails``.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

_METHOD_SERVICE: dict[str, tuple[str, str]] = {
    # epistemic
    "assert_claim": ("epistemic", "assert_claim"),
    "correct_claim": ("epistemic", "correct_claim"),
    "retract_claim": ("epistemic", "retract_claim"),
    "iter_claims_as_of": ("epistemic", "iter_claims_as_of"),
    "get_claim_as_of": ("epistemic", "get_claim_as_of"),
    "claim_status": ("epistemic", "claim_status"),
    "assert_world_accessibility": ("epistemic", "assert_world_accessibility"),
    "entails": ("epistemic", "entails"),
    # rules (with renames)
    "explain_claim": ("rules", "explain"),
    "create_rule": ("rules", "create_rule"),
    "run_rules": ("rules", "run"),
    "maintain_truth": ("rules", "maintain"),
    "get_rule": ("rules", "get_rule"),
    "iter_rule_versions": ("rules", "iter_rule_versions"),
    # temporal
    "get_node_as_of": ("temporal", "get_node_as_of"),
    "get_edge_as_of": ("temporal", "get_edge_as_of"),
    "iter_nodes_as_of": ("temporal", "iter_nodes_as_of"),
    "iter_edges_as_of": ("temporal", "iter_edges_as_of"),
    "put_node_version": ("temporal", "put_node_version"),
    "put_edge_version": ("temporal", "put_edge_version"),
    "rebuild_temporal_indexes": ("temporal", "rebuild_temporal_indexes"),
}


def call_upstream(graph: Any, name: str, *args: Any, **kwargs: Any) -> Any:
    """Call a facade method or its 0.17+ service equivalent."""
    if name == "read_view":
        return open_read_view(graph, **kwargs)
    if name == "entails":
        return evaluate_entailment(graph, *args, **kwargs)
    if hasattr(graph, name):
        return getattr(graph, name)(*args, **kwargs)
    if name not in _METHOD_SERVICE:
        raise AttributeError(f"Unsupported GestaltDB operation: {name}")
    service_name, method_name = _METHOD_SERVICE[name]
    service = getattr(graph, service_name, None)
    if service is None:
        raise AttributeError(f"GestaltDB graph has neither {name} nor {service_name}")
    method = getattr(service, method_name, None)
    if method is None:
        # Fall back to original name on the service (forward-compat).
        method = getattr(service, name, None)
    if method is None:
        raise AttributeError(f"{service_name} has neither {method_name} nor {name}")
    return method(*args, **kwargs)


@contextmanager
def open_read_view(
    graph: Any,
    *,
    valid_time: Any,
    system_time: Any = None,
    through_commit: int | None = None,
) -> Iterator[Any]:
    """Yield one pinned temporal read view on either supported layout.

    0.12 exposes ``GraphDB.read_view`` directly; 0.17 replaces it with
    ``temporal.open_read_view(graph, manifest=..., database_id=..., ...)``.
    Both pin domain time and an optional system horizon (``system_time`` or
    ``through_commit``), so repeated reads see one visible commit prefix.
    """
    facade = getattr(graph, "read_view", None)
    if callable(facade):
        with facade(
            valid_time=valid_time, system_time=system_time, through_commit=through_commit
        ) as view:
            yield view
        return
    temporal = getattr(graph, "temporal", None)
    opener = getattr(temporal, "open_read_view", None)
    if not callable(opener):
        raise AttributeError("GestaltDB graph has neither read_view nor temporal.open_read_view")
    with opener(
        graph,
        manifest=graph.manifest,
        database_id=graph.database_id,
        valid_time=valid_time,
        system_time=system_time,
        through_commit=through_commit,
    ) as view:
        yield view


def evaluate_entailment(
    graph: Any,
    agent: str,
    proposition: Any,
    mode: str,
    *,
    world: str,
    valid_time: Any,
    system_time: Any = None,
    through_commit: int | None = None,
    max_depth: int = 4,
    max_states: int = 1_000,
) -> Any:
    """Evaluate one bounded modal formula on either supported layout.

    0.12 takes the horizon directly on ``GraphDB.entails``. 0.17's
    ``epistemic.entails`` requires a pinned ``GraphReadView`` instead, so open
    one at the same valid/system horizon and pass it in. The returned object
    has the same ``status``/``confidence``/``explanation`` fields either way.
    """
    facade = getattr(graph, "entails", None)
    if callable(facade):
        return facade(
            agent,
            proposition,
            mode,
            world=world,
            valid_time=valid_time,
            system_time=system_time,
            through_commit=through_commit,
            max_depth=max_depth,
            max_states=max_states,
        )
    epistemic = getattr(graph, "epistemic", None)
    method = getattr(epistemic, "entails", None)
    if not callable(method):
        raise AttributeError("GestaltDB graph has neither entails nor epistemic.entails")
    with open_read_view(
        graph, valid_time=valid_time, system_time=system_time, through_commit=through_commit
    ) as view:
        return method(
            view, agent, proposition, mode, world=world, max_depth=max_depth, max_states=max_states
        )


def supports_facade(graph: Any) -> bool:
    """True on 0.12-style graphs exposing claim operations directly."""
    return hasattr(graph, "iter_claims_as_of") and hasattr(graph, "assert_claim")


def service_layout(graph: Any) -> str:
    """Return 'facade' (0.12) or 'services' (0.17+) for diagnostics."""
    if supports_facade(graph):
        return "facade"
    if all(hasattr(graph, name) for name in ("epistemic", "temporal", "rules")):
        return "services"
    return "unknown"
