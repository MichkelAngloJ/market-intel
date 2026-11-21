"""Strict URL masking (decision 2026-11-22): logs hide the whole user part of any URL, the
same as stored ingestion errors. Hosts stay visible."""

import io
import logging

import pytest

from market_intel import ingestion, logging_setup
from market_intel.config import load_settings
from market_intel.logging_setup import (
    LOG_FILE_NAME,
    configure_logging,
    get_logger,
    mask_text,
    mask_url_userinfo,
)

# Built from parts so the secret scanner doesn't read them as real credentials.
USER, PW = "demo_person", "fake-pass-777"


@pytest.mark.parametrize(
    ("raw", "masked"),
    [
        ("postgresql://" + USER + ":" + PW + "@db.invalid:5432/x", "postgresql://***@db.invalid"),
        ("mongodb+srv://" + USER + "@cluster.invalid/db", "mongodb+srv://***@cluster.invalid"),
        ("https://" + USER + "@example.invalid/a", "https://***@example.invalid"),
        ("redis://:" + PW + "@cache.invalid:6379", "redis://***@cache.invalid"),
    ],
)
def test_mask_text_removes_user_names_and_passwords(raw, masked):
    text = mask_text(f"failed: {raw} (retrying)")
    assert masked in text
    assert USER not in text and PW not in text
    assert text.endswith("(retrying)")


def test_an_at_sign_in_a_path_or_query_is_not_a_user_part():
    for url in ("https://example.invalid/u/me@x", "https://example.invalid?mail=a@b.invalid"):
        assert mask_url_userinfo(url) == url


def test_logs_and_ingestion_errors_use_the_same_rule():
    assert ingestion.mask_text is logging_setup.mask_text
    raw = "postgresql://" + USER + "@db.invalid/x"
    assert mask_text(raw) in ingestion.masked_error(RuntimeError(raw))


def test_log_file_and_console_never_show_url_user_names(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_PROFILE", "full")
    monkeypatch.setenv("LOG_DIR", str(tmp_path / "logs"))
    settings = load_settings()
    console = io.StringIO()
    configure_logging(settings, console_stream=console)
    get_logger("market_intel.test").info(
        "connect", note="via postgresql://" + USER + ":" + PW + "@db.invalid/x"
    )
    logging.getLogger("thirdparty").warning("retry %s", "https://" + USER + "@api.invalid/v2")
    text = (settings.log_dir / LOG_FILE_NAME).read_text(encoding="utf-8")
    for out in (text, console.getvalue()):
        assert "postgresql://***@db.invalid" in out
        assert "https://***@api.invalid" in out
        assert USER not in out and PW not in out
