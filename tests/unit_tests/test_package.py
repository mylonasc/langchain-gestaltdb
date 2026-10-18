"""Verify the distribution contract, including packaged typing metadata."""

from importlib import metadata, resources

import langchain_gestaltdb


def test_package_is_importable() -> None:
    assert langchain_gestaltdb.__doc__
    assert metadata.version("langchain-gestaltdb") == "0.1.0.dev0"


def test_typing_marker_is_packaged() -> None:
    assert resources.files("langchain_gestaltdb").joinpath("py.typed").is_file()
