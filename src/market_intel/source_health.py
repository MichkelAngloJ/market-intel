"""Source health: notice when a data source breaks or changes, and email about it.

Reusable by every source (RSS feeds from 1.2; later Upstox files, NSE files, ...). A source
reports each attempt:

- ``record_failure(source, error)``: the request or the parse failed.
- ``record_not_modified(source)``: the server said nothing changed (a success without content).
- ``record_content(source, ...)``: content arrived; how many items were usable, malformed or
  without a publish time.

Rules (each queues one WARNING alert, throttled per source and rule by the alert throttle):

a) ``failures``: 3 consecutive failures.
b) ``empty``: content arrived but 0 items were parsed (a body that isn't a recognisable feed,
   or entries that were all unusable). A well-formed feed that is simply empty is not a
   problem; rule d catches a usually busy feed that goes quiet.
c) ``malformed``: more than 50% of the entries had no link or title.
d) ``volume_drop``: fewer items than 20% of the median of the last 10 content fetches (only
   once 5 or more are known, and only if that median is above 0).
e) ``no_publish_time``: more than 50% of the usable items had no publish time (the date format
   probably changed).
f) ``silent``: 0 items in every content fetch of the last 14 days (only once the source has 14
   days of content history).

A source can also report a **block** (``record_block``: e.g. Google answering with a consent
page): rule ``blocked``, one alert titled "<source> blocked".

A rule stays an **active problem** until a later attempt passes it (a success clears a;
the next content fetch re-checks b to e). Health's ``sources`` check turns WARN while an
enabled source has an active problem.

State: ``DATA_DIR/state/source_health.json``, written atomically under a file lock. It holds
counts and masked error text only, never data.
"""

from __future__ import annotations

import json
import statistics
from collections.abc import Callable, Iterable
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from market_intel.alerts import queue_alert
from market_intel.file_lock import file_lock, safe_replace
from market_intel.logging_setup import get_logger, mask_text

STATE_NAME = "source_health.json"
KEEP_COUNTS = 20
MEDIAN_OF = 10
MIN_HISTORY = 5
FAILURES_IN_A_ROW = 3
MALFORMED_SHARE = 0.5
NO_TIME_SHARE = 0.5
DROP_SHARE = 0.2
SILENT_DAYS = 14
ERROR_MAX_CHARS = 500
NEXT_STEP = "the source may have changed: update config/news_feeds.toml or the parser"

RULES = {
    "failures": f"{FAILURES_IN_A_ROW} consecutive failures",
    "empty": "content fetched but 0 items parsed",
    "malformed": "more than 50% of items malformed (no link or title)",
    "volume_drop": "item count below 20% of the median of the last 10 content fetches",
    "no_publish_time": "more than 50% of items without a publish time (format changed?)",
    "silent": f"0 items in every content fetch for {SILENT_DAYS} days",
    "blocked": "the source refused automated access (block detected)",
}

log = get_logger("market_intel.source_health")

Alert = Callable[..., Any]


def state_path(data_dir: Path) -> Path:
    return data_dir / "state" / STATE_NAME


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def load_state(path: Path) -> dict[str, dict[str, Any]]:
    """Every source's state; {} before the first attempt or if the file is unreadable."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _new_source() -> dict[str, Any]:
    return {
        "consecutive_failures": 0,
        "last_error": None,
        "last_attempt_at": None,
        "last_success_at": None,
        "counts": [],
        "content_log": [],  # [UTC ISO time, items] of content fetches, last SILENT_DAYS + 1
        "first_content_at": None,
        "problems": {},
    }


class SourceHealth:
    """Records attempts and applies the rules. ``alert`` defaults to ``queue_alert``."""

    def __init__(
        self,
        data_dir: Path,
        *,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        alert: Alert | None = None,
    ) -> None:
        self.path = state_path(data_dir)
        self.now = now
        self.alert = alert or queue_alert

    @contextmanager
    def _update(self, source: str):
        with file_lock(self.path.with_name(self.path.name + ".lock")):
            state = load_state(self.path)
            entry = {**_new_source(), **state.get(source, {})}
            yield entry
            state[source] = entry
            tmp = self.path.with_name(self.path.name + ".tmp")
            tmp.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
            safe_replace(tmp, self.path)

    # --- recording ------------------------------------------------------------------------

    def record_failure(self, source: str, error: str) -> list[str]:
        """A failed attempt. Returns the rules that fired (``failures`` at the third)."""
        text = mask_text(error)[:ERROR_MAX_CHARS]
        with self._update(source) as entry:
            entry["consecutive_failures"] += 1
            entry["last_error"] = text
            entry["last_attempt_at"] = _iso(self.now())
            fired = []
            if entry["consecutive_failures"] >= FAILURES_IN_A_ROW:
                fired = self._set(entry, ["failures"], f"last error: {text}")
        self._alert(source, fired, entry)
        return fired

    def record_not_modified(self, source: str) -> None:
        """A successful attempt without new content: clears ``failures`` only."""
        with self._update(source) as entry:
            self._success(entry)

    def record_content(
        self,
        source: str,
        *,
        items: int,
        malformed: int,
        without_time: int,
        recognised: bool = True,
    ) -> list[str]:
        """Content arrived. ``items`` usable items, ``malformed`` entries without link or title,
        ``without_time`` usable items with no publish time, ``recognised`` False when the body
        wasn't a recognisable feed. Returns the rules that fired."""
        entries = items + malformed
        with self._update(source) as entry:
            self._success(entry)
            history = list(entry["counts"])
            checks: dict[str, tuple[bool, str]] = {
                "empty": (
                    items == 0 and (not recognised or malformed > 0),
                    "not a recognisable feed" if not recognised else f"{malformed} unusable",
                ),
                "malformed": (
                    entries > 0 and malformed / entries > MALFORMED_SHARE,
                    f"{malformed} of {entries} entries malformed",
                ),
                "no_publish_time": (
                    items > 0 and without_time / items > NO_TIME_SHARE,
                    f"{without_time} of {items} items without a publish time",
                ),
            }
            recent = history[-MEDIAN_OF:]
            median = statistics.median(recent) if len(history) >= MIN_HISTORY else 0
            checks["volume_drop"] = (
                median > 0 and items < DROP_SHARE * median,
                f"{items} items, usual median {median:g}",
            )
            now = self.now()
            entry["first_content_at"] = entry["first_content_at"] or _iso(now)
            keep_from = now - timedelta(days=SILENT_DAYS + 1)
            log_entries = [
                e for e in entry["content_log"] if datetime.fromisoformat(e[0]) >= keep_from
            ] + [[_iso(now), items]]
            entry["content_log"] = log_entries
            window_from = now - timedelta(days=SILENT_DAYS)
            recent = [n for when, n in log_entries if datetime.fromisoformat(when) >= window_from]
            old_enough = datetime.fromisoformat(entry["first_content_at"]) <= window_from
            checks["silent"] = (
                old_enough and bool(recent) and all(n == 0 for n in recent),
                f"0 items in all {len(recent)} content fetches of the last {SILENT_DAYS} days",
            )
            fired = []
            for rule, (bad, detail) in checks.items():
                if bad:
                    fired += self._set(entry, [rule], detail)
                else:
                    entry["problems"].pop(rule, None)
            entry["counts"] = (history + [items])[-KEEP_COUNTS:]
        self._alert(source, fired, entry)
        return fired

    def record_block(self, source: str, reason: str) -> list[str]:
        """The source refused automated access. Rule ``blocked`` until a later success."""
        text = mask_text(reason)[:ERROR_MAX_CHARS]
        with self._update(source) as entry:
            entry["last_error"] = text
            entry["last_attempt_at"] = _iso(self.now())
            fired = [] if "blocked" in entry["problems"] else ["blocked"]
            self._set(entry, ["blocked"], text)
        self._alert(source, fired, entry)
        return fired

    # --- reading --------------------------------------------------------------------------

    def state(self) -> dict[str, dict[str, Any]]:
        return load_state(self.path)

    # --- internals ------------------------------------------------------------------------

    def _success(self, entry: dict[str, Any]) -> None:
        now = _iso(self.now())
        entry["consecutive_failures"] = 0
        entry["last_attempt_at"] = now
        entry["last_success_at"] = now
        entry["problems"].pop("failures", None)
        entry["problems"].pop("blocked", None)

    def _set(self, entry: dict[str, Any], rules: list[str], detail: str) -> list[str]:
        for rule in rules:
            entry["problems"].setdefault(rule, {"since": _iso(self.now())})
            entry["problems"][rule]["detail"] = detail
        return rules

    def _alert(self, source: str, fired: list[str], entry: dict[str, Any]) -> None:
        for rule in fired:
            detail = entry["problems"][rule]["detail"]
            log.warning("source_problem", source=source, rule=rule, detail=detail)
            self.alert(
                "warning",
                f"{source} blocked" if rule == "blocked" else f"Source problem: {source} ({rule})",
                f"Source: {source}\nRule: {RULES[rule]}\nDetail: {detail}\n"
                f"Last error: {entry['last_error'] or 'none'}\nNext step: {NEXT_STEP}.",
                key=f"source_health:{source}:{rule}",
                context={"source": source, "rule": rule},
            )


def active_problems(
    data_dir: Path, sources: Iterable[str] | None = None
) -> dict[str, dict[str, Any]]:
    """Active problems per source (only ``sources``, if given): {source: {rule: info}}."""
    state = load_state(state_path(data_dir))
    wanted = None if sources is None else set(sources)
    return {
        name: dict(entry.get("problems") or {})
        for name, entry in state.items()
        if (wanted is None or name in wanted) and entry.get("problems")
    }
