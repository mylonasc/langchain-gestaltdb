# Deployment limits and isolation (TD-04)

Canonical deployment limits for `langchain-gestaltdb`. Tool, toolkit, and
adapter documentation links here instead of repeating the full text; the
one-line fail-closed warnings next to each tool stay in place. Upstream
behavior is defined by the installed `gestaltdb` release (pinned
`gestaltdb>=0.17.0,<0.18`; see `docs/COMPAT_0_17.md`); this page states what
this integration does and does not promise on top of it.

## Output caps bound returned bytes, not work

Every bounded surface in this package caps the **returned output**:

| Surface | Caps (application-controlled) | Envelope |
| --- | --- | --- |
| `GestaltDBQueryTool` | `max_rows` (default 100), `max_chars` (default 16000) | `{records, returned_rows, truncated}`; oversized rows omitted whole, never clipped mid-row |
| `GestaltDBSchemaTool` | `max_chars` | oversized schema returns an actionable error, never silently dropped fields |
| `GestaltDBRetriever` | `max_seeds`, `max_paths`, `max_entities`, `max_content_chars`, `max_sources_per_entity` | `result_truncated` (run-level), `content_truncated` / `sources_truncated` (per-document), `truncated` (union); sampling is intentional selection, not truncation |
| Claim read tools | `max_evidence`, `max_claims`, `max_depth`, `max_nodes`, `max_edges`, `max_chars` | `truncated: true` flags; status is computed before evidence truncation; metadata that cannot fit yields a bounded JSON `error`, never clipped identities |
| Claim write tools | `max_chars` | success receipt with `version_id`/`logical_id` and `truncated: true` when the payload cannot fit; the commit already happened (see below) |
| `GestaltDBSamplingTool` | `max_paths`, `max_nodes`, `max_edges`, `max_chars` | `returned_paths` / `returned_nodes` / `returned_edges` plus one shared `truncated` flag; sampling is intentional selection, not truncation |
| Temporal as-of tools | `max_entities`, `max_chars` | bounded version/scan lists with `truncated`; one entity per call for `*_as_of` |
| Rule tools | `max_rules`, `max_versions`, `max_iterations`, `max_derivations`, `max_justifications`, `max_chars` | bounded rule/version lists; limit errors fail closed with no partial conclusions; unchanged repeat runs are idempotent |
| Modal entailment | `max_depth`, `max_states`, `max_chars` | bounded `explanation`; bound exhaustion fails closed with a redacted error, never a partial verdict |
| Visualization (`GestaltDBVizTool` / `render_artifact`) | `max_nodes` (default 2000), `max_edges` (default 5000), `max_chars` | summary JSON only (`path`, counts, truncation, banner); caps truncate deterministically with a warning, and payloads beyond 2x caps raise an error instead of freezing the viewer |
| Index advisor | `max_chars` | one bounded advice record; read-only, never runs DDL |
| Bulk ingestion (`ingest_polars` / `ingest_arrow`) | `chunk_size`, `index_mode` | trusted-app only; returns counts and stale-index families, no whole-import atomicity and no agent output cap |

Caps never bound **execution time, intermediate memory, or database-side
result materialization**. A query that matches millions of rows still matches
them before output formatting drops all but the first `max_rows`. Read-only
validation (`query_read_only`, tool default) is write prevention, not a
computational budget: it rejects mutations, schema changes, unknown syntax,
and non-allowlisted procedures, but an expensive read passes validation and
runs. Query text is capped at 10000 characters and AST nesting at 32 levels;
those are parser input bounds, not execution budgets.

Consequences for deployment:

- Treat graph, source, and claim text as **untrusted data, never instructions**.
- Tool-facing backend errors are redacted to generic messages; low-level
  `GestaltDBGraph.query` still propagates upstream errors unchanged, so do not
  expose the low-level adapter as an arbitrary agent tool.
- Never retry a committed claim write merely because its payload was
  truncated: a receipt with `truncated: true` is a success. Reconcile stored
  state before retrying backend errors, whose outcomes may be uncertain. No
  universal rollback, idempotency, or latest-version compare-and-swap is
  promised; `supersedes_version_id` targets an exact version, not a live
  compare-and-swap. Nothing is ever deleted; fact validity (`valid`) stays
  distinct from system knowledge time (`system_time`).
- Agent inputs can never enlarge caps, enable writes, allowlist procedures, or
  replace the graph: those are constructor-controlled by the trusted
  application.

## Read-only is not isolation (TD-04)

There is **no execution timeout, no cancellable budget, and no multitenant
sandbox** in this integration. Upstream provides no cancellable
adapter-level execution budget, and interrupting a running read from another
thread would be unsound, so the integration does not attempt it. This is
recorded as [TD-04](../TECH_DEBT.md#td-04--cancellable-execution-budgets--p1--open):
documented trusted-application scope with required process/resource isolation
for hostile reads, revisited before any hosted or adversarial deployment.

Cooperative guidance for deployers (no behavior change; all enforcement is
outside this package):

1. Keep output caps small for untrusted agents; caps are the only in-adapter
   cost lever and they bound output only.
2. Run agent-driven reads in a **separate worker process** with OS memory/CPU
   limits, not in the serving process; a runaway read then exhausts the worker,
   not the service.
3. Give the worker its own database handle (close owned handles in `finally`;
   borrowed wrappers never outlive their owner) and reopen rather than reuse
   after killing a worker.
4. Prefer `query_read_only` with an explicit `allowed_procedures` allowlist
   (default: none) over the low-level `query` for any untrusted input.
5. Monitor and bound at the deployment layer (request timeouts, worker
   restarts); do not present output caps as timeouts to operators.

Exit criteria for TD-04 (unchanged): a cooperative cancellation or
process-isolated execution mode with tested budget exhaustion, handle cleanup,
persistence integrity, and measured limits. Until then, do not claim sandboxing.

## Backend snapshot differences

Transactions, snapshots, durability, and concurrency remain backend-specific;
the adapter never strengthens them. `read_snapshot` on `query` /
`query_read_only` only selects backend behavior and propagates unchanged:

| Backend | `read_snapshot=None` | `read_snapshot=True` | `read_snapshot=False` |
| --- | --- | --- | --- |
| LMDB | unified snapshot where supported | required; always supported, with verifiable MVCC provenance via `graph.current_read_view()` | opts out |
| Transactional PyRex (`backend_options={"transactional": True}`) | snapshot where supported | required; pins reads but provenance is not verifiable | opts out |
| Default (non-transactional) PyRex | best-effort reads | rejected with `NotImplementedError` | best-effort reads |
| LevelDB | best-effort reads | rejected with `NotImplementedError` | best-effort reads |

To pin several reads to one visible state, share one horizon explicitly:
`adapter.read_view(...)` pins a temporal commit horizon on both facade and
services layouts, while `adapter.graph.current_read_view()` pins mutable entities,
adjacency, and indexes to one backend snapshot where supported. Both are
context managers, must be closed via the `with` block, and inherit the backend
limits above. Lookup and traversal order is never guaranteed; compare ID sets,
and decode typed traversal byte IDs with `graph.key_to_string(...)` before
comparing with string IDs. There is no whole-graph enumeration API.

Backend selection is explicit and manifest-pinned: `create` defaults to
`pyrex` (plain install includes `pyrex-rocksdb`); `open` always follows the
persisted manifest, never the creation default. Missing backend extras fail
explicitly; the adapter never silently falls back to a different backend, and
reopening never migrates or reinterprets existing LMDB/LevelDB stores.

## Fail-closed index and catalog behavior

- Canonical inspection (schema sampling, entity reads) bypasses stale
  secondary indexes, but **indexed lookups fail closed while their family is
  stale**: rebuild with `graph.rebuild_deferred_indexes()` (trusted
  application only; the integration never silently rebuilds) before relying on
  index results. See `docs/AGENT_WORKFLOW.md` for the recovery sequence.
- Claim search without an indexed filter scans the full claim catalog,
  regardless of the output cap; `object` omitted or null means any object
  (null-literal filtering belongs to status, not search).
- Identity writes are guarded upstream: corrections/retractions with unknown
  or mismatched `supersedes_version_id` targets fail closed, and
  interval-only retractions are not exposed because 0.12.0 accepts orphan
  targets (upstream #111). Corrections use logical IDs to avoid the ignored
  overload keywords (upstream #110).

## Version and release posture

The `gestaltdb` minor is pinned (`>=0.17.0,<0.18`) and moves only through a
verified upgrade PR (probe matrix, TD-01/TD-02/TD-03 revisit, `agent_docs`
diff, reviewed `uv.lock`); `uv.lock` is committed and a version-only change
must not upgrade unrelated dependencies. Merging never authorizes
publication: releases publish from `main` only via the manual `publish.yml`
dispatch with `pypi` environment approval (see `docs/RELEASING.md`); the
integration branch never publishes.
