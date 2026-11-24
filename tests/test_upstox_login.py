"""Upstox login and token (S1 part A): the authorization URL and state, the local callback and
the paste fallback, keyring storage (an in-memory backend, never the real Credential
Manager), expiry, status, the restricted refusal, the API client's 401 handling + alert, and
health's upstox_token check. No network: httpx MockTransport and a loopback callback server."""

import io
import socket
import threading
import time
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import httpx
import keyring
import keyring.backend
import pytest

from market_intel import health, upstox
from market_intel.config import ConfigError, load_settings
from market_intel.logging_setup import LOG_FILE_NAME, configure_logging, get_logger
from market_intel.rate_limit import RateLimiter, Window

FAKE_KEY = "-".join(["fake", "api", "key", "111"])  # built from parts: not real
FAKE_SECRET = "-".join(["fake", "api", "sec", "222"])
FAKE_TOKEN = "-".join(["fake", "access", "tok", "333"])
FAKE_PW = "fake-pass-444"
T0 = datetime(2026, 11, 24, 4, 30, tzinfo=UTC)  # 10:00 IST


class MemoryKeyring(keyring.backend.KeyringBackend):
    priority = 1

    def __init__(self):
        super().__init__()
        self.store: dict[tuple[str, str], str] = {}

    def get_password(self, service, username):
        return self.store.get((service, username))

    def set_password(self, service, username, password):
        self.store[(service, username)] = password

    def delete_password(self, service, username):
        self.store.pop((service, username), None)


@pytest.fixture
def ring():
    original = keyring.get_keyring()
    fake = MemoryKeyring()
    keyring.set_keyring(fake)
    yield fake
    keyring.set_keyring(original)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def settings(tmp_path, monkeypatch, ring):
    port = free_port()
    for key, value in {
        "APP_PROFILE": "full",
        "DATA_DIR": str(tmp_path / "data"),
        "LOG_DIR": str(tmp_path / "logs"),
        "POSTGRES_URL": f"postgresql://fake_user:{FAKE_PW}@db.invalid/x?sslmode=require",
        "MONGODB_URI": f"mongodb+srv://fake_user:{FAKE_PW}@cluster.invalid/",
        "UPSTOX_API_KEY": FAKE_KEY,
        "UPSTOX_API_SECRET": FAKE_SECRET,
        "UPSTOX_REDIRECT_URI": f"http://127.0.0.1:{port}/upstox/callback",
        "ALERT_DRY_RUN": "true",
    }.items():
        monkeypatch.setenv(key, value)
    s = load_settings()
    configure_logging(s, console_stream=io.StringIO())
    return s


def token_server(seen: list):
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"access_token": FAKE_TOKEN, "user_id": "X"})

    return httpx.Client(transport=httpx.MockTransport(handler))


# --- URL, state, callback ----------------------------------------------------------------


def test_the_authorization_url_has_client_redirect_code_and_state(settings):
    url = upstox.authorization_url(settings, "st4te")
    parts = urlsplit(url)
    assert f"{parts.scheme}://{parts.netloc}{parts.path}" == upstox.AUTH_URL
    query = parse_qs(parts.query)
    assert query == {
        "client_id": [FAKE_KEY],
        "redirect_uri": [settings.upstox_redirect_uri],
        "response_type": ["code"],
        "state": ["st4te"],
    }


def test_parse_callback_checks_path_state_and_code():
    good = "http://127.0.0.1:8765/upstox/callback?code=abc&state=s1"
    assert upstox.parse_callback(good, "s1", "/upstox/callback") == "abc"
    with pytest.raises(upstox.LoginError, match="state doesn't match"):
        upstox.parse_callback(good, "other", "/upstox/callback")
    with pytest.raises(upstox.LoginError, match="isn't the callback"):
        upstox.parse_callback(
            "http://127.0.0.1:8765/elsewhere?code=a&state=s1", "s1", "/upstox/callback"
        )
    with pytest.raises(upstox.LoginError, match="no code"):
        upstox.parse_callback("/upstox/callback?state=s1", "s1", "/upstox/callback")


def test_the_local_callback_accepts_one_request_on_its_path():
    port, state = free_port(), "s-123"
    result = {}
    server = threading.Thread(
        target=lambda: result.update(
            code=upstox.wait_for_callback("127.0.0.1", port, "/upstox/callback", state, 10)
        )
    )
    server.start()
    time.sleep(0.3)
    with httpx.Client() as c:
        assert c.get(f"http://127.0.0.1:{port}/favicon.ico").status_code == 404
        reply = c.get(f"http://127.0.0.1:{port}/upstox/callback?code=c0de&state={state}")
    server.join(10)
    assert reply.status_code == 200 and "close this tab" in reply.text
    assert result["code"] == "c0de"


def test_the_callback_times_out_with_none():
    assert upstox.wait_for_callback("127.0.0.1", free_port(), "/cb", "s", timeout_s=0.6) is None


# --- login, storage, status --------------------------------------------------------------


def test_login_with_the_callback_stores_the_token_only_in_the_keyring(settings, ring, tmp_path):
    seen, said, opened = [], [], []
    status = upstox.login(
        settings,
        open_browser=opened.append,
        wait=lambda host, port, path, state: "c0de",
        say=said.append,
        client=token_server(seen),
        now=lambda: T0,
    )
    assert status.valid and opened and opened[0].startswith(upstox.AUTH_URL)
    form = parse_qs(seen[0].content.decode())
    assert form == {
        "code": ["c0de"],
        "client_id": [FAKE_KEY],
        "client_secret": [FAKE_SECRET],
        "redirect_uri": [settings.upstox_redirect_uri],
        "grant_type": ["authorization_code"],
    }
    assert seen[0].headers["Content-Type"] == "application/x-www-form-urlencoded"
    assert ring.store[(upstox.KEYRING_SERVICE, upstox.KEYRING_USER)] == FAKE_TOKEN
    meta = upstox.meta_path(settings.data_dir).read_text(encoding="utf-8")
    assert FAKE_TOKEN not in meta and "expires_at" in meta
    printed = "\n".join(said)
    assert FAKE_TOKEN not in printed and FAKE_KEY not in printed and FAKE_SECRET not in printed


def test_the_paste_fallback_when_no_callback_arrives(settings, ring):
    states = []

    def ask(prompt):
        url = states[0]
        state = parse_qs(urlsplit(url).query)["state"][0]
        return f"http://127.0.0.1:8765/upstox/callback?code=pasted&state={state}"

    upstox.login(
        settings, open_browser=states.append, wait=lambda *a: None, ask=ask,
        say=lambda line: None, client=token_server([]), now=lambda: T0,
    )  # fmt: skip
    assert ring.store[(upstox.KEYRING_SERVICE, upstox.KEYRING_USER)] == FAKE_TOKEN


def test_a_pasted_address_with_a_wrong_state_is_refused(settings, ring):
    with pytest.raises(upstox.LoginError, match="state"):
        upstox.login(
            settings, open_browser=lambda u: None, wait=lambda *a: None,
            ask=lambda p: "http://127.0.0.1:8765/upstox/callback?code=x&state=forged",
            say=lambda line: None, client=token_server([]),
        )  # fmt: skip
    assert not ring.store


def test_a_failed_token_exchange_stores_nothing(settings, ring):
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(401, json={})))
    with pytest.raises(upstox.LoginError, match="HTTP 401"):
        upstox.login(settings, open_browser=lambda u: None, wait=lambda *a: "c",
                     say=lambda line: None, client=client)  # fmt: skip
    assert not ring.store


def test_login_is_refused_on_the_restricted_profile(settings, monkeypatch):
    with pytest.raises(ConfigError, match="restricted"):
        upstox.refuse_restricted(settings.model_copy(update={"app_profile": "restricted"}))


@pytest.mark.parametrize(
    ("issued_ist", "expires_ist"),
    [
        (datetime(2026, 11, 24, 10, 0), datetime(2026, 11, 25, 3, 30)),
        (datetime(2026, 11, 24, 2, 0), datetime(2026, 11, 24, 3, 30)),
        (datetime(2026, 11, 24, 3, 30), datetime(2026, 11, 25, 3, 30)),
    ],
)
def test_expiry_is_the_next_0330_ist(issued_ist, expires_ist):
    issued = issued_ist.replace(tzinfo=upstox.IST)
    assert upstox.next_expiry(issued) == expires_ist.replace(tzinfo=upstox.IST).astimezone(UTC)


def test_status_valid_expired_missing(settings, ring):
    assert upstox.token_status(settings, T0).state == "missing"
    upstox.save_token(settings, FAKE_TOKEN, T0)
    valid = upstox.token_status(settings, T0)
    assert valid.state == "valid" and valid.hours_left == pytest.approx(17.5)
    assert "17.5 h left" in valid.line() and FAKE_TOKEN not in valid.line()
    assert upstox.token_status(settings, T0 + timedelta(hours=18)).state == "expired"
    with pytest.raises(upstox.TokenExpired, match="upstox login"):
        upstox.load_token(settings, T0 + timedelta(hours=18))


def test_a_loaded_token_is_masked_in_logs(settings, ring):
    upstox.save_token(settings, FAKE_TOKEN, T0)
    token = upstox.load_token(settings, T0)
    get_logger("test").info("oops", detail=f"Bearer {token}")
    text = (settings.log_dir / LOG_FILE_NAME).read_text(encoding="utf-8")
    assert FAKE_TOKEN not in text and "Bearer ***" in text


# --- the API client ----------------------------------------------------------------------


def fast_limiter():
    return RateLimiter("t", [Window(1000, 1)], headroom=1.0, sleep=lambda s: None)


def test_the_client_sends_the_bearer_token_and_raises_on_401_with_one_alert(settings):
    seen, alerts = [], []

    def handler(request):
        seen.append(request)
        return httpx.Response(401, json={"status": "error"})

    client = upstox.UpstoxClient(
        settings, token=FAKE_TOKEN, client=httpx.Client(transport=httpx.MockTransport(handler)),
        limiter=fast_limiter(), alert=lambda *a, **k: alerts.append((a, k)),
    )  # fmt: skip
    with pytest.raises(upstox.TokenExpired, match="upstox login"):
        client.get("/v3/historical-candle/x/days/1/2026-11-24/2026-11-01")
    assert seen[0].headers["Authorization"] == f"Bearer {FAKE_TOKEN}"
    assert seen[0].url.host == "api.upstox.com"
    [(args, kwargs)] = alerts
    assert (
        args[:2] == ("warning", "Upstox token expired") and kwargs["key"] == "upstox_token_expired"
    )


def test_429_backs_off_and_retries_then_other_errors_raise(settings):
    answers = [httpx.Response(429, headers={"Retry-After": "0"}),
               httpx.Response(200, json={"status": "success"})]  # fmt: skip
    client = upstox.UpstoxClient(
        settings, token=FAKE_TOKEN,
        client=httpx.Client(transport=httpx.MockTransport(lambda r: answers.pop(0))),
        limiter=fast_limiter(), alert=lambda *a, **k: None,
    )  # fmt: skip
    assert client.get("/x") == {"status": "success"}
    bad = upstox.UpstoxClient(
        settings, token=FAKE_TOKEN,
        client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(
            400, json={"errors": [{"errorCode": "UDAPI1148"}]}))),
        limiter=fast_limiter(), alert=lambda *a, **k: None,
    )  # fmt: skip
    with pytest.raises(upstox.UpstoxError, match="HTTP 400: UDAPI1148"):
        bad.get("/x")


def test_the_client_needs_a_valid_token(settings, ring):
    with pytest.raises(upstox.TokenExpired):
        upstox.UpstoxClient(settings, limiter=fast_limiter())


# --- health ------------------------------------------------------------------------------


def test_health_upstox_token(settings, ring):
    clock = [T0]
    ctx = health.Context(lambda: settings, now=lambda: clock[0])
    missing = health.check_upstox_token(ctx)
    assert missing.status == "WARN" and "missing" in missing.message
    upstox.save_token(settings, FAKE_TOKEN, T0)
    ok = health.check_upstox_token(ctx)
    assert ok.status == "OK" and FAKE_TOKEN not in ok.message
    clock[0] = T0 + timedelta(hours=17)  # 30 minutes left
    assert health.check_upstox_token(ctx).status == "WARN"
    clock[0] = T0 + timedelta(hours=18)
    assert "expired" in health.check_upstox_token(ctx).message


def test_health_skips_without_upstox_settings(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_PROFILE", "full")
    monkeypatch.setenv("LOG_DIR", str(tmp_path / "logs"))
    s = load_settings()
    result = health.check_upstox_token(health.Context(lambda: s))
    assert (result.status, result.message) == ("SKIP", "not set up (Phase 1)")
