"""Upstox's public instrument file for NSE (``NSE.json.gz``, no login, refreshed daily ~06:00 IST).

Two steps, both safe to run on an untrusted file:

- :func:`download` fetches the file with a conditional GET (ETag / If-Modified-Since), a
  timeout, a ``market-intel`` User-Agent and a 50 MB cap on the compressed size, saves it as
  ``DATA_DIR/raw/upstox/instruments/NSE-<YYYYMMDD>.json.gz`` (git-ignored) and keeps the newest
  7. "Not modified" means there is nothing to do.
- :func:`parse_file` stream-decompresses with a hard 300 MB cap, parses with the ``json`` module
  only (nothing is ever executed or evaluated) and turns each kept object into an
  :class:`~market_intel.sources.records.Instrument` tagged ``upstox``. Kept: segment ``NSE_EQ``
  with series (``instrument_type``) EQ or BE, and every ``NSE_INDEX`` object. Everything else
  is skipped and counted by segment; objects that fail validation are skipped and counted as
  malformed.

The validators (:meth:`save_validators`) are written only after the database refresh has
committed, so a refused or failed refresh is retried with a full download next time.
"""

from __future__ import annotations

import gzip
import io
import json
import os
import re
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from market_intel.sources.records import Instrument

SOURCE = "upstox"
USER_AGENT = "market-intel"
TIMEOUT_S = 60.0
MAX_COMPRESSED_BYTES = 50 * 1024 * 1024
MAX_DECOMPRESSED_BYTES = 300 * 1024 * 1024
CHUNK = 1024 * 1024
KEEP_FILES = 7
FILE_PREFIX = "NSE-"
FILE_SUFFIX = ".json.gz"
STATE_FILE = "state.json"
GZIP_MAGIC = b"\x1f\x8b"

EQUITY_SEGMENT = "NSE_EQ"
INDEX_SEGMENT = "NSE_INDEX"
KEPT_SERIES = frozenset({"EQ", "BE"})
# Upstox gives tick sizes in paise (e.g. 5.0 = Rs 0.05); the table stores rupees.
PAISE_PER_RUPEE = Decimal(100)


class InstrumentFileError(Exception):
    """The file is too big, not gzip, not JSON or not a JSON array. Nothing was changed."""


def raw_dir(data_dir: Path) -> Path:
    return data_dir / "raw" / "upstox" / "instruments"


# --- download ----------------------------------------------------------------------------


@dataclass(frozen=True)
class Download:
    """Result of :func:`download`. ``path`` is None when the server said "not modified"."""

    path: Path | None
    etag: str | None = None
    last_modified: str | None = None

    @property
    def not_modified(self) -> bool:
        return self.path is None


def state_file(prefix: str = "NSE") -> str:
    """The validator file of one instrument file: ``state.json`` (NSE), ``state-bse.json``."""
    return STATE_FILE if prefix == "NSE" else f"state-{prefix.lower()}.json"


def _load_state(folder: Path, url: str, prefix: str = "NSE") -> dict[str, str]:
    try:
        state = json.loads((folder / state_file(prefix)).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(state, dict) or state.get("url") != url:
        return {}  # validators belong to another URL: download in full
    return {k: v for k, v in state.items() if isinstance(v, str)}


def save_validators(data_dir: Path, url: str, result: Download, prefix: str = "NSE") -> None:
    """Remember ETag / Last-Modified for the next conditional GET (after a committed refresh)."""
    folder = raw_dir(data_dir)
    folder.mkdir(parents=True, exist_ok=True)
    state = {"url": url}
    if result.etag:
        state["etag"] = result.etag
    if result.last_modified:
        state["last_modified"] = result.last_modified
    name = state_file(prefix)
    tmp = folder / (name + ".tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    os.replace(tmp, folder / name)


def _rotate(folder: Path, keep: int = KEEP_FILES, prefix: str = "NSE") -> None:
    files = sorted(folder.glob(f"{prefix}-*{FILE_SUFFIX}"), reverse=True)
    for old in files[keep:]:
        old.unlink(missing_ok=True)


def download(
    url: str,
    data_dir: Path,
    today: date,
    *,
    conditional: bool = True,
    save: bool = True,
    client: httpx.Client | None = None,
    prefix: str = "NSE",
) -> Download:
    """Fetch an instrument file (``prefix`` NSE or BSE names the file and its validators).
    See the module docstring.

    ``conditional=False`` always downloads in full (dry runs). ``save=False`` keeps the file in a
    temporary name next to the others and leaves the dated files alone (the caller deletes it).
    """
    folder = raw_dir(data_dir)
    folder.mkdir(parents=True, exist_ok=True)
    headers = {"User-Agent": USER_AGENT, "Accept-Encoding": "identity"}
    if conditional:
        state = _load_state(folder, url, prefix)
        if "etag" in state:
            headers["If-None-Match"] = state["etag"]
        if "last_modified" in state:
            headers["If-Modified-Since"] = state["last_modified"]

    own_client = client is None
    client = client or httpx.Client(timeout=TIMEOUT_S, follow_redirects=True, max_redirects=3)
    temp_name = "dry-run.tmp" if prefix == "NSE" else f"dry-run-{prefix.lower()}.tmp"
    target = folder / (f"{prefix}-{today:%Y%m%d}{FILE_SUFFIX}" if save else temp_name)
    part = target.with_name(target.name + ".part")
    try:
        with client.stream("GET", url, headers=headers, timeout=TIMEOUT_S) as response:
            if response.status_code == 304:
                return Download(path=None)
            response.raise_for_status()
            if not str(response.url).startswith("https://"):
                raise InstrumentFileError("the instrument file was served without https")
            length = response.headers.get("Content-Length")
            if length and length.isdigit() and int(length) > MAX_COMPRESSED_BYTES:
                raise InstrumentFileError(
                    f"instrument file too large: {int(length)} bytes (limit {MAX_COMPRESSED_BYTES})"
                )
            size = 0
            with part.open("wb") as out:
                # Raw bytes exactly as sent: the .gz file itself, never decoded by httpx.
                for chunk in response.iter_raw(CHUNK):
                    size += len(chunk)
                    if size > MAX_COMPRESSED_BYTES:
                        raise InstrumentFileError(
                            f"instrument file larger than {MAX_COMPRESSED_BYTES} bytes"
                        )
                    out.write(chunk)
            etag = response.headers.get("ETag")
            last_modified = response.headers.get("Last-Modified")
        os.replace(part, target)
    finally:
        part.unlink(missing_ok=True)
        if own_client:
            client.close()
    if save:
        _rotate(folder, prefix=prefix)
    return Download(path=target, etag=etag, last_modified=last_modified)


# --- parsing -----------------------------------------------------------------------------


def read_json_gz(path: Path, *, max_bytes: int = MAX_DECOMPRESSED_BYTES) -> Any:
    """Decompress in chunks with a hard cap, then parse with the json module."""
    if path.stat().st_size > MAX_COMPRESSED_BYTES:
        raise InstrumentFileError(f"instrument file larger than {MAX_COMPRESSED_BYTES} bytes")
    with path.open("rb") as raw:
        if raw.read(2) != GZIP_MAGIC:
            raise InstrumentFileError("instrument file is not gzip")
        raw.seek(0)
        buffer = io.BytesIO()
        try:
            with gzip.GzipFile(fileobj=raw) as gz:
                while chunk := gz.read(CHUNK):
                    if buffer.tell() + len(chunk) > max_bytes:
                        raise InstrumentFileError(
                            f"instrument file decompresses to more than {max_bytes} bytes"
                        )
                    buffer.write(chunk)
        except (OSError, EOFError) as exc:  # gzip.BadGzipFile is an OSError
            raise InstrumentFileError(f"instrument file is not valid gzip: {exc}") from None
    try:
        return json.loads(buffer.getvalue())
    except (ValueError, RecursionError) as exc:
        raise InstrumentFileError(f"instrument file is not valid JSON: {exc}") from None


def _text(value: Any) -> Any:
    return value.strip() if isinstance(value, str) else value


class _UpstoxObject(BaseModel):
    """The fields we use from one Upstox instrument object; extra fields are ignored."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    segment: str
    instrument_type: str
    instrument_key: str = Field(min_length=1, max_length=100)
    trading_symbol: str = Field(min_length=1, max_length=60)
    name: str | None = Field(default=None, max_length=300)
    isin: str | None = Field(default=None, pattern=r"^[A-Z]{2}[A-Z0-9]{9}[0-9]$")
    lot_size: int | None = Field(default=None, ge=1)
    tick_size: float | None = Field(default=None, gt=0)

    @field_validator(
        "segment",
        "instrument_type",
        "instrument_key",
        "trading_symbol",
        "name",
        "isin",
        mode="before",
    )
    @classmethod
    def _strip(cls, value: Any) -> Any:
        return _text(value)


def _to_record(obj: _UpstoxObject) -> Instrument:
    is_index = obj.segment == INDEX_SEGMENT
    if not obj.instrument_key.startswith(f"{obj.segment}|"):
        raise ValueError("instrument_key doesn't start with its segment")
    if not is_index and obj.isin is None:
        raise ValueError("equity without an ISIN")
    tick = None
    if obj.tick_size is not None:
        try:
            tick = (Decimal(str(obj.tick_size)) / PAISE_PER_RUPEE).normalize()
        except InvalidOperation:
            raise ValueError("bad tick_size") from None
    return Instrument(
        source=SOURCE,
        exchange="NSE",
        segment="INDEX" if is_index else "EQ",
        symbol=obj.trading_symbol,
        name=obj.name or None,
        series=None if is_index else obj.instrument_type,
        isin=None if is_index else obj.isin,
        instrument_key=obj.instrument_key,
        tick_size=None if is_index else tick,
        lot_size=None if is_index else obj.lot_size,
    )


@dataclass
class ParseResult:
    """Kept instruments plus what was skipped and why."""

    instruments: list[Instrument] = field(default_factory=list)
    skipped_by_segment: Counter[str] = field(default_factory=Counter)
    malformed: int = 0

    def count_by(self) -> dict[str, int]:
        """EQ and BE stocks and indices among the kept instruments."""
        counts = Counter(i.series if i.segment == "EQ" else "INDEX" for i in self.instruments)
        return {"EQ": counts["EQ"], "BE": counts["BE"], "INDEX": counts["INDEX"]}


def _kind(obj: dict[str, Any]) -> str | None:
    """'keep', or the skip label (the segment, with the series for other equity series)."""
    segment, series = obj.get("segment"), obj.get("instrument_type")
    if segment == INDEX_SEGMENT:
        return "keep"
    if segment == EQUITY_SEGMENT:
        return "keep" if series in KEPT_SERIES else f"{EQUITY_SEGMENT}:other_series"
    return segment if isinstance(segment, str) and len(segment) <= 40 else None


def parse_objects(objects: Iterable[Any]) -> ParseResult:
    """Filter and validate decoded objects (see the module docstring)."""
    result = ParseResult()
    seen_keys: set[str] = set()
    seen_symbols: set[tuple[str, str]] = set()
    for obj in objects:
        if not isinstance(obj, dict):
            result.malformed += 1
            continue
        kind = _kind(obj)
        if kind is None:
            result.malformed += 1
            continue
        if kind != "keep":
            result.skipped_by_segment[kind] += 1
            continue
        try:
            record = _to_record(_UpstoxObject.model_validate(obj))
        except (ValidationError, ValueError):
            result.malformed += 1
            continue
        symbol_key = (record.segment, record.symbol)
        if record.instrument_key in seen_keys or symbol_key in seen_symbols:
            result.malformed += 1  # a duplicate can't be stored twice
            continue
        seen_keys.add(record.instrument_key or "")
        seen_symbols.add(symbol_key)
        result.instruments.append(record)
    return result


def parse_file(path: Path, *, reader: Callable[[Path], Any] = read_json_gz) -> ParseResult:
    """Read, filter and validate one instrument file."""
    data = reader(path)
    if not isinstance(data, list):
        raise InstrumentFileError("instrument file is not a JSON array")
    return parse_objects(data)


# --- BSE scrip codes ---------------------------------------------------------------------

BSE_EQUITY_SEGMENT = "BSE_EQ"
_ISIN = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}[0-9]$")
# Upstox's BSE file: ``exchange_token`` holds the BSE scrip code (a 6-digit string; checked
# 2026-11-22 against the scrip codes in BSE's own announcement feed).
_SCRIP_CODE = re.compile(r"^[0-9]{6}$")


@dataclass
class BseCodes:
    """ISIN -> BSE scrip code from the BSE_EQ objects, plus what was skipped."""

    by_isin: dict[str, str] = field(default_factory=dict)
    other_segments: int = 0
    malformed: int = 0


def parse_bse_codes(path: Path, *, reader: Callable[[Path], Any] = read_json_gz) -> BseCodes:
    """Read Upstox's BSE file (same untrusted-data rules as the NSE file) into ISIN -> code.

    An ISIN or a code that appears twice is dropped entirely (ambiguous)."""
    data = reader(path)
    if not isinstance(data, list):
        raise InstrumentFileError("instrument file is not a JSON array")
    result = BseCodes()
    seen_codes: Counter[str] = Counter()
    seen_isins: Counter[str] = Counter()
    pairs: list[tuple[str, str]] = []
    for obj in data:
        if not isinstance(obj, dict):
            result.malformed += 1
            continue
        if obj.get("segment") != BSE_EQUITY_SEGMENT:
            result.other_segments += 1
            continue
        isin, code = obj.get("isin"), obj.get("exchange_token")
        isin = isin.strip() if isinstance(isin, str) else ""
        code = str(code).strip() if isinstance(code, (str, int)) else ""
        if not (_ISIN.fullmatch(isin) and _SCRIP_CODE.fullmatch(code)):
            result.malformed += 1
            continue
        seen_isins[isin] += 1
        seen_codes[code] += 1
        pairs.append((isin, code))
    for isin, code in pairs:
        if seen_isins[isin] == 1 and seen_codes[code] == 1:
            result.by_isin[isin] = code
        else:
            result.malformed += 1
    return result
