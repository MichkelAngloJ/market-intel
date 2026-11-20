"""Repo safety: secrets stay out of git."""

import importlib.util
import re
import subprocess
from pathlib import Path

import pytest

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


# --- public repo: no device names or machine details ------------------------------------------

# Built from pieces so this file doesn't match its own pattern.
DEVICE_WORDS = re.compile(
    "|".join(["think" + "pad", "len" + "ovo", "vic" + "tus", r"\bh" + r"p\b"]), re.IGNORECASE
)


def test_no_device_names_in_tracked_files() -> None:
    bad = []
    for name in _tracked():
        path = REPO_ROOT / name
        if name == "uv.lock" or not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue  # binary file
        for number, line in enumerate(text.splitlines(), start=1):
            if DEVICE_WORDS.search(line):
                bad.append(f"{name}:{number}")
    assert not bad, f"device names in tracked files (use profile names instead): {bad}"


# --- public repo: every commit uses the GitHub no-reply address (task 0.9b) ---------------


def _load_guard():
    """scripts/commit_email_guard.py, the same rules the pre-commit hook uses."""
    path = REPO_ROOT / "scripts" / "commit_email_guard.py"
    spec = importlib.util.spec_from_file_location("commit_email_guard", path)
    assert spec and spec.loader, f"cannot load {path}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


guard = _load_guard()

NOREPLY = "12345678+someone@users.noreply.github.com"
BAD_EMAILS = [
    "test@example.com",
    "someone@users.noreply.github.com.example.com",
    "someone@noreply.github.com",
    "users.noreply.github.com",
    "@users.noreply.github.com",
    "",
]


def test_every_commit_uses_a_noreply_email() -> None:
    # CI checks out the full history (fetch-depth: 0); a shallow clone would hide old commits.
    shallow = _git("rev-parse", "--is-shallow-repository")
    assert shallow.stdout.strip() == "false", "shallow clone: set fetch-depth: 0 in ci.yml"
    result = _git("log", "--format=%H%x00%ae%x00%ce")
    assert result.returncode == 0, result.stderr
    commits = result.stdout.splitlines()
    assert commits, "no commits found"
    bad = []
    for line in commits:
        sha, author, committer = line.split("\x00")
        if not guard.author_ok(author):
            bad.append(f"{sha[:12]} author {guard.mask(author)}")
        if not guard.committer_ok(committer, allow_web=True):
            bad.append(f"{sha[:12]} committer {guard.mask(committer)}")
    assert not bad, f"commits without the GitHub no-reply address: {bad}"


@pytest.mark.parametrize(
    "email",
    [NOREPLY, "someone@users.noreply.github.com", " Someone@Users.NoReply.GitHub.com "],
)
def test_noreply_addresses_pass(email: str) -> None:
    assert guard.author_ok(email)
    assert guard.committer_ok(email)


@pytest.mark.parametrize("email", BAD_EMAILS)
def test_other_addresses_fail(email: str) -> None:
    assert not guard.author_ok(email)
    assert not guard.committer_ok(email, allow_web=True)


def test_web_committer_is_allowed_only_in_the_history_check() -> None:
    web = "noreply" + "@github.com"
    assert guard.committer_ok(web, allow_web=True)
    assert not guard.committer_ok(web)  # the local hook never accepts it
    assert not guard.author_ok(web)


@pytest.mark.parametrize(
    ("email", "masked"),
    [("test@example.com", "t***@example.com"), (NOREPLY, "1***@users.noreply.github.com")]
    + [("", "***"), ("no-at-sign", "***")],
)
def test_emails_are_masked(email: str, masked: str) -> None:
    assert guard.mask(email) == masked


def test_ident_email() -> None:
    assert guard.ident_email(f"Some One <{NOREPLY}> 1700000000 +0530") == NOREPLY
    assert guard.ident_email("no email here") == ""


def _fake_git_var(author: str, committer: str, calls: list[list[str]], rc: int = 0):
    def run(cmd, **kwargs):
        calls.append(cmd)
        email = author if cmd[-1] == "GIT_AUTHOR_IDENT" else committer
        return subprocess.CompletedProcess(cmd, rc, f"Some One <{email}> 1700000000 +0530\n", "")

    return run


def test_guard_passes_noreply_author_and_committer(capsys) -> None:
    calls: list[list[str]] = []
    assert guard.main(env={}, run=_fake_git_var(NOREPLY, NOREPLY, calls)) == 0
    assert calls == [["git", "var", "GIT_AUTHOR_IDENT"], ["git", "var", "GIT_COMMITTER_IDENT"]]
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize(
    ("author", "committer", "blocked"),
    [("test@example.com", NOREPLY, "author"), (NOREPLY, "test@example.com", "committer")],
)
def test_guard_blocks_other_emails_and_masks_them(author, committer, blocked, capsys) -> None:
    assert guard.main(env={}, run=_fake_git_var(author, committer, [])) == 1
    out = capsys.readouterr().out
    assert "commit blocked" in out
    assert f"{blocked} email t***@example.com" in out
    assert "test@example.com" not in out


def test_guard_blocks_when_git_var_fails(capsys) -> None:
    assert guard.main(env={}, run=_fake_git_var(NOREPLY, NOREPLY, [], rc=128)) == 1
    assert "could not be read" in capsys.readouterr().out


def test_guard_is_skipped_in_github_actions(capsys) -> None:
    calls: list[list[str]] = []
    bad = _fake_git_var("test@example.com", "test@example.com", calls)
    assert guard.main(env={"GITHUB_ACTIONS": "true"}, run=bad) == 0
    assert calls == []
    assert "skipped in GitHub Actions" in capsys.readouterr().out


def test_guard_hook_is_configured() -> None:
    config = (REPO_ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8")
    hook = config.split("- id: commit-email-guard", 1)[1].split("- id:", 1)[0]
    assert "scripts/commit_email_guard.py" in hook
    assert "always_run: true" in hook and "pass_filenames: false" in hook
    ci = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "fetch-depth: 0" in ci
