# Agent workflow: schema to explanation

Recommended agent sequence against a GestaltDB backend:

```text
schema -> index check -> query / sample / retrieve -> temporal / claim check -> explain
```

Conventions below hold for the 0.2.0 agent surface.
Runbook-only items have no agent tool by design and stay with the trusted
application. Upstream details are authoritative in the installed guides:

```bash
uv run --locked python -m gestaltdb.agent_docs list
uv run --locked python -m gestaltdb.agent_docs get quickstart
uv run --locked python -m gestaltdb.agent_docs get cypher --examples
uv run --locked python -m gestaltdb.agent_docs search '<API name>'
```

## 1. Discover the schema first

Call `gestaltdb_schema` (or `graph.get_structured_schema`) before planning
any query, traversal, or index advice. The schema is a bounded canonical
sample (default 100 nodes / 100 edges plus up to two endpoint nodes per
edge), not a closed-world contract: missing properties are not forbidden, key
order is not representative, and sampling is not a concurrent snapshot.
`refresh_schema()` (tool arg `refresh: true`) reloads after external writes;
pass a trusted `schema_override` to skip scans entirely, or explicit `None`
limits only to allow an exhaustive scan deliberately. Schema output includes
`DIALECT_GUIDANCE` (the supported-Cypher summary also embedded in
`langchain_gestaltdb/_schema.py`; refreshed on every upstream minor),
truncation flags, property types, endpoint labels, index definitions, and
temporal-index currency and constraint summaries. An oversized schema returns
an actionable error rather than silently dropping fields; raise the application
cap instead of assuming absent fields are absent properties.

## 2. Check indexes before indexed access

Indexed access requires an existing, current property index; canonical reads
bypass stale secondary indexes, but indexed lookups fail closed while stale.

- Entity seeding (`seed_label` + `seed_property` on the retriever, Cypher
  `WHERE` predicates the planner can reuse) needs
  `create_node_property_index(...)` / `create_edge_property_index(...)` to
  have run first. The read-only advisor (`advise_index` /
  `gestaltdb_index_advisor`; optionally attached to `GestaltDBToolkit` via
  `include_index_advisor=True`) reports which
  `create_*_property_index` call the trusted application should run, whether
  the index already exists (`index_statistics()`), and whether it is stale
  (`stale_indexes()`). Agents never run DDL: advice strings are instructions
  for the trusted application, not executed actions.
- After any deferred-ingest import (`index_mode="defer"` on the trusted-app
  bulk helpers `ingest_polars` / `ingest_arrow`, or direct upstream Arrow
  ingestion), indexed queries fail until the trusted
  application calls `graph.rebuild_deferred_indexes()`. The integration never
  silently rebuilds. Typed adjacency stays usable during deferred edge
  ingest, so sampling and canonical reads keep working while indexed seeding
  is stale.
- Stale-index recovery sequence: read `stale_indexes()` (also surfaced in
  schema metadata and advisor output) -> trusted app runs
  `rebuild_deferred_indexes()` -> retry the indexed lookup. For temporal
  indexes the same shape applies with `rebuild_temporal_indexes()` before
  bitemporal reads (G17-04/G17-08).

## 3. Decision tree: which tool answers the question?

```text
Need current-state graph facts?
  Single / few-hop entity neighborhood WITH source text? .... GestaltDBRetriever
  Raw multi-hop typed paths or materialized subgraph? ........ GestaltDBSamplingTool
  Arbitrary projection / aggregation / UNION / path pattern? . gestaltdb_query (parameterized Cypher)
  Schema shape / index / constraint inventory? ............... gestaltdb_query SHOW INDEX|INDEXES|CONSTRAINTS
  Modal question (believes / knows / possible / necessary)? .. gestaltdb_entails or allowlisted CALL kg.entails
Need belief-correctness, not current state?
  Is this statement supported / refuted right now? ........... gestaltdb_claim_status
  What was asserted, by whom, in which world / time? ......... gestaltdb_claim_search
  Why: derivation graph with rules and premises? .............. gestaltdb_claim_explanation
Need rules?
  Which rules exist / what does one say? ..................... gestaltdb_rule_list / gestaltdb_rule_get
  Create, run or maintain derived claims? .................... gestaltdb_rule_create/run/maintain (opt-in writes)
Need history?
  Entity / edge as of a bitemporal horizon? .................. FOR VALID_TIME / SYSTEM_TIME qualifiers
                                                               or gestaltdb_node_as_of / edge_as_of / temporal_scan
Need a picture for a human? .................................. GestaltDBVizTool or render_artifact (trusted app)
```

Rules of thumb:

- Use the **retriever** when the answer is "entities like X with evidence
  text": one `Document` per entity, `MENTIONS`-linked sources, deterministic
  non-ranking order. Use the **sampling tool** (`gestaltdb_sample_paths`,
  ordered `SamplingPattern` of `edge_type`/`direction`/`sample_size`) when
  the answer is "paths shaped like drug -> protein -> disease" or a bounded
  subgraph of node/edge records without per-entity documents. Use **Cypher**
  for projections, aggregates (`count/collect/sum/avg/min/max`, `DISTINCT`,
  `count(*)`), `UNWIND`, variable-length / shortest paths (legacy
  `*min..max` form; GQL quantifiers are rejected with a map-to-legacy hint),
  and correlated `CALL { ... }`.
- Use **claim tools** when the question is about sourced belief (support /
  refute / contradiction / provenance), not stored edges. Claims never appear
  in Cypher `MATCH` results; graph edges never carry epistemic status. Status
  first (one resolution, computed before truncation), then search, then
  explanation for the derivation graph.
- Use **`gestaltdb_entails`** (or the allowlisted `CALL kg.entails` procedure
  with required `world` and `validTime`, optional `systemTime`) for modal
  entailment over one pinned read view. World-accessibility writes stay opt-in
  with the trusted application (`GestaltDBModalToolkit(allow_writes=True)`).
- Use **rule tools** for Horn-rule lifecycle: `gestaltdb_rule_get` /
  `gestaltdb_rule_list` are read-only; `gestaltdb_rule_create` / `_run` /
  `_maintain` are opt-in trusted-application writes with application-controlled
  fixpoint bounds.
- Use **visualization** (`GestaltDBVizTool` for `nodes_edges`/`query`, or
  `render_artifact` for `sample` mode) as a trusted-app artifact step for
  humans, never as raw agent input: the agent receives a bounded summary plus
  the artifact path.

## 4. Seeding: exact match by default, fuzzy via resolver

- `seed_ids` (explicit node IDs) is always available on the retriever and the
  sampling tool; absent seeds are skipped, never fabricated.
- Indexed seeding (`seed_label` + `seed_property`) passes the query string as
  an **exact** property value and requires the property index to exist and be
  current (see §2). Non-string properties and fuzzy/typo-tolerant matching
  need a `seed_resolver` (`Callable[[str], Sequence[str]]`, e.g. an external
  vector-store lookup returning node IDs). External vector search may supply
  seeds without making this package a vector store.
- Cypher entity lookup uses `{id: $id}` parameter maps; bind untrusted values
  via `params`, never string interpolation.

## 5. Truncation envelopes

Every tool returns valid JSON with explicit truncation flags; **caps bound
returned output, not execution, memory, or materialization** (full detail in
`docs/LIMITS.md`):

- Query: `{records, returned_rows, truncated}`; oversized rows omitted whole.
- Retriever: `result_truncated` (seed/path/entity caps, copied onto every
  document), `content_truncated` / `sources_truncated` (per-document),
  `truncated` (union). Sampling is deliberate selection, not truncation.
- Sampling tool: `returned_paths` + `truncated`; `subgraph` mode adds bounded
  `nodes` / `edges` under one shared `truncated` flag.
- Claim reads: `truncated` flags plus bounded `error` payloads when even
  metadata cannot fit; status is resolved before truncation.
- Claim writes: a receipt with `version_id` / `logical_id` and
  `truncated: true` is still a committed write; never retry on truncation.

When `truncated` is true, narrow the question (filters, smaller hops,
tighter horizons) rather than re-issuing the same call.

## 6. Full-catalog scan cost

Two calls scan the whole catalog regardless of output caps; use them only
with explicit filters or bounded horizons:

- `gestaltdb_claim_search` with no indexed filter (every filter omitted or
  null) scans the full claim catalog. Always supply at least one of
  statement/subject/predicate/agent/source/world/polarity, or page through
  bounded horizons. (`object` omitted or null means any object; null-literal
  lookup belongs to status, not search.)
- Schema discovery with `None` limits (or an unbounded `SHOW`) walks the
  store; prefer the default bounded sample plus a trusted override.

## 7. Bitemporal reads (G17-04 policy, G17-08 tools)

`FOR VALID_TIME AS OF <datetime>` (plus optional `FOR SYSTEM_TIME AS OF`)
may follow `MATCH` / `OPTIONAL MATCH` and applies query-wide through chains,
paths, unions, and subqueries: one stable read view per query. System time
requires valid time in the same query, repeated values must agree, and any
temporal qualifier makes the whole query write-restricted. `versionId` /
`validFrom` / `validTo` / `systemFrom` are ordinary read projections (null
for null/unqualified current entities; `validTo` null for open intervals).
Temporal reads require current temporal indexes: rebuild deferred temporal
writes first or the read fails closed. Read-only validation enforces all of
the above, including inside subqueries and `UNION` branches. The dedicated
`gestaltdb_node_as_of`, `gestaltdb_edge_as_of` and `gestaltdb_temporal_scan`
tools expose bounded `get_*_as_of` / `iter_*_as_of` reads; for repeated reads
in trusted code, `adapter.read_view(valid_time=..., system_time=...,
through_commit=...)` pins one horizon on both supported layouts. Migration
(`migrate_time_indexed_edges`), orphan recovery (`list_temporal_orphans` /
`reclaim_temporal_orphans`), and serializer-matched reopen stay trusted-app
runbooks, never agent tools.

## 8. `read_snapshot` per backend

`read_snapshot` on `query` / `query_read_only` selects backend behavior only
(full matrix in `docs/LIMITS.md`): `None` snapshots automatically where
supported, `True` requires a snapshot, `False` opts out. LMDB always
supports snapshots (verifiable via `current_read_view()`); transactional
PyRex pins reads without verifiable provenance; LevelDB and default PyRex
raise `NotImplementedError` on explicit snapshots. To pin several reads to
one state, share one `adapter.read_view(...)` (temporal horizon) or
`adapter.graph.current_read_view()` (backend snapshot) context instead of
repeating `read_snapshot` queries.

## 9. CSV and bulk-ingestion pitfalls (trusted app)

Bulk loading (`ingest_polars` / `ingest_arrow`, plus upstream Arrow / Polars
bulk loads) is a trusted-app helper, never an agent tool:

- Declare explicit numeric dtypes when reading CSVs (e.g.
  `schema_overrides={"score": pl.Float64}`); CSV text otherwise loads as
  strings and poisons numeric indexes and predicates.
- Re-wrap a string `labels` column into one list per row; a bare string
  would be shredded into per-character labels.
- Choose `index_mode` deliberately: `MAINTAIN` (slower ingest, indexes
  current), `DEFER` (fast ingest, indexes stale until
  `rebuild_deferred_indexes()`; indexed lookups fail closed meanwhile), or
  `DEFER_REBUILD` (rebuild before returning; the default). Chunk large
  imports (`chunk_size`), expect partial imports on backend I/O failure (no
  whole-import atomicity; TD-02 context), and note the schema cache is
  invalidated on return.
