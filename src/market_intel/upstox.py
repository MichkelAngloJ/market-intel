"""Upstox login, the daily access token and the API client (sprint S1; decision 2026-11-24).

    uv run python -m market_intel.upstox login     # once a day, in the owner's own terminal
    uv run python -m market_intel.upstox status    # valid / expired and the hours left

Login (Upstox's authorization-code flow, checked 2026-11-24):

1. Opens ``https://api.upstox.com/v2/login/authorization/dialog`` with ``client_id``,
   ``redirect_uri``, ``response_type=code`` and a random ``state``. The owner logs in on
   Upstox's own page; this code never sees the password or TOTP and never automates the page.
2. Waits on 127.0.0.1 (the port and path of ``UPSTOX_REDIRECT_URI``) for ONE request to the
   callback path, at most 3 minutes, and checks ``state``. Fallback: the owner pastes the full
   address from the browser bar.
3. Exchanges the single-use code at ``/v2/login/authorization/token`` (form body).

The token is stored ONLY in the operating system's credential store (Windows Credential
Manager, through ``keyring``). Non-secret metadata (issued_at, expires_at) is in
``DATA_DIR/state/upstox_token.json``. The token is never printed or logged; it is registered
with log masking as soon as it is loaded. Upstox tokens last until 03:30 IST: expires_at is
the next 03:30 IST after the login.

The API client sends the Bearer token through the Upstox rate limiter. HTTP 401 raises
:class:`TokenExpired` ("run: upstox login") and queues one throttled WARNING alert. This
module never calls order APIs.
"""

from __future__ import annotations

import argparse
import http.server
import json
import os
import secrets
import socketserver
import sys
import threading
import webbrowser
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit
from zoneinfo import ZoneInfo

import httpx
import keyring

from market_intel import __version__
from market_intel.alerts import queue_alert
from market_intel.config import ConfigError, Settings, get_settings
from market_intel.logging_setup import (
    bound_context,
    configure_logging,
    get_logger,
    new_run_id,
    register_secret,
)
from market_intel.rate_limit import RateLimited, RateLimiter, parse_retry_after, upstox_limiter

COMPONENT = "upstox"
AUTH_URL = "https://api.upstox.com/v2/login/authorization/dialog"
TOKEN_URL = "https://api.upstox.com/v2/login/authorization/token"
API_BASE = "https://api.upstox.com"
KEYRING_SERVICE = "market-intel-upstox"
KEYRING_USER = "access_token"
META_NAME = "upstox_token.json"
CALLBACK_TIMEOUT_S = 180
IST = ZoneInfo("Asia/Kolkata")
EXPIRY_TIME_IST = time(3, 30)
LOW_HOURS = 1.0
USER_AGENT = f"market-intel/{__version__} (personal research)"
EXPIRED_TITLE = "Upstox token expired"

log = get_logger("market_intel.upstox")


class UpstoxError(Exception):
    """An Upstox request failed (not an expired token)."""


class TokenExpired(UpstoxError):
    """No valid access token: run ``uv run python -m market_intel.upstox login``."""


class LoginError(Exception):
    """The login didn't complete (state mismatch, timeout, refused code)."""


# --- token storage -----------------------------------------------------------------------


def next_expiry(issued_at: datetime) -> datetime:
    """The next 03:30 IST after ``issued_at`` (UTC)."""
    local = issued_at.astimezone(IST)
    candidate = datetime.combine(local.date(), EXPIRY_TIME_IST, IST)
    if candidate <= local:
        candidate += timedelta(days=1)
    return candidate.astimezone(UTC)


def meta_path(data_dir: Path) -> Path:
    return data_dir / "state" / META_NAME


def save_token(settings: Settings, token: str, issued_at: datetime) -> datetime:
    """Token -> credential store; metadata -> state file. Returns expires_at."""
    keyring.set_password(KEYRING_SERVICE, KEYRING_USER, token)
    expires_at = next_expiry(issued_at)
    path = meta_path(settings.data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    meta = {
        "issued_at": issued_at.astimezone(UTC).isoformat(),
        "expires_at": expires_at.isoformat(),
    }
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    os.replace(tmp, path)
    return expires_at


def load_meta(settings: Settings) -> dict[str, datetime]:
    try:
        raw = json.loads(meta_path(settings.data_dir).read_text(encoding="utf-8"))
        return {k: datetime.fromisoformat(raw[k]) for k in ("issued_at", "expires_at")}
    except (OSError, ValueError, KeyError, TypeError):
        return {}


@dataclass(frozen=True)
class TokenStatus:
    state: str  # "valid", "expired", "missing"
    hours_left: float = 0.0
    expires_at: datetime | None = None

    @property
    def valid(self) -> bool:
        return self.state == "valid"

    def line(self) -> str:
        if self.state == "missing":
            return "Upstox token: missing (run: uv run python -m market_intel.upstox login)"
        if self.state == "expired":
            return "Upstox token: expired (run: uv run python -m market_intel.upstox login)"
        when = self.expires_at.astimezone(IST).strftime("%Y-%m-%d %H:%M IST")
        return f"Upstox token: valid, {self.hours_left:.1f} h left (until {when})"


def token_status(settings: Settings, now: datetime | None = None) -> TokenStatus:
    """Valid / expired / missing from the metadata and the credential store (token not read
    into any output)."""
    now = now or datetime.now(UTC)
    meta = load_meta(settings)
    if not meta or keyring.get_password(KEYRING_SERVICE, KEYRING_USER) is None:
        return TokenStatus("missing")
    left = (meta["expires_at"] - now).total_seconds() / 3600
    if left <= 0:
        return TokenStatus("expired", 0.0, meta["expires_at"])
    return TokenStatus("valid", left, meta["expires_at"])


def load_token(settings: Settings, now: datetime | None = None) -> str:
    """The access token for API calls. Raises TokenExpired if missing or expired."""
    status = token_status(settings, now)
    if not status.valid:
        raise TokenExpired(f"no valid Upstox token ({status.state}); run: upstox login")
    token = keyring.get_password(KEYRING_SERVICE, KEYRING_USER) or ""
    register_secret(token)  # masked in every log line from now on
    return token


# --- login -------------------------------------------------------------------------------


def authorization_url(settings: Settings, state: str) -> str:
    up = settings.require("upstox")
    params = {
        "client_id": up.upstox_api_key.get_secret_value(),
        "redirect_uri": up.upstox_redirect_uri,
        "response_type": "code",
        "state": state,
    }
    return f"{AUTH_URL}?{urlencode(params)}"


def parse_callback(address: str, expected_state: str, callback_path: str) -> str:
    """The ``code`` from a callback address (``/upstox/callback?code=..&state=..``)."""
    parts = urlsplit(address.strip())
    if parts.path.rstrip("/") != callback_path.rstrip("/"):
        raise LoginError("that isn't the callback address")
    query = parse_qs(parts.query)
    if query.get("state", [""])[0] != expected_state:
        raise LoginError("the state doesn't match: start the login again")
    if "error" in query:
        raise LoginError(f"Upstox refused the login: {query['error'][0]}")
    code = query.get("code", [""])[0]
    if not code:
        raise LoginError("the address has no code")
    return code


def wait_for_callback(
    host: str, port: int, path: str, state: str, timeout_s: float = CALLBACK_TIMEOUT_S
) -> str | None:
    """Serve 127.0.0.1:<port> until ONE request reaches ``path`` (or the timeout). Returns
    the code, or None on timeout."""
    result: dict[str, Any] = {}
    done = threading.Event()

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 (http.server naming)
            if urlsplit(self.path).path.rstrip("/") != path.rstrip("/"):
                self.send_response(404)
                self.end_headers()
                return
            try:
                result["code"] = parse_callback(self.path, state, path)
                body = b"Login received. You can close this tab and return to the terminal."
            except LoginError as exc:
                result["error"] = str(exc)
                body = b"Login failed. Return to the terminal."
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(body)
            done.set()

        def log_message(self, *args: Any) -> None:  # the query holds the code: never log it
            return

    class Server(socketserver.TCPServer):
        allow_reuse_address = True

    with Server((host, port), Handler) as server:
        server.timeout = 0.5
        deadline = datetime.now(UTC) + timedelta(seconds=timeout_s)
        while not done.is_set() and datetime.now(UTC) < deadline:
            server.handle_request()
    if "error" in result:
        raise LoginError(result["error"])
    return result.get("code")


def exchange_code(settings: Settings, code: str, client: httpx.Client | None = None) -> str:
    """POST the single-use code to the token endpoint; returns the access token."""
    up = settings.require("upstox")
    form = {
        "code": code,
        "client_id": up.upstox_api_key.get_secret_value(),
        "client_secret": up.upstox_api_secret.get_secret_value(),
        "redirect_uri": up.upstox_redirect_uri,
        "grant_type": "authorization_code",
    }
    own = client is None
    client = client or httpx.Client(timeout=30)
    try:
        response = client.post(
            TOKEN_URL,
            data=form,
            headers={"Accept": "application/json", "User-Agent": USER_AGENT},
        )
    finally:
        if own:
            client.close()
    if response.status_code != 200:
        raise LoginError(f"the token exchange failed (HTTP {response.status_code})")
    try:
        token = response.json().get("access_token")
    except ValueError:
        token = None
    if not isinstance(token, str) or not token:
        raise LoginError("the token exchange returned no access token")
    register_secret(token)
    return token


def refuse_restricted(settings: Settings) -> None:
    if settings.app_profile == "restricted":
        raise ConfigError("Upstox is not allowed on the restricted profile (rule R1)")


def login(
    settings: Settings,
    *,
    open_browser: Callable[[str], Any] = webbrowser.open,
    wait: Callable[..., str | None] = wait_for_callback,
    ask: Callable[[str], str] = input,
    say: Callable[[str], None] = print,
    client: httpx.Client | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> TokenStatus:
    """The browser login (see the module docstring). Never prints the token."""
    refuse_restricted(settings)
    up = settings.require("upstox")
    redirect = urlsplit(up.upstox_redirect_uri)
    if redirect.hostname not in ("127.0.0.1", "localhost") or not redirect.port:
        raise ConfigError("UPSTOX_REDIRECT_URI must be http://127.0.0.1:<port>/<path>")
    state = secrets.token_urlsafe(24)
    url = authorization_url(settings, state)
    say("Opening the Upstox login page in your browser. Log in there (password and TOTP stay")
    say("with Upstox). If no browser opens, copy this address into one:")
    say(url.replace(up.upstox_api_key.get_secret_value(), "<your API key>"))
    open_browser(url)
    code = wait("127.0.0.1", redirect.port, redirect.path, state)
    if code is None:
        say("No callback within 3 minutes. Paste the full address from the browser bar")
        say("(it starts with your redirect address and contains ?code=...):")
        code = parse_callback(ask("> "), state, redirect.path)
    token = exchange_code(settings, code, client)
    save_token(settings, token, now())
    log.info("upstox_login_ok")
    return token_status(settings, now())


# --- API client --------------------------------------------------------------------------

_limiter: RateLimiter | None = None


def process_limiter(settings: Settings) -> RateLimiter:
    """One Upstox limiter per process (Upstox counts its limits per user)."""
    global _limiter
    if _limiter is None:
        _limiter = upstox_limiter(settings)
    return _limiter


class UpstoxClient:
    """GET requests to the Upstox API with the Bearer token and the rate limiter. Read-only
    market data only: no order APIs."""

    def __init__(
        self,
        settings: Settings,
        *,
        token: str | None = None,
        client: httpx.Client | None = None,
        limiter: RateLimiter | None = None,
        alert: Callable[..., Any] = queue_alert,
    ) -> None:
        self.settings = settings
        self.token = token if token is not None else load_token(settings)
        self.http = client or httpx.Client(timeout=30)
        self.limiter = limiter or process_limiter(settings)
        self.alert = alert

    def close(self) -> None:
        self.http.close()

    def get(self, path: str) -> Any:
        def call() -> Any:
            response = self.http.get(
                f"{API_BASE}{path}",
                headers={
                    "Accept": "application/json",
                    "Authorization": f"Bearer {self.token}",
                    "User-Agent": USER_AGENT,
                },
            )
            if response.status_code == 429:
                raise RateLimited(parse_retry_after(response.headers.get("Retry-After")))
            return response

        try:
            response = self.limiter.call(call)
        except httpx.HTTPError as exc:
            raise UpstoxError(f"{type(exc).__name__}: {exc}") from None
        except RateLimited as exc:
            raise UpstoxError("rate limited by Upstox (HTTP 429)") from exc
        if response.status_code == 401:
            self.alert(
                "warning",
                EXPIRED_TITLE,
                "Upstox refused the access token (HTTP 401). Run in a terminal:\n"
                "uv run python -m market_intel.upstox login",
                key="upstox_token_expired",
            )
            log.warning("upstox_token_rejected")
            raise TokenExpired("Upstox refused the token (HTTP 401); run: upstox login")
        if response.status_code >= 400:
            raise UpstoxError(f"HTTP {response.status_code}: {_error_code(response)}")
        try:
            return response.json()
        except ValueError:
            raise UpstoxError("the response is not JSON") from None


def _error_code(response: httpx.Response) -> str:
    try:
        errors = response.json().get("errors") or []
        return ", ".join(str(e.get("errorCode")) for e in errors if isinstance(e, dict)) or "-"
    except (ValueError, AttributeError):
        return "-"


# --- entry point -------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    """Entry point for ``python -m market_intel.upstox``."""
    parser = argparse.ArgumentParser(prog="python -m market_intel.upstox")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("login", help="log in to Upstox in the browser and store today's token")
    sub.add_parser("status", help="is the stored token valid, and for how long")
    args = parser.parse_args(argv)
    try:
        settings = get_settings()
    except ConfigError as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        return 1
    configure_logging(settings, COMPONENT)
    with bound_context(run_id=new_run_id(), component=COMPONENT):
        try:
            if args.command == "login":
                print(login(settings).line())
            else:
                refuse_restricted(settings)
                status = token_status(settings)
                print(status.line())
                return 0 if status.valid else 1
        except (ConfigError, LoginError) as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
