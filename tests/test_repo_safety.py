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


# --- public repo: no data, predictions, signals, models or notebook outputs ---------------

DATA_DIRS = (
    "data",
    "models",
    "mlruns",
    "logs",
    "outputs",
    "predictions",
    "signals",
    "reports",
    "backtests",
    "paper_trades",
)
DATA_EXTENSIONS = {
    ".csv",
    ".parquet",
    ".feather",
    ".h5",
    ".db",
    ".sqlite",
    ".duckdb",
    ".xlsx",
    ".pkl",
    ".pt",
    ".joblib",
}
FIXTURES = "tests/fixtures/"
MAX_BYTES = 1024 * 1024
SIZE_EXCEPTIONS = {"uv.lock"}


def _tracked() -> list[str]:
    # Includes staged files, so a force-added file is caught before it is committed.
    result = _git("ls-files")
    assert result.returncode == 0, result.stderr
    return result.stdout.splitlines()


def test_no_tracked_files_in_data_folders() -> None:
    bad = [p for p in _tracked() if any(part in DATA_DIRS for part in p.split("/")[:-1])]
    assert not bad, f"files in data/result folders are tracked: {bad}"


def test_no_tracked_data_files_outside_fixtures() -> None:
    bad = [
        p
        for p in _tracked()
        if Path(p).suffix.lower() in DATA_EXTENSIONS and not p.startswith(FIXTURES)
    ]
    assert not bad, f"data files are tracked outside {FIXTURES}: {bad}"


def test_no_tracked_file_over_1_mb() -> None:
    bad = [
        p
        for p in _tracked()
        if p not in SIZE_EXCEPTIONS
        and (REPO_ROOT / p).is_file()
        and (REPO_ROOT / p).stat().st_size > MAX_BYTES
    ]
    assert not bad, f"files over 1 MB are tracked: {bad}"


def test_no_tracked_notebook_has_outputs() -> None:
    import json

    bad = []
    for p in _tracked():
        if not p.endswith(".ipynb") or not (REPO_ROOT / p).is_file():
            continue
        notebook = json.loads((REPO_ROOT / p).read_text(encoding="utf-8"))
        if any(cell.get("outputs") for cell in notebook.get("cells", [])):
            bad.append(p)
    assert not bad, f"notebooks with outputs are tracked (run nbstripout): {bad}"
