"""Daily health check: ``uv run python -m market_intel.health`` (``--json`` for machines).

Runs every check and prints STATUS | CHECK | MESSAGE:
    OK    all good
    WARN  needs attention soon (doesn't fail the run)
    FAIL  broken: exit code 1 and ONE critical alert ("health_fail", throttled)
    SKIP  not built or not configured yet

Each check is isolated: an exception makes only that check FAIL (with a masked message).
Uses ONLY the app logins (POSTGRES_URL, MONGODB_URI), never the admin logins. Writes nothing
except db_check's own write tests (TEMP table, _healthcheck) and delete probes that match
nothing. The JSON output is meant for the future Data Health page; it holds no secrets.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

from pymongo.errors import OperationFailure, PyMongoError

from market_intel import mongo_setup
from market_intel.alerts import AlertSender
from market_intel.config import REPO_ROOT, ConfigError, Settings, get_settings, required_fields
from market_intel.db import mongo, postgres
from market_intel.db.common import DbReport, host_is_local, redactor
from market_intel.logging_setup import (
    bound_context,
    configure_logging,
    get_logger,
    mask_text,
    new_run_id,
)
from market_intel.mongo import access, audit
from market_intel.mongo.schemas import COLLECTIONS

log = get_logger("market_intel.health")

Status = Literal["OK", "WARN", "FAIL", "SKIP"]
SEVERITY = {"SKIP": 0, "OK": 1, "WARN": 2, "FAIL": 3}

DISK_WARN_GB = 10
DISK_FAIL_GB = 2
REFUSAL_CODES = {8000, 13}  # Atlas: 8000 (AtlasError), self-hosted MongoDB: 13 (Unauthorized)
PROBE_PREFIX = "zz_health_probe_"


@dataclass
class CheckResult:
    name: str
    status: Status
    message: str
    details: dict[str, Any] = field(default_factory=dict)


# --- shared resources ----------------------------------------------------------------------


class Context:
    """Settings and lazily opened connections, shared by the checks.

    A connection is opened once; if that fails, the error is remembered and every check that
    needs it fails with the same (masked) message, without waiting for another retry.
    Everything that touches the outside world can be replaced in tests.
    """

    def __init__(
        self,
        load_settings: Callable[[], Settings] = get_settings,
        *,
        pg_checker: Callable[..., DbReport] = postgres.check,
        mongo_checker: Callable[..., DbReport] = mongo.check,
        pg_connect: Callable[..., Any] = postgres.connect,
        mongo_client: Callable[..., Any] = mongo.client,
        script_directory: Callable[[], Any] | None = None,
        disk_usage: Callable[[Path], Any] = shutil.disk_usage,
        dir_size: Callable[[Path], int] | None = None,
        probe_id: Callable[[], str] = lambda: uuid.uuid4().hex,
    ) -> None:
        self._load_settings = load_settings
        self.pg_checker = pg_checker
        self.mongo_checker = mongo_checker
        self._pg_connect = pg_connect
        self._mongo_client = mongo_client
        self._script_directory = script_directory or _alembic_scripts
        self.disk_usage = disk_usage
        self.dir_size = dir_size or _dir_size
        self.probe_id = probe_id
        self._cache: dict[str, Any] = {}
        self._redactors: list[Callable[[str], str]] = []

    def _get(self, key: str, make: Callable[[], Any]) -> Any:
        if key not in self._cache:
            try:
                self._cache[key] = (True, make())
            except Exception as exc:
                self._cache[key] = (False, exc)
        ok, value = self._cache[key]
        if not ok:
            raise value
        return value

    @property
    def settings(self) -> Settings:
        return self._get("settings", self._load_settings)

    @property
    def db(self):
        def make():
            db = self.settings.require("database")
            for url in (db.postgres_url, db.mongodb_uri):
                self._redactors.append(redactor(url.get_secret_value()))
            return db

        return self._get("db", make)

    def pg(self):
        """The app-login Postgres connection (autocommit; read-only use in health)."""
        return self._get("pg", lambda: self._pg_connect(self.db))

    def mongo_db(self):
        """The app-login MongoDB database."""
        client = self._get("mongo_client", lambda: self._mongo_client(self.db))
        return client[self.db.mongodb_db]

    def scripts(self):
        return self._get("scripts", self._script_directory)

    def mask(self, text: str) -> str:
        for redact in self._redactors:
            text = redact(text)
        return mask_text(text)

    def close(self) -> None:
        for key in ("pg", "mongo_client"):
            ok, value = self._cache.get(key, (False, None))
            if ok:
                try:
                    value.close()
                except Exception:
                    pass


def _alembic_scripts():
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    # Reads the migration scripts only; never connects to a database.
    return ScriptDirectory.from_config(Config(str(REPO_ROOT / "alembic.ini")))


def _dir_size(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file())


def _plain(value: Any) -> Any:
    """JSON-friendly values for details."""
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    if isinstance(value, Decimal):
        return float(value)
    return value


# --- checks --------------------------------------------------------------------------------


def check_config(ctx: Context) -> CheckResult:
    s = ctx.settings
    details = {
        "profile": s.app_profile,
        "migration_login": "set" if s.postgres_admin_url else "not set",
        "mongo_setup_login": "set" if s.mongodb_admin_uri else "not set",
    }
    if s.missing("database"):
        return CheckResult("config", "FAIL", f"missing {', '.join(s.missing('database'))}", details)
    if s.missing("alerts"):
        return CheckResult(
            "config",
            "WARN",
            f"profile {s.app_profile}; database set; alerts missing "
            f"{', '.join(s.missing('alerts'))} (no emails)",
            details,
        )
    return CheckResult(
        "config", "OK", f"profile {s.app_profile}; database and alerts configured", details
    )


def _group_check(name: str, group: str) -> Callable[[Context], CheckResult]:
    def check(ctx: Context) -> CheckResult:
        s = ctx.settings
        missing = s.missing(group)  # type: ignore[arg-type]
        if len(missing) == len(required_fields(group)):  # type: ignore[arg-type]
            return CheckResult(name, "SKIP", "not configured yet")
        if missing:
            return CheckResult(name, "WARN", f"incomplete: missing {', '.join(missing)}")
        return CheckResult(name, "OK", f"{group} configured")

    return check


def _db_problems(report: DbReport, profile: str) -> tuple[list[str], list[str]]:
    fails, warns = [], []
    if report.refused:
        fails.append("no TLS (refused to connect)")
    elif not report.reachable or report.error:
        fails.append(f"unreachable: {report.error or 'no answer'}")
    else:
        if report.tls is False and not host_is_local(report.host, profile):
            fails.append("no TLS")
        if not report.write_ok:
            fails.append("write test failed")
    if report.storage_warning:
        warns.append(f"storage at {report.used_pct:.0f}%")
    return fails, warns


def check_databases(ctx: Context) -> CheckResult:
    """Reuses db_check's checks: reachability, TLS, storage and the write test."""
    db = ctx.db
    status: Status = "OK"
    parts, details = [], {}
    for name, checker in (("postgres", ctx.pg_checker), ("mongodb", ctx.mongo_checker)):
        report = checker(db)
        fails, warns = _db_problems(report, db.app_profile)
        details[name] = {
            "host": report.host,
            "version": report.version,
            "latency_ms": report.latency_ms,
            "tls": report.tls,
            "used_pct": None if report.used_pct is None else round(report.used_pct, 1),
            "write_ok": report.write_ok,
            "error": ctx.mask(report.error) if report.error else None,
        }
        if fails:
            status = "FAIL"
            parts.append(f"{name}: {ctx.mask('; '.join(fails))}")
        elif warns:
            status = "WARN" if status == "OK" else status
            parts.append(f"{name}: {'; '.join(warns)}")
        else:
            used = "storage unknown" if report.used_pct is None else f"{report.used_pct:.1f}% used"
            parts.append(f"{name} ok ({report.latency_ms} ms, {used})")
    return CheckResult("databases", status, "; ".join(parts), details)


def check_pg_migrations(ctx: Context) -> CheckResult:
    scripts = ctx.scripts()
    heads = sorted(scripts.get_heads())
    rows = ctx.pg().execute("SELECT version_num FROM alembic_version").fetchall()
    current = sorted(r[0] for r in rows)
    details = {"current": current, "head": heads}
    if not current:
        return CheckResult(
            "pg_migrations", "WARN", "no migration applied: run alembic upgrade head", details
        )
    unknown = []
    for rev in current:
        try:
            if scripts.get_revision(rev) is None:
                unknown.append(rev)
        except Exception:
            unknown.append(rev)
    if unknown:
        return CheckResult(
            "pg_migrations",
            "FAIL",
            f"database at {', '.join(unknown)}, which this code doesn't know (newer code?)",
            details,
        )
    if current != heads:
        return CheckResult(
            "pg_migrations",
            "WARN",
            f"at {', '.join(current)}, head is {', '.join(heads)}: run alembic upgrade head",
            details,
        )
    return CheckResult("pg_migrations", "OK", f"at {', '.join(current)} (head)", details)


def check_mongo_setup_drift(ctx: Context) -> CheckResult:
    """mongo_setup's own read-only plan, made as the app login: anything to do = drift."""
    plan = mongo_setup.make_plan(ctx.mongo_db())
    differences = [a.description for a in plan.actions] + plan.conflicts
    details = {"differences": differences, "up_to_date": len(plan.unchanged)}
    if differences:
        return CheckResult(
            "mongo_setup_drift",
            "WARN",
            f"{len(differences)} difference(s) from schemas.py: run mongo_setup",
            details,
        )
    return CheckResult(
        "mongo_setup_drift", "OK", "collections, validators and indexes match", details
    )


def _try(action: Callable[[], Any]) -> tuple[str, int | None]:
    """('allowed', None), ('refused', code) or ('error', code)."""
    try:
        action()
    except OperationFailure as exc:
        return ("refused", exc.code) if exc.code in REFUSAL_CODES else ("error", exc.code)
    except PyMongoError:
        return "error", None
    return "allowed", None


def check_mongo_rights_probe(ctx: Context) -> CheckResult:
    """The app login's rights, tested by behaviour. Writes nothing when the role is right."""
    db = ctx.mongo_db()
    token = ctx.probe_id()
    results, mismatches, errors = {}, [], []
    for spec in COLLECTIONS:
        expect = "refused" if spec.name == audit.COLLECTION else "allowed"
        coll = access.collection(db, spec.name)
        got, code = _try(lambda coll=coll: coll.delete_many({"_health_probe": token}))
        results[f"delete {spec.name}"] = got if code is None else f"{got} ({code})"
        if got == "error":
            errors.append(f"delete on {spec.name}: error code {code}")
        elif got != expect:
            mismatches.append(f"delete on {spec.name} {got}, expected {expect}")

    stray = f"{PROBE_PREFIX}{token[:12]}"
    # Deliberately bypasses access.collection(): this insert MUST be refused by the role.
    got, code = _try(lambda: db[stray].insert_one({"_health_probe": token}))
    results["insert unknown collection"] = got if code is None else f"{got} ({code})"
    if got == "allowed":
        return CheckResult(
            "mongo_rights_probe",
            "FAIL",
            f"the app login CREATED collection {stray}: fix the role mi_app_rw and drop "
            f"{stray} in Atlas (the app login can't drop)",
            results,
        )
    if got == "error":
        errors.append(f"insert into unknown collection: error code {code}")
    if mismatches:
        return CheckResult(
            "mongo_rights_probe", "FAIL", "rights differ: " + "; ".join(mismatches), results
        )
    if errors:
        return CheckResult("mongo_rights_probe", "WARN", "; ".join(errors), results)
    return CheckResult(
        "mongo_rights_probe",
        "OK",
        "deletes allowed except prediction_audit; unknown collection refused",
        results,
    )


def check_unexpected_collections(ctx: Context) -> CheckResult:
    extra = access.unexpected_collections(ctx.mongo_db().list_collection_names())
    if extra:
        return CheckResult(
            "unexpected_collections",
            "WARN",
            f"not created by mongo_setup: {', '.join(extra)}",
            {"unexpected": extra},
        )
    return CheckResult("unexpected_collections", "OK", "only the known collections")


def check_laptop(ctx: Context) -> CheckResult:
    s = ctx.settings
    usage = ctx.disk_usage(REPO_ROOT)
    free_gb = usage.free / 1024**3
    logs_mb = ctx.dir_size(s.log_dir) / 1024**2
    data_mb = ctx.dir_size(s.data_dir) / 1024**2
    details = {
        "free_gb": round(free_gb, 1),
        "total_gb": round(usage.total / 1024**3, 1),
        "logs_mb": round(logs_mb, 2),
        "data_mb": round(data_mb, 2),
    }
    message = f"{free_gb:.1f} GB free; logs {logs_mb:.2f} MB, data {data_mb:.2f} MB"
    if free_gb < DISK_FAIL_GB:
        return CheckResult("laptop", "FAIL", f"disk almost full: {message}", details)
    if free_gb < DISK_WARN_GB:
        return CheckResult("laptop", "WARN", f"low disk: {message}", details)
    return CheckResult("laptop", "OK", message, details)


def check_data_freshness(ctx: Context) -> CheckResult:
    row = (
        ctx.pg()
        .execute(
            "SELECT job, source, status, started_at, finished_at FROM ingestion_runs"
            " ORDER BY started_at DESC LIMIT 1"
        )
        .fetchone()
    )
    if row is None:
        return CheckResult("data_freshness", "SKIP", "no runs yet")
    job, source, status, started_at, finished_at = row
    details = {
        "job": job,
        "source": source,
        "status": status,
        "started_at": _plain(started_at),
        "finished_at": _plain(finished_at),
    }
    when = _plain(finished_at or started_at)
    if status == "failed":
        return CheckResult("data_freshness", "WARN", f"last run {job} failed at {when}", details)
    return CheckResult("data_freshness", "OK", f"last run {job}: {status} at {when}", details)


def check_upstox_token(ctx: Context) -> CheckResult:
    return CheckResult("upstox_token", "SKIP", "not set up (Phase 1)")


def check_llm_spend(ctx: Context) -> CheckResult:
    s = ctx.settings
    row = (
        ctx.pg()
        .execute(
            "SELECT cost_inr FROM v_llm_spend_monthly"
            " WHERE month_ist = date_trunc('month', now() AT TIME ZONE 'Asia/Kolkata')::date"
        )
        .fetchone()
    )
    spent = float(row[0]) if row and row[0] is not None else 0.0
    cap = s.llm_monthly_cap_inr
    pct = 100 * spent / cap
    details = {"spent_inr": round(spent, 2), "cap_inr": cap, "pct": round(pct, 1)}
    message = f"Rs {spent:.2f} of Rs {cap} this IST month ({pct:.1f}%)"
    if pct >= 100:
        return CheckResult("llm_spend", "FAIL", f"cap reached: {message}", details)
    if pct >= s.llm_warn_pct:
        return CheckResult("llm_spend", "WARN", f"at {s.llm_warn_pct}% warning: {message}", details)
    return CheckResult("llm_spend", "OK", message, details)


CHECKS: tuple[tuple[str, Callable[[Context], CheckResult]], ...] = (
    ("config", check_config),
    ("config_upstox", _group_check("config_upstox", "upstox")),
    ("config_llm", _group_check("config_llm", "llm")),
    ("databases", check_databases),
    ("pg_migrations", check_pg_migrations),
    ("mongo_setup_drift", check_mongo_setup_drift),
    ("mongo_rights_probe", check_mongo_rights_probe),
    ("unexpected_collections", check_unexpected_collections),
    ("laptop", check_laptop),
    ("data_freshness", check_data_freshness),
    ("upstox_token", check_upstox_token),
    ("llm_spend", check_llm_spend),
)


# --- running and reporting -------------------------------------------------------------------


def run_checks(ctx: Context, checks=CHECKS) -> list[CheckResult]:
    """Run every check; an exception fails only that check, with a masked message."""
    results = []
    for name, check in checks:
        try:
            result = check(ctx)
        except Exception as exc:
            result = CheckResult(name, "FAIL", ctx.mask(f"{type(exc).__name__}: {exc}".strip()))
        result.message = ctx.mask(result.message)
        results.append(result)
        level = {"FAIL": log.error, "WARN": log.warning}.get(result.status, log.info)
        level("health_check", check=result.name, status=result.status, detail=result.message)
    return results


def overall(results: list[CheckResult]) -> Status:
    worst = max((SEVERITY[r.status] for r in results), default=1)
    return "FAIL" if worst == 3 else "WARN" if worst == 2 else "OK"


def counts(results: list[CheckResult]) -> dict[str, int]:
    return {s: sum(r.status == s for r in results) for s in ("OK", "WARN", "FAIL", "SKIP")}


def alert_on_failures(
    results: list[CheckResult], settings: Settings | None, sender: AlertSender | None
) -> str | None:
    """ONE critical alert when anything FAILs (WARNs are only logged). Returns its status."""
    failing = [r for r in results if r.status == "FAIL"]
    if not failing:
        return None
    title = f"Health check: {len(failing)} failing"
    if settings is None or settings.missing("alerts"):
        log.warning("alerts_not_configured", would_send=title, alert_level="critical")
        return "not_configured"
    body = "\n".join(f"- {r.name}: {r.message}" for r in failing)
    sender = sender or AlertSender(settings)
    result = sender.send(
        "critical",
        title,
        body,
        key="health_fail",
        context={"failing": [r.name for r in failing]},
    )
    return result.status


def render_table(results: list[CheckResult], alert_status: str | None) -> str:
    width = max(len(r.name) for r in results)
    lines = [f"{'STATUS':<6}  {'CHECK':<{width}}  MESSAGE", f"{'-' * 6}  {'-' * width}  {'-' * 7}"]
    lines += [f"{r.status:<6}  {r.name:<{width}}  {r.message}" for r in results]
    c = counts(results)
    lines.append("")
    lines.append(
        f"overall: {overall(results)}  ({c['OK']} OK, {c['WARN']} WARN, {c['FAIL']} FAIL, "
        f"{c['SKIP']} SKIP)"
    )
    if alert_status:
        lines.append(f"alert: critical -> {alert_status}")
    return "\n".join(lines)


def to_json(results: list[CheckResult], alert_status: str | None, now: datetime) -> str:
    payload = {
        "generated_at": now.astimezone(UTC).isoformat(),
        "overall": overall(results),
        "counts": counts(results),
        "alert": alert_status,
        "checks": [asdict(r) for r in results],
    }
    return json.dumps(payload, indent=2, default=_plain)


def main(argv: list[str] | None = None, *, ctx: Context | None = None, sender=None) -> int:
    """Entry point for ``python -m market_intel.health``. Returns the exit code."""
    parser = argparse.ArgumentParser(prog="python -m market_intel.health")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)

    ctx = ctx or Context()
    try:
        settings = ctx.settings
    except ConfigError:
        settings = None  # the config check reports it; other checks fail on their own
    if settings is not None:
        configure_logging(settings)
    with bound_context(run_id=new_run_id(), component="health"):
        try:
            results = run_checks(ctx)
        finally:
            ctx.close()
        alert_status = alert_on_failures(results, settings, sender)
    if args.json:
        print(to_json(results, alert_status, datetime.now(UTC)))
    else:
        print(render_table(results, alert_status))
    return 1 if any(r.status == "FAIL" for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
