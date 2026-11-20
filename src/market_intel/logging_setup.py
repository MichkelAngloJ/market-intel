"""Structured logging: JSON lines to a file, readable lines to the console, secrets masked.

How it fits together (structlog + the standard ``logging`` module):

    structlog logger --(shared processors)--> wrap_for_formatter --+
                                                                  +--> stdlib root logger
    stdlib logger (e.g. urllib3) ---------------------------------+        |
                                                                    ProcessorFormatter per handler
                                                                    (foreign_pre_chain adds the same
                                                                     fields to stdlib records)
                                                                           |
                                                       mask secrets -> JSON file / console

Masking runs inside each handler's formatter, so it covers structlog and stdlib records alike.
All stored timestamps are UTC; only the console shows DISPLAY_TIMEZONE.

One file per component: each program passes its component name and writes
``LOG_DIR/<component>.jsonl``, which it rotates on its own. On Windows a file that another
process holds open can't be renamed, so two programs sharing one file would block each
other's rotation; separate files avoid that.

Usage::

    configure_logging(get_settings(), "ingest")
    log = get_logger(__name__)
    with bound_context(run_id=new_run_id(), component="ingest"):
        log.info("candles_fetched", instrument="RELIANCE", rows=375)
"""

from __future__ import annotations

import logging
import logging.handlers
import re
import sys
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import datetime
from typing import Any, TextIO
from zoneinfo import ZoneInfo

import structlog
from pydantic import SecretStr

from market_intel.config import Settings

MASK = "***"
DEFAULT_COMPONENT = "market_intel"
LOG_SUFFIX = ".jsonl"
BACKUP_DAYS = 30
_COMPONENT = re.compile(r"^[a-z][a-z0-9_]{0,39}$")


def log_file_name(component: str) -> str:
    """``<component>.jsonl``. Component names are short lower-case words (also safe as paths)."""
    if not _COMPONENT.fullmatch(component):
        raise ValueError(
            f"invalid log component {component!r}: use lower-case letters, digits and _"
        )
    return f"{component}{LOG_SUFFIX}"


LOG_FILE_NAME = log_file_name(DEFAULT_COMPONENT)  # used when no component is given

# Any key whose name contains one of these (case-insensitive) has its value masked.
_SENSITIVE_KEY = re.compile(
    r"key|secret|password|passwd|token|uri|url|authorization|cookie", re.IGNORECASE
)
# scheme://user:password@host -> mask only the password.  # pragma: allowlist secret
_URL_CREDENTIALS = re.compile(r"([a-zA-Z][a-zA-Z0-9+.\-]*://[^\s:/@]*:)([^\s@/]+)(@)")
# Keys structlog itself adds; never masked even if they match the pattern above.
_RESERVED_KEYS = {"event", "level", "logger", "timestamp", "exception"}

_secret_values: tuple[str, ...] = ()
_HANDLER_TAG = "_market_intel_handler"


# --- masking -----------------------------------------------------------------------------


def mask_text(text: str) -> str:
    """Mask URL credentials and any loaded secret value inside a string."""
    text = _URL_CREDENTIALS.sub(rf"\1{MASK}\3", text)
    for secret in _secret_values:  # longest first, so overlapping secrets mask fully
        text = text.replace(secret, MASK)
    return text


def mask_value(value: Any, key: str | None = None) -> Any:
    """Recursively mask a value. ``key`` is the name it's stored under, if any."""
    if key is not None and key not in _RESERVED_KEYS and _SENSITIVE_KEY.search(key):
        return MASK
    if isinstance(value, SecretStr):
        return MASK
    if isinstance(value, str):
        return mask_text(value)
    if isinstance(value, Mapping):
        return {k: mask_value(v, str(k)) for k, v in value.items()}
    if isinstance(value, list | tuple | set):
        return [mask_value(v) for v in value]
    return value


def _mask_processor(_logger: Any, _name: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    # structlog processor: runs last before rendering, on every record of every handler.
    return {k: mask_value(v, k) for k, v in event_dict.items()}


def _collect_secrets(settings: Settings) -> tuple[str, ...]:
    values: set[str] = set()
    for name in type(settings).model_fields:
        value = getattr(settings, name)
        if isinstance(value, SecretStr):
            raw = value.get_secret_value()
            if raw:
                values.add(raw)
                values.add(raw.replace(" ", ""))  # app passwords are often pasted with spaces
    values.discard("")
    return tuple(sorted(values, key=len, reverse=True))


# --- console time zone -------------------------------------------------------------------


def _display_time(tz: ZoneInfo) -> structlog.typing.Processor:
    def processor(_logger: Any, _name: str, event_dict: dict[str, Any]) -> dict[str, Any]:
        ts = event_dict.get("timestamp")
        if isinstance(ts, str):
            local = datetime.fromisoformat(ts).astimezone(tz)
            event_dict["timestamp"] = local.isoformat(timespec="seconds")
        return event_dict

    return processor


# --- setup -------------------------------------------------------------------------------


def _shared_processors() -> list[structlog.typing.Processor]:
    # Run on structlog records before they reach logging, and on stdlib records
    # (as foreign_pre_chain) so both kinds end up with the same fields.
    return [
        structlog.contextvars.merge_contextvars,  # adds run_id etc. bound with bound_context()
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True, key="timestamp"),
    ]


def reset_logging() -> None:
    """Remove and close the handlers added by :func:`configure_logging`."""
    root = logging.getLogger()
    for handler in [h for h in root.handlers if getattr(h, _HANDLER_TAG, False)]:
        root.removeHandler(handler)
        handler.close()


def configure_logging(
    settings: Settings,
    component: str = DEFAULT_COMPONENT,
    *,
    console_stream: TextIO | None = None,
) -> None:
    """Set up JSON file + console logging. Safe to call again (replaces its own handlers).

    ``component`` names the program (``health``, ``backup``, ...): it writes only to
    ``LOG_DIR/<component>.jsonl``. ``console_stream`` defaults to stderr; tests pass a StringIO.
    """
    global _secret_values
    file_name = log_file_name(component)  # validate before touching anything
    _secret_values = _collect_secrets(settings)

    reset_logging()
    settings.log_dir.mkdir(parents=True, exist_ok=True)
    shared = _shared_processors()

    # A new file every midnight UTC; the old ones get a date suffix, 30 are kept.
    file_handler = logging.handlers.TimedRotatingFileHandler(
        settings.log_dir / file_name,
        when="midnight",
        utc=True,
        backupCount=BACKUP_DAYS,
        encoding="utf-8",
    )
    file_handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            foreign_pre_chain=shared,
            processors=[
                structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                structlog.processors.format_exc_info,  # traceback -> "exception" string
                _mask_processor,
                structlog.processors.JSONRenderer(ensure_ascii=False),
            ],
        )
    )

    stream = console_stream or sys.stderr
    console_handler = logging.StreamHandler(stream)
    console_handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            foreign_pre_chain=shared,
            processors=[
                structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                structlog.processors.format_exc_info,
                _mask_processor,
                _display_time(ZoneInfo(settings.display_timezone)),
                structlog.dev.ConsoleRenderer(
                    colors=stream.isatty(),
                    exception_formatter=structlog.dev.plain_traceback,
                ),
            ],
        )
    )

    root = logging.getLogger()
    for handler in (file_handler, console_handler):
        setattr(handler, _HANDLER_TAG, True)
        root.addHandler(handler)
    root.setLevel(settings.log_level)

    structlog.configure(
        processors=[
            *shared,
            structlog.processors.StackInfoRenderer(),
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """A structlog logger that writes through the configured handlers."""
    return structlog.stdlib.get_logger(name)


# --- context -----------------------------------------------------------------------------


def new_run_id() -> str:
    """Short random id to tie together all log lines of one run."""
    return uuid.uuid4().hex[:12]


@contextmanager
def bound_context(**values: Any) -> Iterator[None]:
    """Add fields (run_id, component, instrument, source, ...) to every log line in the block."""
    with structlog.contextvars.bound_contextvars(**values):
        yield
