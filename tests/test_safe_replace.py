"""The shared atomic swap used by every state writer (S5i, after the S4i backfill was stopped
by a locked file on Windows). See market_intel.file_lock.safe_replace."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from market_intel import file_lock
from market_intel.file_lock import safe_replace

# every module that writes a state file must swap it in through safe_replace
STATE_WRITERS = (
    "alerts.py",
    "candles.py",
    "intraday_candles.py",
    "news.py",
    "source_health.py",
    "upstox.py",
    "sources/upstox_instruments.py",
)


def test_a_free_target_is_replaced_at_once(tmp_path):
    src, dst = tmp_path / "new.tmp", tmp_path / "state.json"
    src.write_text('{"a": 1}', encoding="utf-8")
    dst.write_text("old", encoding="utf-8")
    safe_replace(src, dst)
    assert dst.read_text(encoding="utf-8") == '{"a": 1}'
    assert not src.exists()


def test_a_briefly_locked_target_is_retried(tmp_path, monkeypatch):
    calls, waits = [], []
    real = os.replace

    def flaky(src, dst):
        calls.append(1)
        if len(calls) < 3:  # a scanner holds it for the first two attempts
            raise PermissionError(5, "Access is denied")
        real(src, dst)

    monkeypatch.setattr(file_lock.os, "replace", flaky)
    src, dst = tmp_path / "new.tmp", tmp_path / "state.json"
    src.write_text("fresh", encoding="utf-8")
    safe_replace(src, dst, sleep=waits.append)
    assert dst.read_text(encoding="utf-8") == "fresh"
    assert len(calls) == 3
    assert waits == [pytest.approx(0.05), pytest.approx(0.10)]  # backs off a little each time


def test_a_target_that_stays_locked_still_raises(tmp_path, monkeypatch):
    def always_denied(src, dst):
        raise PermissionError(5, "Access is denied")

    monkeypatch.setattr(file_lock.os, "replace", always_denied)
    src = tmp_path / "new.tmp"
    src.write_text("fresh", encoding="utf-8")
    with pytest.raises(PermissionError):
        safe_replace(src, tmp_path / "state.json", attempts=3, sleep=lambda s: None)


def test_other_errors_are_not_retried(tmp_path, monkeypatch):
    calls = []

    def missing(src, dst):
        calls.append(1)
        raise FileNotFoundError("gone")

    monkeypatch.setattr(file_lock.os, "replace", missing)
    with pytest.raises(FileNotFoundError):
        safe_replace(tmp_path / "a", tmp_path / "b", sleep=lambda s: None)
    assert len(calls) == 1


def test_every_state_writer_uses_the_shared_helper():
    src = Path(__file__).resolve().parents[1] / "src" / "market_intel"
    for name in STATE_WRITERS:
        text = (src / name).read_text(encoding="utf-8")
        assert "safe_replace(" in text, f"{name} does not use safe_replace"
        assert "os.replace(" not in text, f"{name} still calls os.replace directly"
