"""Verify the distribution contract, including packaged typing metadata."""

from importlib import metadata, resources

import langchain_gestaltdb


def test_package_is_importable() -> None:
    assert langchain_gestaltdb.__doc__
    assert metadata.version("langchain-gestaltdb")
    assert metadata.metadata("langchain-gestaltdb")["License-Expression"] == "Apache-2.0"


def test_typing_marker_is_packaged() -> None:
    assert resources.files("langchain_gestaltdb").joinpath("py.typed").is_file()


def test_plain_install_declares_pyrex_backend() -> None:
    requirements = metadata.requires("langchain-gestaltdb") or []
    assert any(item.startswith("gestaltdb[rocksdb]") and ";" not in item for item in requirements)
    assert metadata.version("pyrex-rocksdb")
