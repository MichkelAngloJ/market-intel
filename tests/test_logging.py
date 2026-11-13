"""Tests for logging_setup: JSON file fields, context, levels, stdlib routing, masking."""

import io
import json
import logging
import logging.handlers
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from pydantic import SecretStr

from market_intel.config import Settings, load_settings
from market_intel.logging_setup import (
    BACKUP_DAYS,
    LOG_FILE_NAME,
    bound_context,
    configure_logging,
    get_logger,
    new_run_id,
)

FAKE = "fake-secret-value-123"
FAKE_LOADED = "fake-loaded-gemini-456"


def setup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **env: str
) -> tuple[Settings, io.StringIO]:
    monkeypatch.setenv("APP_PROFILE", "hp")
    monkeypatch.setenv("LOG_DIR", str(tmp_path / "logs"))
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    settings = load_settings()
    console = io.StringIO()
    configure_logging(settings, console_stream=console)
    return settings, console


def file_text(settings: Settings) -> str:
    return (settings.log_dir / LOG_FILE_NAME).read_text(encoding="utf-8")


def records(settings: Settings) -> list[dict]:
    return [json.loads(line) for line in file_text(settings).splitlines()]


# --- file format -------------------------------------------------------------------------


def test_json_record_has_required_fields_in_utc(tmp_path, monkeypatch):
    settings, _ = setup(tmp_path, monkeypatch)
    get_logger("market_intel.test").info("hello", rows=3)
    [rec] = records(settings)
    assert rec["event"] == "hello"
    assert rec["level"] == "info"
    assert rec["logger"] == "market_intel.test"
    assert rec["rows"] == 3
    ts = datetime.fromisoformat(rec["timestamp"])
    assert ts.utcoffset() == timedelta(0)


def test_exception_info_is_recorded(tmp_path, monkeypatch):
    settings, _ = setup(tmp_path, monkeypatch)
    try:
        1 / 0  # noqa: B018
    except ZeroDivisionError:
        get_logger("t").exception("boom")
    [rec] = records(settings)
    assert "ZeroDivisionError" in rec["exception"]


def test_bound_context_appears_only_inside_block(tmp_path, monkeypatch):
    settings, _ = setup(tmp_path, monkeypatch)
    log = get_logger("t")
    run_id = new_run_id()
    with bound_context(run_id=run_id, component="ingest", instrument="RELIANCE", source="yf"):
        log.info("inside")
    log.info("outside")
    inside, outside = records(settings)
    assert inside["run_id"] == run_id
    assert inside["component"] == "ingest"
    assert inside["instrument"] == "RELIANCE"
    assert inside["source"] == "yf"
    assert "run_id" not in outside


def test_log_level_is_respected(tmp_path, monkeypatch):
    settings, console = setup(tmp_path, monkeypatch, LOG_LEVEL="WARNING")
    log = get_logger("t")
    log.info("quiet")
    log.warning("loud")
    assert [r["event"] for r in records(settings)] == ["loud"]
    assert "quiet" not in console.getvalue()


def test_stdlib_logger_goes_to_both_outputs(tmp_path, monkeypatch):
    settings, console = setup(tmp_path, monkeypatch)
    logging.getLogger("thirdparty.lib").warning("retrying %s", "request")
    [rec] = records(settings)
    assert rec["event"] == "retrying request"
    assert rec["logger"] == "thirdparty.lib"
    assert rec["level"] == "warning"
    assert "retrying request" in console.getvalue()


def test_console_uses_display_timezone(tmp_path, monkeypatch):
    _, console = setup(tmp_path, monkeypatch)
    get_logger("t").info("tz_check")
    assert "+05:30" in console.getvalue()


def test_rotation_is_utc_midnight_keeping_30(tmp_path, monkeypatch):
    setup(tmp_path, monkeypatch)
    [handler] = [
        h
        for h in logging.getLogger().handlers
        if isinstance(h, logging.handlers.TimedRotatingFileHandler)
    ]
    assert handler.when == "MIDNIGHT"
    assert handler.utc is True
    assert handler.backupCount == BACKUP_DAYS == 30
    assert handler.encoding == "utf-8"
    assert Path(handler.baseFilename).name == LOG_FILE_NAME


def test_configure_is_idempotent(tmp_path, monkeypatch):
    settings, _ = setup(tmp_path, monkeypatch)
    configure_logging(settings, console_stream=io.StringIO())
    get_logger("t").info("once")
    assert len(records(settings)) == 1


# --- masking -----------------------------------------------------------------------------


def assert_masked(settings: Settings, console: io.StringIO, *fakes: str) -> None:
    text = file_text(settings) + console.getvalue()
    assert "***" in text
    for fake in fakes:
        assert fake not in text


def test_masks_secret_looking_keys(tmp_path, monkeypatch):
    settings, console = setup(tmp_path, monkeypatch)
    get_logger("t").info(
        "keys",
        api_key=FAKE,
        password=FAKE,
        token=FAKE,
        mongodb_uri=FAKE,
        source_url=FAKE,
        Authorization=FAKE,
    )
    [rec] = records(settings)
    for key in ("api_key", "password", "token", "mongodb_uri", "source_url", "Authorization"):
        assert rec[key] == "***"
    assert_masked(settings, console, FAKE)


def test_masks_nested_dicts_and_lists(tmp_path, monkeypatch):
    settings, console = setup(tmp_path, monkeypatch)
    get_logger("t").info(
        "nested",
        payload={"outer": {"client_secret": FAKE, "ok": 1}, "items": [{"auth_token": FAKE}]},
    )
    [rec] = records(settings)
    assert rec["payload"]["outer"] == {"client_secret": "***", "ok": 1}
    assert rec["payload"]["items"] == [{"auth_token": "***"}]
    assert_masked(settings, console, FAKE)


def test_masks_secretstr_under_any_key(tmp_path, monkeypatch):
    settings, console = setup(tmp_path, monkeypatch)
    get_logger("t").info("secretstr", harmless_name=SecretStr(FAKE))
    assert records(settings)[0]["harmless_name"] == "***"
    assert_masked(settings, console, FAKE)


def test_masks_url_credentials_in_text_and_event(tmp_path, monkeypatch):
    settings, console = setup(tmp_path, monkeypatch)
    log = get_logger("t")
    log.info(f"connecting to postgresql://demo_user:{FAKE}@db.example.invalid:5432/x")
    log.info("conn", note=f"via mongodb+srv://u:{FAKE}@cluster.example.invalid/db")
    logging.getLogger("thirdparty").error("failed %s", f"redis://:{FAKE}@cache.invalid:6379")
    text = file_text(settings)
    assert "postgresql://demo_user:***@db.example.invalid" in text
    assert "mongodb+srv://u:***@cluster.example.invalid" in text
    assert "redis://:***@cache.invalid" in text
    assert_masked(settings, console, FAKE)


def test_masks_loaded_secret_values_anywhere(tmp_path, monkeypatch):
    settings, console = setup(tmp_path, monkeypatch, GEMINI_API_KEY=FAKE_LOADED)
    get_logger("t").info(f"oops {FAKE_LOADED} leaked", detail=f"x{FAKE_LOADED}y")
    logging.getLogger("thirdparty").warning("header was %s", FAKE_LOADED)
    try:
        raise RuntimeError(f"bad key {FAKE_LOADED}")
    except RuntimeError:
        get_logger("t").exception("with_traceback")
    assert_masked(settings, console, FAKE_LOADED)


def test_masks_app_password_with_and_without_spaces(tmp_path, monkeypatch):
    spaced = "fakq wxyz abcd efgh"
    settings, console = setup(tmp_path, monkeypatch, ALERT_EMAIL_APP_PASSWORD=spaced)
    get_logger("t").info(f"pw {spaced} and {spaced.replace(' ', '')}")
    assert_masked(settings, console, spaced, spaced.replace(" ", ""))
