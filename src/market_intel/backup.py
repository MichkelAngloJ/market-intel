"""Back up the local Docker databases: ``uv run python -m market_intel.backup``.

Full profile only (the local Docker databases of docker/compose.yaml must be running).

    python -m market_intel.backup           one Postgres dump + one MongoDB archive
    python -m market_intel.backup --verify  restore the newest backup into throwaway databases,
                                            compare row counts with the source, drop them

How it works:
- ``pg_dump`` (custom format) and ``mongodump`` (gzip archive) run INSIDE the containers
  (``docker compose exec``), and the files are copied out with ``docker compose cp``.
- No credentials on any command line or in the output: the tools log in with the containers'
  own environment (Postgres over its local socket; MongoDB with the root user, whose password
  goes into a temporary config file inside the container, never into an argument).
- Files go to ``BACKUP_DIR`` (outside the repo, enforced by config), named by UTC timestamp.
  The newest ``KEEP`` of each kind are kept; older ones are deleted.
- ``--verify`` restores into ``market_intel_restore_test`` (Postgres and MongoDB) and always
  drops it again, also when the restore or the comparison fails.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from market_intel.config import REPO_ROOT, ConfigError, Settings, get_settings
from market_intel.logging_setup import (
    bound_context,
    configure_logging,
    get_logger,
    mask_text,
    new_run_id,
)

log = get_logger("market_intel.backup")

Runner = Callable[..., subprocess.CompletedProcess]

PROFILE = "full"
KEEP = 7
RESTORE_DB = "market_intel_restore_test"
PG_SERVICE = "postgres"
MONGO_SERVICE = "mongo"
CONTAINER_TMP = "/tmp"
STAMP_FORMAT = "%Y%m%dT%H%M%SZ"
STAMP = r"\d{8}T\d{6}Z"
SAFE_NAME = re.compile(r"^[A-Za-z0-9_-]+$")
ERROR_LINES = 5
COMPOSE_TIMEOUT_S = 1800


@dataclass(frozen=True)
class Kind:
    """One kind of backup file: ``<prefix><UTC timestamp><suffix>``."""

    label: str
    prefix: str
    suffix: str

    def name(self, stamp: str) -> str:
        return f"{self.prefix}{stamp}{self.suffix}"

    def matches(self, filename: str) -> bool:
        return (
            re.fullmatch(re.escape(self.prefix) + STAMP + re.escape(self.suffix), filename)
            is not None
        )


PG = Kind("postgres", "market_intel-pg-", ".dump")
MONGO = Kind("mongodb", "market_intel-mongo-", ".archive.gz")


class BackupError(Exception):
    """A backup or restore step failed. The message never contains credentials."""


# --- Scripts run inside the containers (POSIX sh). Parameters come in as MI_* variables. ---

# Postgres: the local socket inside the container trusts the image's own superuser.
PG_DUMP = """set -eu
pg_dump --username="$POSTGRES_USER" --dbname="$POSTGRES_DB" --format=custom --file="$MI_FILE"
"""

PG_RESTORE = """set -eu
dropdb --username="$POSTGRES_USER" --if-exists "$MI_DB"
createdb --username="$POSTGRES_USER" "$MI_DB"
pg_restore --username="$POSTGRES_USER" --dbname="$MI_DB" --exit-on-error "$MI_FILE"
"""

PG_DROP = 'set -eu\ndropdb --username="$POSTGRES_USER" --if-exists "$MI_DB"\n'

# Reads the SQL from stdin. "$MI_DB" empty = the source database.
PG_QUERY = """set -eu
psql --username="$POSTGRES_USER" --dbname="${MI_DB:-$POSTGRES_DB}" --no-psqlrc --quiet \
  --tuples-only --no-align --field-separator='|' --set=ON_ERROR_STOP=1 --file=-
"""

# Exact row count of every table (superuser sees all of them), one "schema.table|count" row each.
PG_COUNT_SQL = """SELECT table_schema || '.' || table_name,
       (xpath('/row/c/text()',
              query_to_xml(format('SELECT count(*) AS c FROM %I.%I', table_schema, table_name),
                           false, true, '')))[1]::text::bigint
FROM information_schema.tables
WHERE table_type = 'BASE TABLE' AND table_schema NOT IN ('pg_catalog', 'information_schema')
ORDER BY 1;
"""

PG_EXISTS_SQL = f"SELECT count(*) FROM pg_database WHERE datname = '{RESTORE_DB}';\n"

# MongoDB tools read the password from a config file (mode 600, deleted on exit), not argv.
_MONGO_TOOL = """set -eu
umask 077
cfg=$(mktemp)
trap 'rm -f "$cfg"' EXIT
printf 'password: "%s"\\n' "$MONGO_INITDB_ROOT_PASSWORD" > "$cfg"
{tool} --host=127.0.0.1 --port=27017 --username="$MONGO_INITDB_ROOT_USERNAME" \
  --authenticationDatabase=admin --config="$cfg" --quiet {args}
"""

MONGO_DUMP = _MONGO_TOOL.format(tool="mongodump", args='--db="$MI_DB" --gzip --archive="$MI_FILE"')

MONGO_RESTORE = _MONGO_TOOL.format(
    tool="mongorestore",
    args='--gzip --archive="$MI_FILE" --nsInclude="$MI_SRC_DB.*" '
    '--nsFrom="$MI_SRC_DB.*" --nsTo="$MI_DB.*"',
)

# mongosh logs in from inside the script, with the password read from the environment.
_MONGOSH_LOGIN = (
    'const conn = new Mongo("mongodb://127.0.0.1:27017");'
    'conn.getDB("admin").auth(process.env.MONGO_INITDB_ROOT_USERNAME,'
    " process.env.MONGO_INITDB_ROOT_PASSWORD);"
    "const target = conn.getDB(process.env.MI_DB);"
)

MONGO_COUNT_JS = _MONGOSH_LOGIN + (
    "const out = {};"
    "target.getCollectionNames().sort().forEach(c => {"
    " out[c] = target.getCollection(c).countDocuments({}); });"
    "print(JSON.stringify(out));"
)

MONGO_DROP_JS = _MONGOSH_LOGIN + "target.dropDatabase(); print('dropped');"

MONGO_EXISTS_JS = _MONGOSH_LOGIN + (
    "const names = conn.getDB('admin').adminCommand({listDatabases: 1, nameOnly: true})"
    ".databases.map(d => d.name);"
    "print(names.includes(process.env.MI_DB) ? 1 : 0);"
)


# --- Docker Compose ----------------------------------------------------------------------


@dataclass
class Compose:
    """``docker compose`` for docker/compose.yaml with docker/compose.env (read by compose)."""

    compose_file: Path = REPO_ROOT / "docker" / "compose.yaml"
    env_file: Path = REPO_ROOT / "docker" / "compose.env"
    run: Runner = subprocess.run

    def _call(self, args: Sequence[str], *, stdin: str | None = None) -> str:
        cmd = [
            "docker",
            "compose",
            "-f",
            str(self.compose_file),
            "--env-file",
            str(self.env_file),
            *args,
        ]
        try:
            result = self.run(
                cmd,
                input=stdin,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=COMPOSE_TIMEOUT_S,
                check=False,
            )
        except FileNotFoundError:
            raise BackupError("docker not found: install or start Docker Desktop") from None
        except subprocess.TimeoutExpired:
            raise BackupError(f"docker compose {args[0]} timed out") from None
        if result.returncode != 0:
            tail = "\n".join(
                (result.stderr or result.stdout or "").strip().splitlines()[-ERROR_LINES:]
            )
            raise BackupError(
                f"docker compose {args[0]} failed (exit {result.returncode}): {mask_text(tail)}"
            )
        return result.stdout or ""

    def check_ready(self) -> None:
        if not self.env_file.is_file():
            raise BackupError("docker/compose.env is missing (see docker/compose.env.example)")
        running = set(self._call(["ps", "--status", "running", "--services"]).split())
        missing = [s for s in (PG_SERVICE, MONGO_SERVICE) if s not in running]
        if missing:
            raise BackupError(
                f"not running: {', '.join(missing)}. Start the databases first: "
                "docker compose -f docker/compose.yaml --env-file docker/compose.env up -d --wait"
            )

    def sh(self, service: str, script: str, *, stdin: str | None = None, **env: str) -> str:
        """Run a POSIX sh script in ``service``. ``env`` values must not be secrets."""
        flags = [f"--env=MI_{key.upper()}={value}" for key, value in env.items()]
        return self._call(["exec", "-T", *flags, service, "sh", "-c", script], stdin=stdin)

    def mongosh(self, js: str, *, db: str) -> str:
        return self._call(
            [
                "exec",
                "-T",
                f"--env=MI_DB={db}",
                MONGO_SERVICE,
                "mongosh",
                "--quiet",
                "--nodb",
                "--eval",
                js,
            ]
        )

    def copy_out(self, service: str, path: str, dest: Path) -> None:
        self._call(["cp", f"{service}:{path}", str(dest)])

    def copy_in(self, source: Path, service: str, path: str) -> None:
        self._call(["cp", str(source), f"{service}:{path}"])

    def remove(self, service: str, path: str) -> None:
        """Delete a temporary file in the container; never raises (cleanup only)."""
        try:
            self._call(["exec", "-T", service, "rm", "-f", path])
        except BackupError as exc:
            log.warning("backup_cleanup_failed", service=service, error=str(exc))


# --- Backup ------------------------------------------------------------------------------


@dataclass
class BackupResult:
    files: dict[str, Path]
    removed: list[str] = field(default_factory=list)


def stamp(now: datetime) -> str:
    return now.astimezone(UTC).strftime(STAMP_FORMAT)


def backups(directory: Path, kind: Kind) -> list[Path]:
    """Backup files of ``kind`` in ``directory``, newest first (by their UTC timestamp)."""
    if not directory.is_dir():
        return []
    found = [p for p in directory.iterdir() if p.is_file() and kind.matches(p.name)]
    return sorted(found, key=lambda p: p.name, reverse=True)


def prune(directory: Path, kind: Kind, keep: int = KEEP) -> list[str]:
    """Delete all but the newest ``keep`` files of ``kind``. Other files are never touched."""
    removed = []
    for old in backups(directory, kind)[keep:]:
        old.unlink()
        removed.append(old.name)
    return removed


def _dump(compose: Compose, kind: Kind, service: str, script: str, dest: Path, **env: str) -> None:
    inside = f"{CONTAINER_TMP}/{dest.name}"
    partial = dest.with_name(dest.name + ".partial")
    try:
        compose.sh(service, script, file=inside, **env)
        compose.copy_out(service, inside, partial)
        if not partial.is_file() or partial.stat().st_size == 0:
            raise BackupError(f"{kind.label} backup file is missing or empty after copying")
        partial.replace(dest)
    finally:
        compose.remove(service, inside)
        partial.unlink(missing_ok=True)


def make_backup(settings: Settings, compose: Compose, now: datetime) -> BackupResult:
    directory = settings.backup_dir
    directory.mkdir(parents=True, exist_ok=True)
    ts = stamp(now)
    files = {PG.label: directory / PG.name(ts), MONGO.label: directory / MONGO.name(ts)}
    if any(p.exists() for p in files.values()):
        raise BackupError(f"a backup with timestamp {ts} already exists; try again in a second")
    try:
        _dump(compose, PG, PG_SERVICE, PG_DUMP, files[PG.label])
        _dump(compose, MONGO, MONGO_SERVICE, MONGO_DUMP, files[MONGO.label], db=settings.mongodb_db)
    except BaseException:
        # No half backups: a Postgres dump without its MongoDB archive is removed again.
        for path in files.values():
            path.unlink(missing_ok=True)
        raise
    removed = prune(directory, PG) + prune(directory, MONGO)
    return BackupResult(files=files, removed=removed)


# --- Restore test ------------------------------------------------------------------------


@dataclass
class Comparison:
    label: str
    source: dict[str, int]
    restored: dict[str, int]

    @property
    def ok(self) -> bool:
        return self.source == self.restored


def parse_pg_counts(text: str) -> dict[str, int]:
    counts = {}
    for line in text.strip().splitlines():
        if line.strip():
            name, _, count = line.strip().rpartition("|")
            counts[name] = int(count)
    return counts


def parse_mongo_counts(text: str) -> dict[str, int]:
    lines = [line for line in text.strip().splitlines() if line.strip()]
    if not lines:
        raise BackupError("mongosh returned no collection counts")
    return {str(k): int(v) for k, v in json.loads(lines[-1]).items()}


def pg_counts(compose: Compose, db: str = "") -> dict[str, int]:
    return parse_pg_counts(compose.sh(PG_SERVICE, PG_QUERY, stdin=PG_COUNT_SQL, db=db))


def mongo_counts(compose: Compose, db: str) -> dict[str, int]:
    return parse_mongo_counts(compose.mongosh(MONGO_COUNT_JS, db=db))


def drop_restore_dbs(compose: Compose) -> list[str]:
    """Drop both throwaway databases; returns the problems (empty = both are gone)."""
    problems = []
    try:
        compose.sh(PG_SERVICE, PG_DROP, db=RESTORE_DB)
        left = compose.sh(PG_SERVICE, PG_QUERY, stdin=PG_EXISTS_SQL, db="").strip()
        if left != "0":
            problems.append(f"postgres database {RESTORE_DB} still exists")
    except BackupError as exc:
        problems.append(f"postgres: {exc}")
    try:
        compose.mongosh(MONGO_DROP_JS, db=RESTORE_DB)
        left = compose.mongosh(MONGO_EXISTS_JS, db=RESTORE_DB).strip().splitlines()[-1:]
        if left != ["0"]:
            problems.append(f"mongodb database {RESTORE_DB} still exists")
    except BackupError as exc:
        problems.append(f"mongodb: {exc}")
    return problems


def _restore(compose: Compose, service: str, script: str, source: Path, **env: str) -> None:
    inside = f"{CONTAINER_TMP}/restore-{source.name}"
    try:
        compose.copy_in(source, service, inside)
        compose.sh(service, script, file=inside, **env)
    finally:
        compose.remove(service, inside)


def verify(settings: Settings, compose: Compose) -> tuple[list[Comparison], dict[str, Path]]:
    """Restore the newest backup into throwaway databases and compare row counts."""
    newest = {}
    for kind in (PG, MONGO):
        found = backups(settings.backup_dir, kind)
        if not found:
            raise BackupError(
                f"no {kind.label} backup in BACKUP_DIR; run python -m market_intel.backup first"
            )
        newest[kind.label] = found[0]
    source_db = settings.mongodb_db
    comparisons: list[Comparison] = []
    try:
        drop_restore_dbs(compose)  # leftovers of an interrupted earlier run
        _restore(compose, PG_SERVICE, PG_RESTORE, newest[PG.label], db=RESTORE_DB)
        comparisons.append(Comparison(PG.label, pg_counts(compose), pg_counts(compose, RESTORE_DB)))
        _restore(
            compose,
            MONGO_SERVICE,
            MONGO_RESTORE,
            newest[MONGO.label],
            db=RESTORE_DB,
            src_db=source_db,
        )
        comparisons.append(
            Comparison(
                MONGO.label, mongo_counts(compose, source_db), mongo_counts(compose, RESTORE_DB)
            )
        )
    finally:
        problems = drop_restore_dbs(compose)
    if problems:
        raise BackupError("throwaway databases not dropped: " + "; ".join(problems))
    return comparisons, newest


# --- Output ------------------------------------------------------------------------------


def _size(path: Path) -> str:
    size = float(path.stat().st_size)
    if size < 1024:
        return f"{size:.0f} B"
    for unit in ("KB", "MB"):
        size /= 1024
        if size < 1024:
            return f"{size:.1f} {unit}"
    return f"{size / 1024:.1f} GB"


def render_backup(result: BackupResult, directory: Path) -> str:
    lines = [f"Backup written to {directory}"]
    for label, path in result.files.items():
        lines.append(f"  {label:<9}: {path.name} ({_size(path)})")
    lines.append(
        f"  kept the newest {KEEP} of each kind; removed {len(result.removed)} older file(s)"
    )
    lines.append("result: OK")
    return "\n".join(lines)


def render_verify(comparisons: list[Comparison], newest: dict[str, Path]) -> str:
    lines = ["Restore test of the newest backup:"]
    lines += [f"  {label:<9}: {path.name}" for label, path in newest.items()]
    for comp in comparisons:
        names = sorted(set(comp.source) | set(comp.restored))
        lines += ["", f"{comp.label:<36} {'source':>10} {'restored':>10}"]
        for name in names:
            src, dst = comp.source.get(name), comp.restored.get(name)
            mark = "ok" if src == dst else "MISMATCH"
            lines.append(f"  {name:<34} {_num(src):>10} {_num(dst):>10}  {mark}")
        if not names:
            lines.append("  (no tables or collections)")
    lines += ["", f"throwaway databases ({RESTORE_DB}) dropped: yes"]
    ok = bool(comparisons) and all(c.ok for c in comparisons)
    lines.append("result: OK" if ok else "result: FAILED (row counts differ)")
    return "\n".join(lines)


def _num(value: int | None) -> str:
    return "missing" if value is None else str(value)


# --- Command -----------------------------------------------------------------------------


def run(
    settings: Settings,
    *,
    verify_only: bool = False,
    compose: Compose | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> int:
    if settings.app_profile != PROFILE:
        print(
            f"backup runs only on the '{PROFILE}' profile (local Docker databases); this "
            f"machine's profile is '{settings.app_profile}'. Cloud databases are not backed up "
            "by this command.",
            file=sys.stderr,
        )
        return 1
    if not SAFE_NAME.match(settings.mongodb_db) or settings.mongodb_db == RESTORE_DB:
        print(f"MONGODB_DB must be a plain name other than {RESTORE_DB}", file=sys.stderr)
        return 1
    compose = compose or Compose()
    try:
        compose.check_ready()
        if verify_only:
            comparisons, newest = verify(settings, compose)
            ok = all(c.ok for c in comparisons)
            for comp in comparisons:
                (log.info if comp.ok else log.error)(
                    "restore_test", database=comp.label, ok=comp.ok, tables=len(comp.source)
                )
            print(render_verify(comparisons, newest))
            return 0 if ok else 1
        result = make_backup(settings, compose, now())
        log.info(
            "backup_done",
            files=[p.name for p in result.files.values()],
            removed=len(result.removed),
        )
        print(render_backup(result, settings.backup_dir))
        return 0
    except BackupError as exc:
        log.error("backup_failed", verify=verify_only, error=str(exc))
        print(f"Backup error: {exc}", file=sys.stderr)
        return 1


def main(argv: list[str] | None = None) -> int:
    """Entry point for ``python -m market_intel.backup``. Returns the exit code."""
    parser = argparse.ArgumentParser(prog="python -m market_intel.backup")
    parser.add_argument(
        "--verify",
        action="store_true",
        help="restore the newest backup into throwaway databases and compare row counts",
    )
    args = parser.parse_args(argv)
    try:
        settings = get_settings()
    except ConfigError as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        return 1
    configure_logging(settings)
    with bound_context(run_id=new_run_id(), component="backup"):
        return run(settings, verify_only=args.verify)


if __name__ == "__main__":
    sys.exit(main())
