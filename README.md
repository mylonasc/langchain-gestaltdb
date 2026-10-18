# langchain-gestaltdb

A graph-first LangChain integration for [GestaltDB](https://github.com/mylonasc/gestaltdb).
**In development:** the graph adapter, schema, ingestion, and read-only-default
agent toolkit are available. See [the bootstrap epic](PROJECT_BOOTSTRAP_EPIC.md)
and the [technical debt register](TECH_DEBT.md).

```python
from langchain_gestaltdb import GestaltDBGraph

with GestaltDBGraph.create("my-graph", backend="lmdb") as graph:
    rows = graph.query("MATCH (n) WHERE n.id = $id RETURN n", {"id": "alice"})
```

Wrapping an existing `GraphDB` borrows its handle; create/open own their handles.
Closing a borrowed wrapper leaves the original database open. Queries return
JSON-compatible dictionaries, including native nodes/edges and paths. Temporal
values use a `$type` tag plus exact fields; unsupported objects fail explicitly.
Low-level queries permit writes and propagate upstream errors unchanged: do not
give this API directly to an untrusted agent. Backend transaction/snapshot
guarantees are unchanged; there is no native async or execution timeout.

For parsed-AST write prevention, call `graph.query_read_only(query, params)`.
It rejects mutations, schema changes, unknown syntax, and procedures by default,
including writes inside subqueries/UNION. Known read procedures may be explicitly
allowlisted by a trusted application. Query text is capped at 10000 characters.
Read-only is **not** protection against expensive reads: use process/resource
isolation for hostile workloads. Output caps are not execution timeouts.

### Schema discovery

`graph.get_schema` returns stable JSON text; `graph.get_structured_schema` returns
a defensive copy. Both lazily cache inspection; call `graph.refresh_schema()`
after external writes. Defaults inspect 100 canonical nodes and 100 edges, plus
up to two endpoint nodes per edge. This bypasses stale secondary indexes.

For different bounds, wrap the database with
`GestaltDBGraph(db, schema_node_limit=20, schema_edge_limit=20)`. Set a limit to
`None` only to explicitly allow exhaustive inspection, or supply a trusted
`schema_override` dictionary to avoid scans entirely. Sampling follows backend
key order, is not representative, and is not a coherent concurrent snapshot.
The schema includes truncation, property types, endpoint labels, index definitions,
and GestaltDB dialect guidance. It does not enforce a closed-world schema.

### Graph-document ingestion

```python
from langchain_core.documents import Document
from langchain_gestaltdb import GraphDocument, GraphNode, GraphRelationship

alice = GraphNode(id="alice", type="Person", properties={"name": "Alice"})
bob = GraphNode(id="bob", type="Person", properties={"name": "Bob"})
document = GraphDocument(
    nodes=[alice, bob],
    relationships=[GraphRelationship(source=alice, target=bob, type="KNOWS")],
    source=Document(page_content="Alice knows Bob", metadata={"path": "example.txt"}),
)
# Inside the graph context above:
# graph.add_graph_documents([document], include_source=True)
```

Equivalent mappings/Pydantic graph models can be passed without installing
Community or Neo4j. This is structural compatibility, not a promise to inherit
their graph-store classes. Node labels union and incoming properties shallow-merge
over existing properties. Ordinary string IDs are unchanged; integer IDs use
`__lc_int__:<integer>`, and strings starting `__lc_` are escaped to a hashed
`__lc_str__:` key, preventing integer/string/internal-source collisions.

Default relationship identity hashes endpoints, type, and normalized properties:
exact repeats collapse; distinct properties produce parallel edges. Supply
`GraphRelationship.id` to update properties of a particular edge or preserve
otherwise identical parallel edges. Explicit IDs cannot change endpoints/type.
Relationship `properties['type']` must agree with the declared type if supplied.

With `include_source=True`, each source becomes a `__LangChainSource__` node
with content/metadata and outgoing `MENTIONS` links. `Document.id` controls source
identity when supplied; otherwise content plus metadata is hashed. Sources are
not epistemic claims. Input is prevalidated, but writes are incremental and
**not whole-import atomic**: backend failures can leave partial data. The schema
cache is invalidated. JSON-safe property conversion follows query normalization.

### Agent toolkit

```python
from langchain_gestaltdb import GestaltDBToolkit

# Use inside a GestaltDBGraph context; toolkit/tools borrow the wrapper.
# schema_tool, query_tool = GestaltDBToolkit(graph=graph).get_tools()
# schema_tool.invoke({"refresh": True})
# query_tool.invoke({"query": "MATCH (n:Person) RETURN n.name AS name", "params": {}})
```

Tools implement LangChain `BaseTool` and accept tool-call messages. Query output
is JSON with `records`, `returned_rows`, and `truncated`. Defaults cap output at
100 rows/16000 characters; application configuration can lower those values.
An oversized row is omitted whole, preserving valid JSON. Oversized schema
returns an actionable error rather than silently dropping schema fields.

Only `query` and `params` are query inputs. Agents cannot enable writes, enlarge
limits, choose procedure permissions, or replace the graph via tool arguments.
Applications can explicitly construct `GestaltDBToolkit(graph=graph, allow_writes=True)`;
that grants write capability and propagates upstream limitations (including TD-02).
Known read procedures may be explicitly enabled with `allowed_procedures`.
Tool-facing backend errors are redacted; low-level graph errors still propagate.
Treat graph/source text as untrusted data, not instructions. Read-only prevents
writes but does not bound execution cost, intermediate memory, or result collection.
LangChain's async tool adapter offloads synchronous work; no native async is claimed.

## Development

Run the complete offline ingest → schema → tool-call → source-evidence example:

```bash
uv run --locked python examples/graph_agent.py
```

No model, API key, database server, or network service is required. The scripted
tool call demonstrates the same `ToolMessage` dispatch used by LangChain agents;
it is not an LLM reasoning benchmark. See [the API contract](docs/API.md).

Use Python 3.10–3.13 (3.12 recommended) and [uv](https://docs.astral.sh/uv/):

```bash
uv sync --python 3.12 --locked --group dev --extra lmdb
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked mypy
uv run --locked pytest
uv build --no-sources
uv run --locked twine check dist/*
```

The committed lockfile makes development reproducible; the dependency ranges
in `pyproject.toml` describe the library's consumer contract. CI also exercises
minimum/latest direct dependencies, real deferred-index behavior, and additional
LevelDB/PyRex backend contracts on Linux/Python 3.12. All tests run offline and
enforce at least 90% package coverage in the Python matrix.

GestaltDB's base install has **no storage backend**. Development uses the `lmdb`
extra and JSON serialization explicitly; `leveldb` and `rocksdb` extras are
available for future backend coverage. There is no silent backend fallback.

## Agent-oriented upstream documentation

```bash
uv run --locked python -m gestaltdb.agent_docs list
uv run --locked python -m gestaltdb.agent_docs get quickstart
uv run --locked python -m gestaltdb.agent_docs get cypher --examples
uv run --locked python -m gestaltdb.agent_docs search sample_typed_subgraph
```

## Contributing and releases

Read [AGENTS.md](AGENTS.md) for branch, PR, CI, and release policy. Work goes
through issue-scoped branches and PRs; `main` is protected. Publishing automation
is deliberately deferred. This repository retains its Apache-2.0 license.

## Scaffold provenance

The bootstrap was informed by the `langchain-cli==0.0.37` integration template,
generated selectively with Python 3.12 into a disposable staging directory.
The old Poetry/Core 0.3 configuration was replaced with PEP 621, uv, Hatchling,
and Core 1.x. No provider stubs or template license were copied into this project.
