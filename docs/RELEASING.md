# Release runbook

## Approved policy

Releases use stable `X.Y.Z` project versions, tags `vX.Y.Z`, and a manual
`.github/workflows/publish.yml` dispatch on **main only**. A merge, tag push or
GitHub Release does not upload packages. Each publication requires explicit
maintainer authorization and approval of the `pypi` GitHub environment.
The maintainer authorized the first release, **0.1.0 including BST-11 / PR #31**.
No later release is authorized by that one-off approval.
The maintainer subsequently authorized merging #35 and publishing the PyRex
default change as **0.1.1** (tracked in #36). This is another one-off approval;
later releases still need separate authorization.

The workflow never rebuilds: it downloads the wheel/sdist retained by the exact
main-push CI run, after installed-wheel tests passed, and requires that run's
latest attempt to have succeeded. It checks name/version/Python/license metadata,
public modules, typing marker, license and packaged release docs, rejecting stale
or additional files. The read-only preparation job records SHA-256 hashes.
Only the isolated publication job receives `id-token: write`; it runs no source
checkout, build or dependency-install code. PyPI receives provenance attestations.
Post-publication jobs compare PyPI hashes and install the actual public wheel
outside the checkout on Python 3.10–3.13, running the complete offline test suite.

## One-time setup

- PyPI project/pending publisher: owner `mylonasc`, repository
  `langchain-gestaltdb`, workflow filename **`publish.yml`**. No API token is used.
- The maintainer initially left PyPI's optional environment field blank; PyPI
  therefore does not require an environment name. The workflow still uses a
  protected GitHub environment named `pypi`. Restrict it to main, set a maintainer
  as required reviewer and disallow administrator bypass. Solo-maintainer review
  allows the dispatcher to approve their own deployment; tighten when a second
  maintainer is available. Adding `pypi` to the PyPI publisher is recommended for
  defense in depth, but requires updating PyPI separately, not a new secret.
- Keep protected-main PRs, strict `CI gate` and administrator enforcement enabled.
  Never weaken protection to ship a release. Actions are SHA-pinned in the release
  workflow; review upstream changes deliberately when updating pins.

## Prepare and publish

1. Merge prerequisite feature PRs only after review and green CI. Update issue
   acceptance criteria after merge. Use an issue-scoped release PR from current main.
2. Change `project.version`, run `uv lock`, and review `uv.lock` (a version-only
   change should not upgrade dependencies). Add reviewed `CHANGELOG.md` notes.
3. Run the checks in `AGENTS.md`; verify public exports, compatible dependencies,
   backend limitations and examples. Check wheel/sdist metadata and contents:

   ```bash
   uv build --no-sources --out-dir /tmp/opencode/release-candidate
   uv run --locked twine check /tmp/opencode/release-candidate/*
   uv run --locked python scripts/release.py verify-artifacts X.Y.Z --directory /tmp/opencode/release-candidate
   ```

4. Merge the release PR only after its required CI gate passes. Wait for **main
   push CI** on the exact merge commit to pass and retain its distribution artifact.
   Artifacts expire after 30 days; rerun CI rather than building locally to publish.
5. With explicit maintainer release approval, dispatch:

   ```bash
   gh workflow run publish.yml --ref main -f version=X.Y.Z
   ```

   Review the exact commit, version and preparation hashes, then approve the
   pending `pypi` deployment. No environment bypass and no PyPI password/token.
6. Require the publish run, including public-index hashes and Python matrix, to
   succeed. Record the run URL and checksums. Verify https://pypi.org/project/langchain-gestaltdb/.
7. Only after publication and install verification, create `vX.Y.Z` on that exact
   main commit and a GitHub Release with the changelog and the same CI artifacts:

   ```bash
   gh release create vX.Y.Z --target EXACT_MAIN_SHA --title X.Y.Z --notes-file RELEASE_NOTES dist/*
   ```

   This is a separate maintainer step, so the OIDC publication job needs no
   repository write permission. Do not move an existing release tag.

## Staging decision and recovery

For 0.1.0, TestPyPI is intentionally not configured: no staging publisher was
requested. Exact-artifact offline/installed-wheel tests and metadata checks gate
the approved production upload. Do not add credentials or silently publish to
TestPyPI. A future staging policy needs maintainer approval and separate trust.

PyPI uploads are irreversible; deleting a tag or GitHub Release does **not** undo
an upload and filenames/versions cannot safely be reused. Duplicate versions fail
loudly (`skip-existing` is not enabled). On partial publication, inspect PyPI and
the recorded hashes before taking any recovery action; do not blindly rerun or
rebuild. Yanking a broken release and publishing a corrected **new version** needs
explicit maintainer approval. If upload succeeds but post-install checks fail,
report the release as published with failed verification, not as unpublished.

For propagation-only failures after a confirmed successful upload and matching
public hashes, rerun **failed verification jobs only** (`gh run rerun RUN_ID --failed`),
not the full workflow or a new dispatch. The metadata check allows up to five
minutes for cached PyPI JSON 404s; persistent absence/hash mismatches still fail.
This was exercised on 0.1.0: the initial Python 3.12 metadata check saw cached 404s,
and verification succeeded on attempt 2 without repeating the upload.

## First-release evidence

Version [0.1.0 on PyPI](https://pypi.org/project/langchain-gestaltdb/0.1.0/) and
[GitHub Release v0.1.0](https://github.com/mylonasc/langchain-gestaltdb/releases/tag/v0.1.0)
refer to exact main commit `6886984a64a5a8a6a656d9e5818b48d971d0c90e`.
[Main CI](https://github.com/mylonasc/langchain-gestaltdb/actions/runs/39160676776)
passed all eleven checks; the
[publishing run](https://github.com/mylonasc/langchain-gestaltdb/actions/runs/39160735008)
successfully uploaded with OIDC/attestations and verified the public wheel on
Python 3.10–3.13 (207 tests per interpreter). CI, PyPI and GitHub assets match:

```text
1a2aa6910b16a01e84f7e7d2323c2c47ca6851d965b4892c611b3b827952d76c  langchain_gestaltdb-0.1.0-py3-none-any.whl
e84ba7146a2213322fa624d829809fb8e160d018ae3cfe8cc5896ae98863ad0d  langchain_gestaltdb-0.1.0.tar.gz
```

## Design references (reviewed for BST-12)

- Maintained [Neo4j release workflow](https://github.com/langchain-ai/langchain-neo4j/blob/main/.github/workflows/_release.yml)
  and [Postgres release workflow](https://github.com/langchain-ai/langchain-postgres/blob/main/.github/workflows/_release.yml):
  separate build/publish jobs and transfer artifacts; do not copy their non-main
  override, paid-service tests or legacy attestations workaround.
- [PyPI publisher configuration](https://docs.pypi.org/trusted-publishers/adding-a-publisher/)
  and [PyPA publishing action](https://github.com/pypa/gh-action-pypi-publish):
  workflow identity, optional environment binding, job-scoped OIDC, and attestations.
