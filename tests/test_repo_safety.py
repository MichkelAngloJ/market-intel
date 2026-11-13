"""Repo safety: secrets stay out of git."""

import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=REPO_ROOT, capture_output=True, text=True, check=False
    )


def test_env_file_is_ignored() -> None:
    # check-ignore exits 0 when the path is ignored; works even if .env doesn't exist.
    result = _git("check-ignore", ".env")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == ".env"


def test_env_example_values_are_empty() -> None:
    lines = (REPO_ROOT / ".env.example").read_text(encoding="utf-8").splitlines()
    pairs = [line for line in lines if line.strip() and not line.lstrip().startswith("#")]
    assert pairs, ".env.example has no variables"
    for line in pairs:
        assert "=" in line, f"not a KEY=VALUE line: {line!r}"
        key, value = line.split("=", 1)
        assert key.strip(), f"empty key in: {line!r}"
        assert value.strip() == "", f"{key.strip()} must have an empty value"


def test_no_env_file_is_tracked() -> None:
    result = _git("ls-files")
    assert result.returncode == 0, result.stderr
    names = [Path(p).name for p in result.stdout.splitlines()]
    bad = [n for n in names if (n == ".env" or n.startswith(".env.")) and n != ".env.example"]
    assert not bad, f"env files tracked in git: {bad}"


def test_compose_env_is_ignored() -> None:
    result = _git("check-ignore", "docker/compose.env")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "docker/compose.env"


def test_compose_env_example_values_are_empty() -> None:
    path = REPO_ROOT / "docker" / "compose.env.example"
    lines = path.read_text(encoding="utf-8").splitlines()
    pairs = [line for line in lines if line.strip() and not line.lstrip().startswith("#")]
    assert pairs, "compose.env.example has no variables"
    for line in pairs:
        assert "=" in line, f"not a KEY=VALUE line: {line!r}"
        key, value = line.split("=", 1)
        assert key.strip(), f"empty key in: {line!r}"
        assert value.strip() == "", f"{key.strip()} must have an empty value"
