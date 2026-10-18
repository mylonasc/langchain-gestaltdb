"""Fail-closed release checks. Standard library only; never uploads packages."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import tarfile
import urllib.error
import urllib.request
import zipfile
from email.parser import BytesParser
from pathlib import Path
from typing import Any

PROJECT = "langchain-gestaltdb"
REPOSITORY = "mylonasc/langchain-gestaltdb"


def request_json(url: str, token: str | None = None) -> dict[str, Any]:
    headers = {"Accept": "application/json", "User-Agent": "langchain-gestaltdb-release"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def select_ci_run(runs: list[dict[str, Any]], sha: str) -> dict[str, Any]:
    """Use the newest exact-main push run; never fall back past a failed rerun."""
    matching = [
        run
        for run in runs
        if (
            run["head_sha"] == sha
            and run["event"] == "push"
            and run["head_branch"] == "main"
            and run["path"] == ".github/workflows/ci.yml"
        )
    ]
    if not matching:
        raise ValueError("No main-push CI run exists for this exact commit")
    run = max(matching, key=lambda item: (item["run_number"], item["run_attempt"]))
    if run["status"] != "completed" or run["conclusion"] != "success":
        raise ValueError("The exact main commit has not passed its latest CI run")
    return run


def validate_metadata(payload: bytes, version: str) -> None:
    metadata = BytesParser().parsebytes(payload)
    if metadata["Name"] != PROJECT or metadata["Version"] != version:
        raise ValueError("Distribution name/version mismatch")
    if set(metadata["Requires-Python"].split(",")) != {">=3.10", "<3.14"}:
        raise ValueError("Unexpected Python compatibility metadata")
    if metadata["License-Expression"] != "Apache-2.0":
        raise ValueError("Unexpected license metadata")


def verify_artifacts(directory: Path, version: str) -> dict[str, str]:
    stem = f"langchain_gestaltdb-{version}"
    expected = {f"{stem}-py3-none-any.whl", f"{stem}.tar.gz"}
    # uv creates this housekeeping file; it is never an upload artifact.
    if {path.name for path in directory.iterdir() if path.name != ".gitignore"} != expected:
        raise ValueError("Expected exactly this version's wheel and sdist (no stale/extra files)")
    with zipfile.ZipFile(directory / f"{stem}-py3-none-any.whl") as wheel:
        prefix = f"{stem}.dist-info"
        validate_metadata(wheel.read(f"{prefix}/METADATA"), version)
        required = {"langchain_gestaltdb/py.typed", f"{prefix}/licenses/LICENSE"}
        required.update(
            f"langchain_gestaltdb/{name}.py"
            for name in (
                "__init__",
                "graph",
                "graph_document",
                "retrievers",
                "claims",
                "claim_writes",
                "toolkits",
                "tools",
            )
        )
        if not required.issubset(wheel.namelist()):
            raise ValueError("Wheel is missing public modules, typing marker or license")
    with tarfile.open(directory / f"{stem}.tar.gz") as sdist:
        info = sdist.extractfile(f"{stem}/PKG-INFO")
        if info is None:
            raise ValueError("Missing sdist metadata")
        validate_metadata(info.read(), version)
        required = {
            f"{stem}/{name}"
            for name in (
                "LICENSE",
                "CHANGELOG.md",
                "pyproject.toml",
                "scripts/release.py",
                "langchain_gestaltdb/py.typed",
                "examples/claim_writes.py",
                "docs/RELEASING.md",
            )
        }
        if not required.issubset(sdist.getnames()):
            raise ValueError("Sdist is missing release documentation or package data")
    return {
        name: hashlib.sha256((directory / name).read_bytes()).hexdigest()
        for name in sorted(expected)
    }


def preflight(version: str, root: Path) -> None:
    # The release runner uses Python >=3.11; pure validation helpers also run in
    # the package's Python 3.10 test matrix without a tomli runtime dependency.
    import tomllib

    if not re.fullmatch(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)", version):
        raise ValueError("Only explicit stable X.Y.Z releases are supported")
    if (
        os.environ["GITHUB_REPOSITORY"] != REPOSITORY
        or os.environ["GITHUB_REF"] != "refs/heads/main"
    ):
        raise ValueError("Publishing must be manually dispatched from the canonical main branch")
    project = tomllib.loads((root / "pyproject.toml").read_text())["project"]
    if project["name"] != PROJECT or project["version"] != version:
        raise ValueError("Requested version must match reviewed project metadata")
    if f"## [{version}]" not in (root / "CHANGELOG.md").read_text():
        raise ValueError("Release notes are missing")
    token = os.environ["GH_TOKEN"]
    api = f"https://api.github.com/repos/{REPOSITORY}"
    head = request_json(f"{api}/git/ref/heads/main", token)
    sha = os.environ["GITHUB_SHA"]
    if head["object"]["sha"] != sha:
        raise ValueError("Main advanced; dispatch a new release run for the reviewed head")
    runs = request_json(
        f"{api}/actions/workflows/ci.yml/runs?head_sha={sha}&event=push&per_page=100", token
    )
    run = select_ci_run(runs["workflow_runs"], sha)
    try:
        request_json(f"https://pypi.org/pypi/{PROJECT}/{version}/json")
    except urllib.error.HTTPError as error:
        if error.code != 404:
            raise
    else:
        raise ValueError("Version already exists on PyPI; reconcile instead of overwriting")
    with open(os.environ["GITHUB_OUTPUT"], "a") as output:
        output.write(f"ci-run-id={run['id']}\n")
        output.write(f"artifact-name=distributions-{sha}-{run['run_attempt']}\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("preflight", "verify-artifacts", "verify-pypi"))
    parser.add_argument("version")
    parser.add_argument("--directory", type=Path, default=Path("dist"))
    args = parser.parse_args()
    if args.command == "preflight":
        preflight(args.version, Path.cwd())
        return
    hashes = verify_artifacts(args.directory, args.version)
    if args.command == "verify-pypi":
        published = request_json(f"https://pypi.org/pypi/{PROJECT}/{args.version}/json")
        actual = {item["filename"]: item["digests"]["sha256"] for item in published["urls"]}
        if actual != hashes:
            raise ValueError("PyPI files do not exactly match the CI-validated distributions")
    print(json.dumps(hashes, indent=2))


if __name__ == "__main__":
    main()
