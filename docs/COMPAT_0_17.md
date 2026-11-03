# GestaltDB 0.12 → 0.17 compatibility (G17-01 probe)

Pinned: `gestaltdb>=0.12.0,<0.13` (0.12.0 installed). Target: `0.17.x`.

## Layout change (breaking)

- 0.12: `GraphDB.assert_claim / iter_claims_as_of / explain_claim / get_node_as_of / run_rules / ...` directly on facade.
- 0.17: facade decomposed into `graph.epistemic`, `graph.temporal`, `graph.rules` (+ `nodes`/`edges`/`indexes`/`traversal`/`lookup`). Facade claim/temporal/rule methods removed.
- Unchanged: `GraphDB.create/open/query/put_node/put_edge/get_node/get_edge/sample_neighbors/iter_typed_adjacency/index_statistics/manifest/stale_indexes`, `gestaltdb.cypher.parse_ast` + `cypher_ast` (`EntailsCall`, `SampleTypedPathsCall`, `ShowConstraints`, `ShowIndexes`).

## Bridge

`langchain_gestaltdb/_compat.py:call_upstream(graph, name, ...)`:

- Prefers facade when present (0.12), else service (0.17+).
- Maps renames: `explain_claim→rules.explain`, `run_rules→rules.run`, `maintain_truth→rules.maintain`, `read_view→temporal.open_read_view`.
- `service_layout()` returns `facade`/`services` for diagnostics.

Wired in G17-01: `claims.py` (status/search/explanation), `claim_writes.py` (assert/correct/retract).
Verified:
- `pytest` 210 passed on 0.12.0.
- Isolated 0.17.0 smoke: `assert_claim` → `iter_claims_as_of` → `explain` round-trip OK via bridge.

## Remaining for later G17s

- Temporal facades (`get_*_as_of`, `iter_*_as_of`, `read_view`) still facade-only in code; bridge mapping exists but no tools yet (G17-08, G17-04).
- Rules/modal (`create_rule/run/maintain/entails`) mapping exists, no tools yet (G17-09, G17-10).
- `pyproject` pin unchanged in G17-01; version bump is G17-12 after full matrix passes.
- TD-01 (typing): 0.17 ships more typing; revisit override after bump. TD-02 (bulk JSON): 0.12.1 fixed #109; re-benchmark on 0.17 before enabling fast path (G17-06). TD-03 (AST coupling): `cypher_ast` names stable 0.12→0.17; still version-coupled, needs upstream classifier watch.

## Upgrade policy (pinned minor, G17-16)

- The `gestaltdb` minor stays pinned (`>=0.12.0,<0.13` in `pyproject.toml`)
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
# 0.12 suite (worktree)
uv run --project <worktree> --locked pytest -q
# 0.17 smoke (isolated, worktree on PYTHONPATH)
PYTHONPATH=<worktree> uv run --isolated --python 3.12 \
  --with 'gestaltdb[rocksdb]==0.17.0' \
  --with 'langchain-core>=1.0,<2' --with 'pydantic>=2.7,<3' \
  python -c "from langchain_gestaltdb._compat import service_layout; ..."
```
