"""Docs rules from 1.2: the dry-run alert rule is only for Claude Code's tests and demos (the
owner's real runs send real alerts), and the factor map is linked from the README."""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def read(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


def test_runbook_states_the_alert_rule():
    runbook = " ".join(read("docs/runbook.md").split())
    assert "`ALERT_DRY_RUN=true` is only for Claude Code's tests and demos" in runbook
    assert "the owner's real runs use real alerts" in runbook
    assert "until real alerts are wanted" not in runbook  # the 1.1 wording is gone


def test_owner_commands_in_the_runbook_tables_do_not_use_dry_run_alerts():
    for line in read("docs/runbook.md").splitlines():
        if line.startswith("| Daily refresh") or line.startswith("| Fetch every enabled feed"):
            assert "ALERT_DRY_RUN" not in line


def test_factor_map_is_linked_from_the_readme():
    assert "(docs/factors.md)" in read("README.md")
    assert (ROOT / "docs" / "factors.md").is_file()
