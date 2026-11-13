"""Tests for market_intel.config. They never read the real .env: every test points
MARKET_INTEL_ENV_FILE (or a fake repo root) at tmp files and controls env vars."""

import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from market_intel import config
from market_intel.config import ConfigError, Settings, get_settings, load_settings

FAKE = "fake-secret-value-123"
REAL_REPO = Path(__file__).resolve().parent.parent
SETTING_VARS = [name.upper() for name in Settings.model_fields] + [config.ENV_FILE_VAR]


@pytest.fixture(autouse=True)
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Clear settings env vars and move the repo root to tmp so the real .env is unreachable."""
    for name in SETTING_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(config, "REPO_ROOT", tmp_path)
    get_settings.cache_clear()
    yield tmp_path
    get_settings.cache_clear()


def write_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, text: str) -> Path:
    path = tmp_path / "test.env"
    path.write_text(text, encoding="utf-8")
    monkeypatch.setenv(config.ENV_FILE_VAR, str(path))
    return path


def load_with(monkeypatch: pytest.MonkeyPatch, **env: str) -> Settings:
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return load_settings()


# --- loading -----------------------------------------------------------------------------


def test_loads_with_only_profile(tmp_path, monkeypatch):
    write_env(tmp_path, monkeypatch, "APP_PROFILE=thinkpad\n")
    s = load_settings()
    assert s.app_profile == "thinkpad"
    assert s.log_level == "INFO"
    assert s.display_timezone == "Asia/Kolkata"
    assert s.data_dir == (tmp_path / "data").resolve()
    assert s.missing("database") == ["POSTGRES_URL", "MONGODB_URI"]
    assert s.missing("upstox") == ["UPSTOX_API_KEY", "UPSTOX_API_SECRET", "UPSTOX_REDIRECT_URI"]
    assert s.missing("llm") == ["GEMINI_API_KEY"]
    assert s.missing("alerts") == [
        "ALERT_EMAIL_FROM",
        "ALERT_EMAIL_TO",
        "ALERT_EMAIL_APP_PASSWORD",
    ]


def test_missing_profile_is_clear_error():
    with pytest.raises(ConfigError, match="APP_PROFILE"):
        load_settings()


def test_invalid_profile_lists_allowed_values(monkeypatch):
    with pytest.raises(ConfigError) as err:
        load_with(monkeypatch, APP_PROFILE="laptop")
    assert all(p in str(err.value) for p in ("thinkpad", "hp", "cloud"))


def test_env_example_copy_loads_with_defaults(tmp_path, monkeypatch):
    example = (REAL_REPO / ".env.example").read_text(encoding="utf-8")
    write_env(tmp_path, monkeypatch, example.replace("APP_PROFILE=", "APP_PROFILE=hp"))
    s = load_settings()
    assert s.app_profile == "hp"
    assert s.llm_monthly_cap_inr == 1000
    assert s.llm_warn_pct == 80
    assert s.llm_cap_raise_approved is False
    assert s.postgres_url is None


def test_real_env_var_overrides_env_file(tmp_path, monkeypatch):
    write_env(tmp_path, monkeypatch, "APP_PROFILE=thinkpad\nLOG_LEVEL=DEBUG\n")
    assert load_with(monkeypatch, LOG_LEVEL="ERROR").log_level == "ERROR"


def test_env_found_at_repo_root_from_subfolder(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("APP_PROFILE=cloud\n", encoding="utf-8")
    sub = tmp_path / "a" / "b"
    sub.mkdir(parents=True)
    monkeypatch.chdir(sub)
    assert load_settings().app_profile == "cloud"


def test_env_file_var_overrides_repo_root_env(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("APP_PROFILE=cloud\n", encoding="utf-8")
    write_env(tmp_path, monkeypatch, "APP_PROFILE=hp\n")
    assert load_settings().app_profile == "hp"


def test_env_file_var_pointing_nowhere_is_error(tmp_path, monkeypatch):
    monkeypatch.setenv(config.ENV_FILE_VAR, str(tmp_path / "nope.env"))
    with pytest.raises(ConfigError, match="does not exist"):
        load_settings()


def test_get_settings_is_cached(monkeypatch):
    monkeypatch.setenv("APP_PROFILE", "hp")
    first = get_settings()
    monkeypatch.setenv("APP_PROFILE", "cloud")
    assert get_settings() is first
    get_settings.cache_clear()
    assert get_settings().app_profile == "cloud"


# --- rules -------------------------------------------------------------------------------


def test_r1_thinkpad_rejects_upstox_key(monkeypatch):
    with pytest.raises(ConfigError, match="R1"):
        load_with(monkeypatch, APP_PROFILE="thinkpad", UPSTOX_API_KEY=FAKE)


def test_r1_thinkpad_rejects_upstox_secret(monkeypatch):
    with pytest.raises(ConfigError, match="R1"):
        load_with(monkeypatch, APP_PROFILE="thinkpad", UPSTOX_API_SECRET=FAKE)


def test_r1_hp_allows_upstox_key(monkeypatch):
    assert load_with(monkeypatch, APP_PROFILE="hp", UPSTOX_API_KEY=FAKE).upstox_api_key


@pytest.mark.parametrize(
    ("cap", "approved", "ok"),
    [
        ("1000", "false", True),
        ("1500", "false", False),
        ("1500", "true", True),
        ("2000", "true", True),
        ("2500", "true", False),
        ("0", "false", False),
        ("-5", "true", False),
    ],
)
def test_r2_llm_cap(monkeypatch, cap, approved, ok):
    env = {"APP_PROFILE": "hp", "LLM_MONTHLY_CAP_INR": cap, "LLM_CAP_RAISE_APPROVED": approved}
    if ok:
        assert load_with(monkeypatch, **env).llm_monthly_cap_inr == int(cap)
    else:
        with pytest.raises(ConfigError, match="R2"):
            load_with(monkeypatch, **env)


@pytest.mark.parametrize(
    ("pct", "ok"), [("80", True), ("1", True), ("99", True), ("0", False), ("100", False)]
)
def test_r3_warn_pct(monkeypatch, pct, ok):
    if ok:
        assert load_with(monkeypatch, APP_PROFILE="hp", LLM_WARN_PCT=pct).llm_warn_pct == int(pct)
    else:
        with pytest.raises(ConfigError, match="R3"):
            load_with(monkeypatch, APP_PROFILE="hp", LLM_WARN_PCT=pct)


def test_rule_error_names_decision_date(monkeypatch):
    with pytest.raises(ConfigError, match="2026-11-12"):
        load_with(monkeypatch, APP_PROFILE="hp", LLM_MONTHLY_CAP_INR="0")


# --- secret masking ----------------------------------------------------------------------

ALL_SECRETS = {
    "POSTGRES_URL": FAKE,
    "MONGODB_URI": FAKE,
    "UPSTOX_API_KEY": FAKE,
    "UPSTOX_API_SECRET": FAKE,
    "GEMINI_API_KEY": FAKE,
    "GROQ_API_KEY": FAKE,
    "PAID_LLM_API_KEY": FAKE,
    "ALERT_EMAIL_APP_PASSWORD": FAKE,
}


def test_secrets_masked_in_repr_str_and_dumps(monkeypatch):
    s = load_with(monkeypatch, APP_PROFILE="hp", **ALL_SECRETS)
    for text in (repr(s), str(s), s.model_dump_json(), str(s.model_dump())):
        assert FAKE not in text
    db = s.require("database")
    assert FAKE not in repr(db) and FAKE not in db.model_dump_json()
    assert db.postgres_url.get_secret_value() == FAKE


def test_secrets_masked_in_command_output(monkeypatch, capsys):
    for key, value in {"APP_PROFILE": "hp", **ALL_SECRETS}.items():
        monkeypatch.setenv(key, value)
    assert config.main() == 0
    out = capsys.readouterr()
    assert FAKE not in out.out + out.err
    assert "POSTGRES_URL: set" in out.out


def test_secrets_masked_in_rule_error(monkeypatch):
    with pytest.raises(ConfigError) as err:
        load_with(monkeypatch, APP_PROFILE="thinkpad", **ALL_SECRETS)
    assert FAKE not in str(err.value) and FAKE not in repr(err.value)
    assert err.value.__cause__ is None


def test_secrets_masked_in_validation_error(monkeypatch):
    with pytest.raises(ConfigError) as err:
        load_with(monkeypatch, APP_PROFILE="hp", LLM_WARN_PCT="not-a-number", **ALL_SECRETS)
    assert "LLM_WARN_PCT" in str(err.value)
    assert FAKE not in str(err.value)
    # "from None" drops pydantic's ValidationError, which could otherwise show up in tracebacks
    assert err.value.__cause__ is None and err.value.__suppress_context__


def test_require_database_error_names_only(monkeypatch):
    s = load_with(monkeypatch, APP_PROFILE="hp", POSTGRES_URL=FAKE)
    with pytest.raises(ConfigError) as err:
        s.require("database")
    msg = str(err.value)
    assert "MONGODB_URI" in msg and ".env.example" in msg
    assert FAKE not in msg


def test_require_database_lists_both_missing(monkeypatch):
    s = load_with(monkeypatch, APP_PROFILE="hp")
    with pytest.raises(ConfigError) as err:
        s.require("database")
    assert "POSTGRES_URL" in str(err.value) and "MONGODB_URI" in str(err.value)


def test_require_complete_group_returns_view(monkeypatch):
    s = load_with(monkeypatch, APP_PROFILE="hp", GEMINI_API_KEY=FAKE)
    llm = s.require("llm")
    assert llm.llm_monthly_cap_inr == 1000 and llm.groq_api_key is None


# --- command exit codes (real subprocess, isolated env) ----------------------------------


def run_command(tmp_path: Path, env_text: str, **extra: str) -> subprocess.CompletedProcess[str]:
    env_file = tmp_path / "cmd.env"
    env_file.write_text(env_text, encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if k.upper() not in SETTING_VARS}
    env.update({config.ENV_FILE_VAR: str(env_file), **extra})
    return subprocess.run(
        [sys.executable, "-m", "market_intel.config"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_command_exit_zero_when_valid(tmp_path):
    result = run_command(tmp_path, "APP_PROFILE=thinkpad\n")
    assert result.returncode == 0, result.stderr
    assert "profile: thinkpad" in result.stdout


def test_command_nonzero_on_rule_violation(tmp_path):
    result = run_command(tmp_path, "APP_PROFILE=thinkpad\n", UPSTOX_API_KEY=FAKE)
    assert result.returncode != 0
    assert "R1" in result.stderr
    assert FAKE not in result.stdout + result.stderr


# --- task 0.3 additions ------------------------------------------------------------------


def test_invalid_display_timezone_is_error(monkeypatch):
    with pytest.raises(ConfigError, match="DISPLAY_TIMEZONE") as err:
        load_with(monkeypatch, APP_PROFILE="hp", DISPLAY_TIMEZONE="Mars/Olympus")
    assert "unknown time zone" in str(err.value)


def test_valid_display_timezone_loads(monkeypatch):
    s = load_with(monkeypatch, APP_PROFILE="hp", DISPLAY_TIMEZONE="Europe/London")
    assert s.display_timezone == "Europe/London"


def test_command_masks_emails(monkeypatch, capsys):
    monkeypatch.setenv("APP_PROFILE", "hp")
    monkeypatch.setenv("ALERT_EMAIL_FROM", "sender.box@example.com")
    monkeypatch.setenv("ALERT_EMAIL_TO", "main.box@example.org")
    assert config.main() == 0
    out = capsys.readouterr().out
    assert "ALERT_EMAIL_FROM = s***@example.com" in out
    assert "ALERT_EMAIL_TO = m***@example.org" in out
    assert "sender.box" not in out and "main.box" not in out


@pytest.mark.parametrize(
    ("address", "masked"),
    [("mike@example.com", "m***@example.com"), ("no-at-sign", "***"), (None, "(not set)")],
)
def test_mask_email(address, masked):
    assert config.mask_email(address) == masked


def test_alert_settings_defaults_and_bounds(monkeypatch):
    s = load_with(monkeypatch, APP_PROFILE="hp")
    assert (s.alert_smtp_host, s.alert_smtp_port) == ("smtp.gmail.com", 587)
    assert s.alert_throttle_hours == 6 and s.alert_dry_run is False
    assert s.log_dir.name == "logs" and s.log_dir.is_absolute()
    with pytest.raises(ConfigError, match="ALERT_THROTTLE_HOURS"):
        load_with(monkeypatch, ALERT_THROTTLE_HOURS="-1")
