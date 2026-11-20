"""Task 1.0: the rate limiter never exceeds any window (fake clock, many threads) and backs off
on HTTP 429 / Retry-After."""

import threading
from datetime import UTC, datetime

import pytest

from market_intel.config import ConfigError, load_settings
from market_intel.rate_limit import (
    UPSTOX_WINDOWS,
    RateLimited,
    RateLimiter,
    Window,
    parse_retry_after,
    upstox_limiter,
)


class FakeClock:
    """Time moves only when someone sleeps. Thread-safe."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start
        self.lock = threading.Lock()
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        with self.lock:
            return self.now

    def sleep(self, seconds: float) -> None:
        with self.lock:
            self.sleeps.append(seconds)
            self.now += max(seconds, 0.0)


def make(windows, clock=None, **kwargs):
    clock = clock or FakeClock()
    return RateLimiter("test", windows, clock=clock, sleep=clock.sleep, **kwargs), clock


def assert_within(times: list[float], windows) -> None:
    """In every window, call i and call i+limit are at least `seconds` apart."""
    times = sorted(times)
    for w in windows:
        for i in range(len(times) - w.limit):
            assert times[i + w.limit] - times[i] >= w.seconds, (w, i)


def hammer(limiter: RateLimiter, threads: int, per_thread: int) -> list[float]:
    times: list[float] = []
    lock = threading.Lock()
    start = threading.Barrier(threads)

    def work():
        start.wait()
        for _ in range(per_thread):
            t = limiter.acquire()
            with lock:
                times.append(t)

    pool = [threading.Thread(target=work) for _ in range(threads)]
    for t in pool:
        t.start()
    for t in pool:
        t.join(60)
        assert not t.is_alive()
    return times


# --- windows -------------------------------------------------------------------------------------


def test_headroom_uses_80_percent_of_the_upstox_limits():
    limiter, _ = make(UPSTOX_WINDOWS)
    assert limiter.windows == (Window(40, 1), Window(400, 60), Window(1600, 1800))


def test_headroom_never_goes_below_one():
    limiter, _ = make([Window(1, 1)], headroom=0.1)
    assert limiter.windows == (Window(1, 1),)


def test_many_threads_never_exceed_any_window():
    limiter, _ = make([Window(5, 1), Window(20, 60)], headroom=1)
    times = hammer(limiter, threads=16, per_thread=10)
    assert len(times) == 160
    assert_within(times, limiter.windows)


def test_many_threads_stay_inside_the_upstox_windows():
    limiter, clock = make(UPSTOX_WINDOWS)
    times = hammer(limiter, threads=12, per_thread=200)  # 2400 calls: more than one 30-min budget
    assert_within(times, limiter.windows)
    assert_within(times, UPSTOX_WINDOWS)  # and so inside the published limits too
    # the 1601st call had to wait for the 30-minute window
    assert sorted(times)[1600] - sorted(times)[0] >= 1800


def test_a_free_slot_is_taken_without_sleeping():
    limiter, clock = make([Window(3, 1)], headroom=1)
    for _ in range(3):
        limiter.acquire()
    assert clock.sleeps == []
    limiter.acquire()
    assert clock.sleeps == [1.0]


def test_invalid_setup_is_refused():
    with pytest.raises(ValueError):
        RateLimiter("x", [])
    with pytest.raises(ValueError):
        RateLimiter("x", [Window(1, 1)], headroom=0)


# --- back-off ------------------------------------------------------------------------------------


def test_429_backs_off_exponentially_with_jitter_then_succeeds():
    limiter, clock = make([Window(100, 1)], headroom=1, rand=lambda: 1.0)
    answers = iter([RateLimited(), RateLimited(), RateLimited(), "ok"])

    def call():
        answer = next(answers)
        if isinstance(answer, Exception):
            raise answer
        return answer

    assert limiter.call(call) == "ok"
    # 1 s, 2 s, 4 s, each plus 25% jitter (rand = 1.0)
    assert clock.sleeps == [1.25, 2.5, 5.0]


def test_retry_after_is_honoured_as_a_minimum():
    limiter, clock = make([Window(100, 1)], headroom=1, rand=lambda: 0.0)
    answers = iter([RateLimited(retry_after=30), "ok"])

    def call():
        answer = next(answers)
        if isinstance(answer, Exception):
            raise answer
        return answer

    assert limiter.call(call) == "ok"
    assert clock.sleeps == [30.0]


def test_backoff_blocks_every_caller_and_success_resets_it():
    limiter, clock = make([Window(100, 1)], headroom=1, rand=lambda: 0.0)
    assert limiter.backoff() == 1.0
    assert limiter.backoff() == 2.0
    t = limiter.acquire()  # any caller waits for the back-off
    assert t >= 1002.0
    limiter.success()
    assert limiter.backoff() == 1.0  # starts again from the base delay


def test_backoff_delay_is_capped():
    limiter, _ = make([Window(100, 1)], rand=lambda: 0.0, backoff_cap_s=10)
    delays = [limiter.backoff() for _ in range(8)]
    assert delays[-1] == 10.0


def test_gives_up_after_max_attempts():
    limiter, clock = make([Window(100, 1)], headroom=1, rand=lambda: 0.0)

    def always_429():
        raise RateLimited(retry_after=1)

    with pytest.raises(RateLimited):
        limiter.call(always_429, max_attempts=3)
    assert len(clock.sleeps) == 2


@pytest.mark.parametrize(
    ("header", "seconds"),
    [
        ("120", 120.0),
        (" 5 ", 5.0),
        ("Sat, 21 Nov 2026 06:01:00 GMT", 60.0),
        ("Sat, 21 Nov 2026 05:00:00 GMT", 0.0),  # in the past
        (None, None),
        ("", None),
        ("soon", None),
    ],
)
def test_parse_retry_after(header, seconds):
    now = datetime(2026, 11, 21, 6, 0, tzinfo=UTC)
    assert parse_retry_after(header, now) == seconds


# --- settings ------------------------------------------------------------------------------------


def test_upstox_limiter_from_settings(monkeypatch):
    monkeypatch.setenv("APP_PROFILE", "full")
    clock = FakeClock()
    limiter = upstox_limiter(load_settings(), clock=clock, sleep=clock.sleep)
    assert limiter.name == "upstox"
    assert limiter.windows == (Window(40, 1), Window(400, 60), Window(1600, 1800))
    monkeypatch.setenv("UPSTOX_RATE_HEADROOM", "0.5")
    monkeypatch.setenv("UPSTOX_RATE_PER_MINUTE", "250")
    limiter = upstox_limiter(load_settings())
    assert limiter.windows == (Window(25, 1), Window(125, 60), Window(1000, 1800))


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("UPSTOX_RATE_HEADROOM", "0"),
        ("UPSTOX_RATE_HEADROOM", "1.5"),
        ("UPSTOX_RATE_PER_SECOND", "0"),
        ("UPSTOX_RATE_PER_30MIN", "many"),
    ],
)
def test_invalid_limiter_settings_are_refused(monkeypatch, name, value):
    monkeypatch.setenv("APP_PROFILE", "full")
    monkeypatch.setenv(name, value)
    with pytest.raises(ConfigError, match=name):
        load_settings()
