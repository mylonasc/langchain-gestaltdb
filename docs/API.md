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
| `ReadOnlyQueryError` | Query could not be proven read-only |

`GraphDocument.from_external(model_or_mapping)` validates equivalent fields
without installing Community or Neo4j. This is not inheritance/chain compatibility.

## Graph lifecycle and operations

- `GestaltDBGraph(db, owns_graph=False, schema_node_limit=100,
  schema_edge_limit=100, schema_override=None)` wraps an existing database.
- `GestaltDBGraph.create(path, backend=..., serializer="json", **database_options)`
  creates an owned manifest-backed database. A backend must be installed/selected.
- `GestaltDBGraph.open(path, **database_options)` opens an owned database using
  its persisted manifest. `close()` and context management release owned handles.
- `graph` accesses the raw database only while the wrapper is open.
- `query(query, params=None, read_snapshot=None)` permits upstream reads/writes;
  backend/parser errors propagate unchanged.
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

## Tested platform and non-goals

CI exercises Python 3.10–3.13/LMDB, minimum and latest direct dependencies,
installed wheels, plus LevelDB and PyRex/RocksDB contract tests on Linux/Python 3.12.
The standard tool-suite baseline is pinned to remain compatible with Core 1.0.
No external services/model credentials are required.

Retrievers, claim-aware tools, vector stores, chat history, QA chains, native
async, and publishing automation are not part of this first integration.
See BST-09 onward for approved follow-on scope. This version remains unreleased.
