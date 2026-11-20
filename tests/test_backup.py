"""Tests for market_intel.backup. Docker is never called: a fake runner stands in for
``docker compose`` and records every command. Live counts use injected fakes."""

from __future__ import annotations

import hashlib
import json
import subprocess
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from market_intel import backup
from market_intel.backup import MANIFEST, MONGO, PG, RESTORE_DB, Compose, LiveCounts
from market_intel.config import Settings, load_settings

FAKE = "fake-secret-value-123"
NOW = datetime(2026, 11, 21, 3, 4, 5, tzinfo=timezone(timedelta(hours=5, minutes=30)))
STAMP = "20261120T213405Z"  # NOW in UTC
SOURCE_PG = {"public.alembic_version": 1, "public.instruments": 3}
SOURCE_MONGO = {"news_items": 2, "prediction_audit": 0}
PG_VERSION = "pg_dump (PostgreSQL) 18.7"
MONGO_VERSION = "mongodump version: 100.20.1"


class FakeDocker:
    """Answers ``docker compose`` calls like the real containers would."""

    def __init__(self, *, running: str = "postgres\nmongo\n", fail: str | None = None) -> None:
        self.calls: list[list[str]] = []
        self.running = running
        self.fail = fail  # fail the first command containing this text
        self.source_pg = dict(SOURCE_PG)
        self.source_mongo = dict(SOURCE_MONGO)
        self.restored_pg = dict(SOURCE_PG)
        self.restored_mongo = dict(SOURCE_MONGO)
        self.error_text = "something broke"

    def __call__(self, cmd: list[str], *, input: str | None = None, **_: object):
        self.calls.append(cmd)
        joined = " ".join(cmd) + (input or "")
        if self.fail and self.fail in joined:
            self.fail = None
            return subprocess.CompletedProcess(cmd, 1, "", self.error_text)
        args = cmd[6:]  # after: docker compose -f <file> --env-file <file>
        out = ""
        if args[0] == "ps":
            out = self.running
        elif args[0] == "cp" and args[1].startswith(("postgres:", "mongo:")):
            Path(args[2]).write_bytes(f"dump of {args[1]}".encode())
        elif args[0] == "exec":
            db = _env(args).get("MI_DB", "")
            script = args[-1]
            if script == backup.PG_VERSION:
                out = PG_VERSION + "\n"
            elif script == backup.MONGO_VERSION:
                out = MONGO_VERSION + "\ngit version: abc\n"
            elif input == backup.PG_COUNT_SQL:
                counts = self.restored_pg if db == RESTORE_DB else self.source_pg
                out = "".join(f"{k}|{v}\n" for k, v in counts.items())
            elif input == backup.PG_EXISTS_SQL or script == backup.MONGO_EXISTS_JS:
                out = "0\n"
            elif script == backup.MONGO_COUNT_SETUP_JS:
                out = json.dumps(self.source_mongo) + "\n"
            elif script == backup.MONGO_COUNT_ROOT_JS:
                out = json.dumps(self.restored_mongo) + "\n"
        return subprocess.CompletedProcess(cmd, 0, out, "")

    def execs(self) -> list[tuple[str, str]]:
        """(MI_DB, script or command) of each ``exec`` call, in order."""
        return [(_env(c).get("MI_DB", ""), c[-1]) for c in self.calls if c[6] == "exec"]

    def scripts(self) -> list[str]:
        return [script for _, script in self.execs()]


def _env(args: list[str]) -> dict[str, str]:
    pairs = [a.removeprefix("--env=").split("=", 1) for a in args if a.startswith("--env=")]
    return dict(pairs)


@pytest.fixture
def backup_dir(tmp_path: Path) -> Path:
    return tmp_path / "backups"


@pytest.fixture
def compose_env(tmp_path: Path) -> Path:
    path = tmp_path / "compose.env"
    path.write_text("", encoding="utf-8")  # only its existence is checked
    return path


def settings_for(monkeypatch, backup_dir: Path, profile: str = "full", **env: str) -> Settings:
    monkeypatch.setenv("APP_PROFILE", profile)
    monkeypatch.setenv("BACKUP_DIR", str(backup_dir))
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return load_settings()


def live_same() -> LiveCounts:
    return LiveCounts(counts={PG.label: dict(SOURCE_PG), MONGO.label: dict(SOURCE_MONGO)})


def compose_with(fake: FakeDocker, env_file: Path) -> Compose:
    return Compose(compose_file=Path("compose.yaml"), env_file=env_file, run=fake)


def run_backup(
    settings: Settings,
    fake: FakeDocker,
    env_file: Path,
    *,
    verify: bool = False,
    live=live_same,
    now: datetime = NOW,
) -> int:
    return backup.run(
        settings,
        verify_only=verify,
        compose=compose_with(fake, env_file),
        now=lambda: now,
        live_counts=lambda _settings: live(),
    )


def touch(directory: Path, name: str, content: bytes = b"old") -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_bytes(content)
    return path


def manifest_of(backup_dir: Path, stamp: str = STAMP) -> dict:
    return json.loads((backup_dir / MANIFEST.name(stamp)).read_text(encoding="utf-8"))


def copies(fake: FakeDocker) -> list[list[str]]:
    """(source, destination) of each ``docker compose cp``."""
    return [c[7:9] for c in fake.calls if c[6] == "cp"]


# --- profile and readiness ---------------------------------------------------------------


@pytest.mark.parametrize("profile", ["restricted", "server"])
def test_only_the_full_profile_runs(monkeypatch, backup_dir, compose_env, capsys, profile):
    settings = settings_for(monkeypatch, backup_dir, profile=profile)
    fake = FakeDocker()
    assert run_backup(settings, fake, compose_env) == 1
    assert "only on the 'full' profile" in capsys.readouterr().err
    assert fake.calls == []
    assert not backup_dir.exists()


def test_stopped_databases_give_a_clear_message(monkeypatch, backup_dir, compose_env, capsys):
    settings = settings_for(monkeypatch, backup_dir)
    assert run_backup(settings, FakeDocker(running="postgres\n"), compose_env) == 1
    err = capsys.readouterr().err
    assert "not running: mongo" in err and "up -d" in err


def test_missing_compose_env_is_reported(monkeypatch, backup_dir, tmp_path, capsys):
    settings = settings_for(monkeypatch, backup_dir)
    fake = FakeDocker()
    assert run_backup(settings, fake, tmp_path / "nope.env") == 1
    assert "compose.env is missing" in capsys.readouterr().err
    assert fake.calls == []


def test_docker_not_installed(monkeypatch, backup_dir, compose_env, capsys):
    def no_docker(*args, **kwargs):
        raise FileNotFoundError("docker")

    settings = settings_for(monkeypatch, backup_dir)
    assert backup.run(settings, compose=Compose(env_file=compose_env, run=no_docker)) == 1
    assert "docker not found" in capsys.readouterr().err


# --- backup ------------------------------------------------------------------------------


def test_backup_writes_two_dumps_and_a_manifest_named_by_utc_time(
    monkeypatch, backup_dir, compose_env, capsys
):
    settings = settings_for(monkeypatch, backup_dir)
    assert run_backup(settings, FakeDocker(), compose_env) == 0
    names = sorted(p.name for p in backup_dir.iterdir())
    assert names == [
        f"market_intel-manifest-{STAMP}.json",
        f"market_intel-mongo-{STAMP}.archive.gz",
        f"market_intel-pg-{STAMP}.dump",
    ]
    out = capsys.readouterr().out
    assert "2 tables, 4 rows" in out and "2 collections, 2 documents" in out
    assert "result: OK" in out


def test_manifest_records_time_counts_versions_size_and_hash(monkeypatch, backup_dir, compose_env):
    settings = settings_for(monkeypatch, backup_dir)
    run_backup(settings, FakeDocker(), compose_env)
    m = manifest_of(backup_dir)
    assert m["format"] == 1
    assert m["created_utc"] == "2026-11-20T21:34:05Z"
    assert m["tools"] == {"pg_dump": PG_VERSION, "mongodump": MONGO_VERSION}
    assert m["postgres"]["counts"] == SOURCE_PG
    assert m["mongodb"]["counts"] == SOURCE_MONGO
    assert m["mongodb"]["database"] == "market_intel"
    for kind in (PG, MONGO):
        path = backup_dir / m[kind.label]["file"]
        assert m[kind.label]["file"] == kind.name(STAMP)
        assert m[kind.label]["size"] == path.stat().st_size
        assert m[kind.label]["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()


def test_counts_are_taken_right_before_each_dump(monkeypatch, backup_dir, compose_env):
    settings = settings_for(monkeypatch, backup_dir)
    fake = FakeDocker()
    run_backup(settings, fake, compose_env)
    steps = [s for s in fake.scripts() if not s.startswith("/tmp/")]  # without rm cleanups
    assert steps == [
        backup.PG_VERSION,
        backup.MONGO_VERSION,
        backup.PG_QUERY,  # Postgres counts
        backup.PG_DUMP,
        backup.MONGO_COUNT_SETUP_JS,
        backup.MONGO_DUMP,
    ]


def test_dumps_run_inside_containers_and_are_copied_out(monkeypatch, backup_dir, compose_env):
    settings = settings_for(monkeypatch, backup_dir)
    fake = FakeDocker()
    run_backup(settings, fake, compose_env)
    for cmd in fake.calls:
        assert cmd[:6] == [
            "docker",
            "compose",
            "-f",
            "compose.yaml",
            "--env-file",
            str(compose_env),
        ]
    assert "--format=custom" in backup.PG_DUMP
    assert "--gzip --archive" in backup.MONGO_DUMP
    assert [src for src, _ in copies(fake)] == [
        f"postgres:/tmp/{PG.name(STAMP)}",
        f"mongo:/tmp/{MONGO.name(STAMP)}",
    ]
    # the temporary dump files inside the containers are removed again
    cleanups = [c[-3:] for c in fake.calls]
    assert ["rm", "-f", f"/tmp/{PG.name(STAMP)}"] in cleanups
    assert ["rm", "-f", f"/tmp/{MONGO.name(STAMP)}"] in cleanups


def test_logins_per_step():
    # Postgres: the superuser over the container's local socket (no host, no password).
    for script in (backup.PG_DUMP, backup.PG_RESTORE, backup.PG_QUERY, backup.PG_DROP):
        assert '--username="$POSTGRES_USER"' in script and "--host" not in script
    # MongoDB backup: the setup user.
    assert '"$MONGO_SETUP_USER"' in backup.MONGO_DUMP and "ROOT" not in backup.MONGO_DUMP
    assert "process.env.MONGO_SETUP_USER" in backup.MONGO_COUNT_SETUP_JS
    # MongoDB restore test: root.
    assert '"$MONGO_INITDB_ROOT_USERNAME"' in backup.MONGO_RESTORE
    assert "SETUP" not in backup.MONGO_RESTORE
    for js in (backup.MONGO_COUNT_ROOT_JS, backup.MONGO_DROP_JS, backup.MONGO_EXISTS_JS):
        assert "process.env.MONGO_INITDB_ROOT_USERNAME" in js


def test_mongo_root_is_used_only_on_the_throwaway_database(monkeypatch, backup_dir, compose_env):
    settings = settings_for(monkeypatch, backup_dir)
    fake = FakeDocker()
    run_backup(settings, fake, compose_env)
    run_backup(settings, fake, compose_env, verify=True)
    root_steps = [db for db, s in fake.execs() if "MONGO_INITDB_ROOT" in s]
    assert root_steps, "the restore test uses the root login"
    assert set(root_steps) == {RESTORE_DB}
    setup_steps = [db for db, s in fake.execs() if "MONGO_SETUP" in s]
    assert setup_steps and set(setup_steps) == {"market_intel"}


def test_mongo_passwords_only_via_temporary_config_file():
    for script in (backup.MONGO_DUMP, backup.MONGO_RESTORE):
        assert "umask 077" in script
        assert "trap 'rm -f \"$cfg\"' EXIT" in script
        assert "printf 'password:" in script  # a shell builtin: not a process argument
        assert '--config="$cfg"' in script and "--password" not in script


def test_no_credentials_on_any_command_line(monkeypatch, backup_dir, compose_env):
    url = f"postgresql://owner:{FAKE}@127.0.0.1:5432/market_intel"
    settings = settings_for(
        monkeypatch,
        backup_dir,
        POSTGRES_URL=url,
        POSTGRES_ADMIN_URL=url,
        MONGODB_URI=f"mongodb://app:{FAKE}@127.0.0.1:27017/market_intel?authSource=admin",
    )
    fake = FakeDocker()
    run_backup(settings, fake, compose_env)
    run_backup(settings, fake, compose_env, verify=True)
    for cmd in fake.calls:
        joined = " ".join(cmd)
        assert FAKE not in joined
        assert "--password" not in joined
    assert FAKE not in (backup_dir / MANIFEST.name(STAMP)).read_text(encoding="utf-8")


def test_keeps_the_newest_seven_of_each_kind(monkeypatch, backup_dir, compose_env, capsys):
    old = [f"2026110{d}T000000Z" for d in range(1, 10)]  # 9 older backups of each kind
    for ts in old:
        for kind in (PG, MONGO, MANIFEST):
            touch(backup_dir, kind.name(ts))
    unrelated = [touch(backup_dir, "notes.txt"), touch(backup_dir, "market_intel-pg-latest.dump")]
    settings = settings_for(monkeypatch, backup_dir)
    assert run_backup(settings, FakeDocker(), compose_env) == 0
    for kind in (PG, MONGO, MANIFEST):
        kept = [p.name for p in backup.backups(backup_dir, kind)]
        assert kept == [kind.name(ts) for ts in [STAMP, *reversed(old)][:7]]
    assert all(p.exists() for p in unrelated)
    assert "removed 9 older file(s)" in capsys.readouterr().out


@pytest.mark.parametrize("step", ["pg_dump --username", "mongodump --host", "MONGO_SETUP_USER,"])
def test_failed_step_leaves_no_half_backup(monkeypatch, backup_dir, compose_env, capsys, step):
    settings = settings_for(monkeypatch, backup_dir)
    fake = FakeDocker(fail=step)
    assert run_backup(settings, fake, compose_env) == 1
    assert list(backup_dir.iterdir()) == []  # no dump, no manifest, no partial files
    assert "Backup error" in capsys.readouterr().err


def test_error_output_is_masked(monkeypatch, backup_dir, compose_env, capsys):
    settings = settings_for(monkeypatch, backup_dir)
    fake = FakeDocker(fail="pg_dump --username")
    fake.error_text = f"connection to postgresql://owner:{FAKE}@db/x failed"
    assert run_backup(settings, fake, compose_env) == 1
    err = capsys.readouterr().err
    assert FAKE not in err and "connection to" in err


def test_timestamp_names_and_matching():
    assert backup.stamp(NOW) == STAMP
    assert PG.matches(f"market_intel-pg-{STAMP}.dump")
    assert not PG.matches(f"market_intel-pg-{STAMP}.dump.partial")
    assert not MONGO.matches(f"market_intel-pg-{STAMP}.dump")
    assert MANIFEST.matches(f"market_intel-manifest-{STAMP}.json")
    assert backup.stamp(datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)) == "20260102T030405Z"


# --- restore test ------------------------------------------------------------------------


@pytest.fixture
def backed_up(monkeypatch, backup_dir, compose_env, capsys) -> tuple[Settings, FakeDocker]:
    settings = settings_for(monkeypatch, backup_dir)
    fake = FakeDocker()
    assert run_backup(settings, fake, compose_env) == 0
    fake.calls.clear()
    capsys.readouterr()
    return settings, fake


def test_verify_passes_when_restored_counts_match_the_manifest(backed_up, compose_env, capsys):
    settings, fake = backed_up
    assert run_backup(settings, fake, compose_env, verify=True) == 0
    out = capsys.readouterr().out
    assert "size and SHA-256 match" in out
    assert "public.instruments" in out and "news_items" in out
    assert "MISMATCH" not in out and "result: OK" in out
    assert [dest for _, dest in copies(fake)] == [
        f"postgres:/tmp/restore-{PG.name(STAMP)}",
        f"mongo:/tmp/restore-{MONGO.name(STAMP)}",
    ]


def test_verify_compares_with_the_manifest_not_with_live_counts(backed_up, compose_env, capsys):
    settings, fake = backed_up
    fake.source_pg = {**SOURCE_PG, "public.instruments": 99}  # live data changed since
    fake.source_mongo = {**SOURCE_MONGO, "news_items": 50}

    def live_changed() -> LiveCounts:
        return LiveCounts(counts={PG.label: fake.source_pg, MONGO.label: fake.source_mongo})

    assert run_backup(settings, fake, compose_env, verify=True, live=live_changed) == 0
    out = capsys.readouterr().out
    assert " 99 " in out and " 50 " in out  # shown for information
    assert "MISMATCH" not in out and "result: OK" in out


def test_verify_restores_into_throwaway_databases_and_drops_them(backed_up, compose_env):
    settings, fake = backed_up
    run_backup(settings, fake, compose_env, verify=True)
    restores = [c for c in fake.calls if "pg_restore" in c[-1] or "mongorestore" in c[-1]]
    assert len(restores) == 2
    assert all(f"--env=MI_DB={RESTORE_DB}" in c for c in restores)
    assert "--env=MI_SRC_DB=market_intel" in restores[1]
    # the last steps drop both throwaway databases and confirm they are gone
    assert fake.scripts()[-4:] == [
        backup.PG_DROP,
        backup.PG_QUERY,
        backup.MONGO_DROP_JS,
        backup.MONGO_EXISTS_JS,
    ]


def test_verify_fails_on_count_mismatch_and_still_drops(backed_up, compose_env, capsys):
    settings, fake = backed_up
    fake.restored_pg = {"public.alembic_version": 1}  # a table went missing in the restore
    fake.restored_mongo = {**SOURCE_MONGO, "news_items": 1}
    assert run_backup(settings, fake, compose_env, verify=True) == 1
    out = capsys.readouterr().out
    assert out.count("MISMATCH") == 2
    assert "result: FAILED" in out
    assert fake.scripts()[-4:-2] == [backup.PG_DROP, backup.PG_QUERY]


def test_verify_drops_throwaway_databases_when_restore_fails(backed_up, compose_env, capsys):
    settings, fake = backed_up
    fake.fail = "pg_restore"
    assert run_backup(settings, fake, compose_env, verify=True) == 1
    assert "Backup error" in capsys.readouterr().err
    assert fake.scripts()[-4:] == [
        backup.PG_DROP,
        backup.PG_QUERY,
        backup.MONGO_DROP_JS,
        backup.MONGO_EXISTS_JS,
    ]


@pytest.mark.parametrize("damage", ["changed", "truncated", "missing"])
def test_verify_rejects_damaged_files_before_restoring(
    backed_up, backup_dir, compose_env, capsys, damage
):
    settings, fake = backed_up
    path = backup_dir / MONGO.name(STAMP)
    original = path.read_bytes()
    if damage == "changed":
        path.write_bytes(original[:-1] + b"X")  # same size, different content
    elif damage == "truncated":
        path.write_bytes(original[:-1])
    else:
        path.unlink()
    assert run_backup(settings, fake, compose_env, verify=True) == 1
    err = capsys.readouterr().err
    expected = {"changed": "different SHA-256", "truncated": "different size", "missing": "missing"}
    assert expected[damage] in err
    assert copies(fake) == []  # nothing was restored


def test_verify_without_a_manifest(monkeypatch, backup_dir, compose_env, capsys):
    touch(backup_dir, PG.name(STAMP))
    touch(backup_dir, MONGO.name(STAMP))  # dumps without a manifest are incomplete
    settings = settings_for(monkeypatch, backup_dir)
    assert run_backup(settings, FakeDocker(), compose_env, verify=True) == 1
    assert "no backup manifest" in capsys.readouterr().err


@pytest.mark.parametrize(
    "content",
    [
        "not json",
        json.dumps({"format": 1}),
        json.dumps({"format": 2, "postgres": {}, "mongodb": {}}),
    ],
)
def test_verify_rejects_a_broken_manifest(monkeypatch, backup_dir, compose_env, capsys, content):
    touch(backup_dir, MANIFEST.name(STAMP), content.encode())
    settings = settings_for(monkeypatch, backup_dir)
    fake = FakeDocker()
    assert run_backup(settings, fake, compose_env, verify=True) == 1
    assert MANIFEST.name(STAMP) in capsys.readouterr().err
    assert copies(fake) == []


def test_verify_rejects_a_manifest_naming_other_files(backed_up, backup_dir, compose_env, capsys):
    settings, fake = backed_up
    m = manifest_of(backup_dir)
    m["postgres"]["file"] = "../elsewhere.dump"
    (backup_dir / MANIFEST.name(STAMP)).write_text(json.dumps(m), encoding="utf-8")
    assert run_backup(settings, fake, compose_env, verify=True) == 1
    assert "unexpected postgres file name" in capsys.readouterr().err


def test_verify_uses_the_newest_manifest(monkeypatch, backup_dir, compose_env):
    settings = settings_for(monkeypatch, backup_dir)
    fake = FakeDocker()
    for when in (NOW - timedelta(days=2), NOW, NOW - timedelta(days=1)):
        assert run_backup(settings, fake, compose_env, now=when) == 0
    fake.calls.clear()
    assert run_backup(settings, fake, compose_env, verify=True) == 0
    assert [Path(src).name for src, _ in copies(fake)] == [PG.name(STAMP), MONGO.name(STAMP)]


def test_live_count_errors_are_shown_but_do_not_fail(backed_up, compose_env, capsys):
    settings, fake = backed_up

    def live_down() -> LiveCounts:
        return LiveCounts(errors=["postgres: OperationalError: connection refused"])

    assert run_backup(settings, fake, compose_env, verify=True, live=live_down) == 0
    out = capsys.readouterr().out
    assert "live counts unavailable: postgres: OperationalError" in out
    assert "result: OK" in out


# --- live counts with the app logins -----------------------------------------------------


class _Rows:
    def __init__(self, rows):
        self.rows = rows

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return self.rows[0]


class FakePgConn:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, query):
        if query == backup.LIVE_TABLES_SQL:
            return _Rows([("public", "instruments"), ("public", "llm_spend")])
        return _Rows([(5,)])


class FakeCollection:
    def __init__(self, count: int) -> None:
        self.count = count

    def count_documents(self, query):
        assert query == {}
        return self.count


class FakeDatabase:
    counts = {"news_items": 7, "_healthcheck": 0}

    def list_collection_names(self):
        return list(self.counts)

    def __getitem__(self, name):
        return FakeCollection(self.counts[name])


class FakeMongoClient:
    def __init__(self) -> None:
        self.closed = False

    def __getitem__(self, name):
        assert name == "market_intel"
        return FakeDatabase()

    def close(self):
        self.closed = True


def test_live_counts_use_the_app_logins(monkeypatch, backup_dir):
    settings = settings_for(
        monkeypatch,
        backup_dir,
        POSTGRES_URL=f"postgresql://mi_app:{FAKE}@127.0.0.1:5432/market_intel",
        MONGODB_URI=f"mongodb://app:{FAKE}@127.0.0.1:27017/market_intel?authSource=admin",
    )
    used: list[str] = []
    client = FakeMongoClient()

    def fake_pg_connect(db):
        used.append(db.postgres_url.get_secret_value())
        return FakePgConn()

    def fake_mongo_client(db):
        used.append(db.mongodb_uri.get_secret_value())
        return client

    monkeypatch.setattr(backup.postgres, "connect", fake_pg_connect)
    monkeypatch.setattr(backup.mongo, "client", fake_mongo_client)
    live = backup.app_live_counts(settings)
    assert live.errors == []
    assert live.counts == {
        "postgres": {"public.instruments": 5, "public.llm_spend": 5},
        "mongodb": {"_healthcheck": 0, "news_items": 7},
    }
    assert used[0].startswith("postgresql://mi_app:") and used[1].startswith("mongodb://app:")
    assert client.closed


def test_live_count_errors_are_masked(monkeypatch, backup_dir):
    url = f"postgresql://mi_app:{FAKE}@127.0.0.1:5432/market_intel"
    settings = settings_for(
        monkeypatch,
        backup_dir,
        POSTGRES_URL=url,
        MONGODB_URI=f"mongodb://app:{FAKE}@127.0.0.1:27017/market_intel?authSource=admin",
    )

    def broken(db):
        raise RuntimeError(f"cannot reach {url} (password {FAKE})")

    monkeypatch.setattr(backup.postgres, "connect", broken)
    monkeypatch.setattr(backup.mongo, "client", broken)
    live = backup.app_live_counts(settings)
    assert live.counts == {}
    assert len(live.errors) == 2
    assert all(FAKE not in e and "mi_app" not in e for e in live.errors)


def test_live_counts_without_database_settings(monkeypatch, backup_dir):
    live = backup.app_live_counts(settings_for(monkeypatch, backup_dir))
    assert live.counts == {} and "POSTGRES_URL" in live.errors[0]


def test_parse_counts():
    assert backup.parse_pg_counts("public.a|3\npublic.b|0\n") == {"public.a": 3, "public.b": 0}
    assert backup.parse_mongo_counts('noise\n{"x": 2}\n') == {"x": 2}
    with pytest.raises(backup.BackupError):
        backup.parse_mongo_counts("")
