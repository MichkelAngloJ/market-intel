"""Commit-email guard: commits must use the GitHub no-reply address (the repo is public).

As a pre-commit hook (``commit-email-guard`` in .pre-commit-config.yaml) it reads the author
and committer of the commit being made with ``git var GIT_AUTHOR_IDENT`` and
``git var GIT_COMMITTER_IDENT`` (which also see GIT_AUTHOR_EMAIL / GIT_COMMITTER_EMAIL) and
blocks the commit unless both emails end with ``@users.noreply.github.com``.

In GitHub Actions (GITHUB_ACTIONS=true) it exits 0: CI checks every commit in the history
instead (tests/test_repo_safety.py, which reuses the rules below).

Emails are only ever printed masked: first character + ``***@domain``.
Standard library only, so the hook works even when the project itself is broken.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from collections.abc import Callable, Mapping

NOREPLY_SUFFIX = "@users.noreply.github.com"
# Commits made on github.com (web edits, merges in the web UI) have this committer.
WEB_COMMITTER = "noreply" + "@github.com"

_IDENT_EMAIL = re.compile(r"<([^<>]*)>")


def is_noreply(email: str) -> bool:
    """True for a GitHub no-reply address: ``<something>@users.noreply.github.com``."""
    email = email.strip().lower()
    return email.endswith(NOREPLY_SUFFIX) and len(email) > len(NOREPLY_SUFFIX)


def author_ok(email: str) -> bool:
    return is_noreply(email)


def committer_ok(email: str, *, allow_web: bool = False) -> bool:
    """The committer must be a no-reply address; ``allow_web`` also accepts commits made on
    github.com (only in the history check: a local commit never has that committer)."""
    return is_noreply(email) or (allow_web and email.strip().lower() == WEB_COMMITTER)


def mask(email: str) -> str:
    """'mike@example.com' -> 'm***@example.com'."""
    local, sep, domain = email.strip().rpartition("@")
    if not sep or not local:
        return "***"
    return f"{local[0]}***@{domain}"


def ident_email(ident: str) -> str:
    """Email from a git ident line: 'Name <email> 1700000000 +0530' -> 'email'."""
    match = _IDENT_EMAIL.search(ident)
    return match.group(1) if match else ""


def main(
    env: Mapping[str, str] | None = None,
    run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> int:
    env = os.environ if env is None else env
    if env.get("GITHUB_ACTIONS") == "true":
        print("commit-email-guard: skipped in GitHub Actions (CI checks every commit's email)")
        return 0
    problems = []
    for role, var in (("author", "GIT_AUTHOR_IDENT"), ("committer", "GIT_COMMITTER_IDENT")):
        result = run(["git", "var", var], capture_output=True, text=True, check=False)
        if result.returncode != 0:
            problems.append(f"{role} email could not be read (git var {var} failed)")
            continue
        email = ident_email(result.stdout)
        if not is_noreply(email):
            problems.append(f"{role} email {mask(email)} is not a GitHub no-reply address")
    if not problems:
        return 0
    print("commit-email-guard: commit blocked.")
    for problem in problems:
        print(f"  - {problem}")
    print(
        f"  Commits are public: use your GitHub no-reply address (ends with {NOREPLY_SUFFIX};\n"
        '  GitHub: Settings > Emails). For this repo only: git config user.email "<address>"'
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
