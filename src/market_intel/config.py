"""Typed settings for market-intel, loaded from environment variables and a .env file.

Loading order (highest priority first):
    1. Real environment variables.
    2. The .env file: ``MARKET_INTEL_ENV_FILE`` if set, otherwise ``.env`` at the repo root
       (found from this file's location, not the current directory).
    3. Defaults in this module.

Empty values (``KEY=``) count as "not set", so a copy of ``.env.example`` loads cleanly.

Settings are split into groups (database, database_admin, mongo_admin, upstox, llm,
alerts). A group is only checked when the code that needs it calls :meth:`Settings.require`,
so the app starts with just ``APP_PROFILE`` set.

Secret values are ``SecretStr`` and never appear in repr, dumps or error messages.

Run ``uv run python -m market_intel.config`` to print the settings with secrets masked.
"""

from __future__ import annotations

import os
import sys
from functools import lru_cache
from pathlib import Path
from typing import Literal, overload
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    ValidationError,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[2]
ENV_FILE_VAR = "MARKET_INTEL_ENV_FILE"
DECISION_DATE = "2026-11-12"

LLM_DEFAULT_CAP_INR = 1000
LLM_ABSOLUTE_MAX_INR = 2000

# restricted: testing, cloud databases, no broker keys (rule R1)
# full: main work, Docker databases, broker keys allowed
# server: later hosting
Profile = Literal["restricted", "full", "server"]
LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR"]
GroupName = Literal["database", "database_admin", "mongo_admin", "upstox", "llm", "alerts"]


class ConfigError(Exception):
    """Invalid or incomplete configuration. The message never contains secret values."""


# --- Group views -------------------------------------------------------------------------


class _Group(BaseModel):
    model_config = ConfigDict(frozen=True, hide_input_in_errors=True)


class DatabaseSettings(_Group):
    postgres_url: SecretStr
    mongodb_uri: SecretStr
    mongodb_db: str
    db_connect_timeout_s: int
    pg_storage_limit_mb: int
    mongo_storage_limit_mb: int
    app_profile: str  # decides whether Docker service names count as local hosts


class DatabaseAdminSettings(_Group):
    """Owner login, used ONLY by migrations (alembic). App code uses DatabaseSettings."""

    postgres_admin_url: SecretStr
    db_connect_timeout_s: int
    app_profile: str


class MongoAdminSettings(_Group):
    """Setup login (readWrite + dbAdmin), used ONLY by mongo_setup. App code never uses it."""

    mongodb_admin_uri: SecretStr
    mongodb_db: str
    db_connect_timeout_s: int
    app_profile: str


class UpstoxSettings(_Group):
    upstox_api_key: SecretStr
    upstox_api_secret: SecretStr
    upstox_redirect_uri: str


class LLMSettings(_Group):
    gemini_api_key: SecretStr
    groq_api_key: SecretStr | None
    paid_llm_api_key: SecretStr | None
    llm_monthly_cap_inr: int
    llm_warn_pct: int


class AlertSettings(_Group):
    alert_email_from: str
    alert_email_to: str
    alert_email_app_password: SecretStr


# Each group: (view model, fields that must be set, optional fields).
_GROUPS: dict[str, tuple[type[_Group], tuple[str, ...], tuple[str, ...]]] = {
    "database": (
        DatabaseSettings,
        ("postgres_url", "mongodb_uri"),
        (
            "mongodb_db",
            "db_connect_timeout_s",
            "pg_storage_limit_mb",
            "mongo_storage_limit_mb",
            "app_profile",
        ),
    ),
    "database_admin": (
        DatabaseAdminSettings,
        ("postgres_admin_url",),
        ("db_connect_timeout_s", "app_profile"),
    ),
    "mongo_admin": (
        MongoAdminSettings,
        ("mongodb_admin_uri",),
        ("mongodb_db", "db_connect_timeout_s", "app_profile"),
    ),
    "upstox": (
        UpstoxSettings,
        ("upstox_api_key", "upstox_api_secret", "upstox_redirect_uri"),
        (),
    ),
    "llm": (
        LLMSettings,
        ("gemini_api_key",),
        ("groq_api_key", "paid_llm_api_key", "llm_monthly_cap_inr", "llm_warn_pct"),
    ),
    "alerts": (
        AlertSettings,
        ("alert_email_from", "alert_email_to", "alert_email_app_password"),
        (),
    ),
}


# --- Settings ----------------------------------------------------------------------------


class Settings(BaseSettings):
    """All settings. Field names map to upper-case env vars (``app_profile`` -> APP_PROFILE).

    Times are always stored in UTC; ``display_timezone`` is only used when showing them.
    """

    model_config = SettingsConfigDict(
        env_ignore_empty=True,
        extra="ignore",
        hide_input_in_errors=True,
        frozen=True,
    )

    app_profile: Profile
    log_level: LogLevel = "INFO"
    display_timezone: str = "Asia/Kolkata"
    data_dir: Path = Path("data")
    log_dir: Path = Path("logs")
    # Backups are data: the folder must be outside the repo (checked below). Default: a
    # market-intel-backups folder next to the repo folder.
    backup_dir: Path = Path("../market-intel-backups")
    # health warns (backup_age) when the newest complete backup is older than this
    backup_max_age_days: int = Field(default=7, ge=1)

    # database
    postgres_url: SecretStr | None = None
    mongodb_uri: SecretStr | None = None
    postgres_admin_url: SecretStr | None = None  # migrations only
    mongodb_admin_uri: SecretStr | None = None  # mongo_setup only
    mongodb_db: str = "market_intel"
    db_connect_timeout_s: int = Field(default=15, ge=1)
    pg_storage_limit_mb: int = Field(default=1024, ge=1)  # Neon free plan
    mongo_storage_limit_mb: int = Field(default=512, ge=1)  # Atlas M0
    # upstox
    upstox_api_key: SecretStr | None = None
    upstox_api_secret: SecretStr | None = None
    upstox_redirect_uri: str | None = None
    # Upstox's published limits per API per user, and the share of them we use
    # (market_intel.rate_limit). Change the limits only if Upstox changes them.
    upstox_rate_per_second: int = Field(default=50, ge=1)
    upstox_rate_per_minute: int = Field(default=500, ge=1)
    upstox_rate_per_30min: int = Field(default=2000, ge=1)
    upstox_rate_headroom: float = Field(default=0.8, gt=0, le=1)
    # Upstox's public daily instrument file (no login), and the instrument refresh's safety
    # valve: refuse a file that keeps fewer than this share of the currently active list.
    upstox_instruments_url: str = (
        "https://assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz"
    )
    instruments_min_keep_ratio: float = Field(default=0.8, gt=0, le=1)
    # Upstox's public BSE instrument file: BSE scrip codes for NSE stocks, matched by ISIN
    upstox_bse_instruments_url: str = (
        "https://assets.upstox.com/market-quote/instruments/exchange/BSE.json.gz"
    )
    # news: the committed feed list (relative paths resolve from the repo root) and the size
    # cap of one feed response
    news_feeds_file: Path = Path("config/news_feeds.toml")
    news_fetch_max_bytes: int = Field(default=5 * 1024 * 1024, ge=1024)
    # per-company news from Google News RSS search feeds (market_intel.sources.google_news)
    google_news_rss_url: str = "https://news.google.com/rss/search"
    google_news_min_seconds: float = Field(default=10, ge=1)  # one request per this, process-wide
    google_news_company_interval_minutes: int = Field(default=60, ge=1)
    google_news_filers_cap: int = Field(default=30, ge=0)
    # llm
    gemini_api_key: SecretStr | None = None
    groq_api_key: SecretStr | None = None
    paid_llm_api_key: SecretStr | None = None
    llm_monthly_cap_inr: int = LLM_DEFAULT_CAP_INR
    llm_warn_pct: int = 80
    llm_cap_raise_approved: bool = False
    # alerts
    alert_email_from: str | None = None
    alert_email_to: str | None = None
    alert_email_app_password: SecretStr | None = None
    alert_smtp_host: str = "smtp.gmail.com"
    alert_smtp_port: int = Field(default=587, ge=1, le=65535)  # 587 STARTTLS, 465 SSL
    alert_throttle_hours: float = Field(default=6, ge=0)
    alert_dry_run: bool = False

    @field_validator("upstox_instruments_url", "upstox_bse_instruments_url", "google_news_rss_url")
    @classmethod
    def _https_only(cls, value: str) -> str:
        if not value.startswith("https://"):
            raise ValueError("this URL setting must start with https://")
        return value

    @field_validator("display_timezone")
    @classmethod
    def _valid_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)  # tzdata supplies the zone database on Windows
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError(
                f"unknown time zone {value!r}; use an IANA name like Asia/Kolkata"
            ) from None
        return value

    @model_validator(mode="after")
    def _resolve_and_check(self) -> Settings:
        for name in ("data_dir", "log_dir", "backup_dir", "news_feeds_file"):
            path: Path = getattr(self, name)
            if not path.is_absolute():
                # frozen model: bypass the setattr guard for these derived values
                object.__setattr__(self, name, (REPO_ROOT / path).resolve())
        # ConfigError is not a ValueError, so pydantic lets it through unwrapped.
        if is_inside(self.backup_dir, REPO_ROOT):
            raise ConfigError(
                "BACKUP_DIR must be outside the repo folder: backups are data and must never be "
                "committed. Leave it empty for the default (market-intel-backups next to the repo)."
            )
        _check_rules(self)
        return self

    def missing(self, group: GroupName) -> list[str]:
        """Env var names of the required settings in ``group`` that are not set."""
        _, required, _ = _group_spec(group)
        return [name.upper() for name in required if getattr(self, name) is None]

    @overload
    def require(self, group: Literal["database"]) -> DatabaseSettings: ...
    @overload
    def require(self, group: Literal["database_admin"]) -> DatabaseAdminSettings: ...
    @overload
    def require(self, group: Literal["mongo_admin"]) -> MongoAdminSettings: ...
    @overload
    def require(self, group: Literal["upstox"]) -> UpstoxSettings: ...
    @overload
    def require(self, group: Literal["llm"]) -> LLMSettings: ...
    @overload
    def require(self, group: Literal["alerts"]) -> AlertSettings: ...

    def require(self, group: GroupName) -> _Group:
        """Return the typed settings for ``group``, or raise ConfigError naming what's missing.

        Call this where a part of the system starts, e.g. ``settings.require("database")``.
        """
        view, required, optional = _group_spec(group)
        missing = self.missing(group)
        if missing:
            raise ConfigError(
                f"Settings group '{group}' is incomplete. Missing: {', '.join(missing)}. "
                "Set them in your .env (see .env.example for the names)."
            )
        return view(**{name: getattr(self, name) for name in required + optional})


def required_fields(group: GroupName) -> list[str]:
    """Env var names of the settings ``group`` needs."""
    _, required, _ = _group_spec(group)
    return [name.upper() for name in required]


def is_inside(path: Path, root: Path) -> bool:
    """True if ``path`` is ``root`` or anywhere below it (case-insensitive on Windows)."""
    resolved = Path(os.path.normcase(path.resolve()))
    base = Path(os.path.normcase(root.resolve()))
    return resolved == base or resolved.is_relative_to(base)


def _group_spec(group: str) -> tuple[type[_Group], tuple[str, ...], tuple[str, ...]]:
    if group not in _GROUPS:
        raise ConfigError(f"Unknown settings group '{group}'. Allowed: {', '.join(_GROUPS)}")
    return _GROUPS[group]


def _check_rules(s: Settings) -> None:
    """Decisions from docs/decisions.md enforced at load time."""
    if s.app_profile == "restricted" and (s.upstox_api_key or s.upstox_api_secret):
        raise ConfigError(
            "Rule R1: Upstox keys are not allowed on the restricted profile "
            f"(decision {DECISION_DATE}). Remove UPSTOX_API_KEY / UPSTOX_API_SECRET."
        )

    cap = s.llm_monthly_cap_inr
    if cap <= 0:
        raise ConfigError(f"Rule R2: LLM_MONTHLY_CAP_INR must be > 0 (decision {DECISION_DATE}).")
    if cap > LLM_ABSOLUTE_MAX_INR:
        raise ConfigError(
            f"Rule R2: LLM_MONTHLY_CAP_INR={cap} is above the absolute maximum of "
            f"Rs {LLM_ABSOLUTE_MAX_INR} (decision {DECISION_DATE})."
        )
    if cap > LLM_DEFAULT_CAP_INR and not s.llm_cap_raise_approved:
        raise ConfigError(
            f"Rule R2: LLM_MONTHLY_CAP_INR={cap} is above Rs {LLM_DEFAULT_CAP_INR} and needs "
            f"LLM_CAP_RAISE_APPROVED=true, after an approval logged in docs/decisions.md "
            f"(decision {DECISION_DATE})."
        )

    if not 1 <= s.llm_warn_pct <= 99:
        raise ConfigError(
            f"Rule R3: LLM_WARN_PCT must be between 1 and 99, got {s.llm_warn_pct} "
            f"(decision {DECISION_DATE})."
        )


# --- Loading -----------------------------------------------------------------------------


def env_file_path() -> Path:
    """The .env file to read: MARKET_INTEL_ENV_FILE if set, else ``<repo root>/.env``."""
    override = os.environ.get(ENV_FILE_VAR)
    if override:
        path = Path(override)
        if not path.is_file():
            raise ConfigError(f"{ENV_FILE_VAR} points to a file that does not exist: {path}")
        return path
    return REPO_ROOT / ".env"


def load_settings() -> Settings:
    """Load and validate settings (uncached). Raises ConfigError on any problem."""
    env_file = env_file_path()
    try:
        return Settings(_env_file=env_file if env_file.is_file() else None)
    except ValidationError as exc:
        raise ConfigError(_describe(exc)) from None


def _describe(exc: ValidationError) -> str:
    # Only the field name and pydantic's message; never the input value.
    lines = [f"{'.'.join(str(p) for p in e['loc']).upper()}: {e['msg']}" for e in exc.errors()]
    return "Invalid settings:\n  " + "\n  ".join(lines)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached settings for the app. In tests, call ``get_settings.cache_clear()`` to reload."""
    return load_settings()


# --- Command -----------------------------------------------------------------------------


_SECRET_TYPES = (SecretStr, SecretStr | None)
_EMAIL_FIELDS = ("alert_email_from", "alert_email_to")


def mask_email(address: str | None) -> str:
    """'mike@example.com' -> 'm***@example.com'. Output gets pasted into chats/screenshots."""
    if not address:
        return "(not set)"
    local, sep, domain = address.strip().rpartition("@")
    if not sep or not local:
        return "***"
    return f"{local[0]}***@{domain}"


def _plain_fields() -> list[str]:
    return [n for n, f in Settings.model_fields.items() if f.annotation not in _SECRET_TYPES]


def render(s: Settings) -> str:
    """Human-readable settings report. Secrets show only 'set' / 'not set'."""
    out = [f"profile: {s.app_profile}", ""]
    for name in _plain_fields():
        if name == "app_profile":
            continue
        value = getattr(s, name)
        if name in _EMAIL_FIELDS and value is not None:
            value = mask_email(value)
        out.append(f"  {name.upper()} = {'(default/unset)' if value is None else value}")
    for group, (_, required, optional) in _GROUPS.items():
        missing = s.missing(group)  # type: ignore[arg-type]
        status = "complete" if not missing else "missing " + ", ".join(missing)
        out += ["", f"[{group}] {status}"]
        for name in required + optional:
            if Settings.model_fields[name].annotation in _SECRET_TYPES:
                out.append(f"  {name.upper()}: {'set' if getattr(s, name) else 'not set'}")
    return "\n".join(out)


def main() -> int:
    """Entry point for ``python -m market_intel.config``. Returns the exit code."""
    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        return 1
    print(render(settings))
    return 0


if __name__ == "__main__":
    sys.exit(main())
