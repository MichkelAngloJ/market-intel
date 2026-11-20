"""Back up the local Docker databases: ``uv run python -m market_intel.backup``.

Full profile only (the local Docker databases of docker/compose.yaml must be running).

    python -m market_intel.backup           one Postgres dump, one MongoDB archive, a manifest
    python -m market_intel.backup --verify  check the newest backup against its manifest by
                                            restoring it into throwaway databases

Backup:
- Right before each dump it counts the rows of every table and the documents of every
  collection. ``pg_dump`` (custom format) and ``mongodump`` (gzip archive) run INSIDE the
  containers (``docker compose exec``); the files are copied out with ``docker compose cp``.
- A manifest (JSON) next to the dumps records the UTC time, those counts, the tool versions and
  each file's size and SHA-256. It is written last: a backup without a manifest is incomplete.
- Files go to ``BACKUP_DIR`` (outside the repo, enforced by config), named by UTC timestamp.
  The newest ``KEEP`` of each kind are kept; older ones are deleted.
- MongoDB users and roles live in the ``admin`` database and are NOT in the backup: on empty
  volumes the init scripts recreate them from docker/compose.env.

Logins (no password on any command line or in the output):
- Postgres: the image's superuser over the container's local socket (needs no password).
- MongoDB: the setup user for the backup; the root user only for the restore test, and only
  on the throwaway database. mongodump/mongorestore read the password from a temporary config
  file (umask 077, deleted on exit); mongosh reads it from the container's environment inside
  its own process.
- The live counts that ``--verify`` shows (information only) use the app logins from .env.

Verify:
- checks each dump file's size and SHA-256 against the manifest;
- restores into ``market_intel_restore_test`` (Postgres and MongoDB) and compares the counts
  with the manifest: they must match exactly;
- prints today's live counts for information;
- always drops the throwaway databases, also when the restore or the comparison fails.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from psycopg import sql

from market_intel.config import REPO_ROOT, ConfigError, Settings, get_settings
from market_intel.db import mongo, postgres
from market_intel.db.common import redactor
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
MANIFEST_FORMAT = 1


@dataclass(frozen=True)
class Kind:
    """One kind of backup file: ``<prefix><UTC timestamp><suffix>``."""

    label: str
    prefix: str
    suffix: str

    def name(self, stamp: str) -> str:
        return f"{self.prefix}{stamp}{self.suffix}"

    def matches(self, filename: str) -> bool:
        pattern = re.escape(self.prefix) + STAMP + re.escape(self.suffix)
        return re.fullmatch(pattern, filename) is not None


PG = Kind("postgres", "market_intel-pg-", ".dump")
MONGO = Kind("mongodb", "market_intel-mongo-", ".archive.gz")
MANIFEST = Kind("manifest", "market_intel-manifest-", ".json")
DUMPS = (PG, MONGO)


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

PG_VERSION = "pg_dump --version"
MONGO_VERSION = "mongodump --version"

# MongoDB logins: the names of the container's environment variables, never their values.
SETUP = ("MONGO_SETUP_USER", "MONGO_SETUP_PASSWORD")
ROOT = ("MONGO_INITDB_ROOT_USERNAME", "MONGO_INITDB_ROOT_PASSWORD")


def _mongo_tool(tool: str, args: str, login: tuple[str, str]) -> str:
    """mongodump/mongorestore with the password in a config file (mode 600, deleted on exit).
    printf is a shell builtin, so the password is never a process argument."""
    user, password = login
    return f"""set -eu
umask 077
cfg=$(mktemp)
trap 'rm -f "$cfg"' EXIT
printf 'password: "%s"\\n' "${password}" > "$cfg"
{tool} --host=127.0.0.1 --port=27017 --username="${user}" \
  --authenticationDatabase=admin --config="$cfg" --quiet {args}
"""


MONGO_DUMP = _mongo_tool("mongodump", '--db="$MI_DB" --gzip --archive="$MI_FILE"', SETUP)

MONGO_RESTORE = _mongo_tool(
    "mongorestore",
    '--gzip --archive="$MI_FILE" --nsInclude="$MI_SRC_DB.*" '
    '--nsFrom="$MI_SRC_DB.*" --nsTo="$MI_DB.*"',
    ROOT,
)


def _mongosh(login: tuple[str, str], body: str) -> str:
    """mongosh script that logs in with the password read from the container's environment."""
    user, password = login
    return (
        'const conn = new Mongo("mongodb://127.0.0.1:27017");'
        f'conn.getDB("admin").auth(process.env.{user}, process.env.{password});'
        "const target = conn.getDB(process.env.MI_DB);" + body
    )


_COUNT = (
    "const out = {};"
    "target.getCollectionNames().sort().forEach(c => {"
    " out[c] = target.getCollection(c).countDocuments({}); });"
    "print(JSON.stringify(out));"
)
MONGO_COUNT_SETUP_JS = _mongosh(SETUP, _COUNT)  # the source database
MONGO_COUNT_ROOT_JS = _mongosh(ROOT, _COUNT)  # the throwaway database only
MONGO_DROP_JS = _mongosh(ROOT, "target.dropDatabase(); print('dropped');")
MONGO_EXISTS_JS = _mongosh(
    ROOT,
    "const names = conn.getDB('admin').adminCommand({listDatabases: 1, nameOnly: true})"
    ".databases.map(d => d.name);"
    "print(names.includes(process.env.MI_DB) ? 1 : 0);",
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
            output = (result.stderr or result.stdout or "").strip().splitlines()
            tail = "\n".join(output[-ERROR_LINES:])
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
        args = ["exec", "-T", f"--env=MI_DB={db}", MONGO_SERVICE]
        return self._call([*args, "mongosh", "--quiet", "--nodb", "--eval", js])

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


# --- Counting ----------------------------------------------------------------------------


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
    """Row counts with the Postgres superuser; ``db`` empty = the source database."""
    return parse_pg_counts(compose.sh(PG_SERVICE, PG_QUERY, stdin=PG_COUNT_SQL, db=db))


def mongo_counts(compose: Compose, db: str, js: str = MONGO_COUNT_SETUP_JS) -> dict[str, int]:
    return parse_mongo_counts(compose.mongosh(js, db=db))


# --- Backup ------------------------------------------------------------------------------


@dataclass
class BackupResult:
    files: dict[str, Path]
    manifest: dict[str, Any]
    removed: list[str] = field(default_factory=list)


def stamp(now: datetime) -> str:
    return now.astimezone(UTC).strftime(STAMP_FORMAT)


def sha256(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def backups(directory: Path, kind: Kind) -> list[Path]:
    """Files of ``kind`` in ``directory``, newest first (by their UTC timestamp)."""
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


def _file_entry(path: Path) -> dict[str, Any]:
    return {"file": path.name, "size": path.stat().st_size, "sha256": sha256(path)}


def _write_json(path: Path, data: dict[str, Any]) -> None:
    partial = path.with_name(path.name + ".partial")
    partial.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    partial.replace(path)


def make_backup(settings: Settings, compose: Compose, now: datetime) -> BackupResult:
    directory = settings.backup_dir
    directory.mkdir(parents=True, exist_ok=True)
    ts = stamp(now)
    files = {kind.label: directory / kind.name(ts) for kind in (*DUMPS, MANIFEST)}
    if any(p.exists() for p in files.values()):
        raise BackupError(f"a backup with timestamp {ts} already exists; try again in a second")
    try:
        tools = {
            "pg_dump": compose.sh(PG_SERVICE, PG_VERSION).strip(),
            "mongodump": compose.sh(MONGO_SERVICE, MONGO_VERSION).strip().splitlines()[0],
        }
        # Counts right before each dump; take backups while nothing writes.
        counts_pg = pg_counts(compose)
        _dump(compose, PG, PG_SERVICE, PG_DUMP, files[PG.label])
        counts_mongo = mongo_counts(compose, settings.mongodb_db)
        _dump(compose, MONGO, MONGO_SERVICE, MONGO_DUMP, files[MONGO.label], db=settings.mongodb_db)
        manifest = {
            "format": MANIFEST_FORMAT,
            "created_utc": now.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "tools": tools,
            PG.label: {**_file_entry(files[PG.label]), "counts": counts_pg},
            MONGO.label: {
                **_file_entry(files[MONGO.label]),
                "database": settings.mongodb_db,
                "counts": counts_mongo,
            },
        }
        _write_json(files[MANIFEST.label], manifest)  # last: marks the backup as complete
    except BaseException:
        # No half backups: nothing stays behind unless all three files were written.
        for path in files.values():
            path.unlink(missing_ok=True)
            path.with_name(path.name + ".partial").unlink(missing_ok=True)
        raise
    removed = [name for kind in (*DUMPS, MANIFEST) for name in prune(directory, kind)]
    return BackupResult(files=files, manifest=manifest, removed=removed)


# --- Restore test ------------------------------------------------------------------------


@dataclass
class Comparison:
    label: str
    expected: dict[str, int]  # from the manifest
    restored: dict[str, int]
    live: dict[str, int] | None = None  # information only

    @property
    def ok(self) -> bool:
        return self.expected == self.restored


@dataclass
class LiveCounts:
    counts: dict[str, dict[str, int]] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)


@dataclass
class VerifyResult:
    manifest_name: str
    manifest: dict[str, Any]
    comparisons: list[Comparison]
    live_errors: list[str]

    @property
    def ok(self) -> bool:
        return bool(self.comparisons) and all(c.ok for c in self.comparisons)


def load_manifest(directory: Path) -> tuple[Path, dict[str, Any]]:
    """The newest manifest in ``directory``, checked for the fields verify needs."""
    found = backups(directory, MANIFEST)
    if not found:
        raise BackupError(
            "no backup manifest in BACKUP_DIR; run python -m market_intel.backup first"
        )
    path = found[0]
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise BackupError(f"manifest {path.name} is unreadable") from None
    try:
        if data["format"] != MANIFEST_FORMAT:
            raise BackupError(f"manifest {path.name} has unknown format {data['format']!r}")
        for kind in DUMPS:
            entry = data[kind.label]
            if not kind.matches(entry["file"]):
                raise BackupError(f"manifest {path.name}: unexpected {kind.label} file name")
            entry["size"] = int(entry["size"])
            entry["sha256"] = str(entry["sha256"])
            entry["counts"] = {str(k): int(v) for k, v in entry["counts"].items()}
    except (KeyError, TypeError, ValueError, AttributeError):
        raise BackupError(f"manifest {path.name} is incomplete") from None
    return path, data


def check_files(directory: Path, manifest: dict[str, Any]) -> list[str]:
    """Problems with the dump files named in the manifest (missing, size, SHA-256)."""
    problems = []
    for kind in DUMPS:
        entry = manifest[kind.label]
        path = directory / entry["file"]
        if not path.is_file():
            problems.append(f"{kind.label}: {entry['file']} is missing")
        elif path.stat().st_size != entry["size"]:
            problems.append(f"{kind.label}: {entry['file']} has a different size")
        elif sha256(path) != entry["sha256"]:
            problems.append(f"{kind.label}: {entry['file']} has a different SHA-256")
    return problems


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


LIVE_TABLES_SQL = (
    "SELECT table_schema, table_name FROM information_schema.tables"
    " WHERE table_type = 'BASE TABLE'"
    " AND table_schema NOT IN ('pg_catalog', 'information_schema') ORDER BY 1, 2"
)


def app_live_counts(settings: Settings) -> LiveCounts:
    """Today's counts with the APP logins (information only). Never raises."""
    live = LiveCounts()
    try:
        db = settings.require("database")
    except ConfigError as exc:
        live.errors.append(str(exc))
        return live
    redacts = [redactor(u.get_secret_value()) for u in (db.postgres_url, db.mongodb_uri)]

    def masked(exc: Exception) -> str:
        text = f"{type(exc).__name__}: {exc}"
        for redact in redacts:
            text = redact(text)
        return mask_text(text)

    try:
        with postgres.connect(db) as conn:
            counts = {}
            for schema, table in conn.execute(LIVE_TABLES_SQL).fetchall():
                query = sql.SQL("SELECT count(*) FROM {}.{}").format(
                    sql.Identifier(schema), sql.Identifier(table)
                )
                counts[f"{schema}.{table}"] = conn.execute(query).fetchone()[0]
            live.counts[PG.label] = counts
    except Exception as exc:  # information only: report and carry on
        live.errors.append(f"{PG.label}: {masked(exc)}")
    try:
        client = mongo.client(db)
        try:
            database = client[db.mongodb_db]
            live.counts[MONGO.label] = {
                name: database[name].count_documents({})
                for name in sorted(database.list_collection_names())
            }
        finally:
            client.close()
    except Exception as exc:  # information only: report and carry on
        live.errors.append(f"{MONGO.label}: {masked(exc)}")
    return live


def verify(
    settings: Settings,
    compose: Compose,
    live_counts: Callable[[Settings], LiveCounts] = app_live_counts,
) -> VerifyResult:
    """Check the newest backup against its manifest by restoring it into throwaway databases."""
    directory = settings.backup_dir
    manifest_path, manifest = load_manifest(directory)
    problems = check_files(directory, manifest)
    if problems:
        raise BackupError(f"backup {manifest_path.name} is damaged: " + "; ".join(problems))
    source_db = manifest[MONGO.label].get("database", settings.mongodb_db)
    if not SAFE_NAME.match(str(source_db)):
        raise BackupError(f"manifest {manifest_path.name}: unexpected MongoDB database name")
    comparisons: list[Comparison] = []
    try:
        drop_restore_dbs(compose)  # leftovers of an interrupted earlier run
        _restore(
            compose, PG_SERVICE, PG_RESTORE, directory / manifest[PG.label]["file"], db=RESTORE_DB
        )
        restored_pg = pg_counts(compose, RESTORE_DB)
        _restore(
            compose,
            MONGO_SERVICE,
            MONGO_RESTORE,
            directory / manifest[MONGO.label]["file"],
            db=RESTORE_DB,
            src_db=source_db,
        )
        restored_mongo = mongo_counts(compose, RESTORE_DB, MONGO_COUNT_ROOT_JS)
        live = live_counts(settings)
        for kind, restored in ((PG, restored_pg), (MONGO, restored_mongo)):
            expected = manifest[kind.label]["counts"]
            comparisons.append(
                Comparison(kind.label, expected, restored, live.counts.get(kind.label))
            )
    finally:
        problems = drop_restore_dbs(compose)
    if problems:
        raise BackupError("throwaway databases not dropped: " + "; ".join(problems))
    return VerifyResult(manifest_path.name, manifest, comparisons, live.errors)


# --- Output ------------------------------------------------------------------------------


def _size(size_bytes: int) -> str:
    size = float(size_bytes)
    if size < 1024:
        return f"{size:.0f} B"
    for unit in ("KB", "MB"):
        size /= 1024
        if size < 1024:
            return f"{size:.1f} {unit}"
    return f"{size / 1024:.1f} GB"


def _num(value: int | None) -> str:
    return "-" if value is None else str(value)


def render_backup(result: BackupResult, directory: Path) -> str:
    lines = [f"Backup written to {directory}"]
    units = {PG.label: ("tables", "rows"), MONGO.label: ("collections", "documents")}
    for kind in DUMPS:
        entry = result.manifest[kind.label]
        things, items = units[kind.label]
        total = sum(entry["counts"].values())
        lines.append(
            f"  {kind.label:<9}: {entry['file']} ({_size(entry['size'])}; "
            f"{len(entry['counts'])} {things}, {total} {items})"
        )
    lines.append(f"  {MANIFEST.label:<9}: {result.files[MANIFEST.label].name}")
    lines.append(
        f"  kept the newest {KEEP} of each kind; removed {len(result.removed)} older file(s)"
    )
    lines.append("result: OK")
    return "\n".join(lines)


def render_verify(result: VerifyResult) -> str:
    m = result.manifest
    lines = [f"Restore test of {result.manifest_name} (backup taken {m['created_utc']}):"]
    for kind in DUMPS:
        lines.append(f"  {kind.label:<9}: {m[kind.label]['file']}  size and SHA-256 match")
    for comp in result.comparisons:
        names = sorted(set(comp.expected) | set(comp.restored) | set(comp.live or {}))
        header = f"{comp.label:<36} {'manifest':>10} {'restored':>10} {'live now':>10}"
        lines += ["", header]
        for name in names:
            expected, restored = comp.expected.get(name), comp.restored.get(name)
            live = None if comp.live is None else comp.live.get(name)
            mark = "ok" if expected == restored else "MISMATCH"
            lines.append(
                f"  {name:<34} {_num(expected):>10} {_num(restored):>10} {_num(live):>10}  {mark}"
            )
        if not names:
            lines.append("  (no tables or collections)")
    lines += ["", "'live now' is information only (app logins); it may differ once data changes."]
    lines += [f"live counts unavailable: {error}" for error in result.live_errors]
    lines.append(f"throwaway databases ({RESTORE_DB}) dropped: yes")
    lines.append(
        "result: OK" if result.ok else "result: FAILED (restored counts differ from the manifest)"
    )
    return "\n".join(lines)


# --- Command -----------------------------------------------------------------------------


def run(
    settings: Settings,
    *,
    verify_only: bool = False,
    compose: Compose | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    live_counts: Callable[[Settings], LiveCounts] = app_live_counts,
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
            result = verify(settings, compose, live_counts)
            for comp in result.comparisons:
                (log.info if comp.ok else log.error)(
                    "restore_test", database=comp.label, ok=comp.ok, tables=len(comp.expected)
                )
            print(render_verify(result))
            return 0 if result.ok else 1
        backup = make_backup(settings, compose, now())
        log.info(
            "backup_done",
            files=[p.name for p in backup.files.values()],
            removed=len(backup.removed),
        )
        print(render_backup(backup, settings.backup_dir))
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
        help="restore the newest backup into throwaway databases and compare with its manifest",
    )
    args = parser.parse_args(argv)
    try:
        settings = get_settings()
    except ConfigError as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        return 1
    configure_logging(settings, "backup")
    with bound_context(run_id=new_run_id(), component="backup"):
        return run(settings, verify_only=args.verify)


if __name__ == "__main__":
    sys.exit(main())
