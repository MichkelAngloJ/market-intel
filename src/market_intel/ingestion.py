"""Every data-loading job records its run in Postgres ``ingestion_runs``.

    with ingestion_run(settings, "daily_candles", source="upstox", params={...}) as run:
        rows = load_something()
        run.add_rows(len(rows))

- On entry: a row with ``status = 'running'`` (job, source, params) is inserted and committed.
- On success: ``finished_at``, ``status = 'success'`` and ``rows_written``.
- On any error (also Ctrl+C): ``status = 'failed'``, ``finished_at``, the rows counted so far
  and a MASKED error text; then the exception is re-raised unchanged.

The helper uses its OWN connection with the app login (``POSTGRES_URL``) in autocommit mode,
separate from the job's connection. So the run row is visible while the job runs, and a job
whose own transaction fails can still be marked ``failed``.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from typing import Any

from market_intel.config import Settings
from market_intel.db import postgres
from market_intel.db.common import redactor
from market_intel.logging_setup import MASK, get_logger, mask_text, mask_value

ERROR_MAX_CHARS = 2000
# Everything between "scheme://" and "@" (user name and password) becomes ***.
_URL_USERINFO = re.compile(r"([a-zA-Z][a-zA-Z0-9+.\-]*://)[^\s/@]+@")

log = get_logger("market_intel.ingestion")


class Run:
    """The handle a job gets inside ``ingestion_run``: its row id and a row counter."""

    def __init__(self, run_id: int, job: str, source: str | None) -> None:
        self.id = run_id
        self.job = job
        self.source = source
        self.rows_written = 0

    def add_rows(self, n: int) -> None:
        """Count rows the job has written (inserted or updated)."""
        if n < 0:
            raise ValueError("rows written can't be negative")
        self.rows_written += n


def masked_error(exc: BaseException, url: str | None = None) -> str:
    """``Type: message`` with URL user names, passwords and loaded secrets masked, cut to a
    safe length. Stricter than log masking: the stored row also hides user names in URLs.

    ``url`` (the database URL) is removed explicitly too, in case logging isn't configured.
    """
    text = f"{type(exc).__name__}: {exc}"
    if url:
        text = redactor(url)(text)
    text = _URL_USERINFO.sub(rf"\1{MASK}@", text)
    text = mask_text(text)
    if len(text) > ERROR_MAX_CHARS:
        text = text[: ERROR_MAX_CHARS - 3] + "..."
    return text


@contextmanager
def ingestion_run(
    settings: Settings,
    job: str,
    *,
    source: str | None = None,
    params: Mapping[str, Any] | None = None,
    connect: Callable[..., Any] | None = None,
) -> Iterator[Run]:
    """Record one job run in ``ingestion_runs`` (see the module docstring).

    ``connect`` is for tests; it receives the ``database`` settings group.
    """
    if not job:
        raise ValueError("job name is required")
    db = settings.require("database")
    url = db.postgres_url.get_secret_value()
    # params may hold secret-looking keys by mistake: mask them like logs before storing.
    params_json = json.dumps(mask_value(dict(params))) if params is not None else None

    with (connect or postgres.connect)(db) as conn:
        run_id = conn.execute(
            "INSERT INTO ingestion_runs (job, source, params, status)"
            " VALUES (%s, %s, %s::jsonb, 'running') RETURNING id",
            (job, source, params_json),
        ).fetchone()[0]
        run = Run(run_id, job, source)
        log.info("ingestion_run_started", run=run_id, job=job, source=source)
        try:
            yield run
        except BaseException as exc:
            error = masked_error(exc, url)
            try:
                conn.execute(
                    "UPDATE ingestion_runs SET status = 'failed', finished_at = now(),"
                    " rows_written = %s, error = %s WHERE id = %s",
                    (run.rows_written, error, run_id),
                )
            except Exception as update_exc:  # never hide the job's own error
                log.error(
                    "ingestion_run_update_failed",
                    run=run_id,
                    error=masked_error(update_exc, url),
                )
            log.error("ingestion_run_failed", run=run_id, job=job, error=error)
            raise
        conn.execute(
            "UPDATE ingestion_runs SET status = 'success', finished_at = now(),"
            " rows_written = %s WHERE id = %s",
            (run.rows_written, run_id),
        )
        log.info("ingestion_run_finished", run=run_id, job=job, rows_written=run.rows_written)
