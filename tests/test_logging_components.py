"""Task 1.0: one log file per component. Two programs (real processes) log and rotate at the
same time; each keeps its own files, and secrets are masked in both."""

import io
import json
import logging
import logging.handlers
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from market_intel.config import load_settings
from market_intel.logging_setup import (
    LOG_FILE_NAME,
    configure_logging,
    get_logger,
    log_file_name,
)

FAKE_PW = "fake-component-pass-777"
PG_URL = f"postgresql://fake_user:{FAKE_PW}@db.invalid:5432/x?sslmode=require"
LINES = 300

# Runs in a child process: wait for the go-file, then log, rotate halfway, and log more.
CHILD = textwrap.dedent(
    """
    import logging, logging.handlers, sys, time
    from pathlib import Path
    from market_intel.config import load_settings
    from market_intel.logging_setup import configure_logging, get_logger

    component, go = sys.argv[1], Path(sys.argv[2])
    settings = load_settings()
    configure_logging(settings, component)
    Path(str(go) + "." + component + ".ready").write_text("ready")
    deadline = time.monotonic() + 60
    while not go.exists():
        if time.monotonic() > deadline:
            sys.exit("no go signal")
        time.sleep(0.01)
    [handler] = [h for h in logging.getLogger().handlers
                 if isinstance(h, logging.handlers.TimedRotatingFileHandler)]
    log = get_logger("market_intel." + component)
    for i in range(LINES):
        log.info("line", component_name=component, i=i,
                 note="connecting to " + settings.postgres_url.get_secret_value())
        if i == LINES // 2:
            handler.doRollover()
    """
).replace("LINES", str(LINES))


def child_env(tmp_path: Path) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k in ("PATH", "SYSTEMROOT", "TEMP", "TMP")}
    empty = tmp_path / "empty.env"
    empty.write_text("", encoding="utf-8")
    env.update(
        MARKET_INTEL_ENV_FILE=str(empty),  # never the real .env
        APP_PROFILE="full",
        LOG_DIR=str(tmp_path / "logs"),
        POSTGRES_URL=PG_URL,
        PYTHONIOENCODING="utf-8",
    )
    return env


def test_two_components_log_and_rotate_at_the_same_time(tmp_path):
    go = tmp_path / "go"
    env = child_env(tmp_path)
    children = [
        subprocess.Popen(
            [sys.executable, "-c", CHILD, name, str(go)],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        for name in ("comp_a", "comp_b")
    ]
    deadline = time.monotonic() + 60
    while not all((tmp_path / f"go.{n}.ready").exists() for n in ("comp_a", "comp_b")):
        assert time.monotonic() < deadline, "children never got ready"
        assert all(c.poll() is None for c in children), "a child exited early"
        time.sleep(0.02)
    go.write_text("go")  # both start logging (and rotating) at the same moment
    for child in children:
        _, err = child.communicate(timeout=120)
        assert child.returncode == 0, err.decode(errors="replace")

    logs = tmp_path / "logs"
    for name in ("comp_a", "comp_b"):
        current = logs / log_file_name(name)
        rotated = [p for p in logs.iterdir() if p.name.startswith(current.name + ".")]
        assert current.is_file() and len(rotated) == 1, sorted(p.name for p in logs.iterdir())
        lines = []
        for path in (rotated[0], current):
            text = path.read_text(encoding="utf-8")
            assert FAKE_PW not in text  # masked in every file
            lines += [json.loads(line) for line in text.splitlines()]
        assert [r["i"] for r in lines] == list(range(LINES))  # nothing lost, nothing foreign
        assert {r["component_name"] for r in lines} == {name}
        assert all("***" in r["note"] for r in lines)
    assert not (logs / LOG_FILE_NAME).exists()  # nobody wrote the shared default file


def test_component_writes_only_its_own_file(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_PROFILE", "full")
    monkeypatch.setenv("LOG_DIR", str(tmp_path / "logs"))
    settings = load_settings()
    configure_logging(settings, "health", console_stream=io.StringIO())
    get_logger("market_intel.health").info("hello")
    names = [p.name for p in (tmp_path / "logs").iterdir()]
    assert names == ["health.jsonl"]
    [handler] = [
        h
        for h in logging.getLogger().handlers
        if isinstance(h, logging.handlers.TimedRotatingFileHandler)
    ]
    assert Path(handler.baseFilename).name == "health.jsonl"


def test_default_component_keeps_the_old_file_name():
    assert LOG_FILE_NAME == "market_intel.jsonl"


@pytest.mark.parametrize("bad", ["", "Health", "../x", "a/b", "a b", "x" * 41])
def test_invalid_component_names_are_refused(bad):
    with pytest.raises(ValueError, match="invalid log component"):
        log_file_name(bad)


@pytest.mark.parametrize(
    ("module", "component"),
    [
        ("health", "health"),
        ("db_check", "db_check"),
        ("backup", "backup"),
        ("alerts", "alerts"),
        ("mongo_setup", "mongo_setup"),
    ],
)
def test_entry_points_pass_their_component(module, component):
    source = (
        Path(__file__).resolve().parents[1] / "src" / "market_intel" / f"{module}.py"
    ).read_text(encoding="utf-8")
    assert f'configure_logging(settings, "{component}")' in source
