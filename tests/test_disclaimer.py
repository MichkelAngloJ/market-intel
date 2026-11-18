"""The disclaimer constant exists and matches the README."""

from pathlib import Path

from market_intel.disclaimer import DISCLAIMER

README = Path(__file__).resolve().parent.parent / "README.md"


def test_disclaimer_is_non_empty_and_mentions_sebi() -> None:
    assert DISCLAIMER.strip()
    assert "SEBI" in DISCLAIMER


def test_readme_carries_the_same_disclaimer() -> None:
    lines = README.read_text(encoding="utf-8").splitlines()
    quoted = " ".join(line.removeprefix(">").strip() for line in lines if line.startswith(">"))
    assert DISCLAIMER in quoted
