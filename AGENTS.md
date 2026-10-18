# Contributor and Agent Guide

## Scope and orientation

This is `langchain-gestaltdb`, not the GestaltDB database implementation.
Read `PROJECT_BOOTSTRAP_EPIC.md` and the linked issue before changing behavior.
The graph adapter is implemented incrementally; consult public exports and tests.
Do not implement unrelated roadmap items or modify upstream database internals
without authorization. Preserve the existing Apache-2.0 license.

Prefer installed upstream documentation over guessing API names:

```bash
uv run --locked python -m gestaltdb.agent_docs list
uv run --locked python -m gestaltdb.agent_docs get quickstart
uv run --locked python -m gestaltdb.agent_docs get cypher --examples
uv run --locked python -m gestaltdb.agent_docs search '<API name>'
```

Import `GraphDB`, `Node`, and `Edge` from `gestaltdb.graphdb`, not the package root.
Use explicit installed backend selection, JSON for simple fixtures, temporary
database paths, and `try/finally` to close owned handles. Claims are not ordinary
Cypher-visible graph edges. Never promise atomicity/snapshots across all backends.

## Branch and PR policy (GitHub's equivalent of an MR pipeline)

- Never push directly to `main`, including from automation or administrator sessions.
- Branch from current `origin/main`: `feat/BST-03-short-description`,
  `fix/BST-06-short-description`, or `docs/BST-08-short-description`.
  Bootstrap infrastructure uses `bootstrap/BST-01-project-foundation`.
- Use small, coherent commits; commit and push regularly when authorized. Do not
  commit unrelated user changes. Never force-push `main` or rewrite others' work.
- Open a PR targeting `main`, link the indexed issue and blocking issues/PRs,
  and fill out `.github/pull_request_template.md`. Use `Closes #N` only when the
  PR fully satisfies that issue; otherwise use `Refs #N`.
- Resolve prerequisite issues before merging dependent work. An exception for
  jointly delivered bootstrap issues must be explicit in the PR.
- Required branch protection: PRs, strict/up-to-date **`CI gate`** status,
  resolved conversations, no force-pushes/deletion, and administrator enforcement.
- Solo-maintainer policy: zero mandatory approvals, because authors cannot
  approve their own PRs. Request independent review where available. Raise the
  required count to one when a second active maintainer is available.
- Squash-merge only after CI passes and conversations are resolved; delete the
  merged branch. Agents must not merge PRs without explicit authorization.
- Update epic checkboxes and issue acceptance criteria when work is merged,
  not merely when local code exists. Keep dependency links bidirectional.

## Technical debt

Use `TECH_DEBT.md` and the GitHub `tech-debt` issue template for worthwhile work
that must wait. Allocate stable `TD-NN` IDs; record impact, owner, safe workaround,
reason for deferral, revisit trigger, introducing PR, and testable exit criteria.
Review debt during BST-08 and dependency upgrades. Never defer security,
data-integrity requirements, or failing required tests as technical debt.

## CI and local validation

Use Python 3.10–3.13; prefer 3.12 locally. The host's default Python may be outside
this range. Do not loosen the declared range simply to use that interpreter.

```bash
uv sync --python 3.12 --locked --group dev --extra lmdb
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked mypy
uv run --locked pytest
uv build --no-sources
uv run --locked twine check dist/*
```

Commit `uv.lock`. For intentional dependency changes, regenerate it with `uv lock`,
review the diff, and rerun checks. CI runs on every PR to main and every main push,
with no path filters that could leave the required gate pending. It includes:

1. Ruff lint/format and strict package typing.
2. Real embedded-database tests on Python 3.10, 3.11, 3.12, and 3.13.
3. A fresh lowest-direct-dependency resolution on Python 3.10.
4. Wheel/sdist build and metadata validation, plus wheel-installed tests outside
   the source checkout.
5. A stable `CI gate` that fails for failed, skipped, or cancelled prerequisites.

Use offline fixtures and fake models; never require paid model credentials in
required CI. Add applicable `langchain-tests` suites as real components arrive.
Tests must actually collect and run; do not hide missing backends with blanket skips.
Optional backend jobs may have their own explicit availability policy later.

CI uses read-only repository permissions and no publishing secrets. Never run
untrusted PR code via `pull_request_target` or grant write permissions to test jobs.
Do not rename `CI gate` without coordinating the protected-main status context.
The YAML gate's check-run name is `CI gate`, not `CI / CI gate` in the API.
Its protected-main requirement is bound to the GitHub Actions app (ID 15368),
not an arbitrary status publisher.

## Release policy — publishing pipeline deferred

- No automatic releases, tags, GitHub Releases, PyPI uploads, or TestPyPI uploads
  are authorized by merging code. Do not run `twine upload` or `uv publish`.
- `0.1.0.dev0` is an unreleased bootstrap version. Version changes require a PR;
  release version/tag conventions and the publication trigger are not finalized.
- BST-12 tracks release automation. Inspect maintained LangChain partner-package
  workflows before adapting one; the legacy CLI scaffold is not a release policy.
- A future workflow should reuse successful CI/build validation, use PyPI Trusted
  Publishing/OIDC with a protected environment and least privilege, and require
  explicit maintainer approval. Do not add credentials in advance.
- Before any explicitly authorized release, verify the exact main commit passed
  CI, dependency compatibility and examples are documented, wheel/sdist metadata
  and packaged files are correct, and release notes/version changes are reviewed.
- After publication, verify the installed wheel in a clean environment against
  the declared Python range. Never assume deleting a tag reverses a PyPI release.
