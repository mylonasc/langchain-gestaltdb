# langchain-gestaltdb

[![Coverage](https://img.shields.io/badge/coverage-99%25-brightgreen)](https://github.com/mylonasc/langchain-gestaltdb/actions/workflows/ci.yml)

<!-- Snapshot value; CI enforces >=90% package coverage in the Python matrix.
     Automating this badge is tracked as TD-05 in TECH_DEBT.md. -->

A graph-first LangChain integration for [GestaltDB](https://github.com/mylonasc/gestaltdb):
embedded graph lifecycle, parameterized Cypher, bounded schema discovery,
graph-document ingestion, read-only-default agent tools, bounded typed-graph
retrieval, bitemporal claim and rule tools, and offline visualization artifacts.
Requires `gestaltdb>=0.17.0,<0.18` and Python 3.10–3.13. See
[the API contract](docs/API.md), [the agent workflow](docs/AGENT_WORKFLOW.md),
and [the deployment limits](docs/LIMITS.md).

```bash
pip install 'langchain-gestaltdb==0.2.0'
```

```python
from langchain_gestaltdb import GestaltDBGraph

with GestaltDBGraph.create("my-graph") as graph:  # default backend: PyRex/RocksDB
    graph.query("CREATE (n:Person {id: $id}) RETURN n", {"id": "alice"})
    rows = graph.query_read_only("MATCH (n:Person) WHERE n.id = $id RETURN n", {"id": "alice"})
```

`create(path)` selects the persisted backend (`pyrex` by default; a plain
install includes `pyrex-rocksdb`). `open(path)` reopens with the persisted
manifest. `lmdb` and `leveldb` are optional extras requiring an explicit
`backend=` override; missing backends fail explicitly, never fall back.
Closing a borrowed wrapper leaves the original database open. Queries return
JSON-compatible dictionaries; temporal values use tagged exact forms.
`query()` permits writes and propagates upstream errors unchanged: do not hand
it to an untrusted agent. Transaction/snapshot guarantees vary by backend;
there is no native async or execution timeout.

## Agent toolkit

```python
from langchain_gestaltdb import GestaltDBToolkit

# Inside a GestaltDBGraph context; tools borrow the wrapper.
schema_tool, query_tool = GestaltDBToolkit(graph=graph).get_tools()
schema_tool.invoke({"refresh": True})
query_tool.invoke({"query": "MATCH (n:Person) RETURN n.name AS name", "params": {}})
```

Tools are read-only by default with bounded output (100 rows / 16000 chars
unless the application lowers them). Agents cannot enable writes, enlarge
limits, or allowlist procedures; applications opt in explicitly
(`allow_writes=True`, `allowed_procedures=...`). Read-only prevents writes,
not expensive reads: isolate hostile workloads at the process level.

Related surfaces: `GestaltDBRetriever` (bounded seeded retrieval with
`MENTIONS` source links), `GestaltDBSamplingTool` (raw multi-hop typed
paths/subgraphs), `GestaltDBClaimToolkit` (four-valued claim status, search,
and provenance explanations over bitemporal claims — claims are sourced
assertions, not Cypher-visible edges), `GestaltDBClaimToolkit(allow_writes=True)`
(opt-in assert/correct/retract), `GestaltDBTemporalToolkit` (as-of reads with
pinned `read_view` horizons), `GestaltDBRuleToolkit` (Horn-rule read tools;
create/run/maintain are opt-in), `GestaltDBModalToolkit` (bounded entailment;
accessibility writes are opt-in), `GestaltDBVizTool` / `render_artifact`
(offline HTML/SVG summaries with node/edge caps), `advise_index` (read-only
index advice; agents never run DDL), and trusted-app bulk ingestion
(`ingest_polars` / `ingest_arrow`, chunked, no whole-import atomicity).

## Examples

No model, API key, database server, or network service is required:

```bash
uv run --locked python examples/graph_agent.py      # ingest -> schema -> tool call
uv run --locked python examples/claim_agent.py      # claim status / explanation
uv run --locked python examples/agent_workflow.py   # schema -> query/retrieve -> explain
uv run --locked python examples/claim_writes.py     # opt-in claim writes
```

## Development

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

The committed lockfile makes development reproducible; the ranges in
`pyproject.toml` describe the consumer contract. All tests run offline and
enforce at least 90% package coverage in the Python matrix. Upstream behavior
is defined by the installed `gestaltdb` release:

```bash
uv run --locked python -m gestaltdb.agent_docs list
uv run --locked python -m gestaltdb.agent_docs get cypher --examples
```

## Contributing and releases

Read [AGENTS.md](AGENTS.md) for branch, PR, CI, and release policy. Work goes
through issue-scoped branches and PRs; `main` is protected. Releases use
explicit maintainer approval and PyPI Trusted Publishing via `publish.yml`;
merging alone never publishes. See [release notes](CHANGELOG.md) and the
[release runbook](docs/RELEASING.md). This repository retains its Apache-2.0 license.
