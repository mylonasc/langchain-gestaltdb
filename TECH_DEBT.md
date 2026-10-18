# Technical debt register

Track improvements intentionally deferred to deliver the first integration here.
Never waive security, data-integrity, or failing-test blockers. Roadmap features
stay in `PROJECT_BOOTSTRAP_EPIC.md` instead.

## Workflow

1. Allocate the next permanent `TD-NN` ID and open a GitHub `tech-debt` issue
   using the template before accepting a shortcut.
2. Record area, impact, reason, safe interim approach, owner, revisit trigger,
   measurable exit criteria, and introducing PR. Mention the debt in that PR.
3. If the interim approach cannot be safe/tested, fix it now or block the feature.
4. Review debt during BST-08, dependency upgrades, and release planning. Resolve
   through a tested PR; retain IDs/history and link the resolution.

## Open items

### TD-01 — Untyped upstream boundary / P2 / open

- Issue: [#15](https://github.com/mylonasc/langchain-gestaltdb/issues/15).
- Area: `pyproject.toml` scoped mypy override for `gestaltdb.*`.
- A second narrow override skips NumPy's 3.12-syntax stubs when checking our
  Python 3.10 target. NumPy is only transitive and not part of our public API.
- Impact: static checking cannot verify upstream method signatures.
- Reason: GestaltDB 0.12.0 does not ship a typed package contract.
- Interim approach: strict typing locally, real installed-release tests for
  lifecycle, parameters, entities/temporal values and persistence. No global
  missing-import or untyped-call suppression.
- Owner: integration maintainers. Revisit on the next GestaltDB minor upgrade.
- Exit: upstream typing or narrow maintained stubs adopted; remove the override
  and pass strict mypy and integration tests.
- Introduced with: BST-03 graph adapter PR (linked in #15).

### TD-02 — JSON bulk edge writes / P2 / open

- Issue: [#16](https://github.com/mylonasc/langchain-gestaltdb/issues/16).
- Area: graph-document ingestion and explicit Cypher relationship writes.
- Impact: GestaltDB 0.12.0 LMDB/JSON bulk writes can raise `TypeError: Object of
  type bytes is not JSON serializable` while serializing adjacency records.
- Reason: upstream optimization/compatibility issue; do not patch database internals.
- Interim approach: tested incremental `put_edge` ingestion, no whole-import
  atomicity promise. Low-level queries propagate upstream errors; agents default
  to read-only. Do not advertise arbitrary Cypher writes as safe or atomic.
- Owner: integration maintainers. Revisit on upgrades or measured ingestion bottlenecks.
- Exit: verify an upstream fix, test JSON bulk writes across supported backends,
  then benchmark before adopting the optimized path.
- Discovered during: BST-03 installed-release probes; regression coverage belongs
  with BST-05 ingestion. Related PR linked in #16.

### TD-03 — AST policy coupling / P2 / open

- Issue: [#20](https://github.com/mylonasc/langchain-gestaltdb/issues/20).
- Area: `_read_only.py`, supported GestaltDB minor-version dependency bound.
- Impact: upstream AST changes need policy review; no public read-only classifier exists.
- Reason: an upstream API proposal is outside this integration's first delivery.
- Interim approach: isolated positive clause/root allowlists, recursive subquery
  and UNION inspection, unknown-syntax rejection, no procedures by default, and
  state-unchanged adversarial tests. Never call private `_plan_has_writes`.
- Owner: integration maintainers. Revisit on every GestaltDB upgrade.
- Exit: verify a public upstream classifier against the adversarial suite before
  replacing the isolated adapter. Write prevention itself is not deferred.
