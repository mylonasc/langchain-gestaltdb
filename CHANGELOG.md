# Changelog

## Unreleased

- Install `pyrex-rocksdb` by default through `gestaltdb[rocksdb]` and default
  `GestaltDBGraph.create(path)` to backend `pyrex`. Default offline examples and
  plain-wheel installation checks now exercise PyRex/RocksDB.
- Preserve explicit LMDB/LevelDB overrides and the existing `rocksdb` extra;
  reopening continues to use the persisted manifest, with no automatic migration
  or fallback. Existing backend-specific transaction/snapshot limits still apply.
- Add LMDB to the required cross-backend contract matrix alongside LevelDB/PyRex.
  This change is not included in published 0.1.0; no new release is authorized.

## [0.1.0] - 2026-10-18

First public release of the Apache-2.0 LangChain integration for GestaltDB.

- Embedded graph lifecycle, parameterized Cypher, bounded schema discovery,
  graph-document ingestion and source provenance.
- Fail-closed read-only query validation, bounded query/schema tools, and an
  application-controlled toolkit with explicit write opt-in.
- Bounded typed-graph retrieval with indexed/external seeds, source-linked
  Documents, deterministic output ordering and explicit truncation metadata.
- Four-valued historical claim status, filtered claim search, and bounded
  provenance explanations; claims are separate from Cypher-visible graph edges.
- Explicitly enabled assertion, correction and retraction tools with validated
  provenance/validity, historical identity preservation and bounded commit receipts.
- Tested on Python 3.10–3.13 with minimum/latest direct dependencies, installed
  wheels, LMDB, LevelDB and PyRex contracts, and offline agent-style examples.
- Manual, environment-approved PyPI Trusted Publishing via `publish.yml`, using
  exact-main CI artifacts, attestations and post-publication installation tests.

### Compatibility and limitations

Requires `gestaltdb>=0.12.0,<0.13`, `langchain-core>=1.0,<2`, and
`pydantic>=2.7,<3`. Explicit backend extras: `lmdb`, `leveldb`, `rocksdb` (PyRex).
Backend guarantees vary; no universal atomicity/snapshot, native async, native
vector store or computational timeout is claimed. Read-only tools are not a
multitenant sandbox. See `docs/API.md` and `TECH_DEBT.md` before deployment.
