"""Smoke tests: the package imports and runs on the pinned interpreter."""

import sys

import market_intel


def test_package_imports() -> None:
    assert market_intel.__name__ == "market_intel"


def test_version_exists() -> None:
    assert isinstance(market_intel.__version__, str)
    assert market_intel.__version__


def test_interpreter_is_312() -> None:
    assert sys.version_info[:2] == (3, 12)
