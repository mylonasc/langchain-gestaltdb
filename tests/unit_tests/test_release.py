"""Offline release-gate tests; no publication or network is performed."""

import hashlib
import io
import runpy
import sys
import tarfile
import types
import urllib.error
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
RELEASE = runpy.run_path(str(ROOT / "scripts" / "release.py"))


def ci_run(**overrides) -> dict:
    result = {
        "id": 123,
        "head_sha": "a" * 40,
        "event": "push",
        "head_branch": "main",
        "path": ".github/workflows/ci.yml",
        "run_number": 1,
        "run_attempt": 1,
        "status": "completed",
        "conclusion": "success",
    }
    result.update(overrides)
    return result


def test_ci_selection_uses_exact_main_workflow_and_latest_attempt() -> None:
    select = RELEASE["select_ci_run"]
    good = ci_run()
    irrelevant = [
        ci_run(head_sha="b" * 40),
        ci_run(event="pull_request"),
        ci_run(head_branch="other"),
        ci_run(path=".github/workflows/other.yml"),
    ]
    assert select([good, *irrelevant], "a" * 40) == good
    with pytest.raises(ValueError, match="No main-push"):
        select(irrelevant, "a" * 40)
    for bad in (
        ci_run(run_attempt=2, conclusion="failure"),
        ci_run(run_number=2, status="in_progress", conclusion=None),
    ):
        with pytest.raises(ValueError, match="not passed"):
            select([good, bad], "a" * 40)


@pytest.fixture
def distributions(tmp_path: Path) -> Path:
    stem = "langchain_gestaltdb-0.1.0"
    metadata = (
        b"Metadata-Version: 2.4\nName: langchain-gestaltdb\nVersion: 0.1.0\n"
        b"Requires-Python: <3.14,>=3.10\nLicense-Expression: Apache-2.0\n\nDescription\n"
    )
    with zipfile.ZipFile(tmp_path / f"{stem}-py3-none-any.whl", "w") as wheel:
        wheel.writestr(f"{stem}.dist-info/METADATA", metadata)
        wheel.writestr(f"{stem}.dist-info/licenses/LICENSE", "Apache-2.0")
        wheel.writestr("langchain_gestaltdb/py.typed", "")
        for name in (
            "__init__",
            "graph",
            "graph_document",
            "retrievers",
            "claims",
            "claim_writes",
            "toolkits",
            "tools",
        ):
            wheel.writestr(f"langchain_gestaltdb/{name}.py", "")
    with tarfile.open(tmp_path / f"{stem}.tar.gz", "w:gz") as sdist:
        for name in (
            "PKG-INFO",
            "LICENSE",
            "CHANGELOG.md",
            "pyproject.toml",
            "scripts/release.py",
            "langchain_gestaltdb/py.typed",
            "examples/claim_writes.py",
            "docs/RELEASING.md",
        ):
            data = metadata if name == "PKG-INFO" else b""
            item = tarfile.TarInfo(f"{stem}/{name}")
            item.size = len(data)
            sdist.addfile(item, io.BytesIO(data))
    return tmp_path


def test_artifact_metadata_contents_and_hashes(distributions: Path) -> None:
    hashes = RELEASE["verify_artifacts"](distributions, "0.1.0")
    assert len(hashes) == 2
    assert hashes == {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in distributions.iterdir()
    }


def test_reject_extra_or_wrong_version_distributions(distributions: Path) -> None:
    with pytest.raises(ValueError, match="exactly"):
        RELEASE["verify_artifacts"](distributions, "0.2.0")
    (distributions / "stale.whl").touch()
    with pytest.raises(ValueError, match="exactly"):
        RELEASE["verify_artifacts"](distributions, "0.1.0")


def test_uv_gitignore_is_not_a_distribution(distributions: Path) -> None:
    (distributions / ".gitignore").write_text("*\n")
    assert len(RELEASE["verify_artifacts"](distributions, "0.1.0")) == 2


@pytest.mark.parametrize(
    "missing", ["langchain_gestaltdb/py.typed", "langchain_gestaltdb/claim_writes.py"]
)
def test_reject_missing_wheel_data(distributions: Path, missing: str) -> None:
    wheel = next(distributions.glob("*.whl"))
    with zipfile.ZipFile(wheel) as archive:
        entries = {name: archive.read(name) for name in archive.namelist() if name != missing}
    with zipfile.ZipFile(wheel, "w") as archive:
        for name, payload in entries.items():
            archive.writestr(name, payload)
    with pytest.raises(ValueError, match="Wheel is missing"):
        RELEASE["verify_artifacts"](distributions, "0.1.0")


@pytest.mark.parametrize(
    "field,value",
    [
        ("Name", "another-package"),
        ("Version", "0.2.0"),
        ("Requires-Python", ">=3.14"),
        ("License-Expression", "MIT"),
    ],
)
def test_reject_incorrect_metadata(field: str, value: str) -> None:
    fields = {
        "Name": "langchain-gestaltdb",
        "Version": "0.1.0",
        "Requires-Python": ">=3.10,<3.14",
        "License-Expression": "Apache-2.0",
    }
    fields[field] = value
    with pytest.raises(ValueError):
        RELEASE["validate_metadata"](
            "\n".join(f"{k}: {v}" for k, v in fields.items()).encode(), "0.1.0"
        )


def test_publish_workflow_has_only_manual_trigger_and_isolated_oidc() -> None:
    workflow = (ROOT / ".github/workflows/publish.yml").read_text()
    assert "  workflow_dispatch:" in workflow
    assert "pull_request:" not in workflow and "  push:" not in workflow
    assert workflow.count("id-token: write") == 1
    publish = workflow.split("  publish:\n", 1)[1].split("  verify-install:\n", 1)[0]
    assert "name: pypi" in publish and "actions/checkout@" not in publish
    assert "uv build" not in publish and "contents: write" not in workflow
    assert "attestations: true" in publish and "skip-existing:" not in workflow


@pytest.mark.parametrize(
    "failure",
    [
        None,
        "preref",
        "repository",
        "version",
        "main",
        "ci",
        "existing",
        "index-error",
        "notes",
    ],
)
def test_preflight_fails_closed_before_exposing_artifacts(
    tmp_path: Path, monkeypatch, failure
) -> None:
    preflight = RELEASE["preflight"]
    # Mock only TOML parsing so these authorization tests also run on Python 3.10.
    parser = types.ModuleType("tomllib")
    parser.loads = lambda _: {"project": {"name": "langchain-gestaltdb", "version": "0.1.0"}}
    monkeypatch.setitem(sys.modules, "tomllib", parser)
    (tmp_path / "pyproject.toml").write_text("reviewed metadata")
    (tmp_path / "CHANGELOG.md").write_text("missing" if failure == "notes" else "## [0.1.0]")
    output = tmp_path / "outputs"
    for name, value in {
        "GITHUB_REPOSITORY": "other/repo"
        if failure == "repository"
        else "mylonasc/langchain-gestaltdb",
        "GITHUB_REF": "refs/heads/other" if failure == "preref" else "refs/heads/main",
        "GITHUB_SHA": "a" * 40,
        "GITHUB_OUTPUT": str(output),
        "GH_TOKEN": "offline-test",
    }.items():
        monkeypatch.setenv(name, value)

    def fake_request(url, token=None):
        if url.endswith("/git/ref/heads/main"):
            return {"object": {"sha": ("b" if failure == "main" else "a") * 40}}
        if "/actions/workflows/ci.yml/runs?" in url:
            return {
                "workflow_runs": [ci_run(conclusion="failure" if failure == "ci" else "success")]
            }
        if "pypi.org" in url:
            if failure == "existing":
                return {}
            raise urllib.error.HTTPError(
                url, 500 if failure == "index-error" else 404, "test", {}, None
            )
        raise AssertionError(url)

    monkeypatch.setitem(preflight.__globals__, "request_json", fake_request)
    version = "0.1.0rc1" if failure == "version" else "0.1.0"
    if failure is None:
        preflight(version, tmp_path)
        assert output.read_text() == f"ci-run-id=123\nartifact-name=distributions-{'a' * 40}-1\n"
    else:
        with pytest.raises((ValueError, urllib.error.HTTPError)):
            preflight(version, tmp_path)
        assert not output.exists()
