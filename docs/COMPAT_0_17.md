# GestaltDB 0.12 → 0.17 compatibility (G17-01 probe)

Pinned: `gestaltdb>=0.17.0,<0.18` (0.17.0 installed) since `0.2.0`.
Previously: `gestaltdb>=0.12.0,<0.13` (0.12.0 installed) through `0.1.1`.
The compat bridge below keeps working on both upstream layouts.

## Layout change (breaking)

- 0.12: `GraphDB.assert_claim / iter_claims_as_of / explain_claim / get_node_as_of / run_rules / ...` directly on facade.
- 0.17: facade decomposed into `graph.epistemic`, `graph.temporal`, `graph.rules` (+ `nodes`/`edges`/`indexes`/`traversal`/`lookup`). Facade claim/temporal/rule methods removed.
- Unchanged: `GraphDB.create/open/query/put_node/put_edge/get_node/get_edge/sample_neighbors/iter_typed_adjacency/index_statistics/manifest/stale_indexes`, `gestaltdb.cypher.parse_ast` + `cypher_ast` (`EntailsCall`, `SampleTypedPathsCall`, `ShowConstraints`, `ShowIndexes`).

## Bridge

`langchain_gestaltdb/_compat.py`:

- `call_upstream(graph, name, ...)` prefers the facade when present (0.12),
  else the owning service (0.17+). Renames: `explain_claim→rules.explain`,
  `run_rules→rules.run`, `maintain_truth→rules.maintain`.
- `open_read_view(graph, *, valid_time, system_time=None, through_commit=None)`
  bridges `GraphDB.read_view` (0.12) and
  `temporal.open_read_view(graph, manifest=..., database_id=..., ...)` (0.17).
  `GestaltDBGraph.read_view(...)` exposes the same helper to adapter users.
- `evaluate_entailment(graph, ...)` calls `GraphDB.entails` on 0.12; on 0.17 it
  opens one pinned `GraphReadView` and calls
  `epistemic.entails(view, agent, proposition, mode, world=..., ...)`, because
  the 0.17 service takes a view instead of `valid_time`/`system_time`.
- `service_layout()` returns `facade`/`services` for diagnostics.

Wired across the integration: `claims.py`, `claim_writes.py`,
`temporal_tools.py`, `rules.py`, `modal.py` and `_bulk.py` route through the
bridge (or through shared helpers that do).

## Integration coverage (G17-02 … G17-12)

All twelve slices ship in `0.2.0` on `main` (developed on
`integration/gestalt-agent-backend`) and are tested against
`gestaltdb==0.17.0`:

- Lifecycle inspection: `manifest`, `database_id`, `index_statistics()`,
  `stale_indexes()`, `read_snapshot`, and `adapter.read_view(...)`.
- Cypher: `SHOW INDEX[ES]`, `SHOW CONSTRAINTS`, `kg.entails` with
  `world`+`validTime`, `FOR VALID_TIME` / `FOR SYSTEM_TIME`, version functions,
  and write-restricted temporal queries.
- Schema metadata + read-only index advisor; trusted-app `ingest_polars` /
  `ingest_arrow`; bounded sampling; temporal as-of/scan tools; rule
  get/list/create/run/maintain; modal entails + accessibility writes; offline
  visualization artifacts.
- Claim assert/status/search/explain, deferred-index fail-closed behavior, and
  the full 309-test suite all pass on the 0.17 line.

Verified for `0.2.0`:

- `pytest` 309 passed / 5 skipped on 0.17.0 (the 5 skips are optional
  `polars`/`pyarrow` tests; the isolated bulk smoke covers them when installed).
- `ruff check`, `ruff format --check`, and strict `mypy` pass on the 0.17 line
  (upstream now ships partial typing; the boundary keeps narrow
  `no-untyped-call` ignores, see TD-01).
- Offline `agent_workflow`, `graph_agent`, `claim_agent`, and `claim_writes`
  examples run on 0.17; wheel build, `twine check`, and release-artifact
  verification pass.

## Remaining follow-up

- TD-01 (typing): 0.17 ships partial typing, adopted with narrow boundary
  ignores plus fixes to real errors surfaced by the upgrade; remove the ignores
  as upstream typing completes.
- TD-02 (bulk JSON): 0.12.1 fixed #109; re-benchmark on 0.17 before enabling a
  fast path.
- TD-03 (AST coupling): `cypher_ast` names are stable 0.12→0.17 but remain
  version-coupled; keep watching the upstream classifier.

## Upgrade policy (pinned minor)

- The `gestaltdb` minor stays pinned (`>=0.17.0,<0.18` in `pyproject.toml`)
  until a dedicated, verified upgrade PR moves it. No silent fallback and no
  unpinned drift, so services-layout behavior (0.17+) can never leak in
  unnoticed.
- Each upgrade PR must: run the full suite against the new minor (including
  the LMDB/LevelDB/PyRex contract jobs and offline examples), revisit
  TD-01 (typing), TD-02 (bulk fast path), and TD-03 (AST coupling), re-run
  the `agent_docs` diff (`list`, `get cypher --examples`,
  `search '<API name>'`) and refresh `DIALECT_GUIDANCE` plus
  `docs/AGENT_WORKFLOW.md` for any drift, and review the regenerated
  `uv.lock`. A version-only change must not upgrade unrelated dependencies;
  `uv.lock` is committed and CI enforces locked installs.
- The bridge (`_compat.call_upstream`) keeps facade-first resolution after a
  bump so 0.12-era call sites keep working while new service paths are
  verified; renames stay mapped in one place.

## publish.yml reuse (no behavior change)

Releases keep using the existing `.github/workflows/publish.yml` on `main`
only: manual dispatch of an exact stable `X.Y.Z`, Trusted Publishing/OIDC
with the required `pypi` environment approval, and post-publication wheel
verification (see `docs/RELEASING.md`). Merging to the integration branch or
to `main` never authorizes publication; every release needs its own explicit
maintainer authorization. The integration branch itself never publishes.

## How to probe locally

```bash
# Full suite on the pinned 0.17 line
uv run --locked pytest -q
# Layout check through the bridge
uv run --locked python -c "from langchain_gestaltdb._compat import service_layout; ..."
```
