# langchain-gestaltdb

A graph-first LangChain integration for [GestaltDB](https://github.com/mylonasc/gestaltdb).
**In development:** the lifecycle/query adapter is available; schema, ingestion,
and tools are being implemented. See [the bootstrap epic](PROJECT_BOOTSTRAP_EPIC.md)
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

The committed lockfile makes development reproducible; the dependency ranges
in `pyproject.toml` describe the library's consumer contract. CI also exercises
minimum direct dependencies. No external services or model API keys are needed.

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
