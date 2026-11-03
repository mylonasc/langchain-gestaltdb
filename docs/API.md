# API and compatibility contract

## Public exports

Import from `langchain_gestaltdb`:

| Export | Purpose |
| --- | --- |
| `GestaltDBGraph` | Embedded graph lifecycle, query, schema and ingestion adapter |
| `GraphNode` | Strict string/integer entity ID, type/label, properties |
| `GraphRelationship` | Source/target, type, properties, optional explicit edge ID |
| `GraphDocument` | Nodes, relationships, optional LangChain `Document` source |
| `GestaltDBSchemaTool` | Cached/sampled schema and bounded refresh |
| `GestaltDBQueryTool` | Read-only-default parameterized Cypher tool |
| `GestaltDBToolkit` | Consistently configured schema/query tools |
| `GestaltDBRetriever` | Bounded typed-graph `BaseRetriever` over seeds |
| `RetrievalHop` | One typed traversal hop (edge type, direction, fanout) |
| `GestaltDBClaimStatusTool` | Read-only four-valued claim status with bounded evidence |
| `GestaltDBClaimSearchTool` | Read-only indexed historical claim search |
| `GestaltDBClaimExplanationTool` | Bounded claim/rule derivation graph with truncation |
| `GestaltDBClaimToolkit` | Consistently configured claim tools (read-only by default) |
| `GestaltDBClaimAssertTool` | Opt-in append-only sourced assertion/denial |
| `GestaltDBClaimCorrectTool` | Opt-in append-only claim correction |
| `GestaltDBClaimRetractTool` | Opt-in append-only claim retraction |
| `GestaltDBEntailsTool` | Read-only bounded modal entailment with four-valued status |
| `GestaltDBWorldAccessibilityTool` | Opt-in append-only world-accessibility assertion |
| `GestaltDBModalToolkit` | Consistently configured modal tools (read-only by default) |
| `GestaltDBVizTool` | Opt-in offline visualization artifact tool (bounded summary) |
| `render_artifact` | Trusted-app offline HTML/SVG artifact helper (bounded summary) |
| `VizDependencyError` | Packaged `gestaltdb.viz` module is unavailable |
| `ReadOnlyQueryError` | Query could not be proven read-only |

`GraphDocument.from_external(model_or_mapping)` validates equivalent fields
without installing Community or Neo4j. This is not inheritance/chain compatibility.

## Graph lifecycle and operations

- `GestaltDBGraph(db, owns_graph=False, schema_node_limit=100,
  schema_edge_limit=100, schema_override=None)` wraps an existing database.
- `GestaltDBGraph.create(path, backend="pyrex", serializer="json", **database_options)`
  creates an owned manifest-backed database. The default is PyRex/RocksDB and a
  plain installation includes `pyrex-rocksdb` through `gestaltdb[rocksdb]`.
  Explicit `backend="lmdb"`/`"leveldb"` overrides require their optional extras;
  missing backends fail explicitly, never fall back to a different backend.
- `GestaltDBGraph.open(path, **database_options)` opens an owned database using
  its persisted manifest. `close()` and context management release owned handles.
  Opening existing stores is unaffected by the creation default; no migration
  from LMDB or LevelDB is performed.
- `graph` accesses the raw database only while the wrapper is open.
- `manifest` / `database_id` are read-only inspection properties; `index_statistics()`
  reports persisted property-index definitions and `stale_indexes()` reports sorted
  families needing `graph.rebuild_deferred_indexes()`. All four pass through to the
  underlying handle and fail closed once the adapter is closed. No raw write
  transaction is exposed; pin several reads with `adapter.graph.read_view(...)`
  (temporal horizon) or `adapter.graph.current_read_view()` (backend snapshot).
- `query(query, params=None, read_snapshot=None)` permits upstream reads/writes;
  backend/parser errors propagate unchanged. `read_snapshot=None` snapshots
  automatically where supported, `True` requires one, `False` opts out. LMDB
  snapshots verifiably (MVCC); transactional PyRex (`backend_options=
  {"transactional": True}`) pins reads without verifiable provenance; LevelDB and
  default PyRex reject explicit snapshots with `NotImplementedError`.
- `query_read_only(query, params=None, allowed_procedures=(), read_snapshot=None)`
  recursively validates before execution. Query length <=10000, AST nesting <=32.
- `get_schema` / `get_structured_schema` are cached text/dictionary properties;
  `refresh_schema()` reloads canonical entities or re-copies the trusted override.
- `add_graph_documents(iterable, include_source=False)` prevalidates, merges
  incrementally and invalidates the schema cache. No whole-import transaction.

Schema defaults scan at most 100 node records and 100 edge records, plus at most
200 endpoint nodes. One extra key per scan detects truncation. Explicit None
limits permit exhaustive scans. Sampling is key-ordered, not representative or
concurrently snapshot-consistent. Canonical schema reads bypass stale secondary
indexes; indexed queries still require `graph.graph.rebuild_deferred_indexes()`
after an external deferred import. The integration never silently rebuilds indexes.

## Identity and normalization

Ordinary string entity IDs are preserved. Integers and strings in the reserved
`__lc_` namespace are encoded separately. Nodes shallow-merge incoming properties
and union labels. Default edge identity includes normalized properties; provide
an explicit edge ID for property updates or identical parallel edges. Explicit
IDs cannot move an edge or change its type.

Sources use Document.id or content/metadata identity, retain evidence on
`__LangChainSource__` nodes, and link to entities with MENTIONS. These are ordinary
graph records, not temporal epistemic assertions. No claim authority is inferred.

Query values are copied recursively: nodes/edges become dictionaries; paths
become nodes/edges maps; UTF-8 byte IDs decode strictly; Python date/time values
become ISO strings; native temporal dataclasses retain exact fields and `$type`.
Non-finite floats, unsupported objects/map keys, cycles and depth >64 fail.
Ingestion properties use the same JSON-compatible normalization.

## Tools, limits, and authorization

Toolkit configuration (`graph`, `allow_writes=False`, `allowed_procedures=()`,
`max_rows=100`, `max_chars=16000`) is application-controlled. Tool inputs cannot
change permissions or caps. Query inputs are `query` and `params`; schema input
has optional `refresh=False`. Tool errors are redacted and handled as tool output
by default; set `handle_tool_error=False` to raise `ToolException` instead.

The write-prevention allowlist rejects unknown/mutating clauses, DDL, and
procedures by default, recursively including subqueries and UNION. Only known
read procedures (`pg.sample_typed_paths`, `kg.entails`) may be explicitly enabled.
Trusted construction with `allow_writes=True` removes write prevention. The
low-level wrapper and raw database are not safe to expose as arbitrary agent tools.

Query output is valid JSON with records/returned_rows/truncated. Oversized rows
are omitted whole. Schema output exceeding its cap returns an actionable error.
**Output caps do not limit execution time, intermediate memory, or database
result materialization.** Use resource/process isolation for hostile reads.
Do not treat graph/source content as trusted instructions.

Transactions, snapshots, durability and concurrency remain backend-specific.
Async BaseTool invocation is a thread adapter, not native async or a thread-safety
promise. JSON bulk relationship writes have an upstream limitation (TD-02);
ingestion deliberately uses incremental object writes. See `TECH_DEBT.md`.

Trusted apps can also bulk-load columnar tables with
`GestaltDBGraph.ingest_polars(node_df, edge_df, ...)` and
`GestaltDBGraph.ingest_arrow(node_ids, edge_ids, sources, targets, edge_types, ...)`.
These helpers validate frames/columns, accept `node_property_columns` /
`edge_property_columns`, `chunk_size`, and `index_mode` (`MAINTAIN`, `DEFER`,
`DEFER_REBUILD` via `gestaltdb.IndexMaintenanceMode` when available), invalidate
the schema cache, and return `nodes`/`edges`/`rebuilt_indexes`/`stale_indexes`
counts. They are not agent tools, promise no whole-import atomicity, and leave
`DEFER`ed indexes stale until the trusted app rebuilds them. CSV-built frames
need explicit numeric dtypes and one label list per row (bare label strings are
rejected). `polars`/`pyarrow` stay optional dependencies imported inside the call.

## Bounded graph retrieval

`GestaltDBRetriever(graph, hops=(), seed_ids=(), seed_label=None,
seed_property=None, seed_resolver=None, max_seeds=10, max_paths=50,
max_entities=100, max_content_chars=2000, include_sources=True,
max_sources_per_entity=5, rng_seed=0)` is a LangChain `BaseRetriever`.

- Exactly one seed strategy is required: `seed_ids`, a `seed_resolver`
  (`Callable[[str], Sequence[str]]`, e.g. an external vector-store lookup), or the
  indexed `seed_label` + `seed_property` pair. Indexed resolution passes the query
  string as an exact property value and therefore needs a pre-created
  `create_node_property_index`; non-string or fuzzy matches require a resolver.
- `hops` is an ordered tuple of `RetrievalHop(edge_type, direction=out, sample_size=10)`.
  `direction` is `out`/`in`/`any`; `edge_type` matches `properties['type']`.
- Output is one `Document` per distinct entity. Metadata includes `id`, `labels`,
  `properties`, normalized `paths` (`edge_id`, `source_id`, `target_id`,
  `edge_type`, `direction`), `relationship_types`, `hops`, `is_seed`, `sources`
  (`document_id` + metadata), and truncation flags. `page_content` is a deterministic
  entity rendering plus linked source text, capped by `max_content_chars`.

Caps: `len(hops) <= 8`, `1 <= sample_size <= 100`, plus `max_seeds`, `max_paths`,
`max_entities`, and `max_sources_per_entity`. Sampled IDs are decoded from bytes;
absent seeds are skipped and metadata stays JSON-safe. Ordering is deterministic
(seeds first, then sorted IDs) and is **not** a relevance ranking; no database
ordering is promised. The retriever only reads, never fabricates entities, never
writes, and never rebuilds indexes. Typed adjacency stays usable during deferred
edge ingest; indexed seeding fails closed on stale node-label/property indexes. Seeding
through an external vector store does not make this package a vector store.

`result_truncated` marks seed/path/entity caps across the run and is copied onto
every document. `content_truncated` and `sources_truncated` mark per-document
clipping; `truncated` is their union. Sampling is intentional selection, not
flagged truncation. Setting `max_sources_per_entity=0` disables source lookup.
Caps bound returned entities/content, not execution, intermediate memory or total
metadata size. Indexed seed candidates are consumed incrementally, with one extra
valid seed to detect truncation.

## Multi-hop sampling tool

`GestaltDBSamplingTool(graph, max_paths=50, max_nodes=1000, max_edges=2000,
max_chars=16000, rng_seed=0)` (`gestaltdb_sample_paths`) wraps
`sample_typed_paths`/`sample_typed_subgraph` over an ordered `SamplingPattern`.

- Agent input: `seed_ids` (explicit node IDs), `hops` (ordered
  `SamplingHopInput(edge_type, direction=out, sample_size=10)`, `direction`
  `out`/`in`/`any`), and `mode` (`paths` or `subgraph`). Caps and `rng_seed`
  are application-controlled.
- `paths` mode returns decoded `paths` (`seed` plus `edge_id`/`source_id`/
  `target_id`/`neighbor_id`/`edge_type`/`direction` steps) with
  `returned_paths` and `truncated`. `subgraph` mode additionally returns
  `nodes` (`id`/labels/properties) and `edges` (`id`/source/target/type/
  properties) with their own counts; all lists share one `truncated` flag via
  the bounded-JSON envelope.
- Use the retriever for seeded entity documents with source text; use this tool
  for raw multi-hop paths or a materialized subgraph without per-entity
  documents or source linking.

Caps: `len(hops) <= 8`, `1 <= sample_size <= 100`, plus `max_paths`,
`max_nodes`, `max_edges`, `max_chars`. Absent seeds are skipped and never
fabricated; output ordering is deterministic (sorted) and is **not** a
relevance ranking. The tool only reads, never writes, and never rebuilds
indexes; typed adjacency stays usable during deferred edge ingest. Caps bound
returned output, not execution or intermediate materialization.

## Claim (epistemic) tools

Claims are sourced assertions/denials in a bitemporal epistemic layer, distinct
from mutable graph edges: they create no current-state edges and never appear in
Cypher matching. `GestaltDBClaimToolkit(graph=graph)` returns three read-only
tools that call the epistemic API directly; the default toolkit exposes no write
path (writes require explicit opt-in, below).

- `gestaltdb_claim_status` reports `supported`/`refuted`/`both`/`unknown` with
  bounded supporting and refuting evidence for one statement.
- `gestaltdb_claim_search` lists historical claim versions by `statement_id`,
  subject, predicate, object (`object_kind` entity/literal), agent, source, world
  or polarity.
- `gestaltdb_claim_explanation` returns a bounded claim/rule derivation graph with
  premise/derivation edges, preserving historical version/logical ids and an
  explicit `truncated` flag. It is not a relevance ranking.

Agent inputs are limited to filters and bitemporal horizons (`valid_time`,
`system_time`, or `through_commit`). The graph handle and caps (`max_evidence`,
`max_claims`, `max_depth`, `max_nodes`, `max_edges`, `max_chars`) are
application-controlled; extra arguments are rejected. Serialized claim versions
keep statement/claim/logical/version ids, commit metadata, valid interval, system
time, operation, payload hash and provenance. As with query output, caps bound
returned JSON, not execution time, intermediate memory, or materialization.
Tool-facing claim errors are redacted to a single generic message.

Writing is explicitly opt-in. `GestaltDBClaimToolkit(graph=graph,
allow_writes=True)` exposes append-only `gestaltdb_claim_assert`,
`gestaltdb_claim_correct` and `gestaltdb_claim_retract`; the default toolkit never
includes them, and they can also be constructed directly as the opt-in. Assertions
require explicit `agent`, `polarity` and `valid_from`; corrections require a
`supersedes_version_id` plus an explicit change (confidence, provenance or
`valid_from`); retractions always require `supersedes_version_id` as well. Assertion
provenance is required JSON (explicit `{}` allowed). Agent authority and confidence
are never inferred. Validity strings must be timezone-aware ISO-8601 instants;
`valid_to` requires `valid_from` and is an exclusive end strictly after the start.
No delete path exists. Every write returns a success receipt and, when it fits, the appended version
with fact validity (`valid`) distinct from system knowledge time (`system_time`)
and a `supersedes_version_id` link; prior versions remain retrievable. Identity is
enforced upstream: a correction whose `claim_id` does not match the superseded
version's logical id fails closed. Retractions also reject unknown/mismatched
version targets; interval-only retractions are deliberately not exposed because
GestaltDB 0.12.0 accepts orphan targets ([upstream #111](https://github.com/mylonasc/gestaltdb/issues/111)).
Corrections use logical IDs to avoid the Claim-object overload's ignored keyword
arguments ([upstream #110](https://github.com/mylonasc/gestaltdb/issues/110)).

Successful write output includes `written: true`, `truncated: false`, and serialized
version fields. If the payload exceeds `max_chars` or full serialization fails,
return a bounded success receipt with `written: true`, `version_id`, `logical_id`,
and `truncated: true`. The commit has already happened: an output cap must never
turn it into an apparent failure. Reconcile backend-error outcomes before retrying;
no universal rollback, idempotency or latest-version compare-and-swap is promised.
Supplying identities does not authenticate an agent or authorize individual
claims; trusted application policy must govern tool exposure and inputs.
Correction `confidence=None`/`provenance=None` mean unchanged; clearing confidence
is not exposed. An explicit `provenance={}` replaces provenance with an empty map.

Read-tool status is computed from the same resolved claim versions as its evidence, before
output truncation. Character caps include the whole envelope; metadata that
cannot fit yields a bounded JSON `error` with `truncated: true`. Explanation
responses have the same keys whether found or missing (null identities and empty
lists for missing claims). Search treats omitted or null `object` as any object;
null-literal lookup is available through status instead. A search without indexed
filters scans the full catalog, regardless of the output cap.

Deferred claim writes (raw database API with `index_mode=DEFER`) make the
temporal index family stale; indexed claim reads then fail closed until the
trusted application calls `rebuild_temporal_indexes()`. The toolkits never
rebuild implicitly. Historical reads accept either `system_time` or
`through_commit` (an inclusive commit-ordinal prefix horizon; commit IDs are
never reused): prefer `through_commit` when replaying an exact write receipt
and `system_time` when aligning with external timestamps.

## Modal entailment tools

`GestaltDBModalToolkit(graph=graph)` returns the read-only
`gestaltdb_entails` tool; `allow_writes=True` additionally exposes the
opt-in append-only `gestaltdb_world_accessibility_assert` tool. Entailment
evaluates one modal formula (`BELIEVES`, `KNOWS`, `POSSIBLE` or `NECESSARY`,
case-insensitive) for one agent in one world at a valid/system time, in a
single pinned read view. It is the same bounded evaluator as
`CALL kg.entails(...) YIELD status, confidence, explanation` (which requires
`world` and `validTime`, with optional `systemTime`): the tool returns the same
four-valued `status` (`supported`/`refuted`/`both`/`unknown`), `confidence`
and evaluation `explanation`, so procedure output and tool output agree.

Frames are independent: `BELIEVES` follows `belief` links, `KNOWS` follows
`knowledge` links, and `POSSIBLE`/`NECESSARY` follow `modal` links. Missing
accessibility in the evaluated frame is `unknown`, never false, and
contradictions yield `both` without entailing unrelated atoms. Formulas accept
atoms plus `NOT`/`AND`/`OR` and nested modal operators. Bounds (`max_depth`,
`max_states`) are application-controlled at construction; extra agent arguments
are rejected, and bound exhaustion fails closed with a redacted error rather
than a partial verdict. Accessibility links are temporal claims with explicit
validity, so deferred external writes need the same trusted rebuild as
deferred claim writes before indexed reads.

## Visualization artifacts (opt-in)

`render_artifact(graph, mode, output_path, ...)` is the trusted-application
entry point for offline visualization. `mode` is `"nodes_edges"` (explicit
collections, no database needed), `"query"` (Cypher matches, enforced read-only
by the same fail-closed policy as the query tool) or `"sample"` (typed
subgraph sampling around seeds via `SamplingPattern`, the large-graph path).
`format` is `"html"` (self-contained offline viewer: the D3.js/React bundle
is prebuilt and packaged, no network, CDN, remote fonts, server or notebook
channel) or `"svg"` (dependency-free static `circle`/`grid` layout). The
artifact is written to the explicit `output_path` (suffix must match the
format); the returned value is bounded summary JSON with `path`, `format`,
`mode`, `node_count`/`edge_count`, `truncated`, `nodes_dropped`/`edges_dropped`,
`banner` and `warning`. Raw HTML/SVG is never returned to the agent.

Caps default to `VizOptions` (2000 nodes / 5000 edges) with absolute ceilings
at twice the caps. Cap truncation is deterministic, emits `TruncationWarning`
(captured into the summary) and renders an on-canvas capped-sample banner in
the viewer (mirrored as summary `banner` text and embedded in static SVG).
Payloads beyond the absolute ceiling raise an error naming the sampling
alternative instead of freezing the browser. Visualization imports stay lazy:
without the packaged `gestaltdb.viz` module, calls fail with
`VizDependencyError` and an actionable upgrade message.

`GestaltDBVizTool(graph=graph, output_dir=...)` exposes `nodes_edges`/`query`
modes to agents. The artifact directory is application-controlled; the agent
supplies only a basename (traversal rejected), and output is bounded by
`max_chars`. Sample mode needs caller-side seeds/pattern, so it stays in
`render_artifact`.

## Tested platform and non-goals

CI exercises Python 3.10–3.13 with the mandatory PyRex runtime and explicit LMDB
compatibility/snapshot tests, minimum and latest direct dependencies,
installed wheels, plus LMDB, LevelDB and PyRex/RocksDB contracts on Linux/Python 3.12.
The standard tool-suite baseline is pinned to remain compatible with Core 1.0.
No external services/model credentials are required.

Vector stores, chat history, QA chains and native async
are not implemented. Bounded typed-graph retrieval (BST-09) and read-only claim
tools (BST-10) and opt-in claim writes (BST-11) are additive follow-ons to the first
integration. Version 0.1.0 is the first public release; publishing requires explicit
approval through `publish.yml`. See `RELEASING.md` and the root changelog.

The PyRex plain-install/creation default was introduced in 0.1.1. Version 0.1.0
requires `langchain-gestaltdb[rocksdb]` plus explicit `backend="pyrex"`. Explicit
LMDB/LevelDB selection and manifest-based reopen remain supported in both versions.
