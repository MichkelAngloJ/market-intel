"""Fake Upstox instrument objects and a fake file server for the instrument tests (fake ISINs
and symbols only; no real Upstox file is ever stored in the repo)."""

import gzip
import json
from pathlib import Path

import httpx


def eq(symbol: str, isin: str, series: str = "EQ", **extra) -> dict:
    """A fake NSE equity object shaped like Upstox's (fake ISINs and symbols)."""
    return {
        "segment": "NSE_EQ",
        "name": f"{symbol} FAKE LIMITED",
        "exchange": "NSE",
        "isin": isin,
        "instrument_type": series,
        "instrument_key": f"NSE_EQ|{isin}",
        "lot_size": 1,
        "freeze_quantity": 100000.0,
        "exchange_token": "99901",
        "tick_size": 5.0,
        "trading_symbol": symbol,
        "short_name": symbol,
        "security_type": "NORMAL",
        **extra,
    }


def index(symbol: str) -> dict:
    return {
        "segment": "NSE_INDEX",
        "name": f"{symbol} Fake Index",
        "exchange": "NSE",
        "instrument_type": "INDEX",
        "instrument_key": f"NSE_INDEX|{symbol}",
        "exchange_token": "26000",
        "trading_symbol": symbol,
    }


def gz_bytes(objects) -> bytes:
    return gzip.compress(json.dumps(objects).encode())


def write_gz(tmp_path: Path, objects, name: str = "fake.json.gz") -> Path:
    path = tmp_path / name
    path.write_bytes(gz_bytes(objects))
    return path


class Server:
    """A fake file server: serves ``body`` with an ETag, answers 304 to a matching ETag."""

    def __init__(self, body: bytes, etag: str = '"v1"', headers: dict | None = None):
        self.body, self.etag, self.extra = body, etag, headers or {}
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.headers.get("If-None-Match") == self.etag:
            return httpx.Response(304)
        headers = {
            "ETag": self.etag,
            "Last-Modified": "Sun, 22 Nov 2026 00:30:00 GMT",
            "Content-Type": "application/gzip",
            **self.extra,
        }
        # A stream, like a real server (iter_raw can't re-read pre-loaded content).
        return httpx.Response(200, stream=httpx.ByteStream(self.body), headers=headers)

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self))
