"""Rate limiting for data-source APIs: several windows at once, thread-safe, with back-off.

    limiter = upstox_limiter(settings)
    candles = limiter.call(lambda: client.get_candles(...))   # waits for a free slot

- Every window (e.g. 50 per second, 500 per minute, 2000 per 30 minutes) is a sliding window:
  a call is allowed only if each window has had fewer than its limit in the last N seconds.
- Headroom: a limiter uses only part of each published limit (default 80%), so clock skew
  and the provider's own counting can't push us over.
- On HTTP 429 the caller raises ``RateLimited(retry_after)``; the limiter then blocks EVERY
  caller (the budget is shared) for an exponential delay plus jitter, at least Retry-After.
  A successful call resets the back-off.
- Clock, sleep and random are injectable, so tests run instantly and deterministically.

Upstox counts its limits per user, not per program or per process: one limiter in one process
is the only safe way to stay inside them. So only ONE process may call Upstox at a time; from
task 1.8 that is the scheduler. Running a second Upstox job by hand while the scheduler runs
can exceed the limits.
"""

from __future__ import annotations

import math
import random
import threading
import time
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import TypeVar

from market_intel.config import Settings
from market_intel.logging_setup import get_logger

log = get_logger("market_intel.rate_limit")

T = TypeVar("T")

DEFAULT_HEADROOM = 0.8
BACKOFF_BASE_S = 1.0
BACKOFF_CAP_S = 120.0
JITTER = 0.25  # up to +25% of the exponential delay
DEFAULT_MAX_ATTEMPTS = 5


@dataclass(frozen=True)
class Window:
    """At most ``limit`` calls in any ``seconds``-long period."""

    limit: int
    seconds: float


# Upstox standard limits per API per user (official docs, checked 2026-11-21).
UPSTOX_WINDOWS = (Window(50, 1), Window(500, 60), Window(2000, 30 * 60))


class RateLimited(Exception):
    """Raise from inside ``RateLimiter.call`` when the provider answers HTTP 429."""

    def __init__(self, retry_after: float | None = None, message: str = "rate limited") -> None:
        super().__init__(message)
        self.retry_after = retry_after


def parse_retry_after(value: str | None, now: datetime | None = None) -> float | None:
    """Seconds to wait from a Retry-After header: delta-seconds or an HTTP date."""
    if value is None or not value.strip():
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max(0.0, (when - (now or datetime.now(UTC))).total_seconds())


class RateLimiter:
    def __init__(
        self,
        name: str,
        windows: Sequence[Window],
        *,
        headroom: float = DEFAULT_HEADROOM,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        rand: Callable[[], float] = random.random,
        backoff_base_s: float = BACKOFF_BASE_S,
        backoff_cap_s: float = BACKOFF_CAP_S,
    ) -> None:
        if not windows:
            raise ValueError("a rate limiter needs at least one window")
        if not 0 < headroom <= 1:
            raise ValueError("headroom must be > 0 and <= 1")
        self.name = name
        # The limits actually used: a share of each published limit, never below 1.
        self.windows = tuple(
            Window(max(1, math.floor(w.limit * headroom)), w.seconds) for w in windows
        )
        self._calls: list[deque[float]] = [deque() for _ in self.windows]
        self._clock, self._sleep, self._rand = clock, sleep, rand
        self._base, self._cap = backoff_base_s, backoff_cap_s
        self._lock = threading.Lock()
        self._blocked_until = -math.inf
        self._failures = 0

    def _wait_needed(self, now: float) -> float:
        """Seconds until a call is allowed (0 = now). Caller holds the lock."""
        wait = self._blocked_until - now
        for window, calls in zip(self.windows, self._calls, strict=True):
            while calls and calls[0] <= now - window.seconds:
                calls.popleft()
            if len(calls) >= window.limit:
                wait = max(wait, calls[0] + window.seconds - now)
        return wait

    def acquire(self) -> float:
        """Block until every window has room, then take a slot. Returns the slot's time."""
        while True:
            with self._lock:
                now = self._clock()
                wait = self._wait_needed(now)
                if wait <= 0:
                    for calls in self._calls:
                        calls.append(now)
                    return now
            self._sleep(wait)  # outside the lock, so other threads can check too

    def backoff(self, retry_after: float | None = None) -> float:
        """After a 429: block all callers for an exponential delay plus jitter (at least
        ``retry_after``). Returns the delay."""
        with self._lock:
            self._failures += 1
            exp = min(self._cap, self._base * 2 ** (self._failures - 1))
            delay = max(retry_after or 0.0, exp) + JITTER * exp * self._rand()
            self._blocked_until = max(self._blocked_until, self._clock() + delay)
            failures = self._failures
        log.warning(
            "rate_limited",
            limiter=self.name,
            attempt=failures,
            delay_s=round(delay, 2),
            retry_after_s=retry_after,
        )
        return delay

    def success(self) -> None:
        """A call went through: reset the back-off."""
        with self._lock:
            self._failures = 0

    def call(self, func: Callable[[], T], *, max_attempts: int = DEFAULT_MAX_ATTEMPTS) -> T:
        """Run ``func`` inside the limits; retry after ``RateLimited`` up to ``max_attempts``."""
        for attempt in range(1, max_attempts + 1):
            self.acquire()
            try:
                result = func()
            except RateLimited as exc:
                if attempt == max_attempts:
                    raise
                self.backoff(exc.retry_after)
                continue
            self.success()
            return result
        raise AssertionError("unreachable")  # pragma: no cover


def upstox_limiter(settings: Settings, **kwargs) -> RateLimiter:
    """The Upstox limiter from settings (published limits and headroom). One per process."""
    windows = (
        Window(settings.upstox_rate_per_second, 1),
        Window(settings.upstox_rate_per_minute, 60),
        Window(settings.upstox_rate_per_30min, 30 * 60),
    )
    return RateLimiter("upstox", windows, headroom=settings.upstox_rate_headroom, **kwargs)
