"""Tests for market_intel.backup. Docker is never called: a fake runner stands in for
``docker compose`` and records every command."""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from market_intel import backup
from market_intel.backup import MONGO, PG, RESTORE_DB, Compose
from market_intel.config import Settings, load_settings

FAKE = "fake-secret-value-123"
NOW = datetime(2026, 11, 21, 3, 4, 5, tzinfo=timezone(timedelta(hours=5, minutes=30)))
STAMP = "20261120T213405Z"  # NOW in UTC
SOURCE_PG = {"public.alembic_version": 1, "public.instruments": 3}
SOURCE_MONGO = {"news_items": 2, "prediction_audit": 0}


class FakeDocker:
    """Answers ``docker compose`` calls like the real containers would."""

    def __init__(self, *, running: str = "postgres\nmongo\n", fail: str | None = None) -> None:
        self.calls: list[list[str]] = []
        self.running = running
        self.fail = fail  # fail the first command containing this text
        self.pg_restored = dict(SOURCE_PG)
        self.mongo_restored = dict(SOURCE_MONGO)
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
        elif args[0] == "cp" and not args[2].startswith(("postgres:", "mongo:")):
            Path(args[2]).write_bytes(b"dump bytes")
        elif args[0] == "exec":
            env = dict(
                a.removeprefix("--env=").split("=", 1) for a in args if a.startswith("--env=")
            )
            db = env.get("MI_DB", "")
            if input == backup.PG_COUNT_SQL:
                counts = self.pg_restored if db == RESTORE_DB else SOURCE_PG
                out = "".join(f"{k}|{v}\n" for k, v in counts.items())
            elif input == backup.PG_EXISTS_SQL or args[-1] == backup.MONGO_EXISTS_JS:
                out = "0\n"
            elif args[-1] == backup.MONGO_COUNT_JS:
                counts = self.mongo_restored if db == RESTORE_DB else SOURCE_MONGO
                out = json.dumps(counts) + "\n"
        return subprocess.CompletedProcess(cmd, 0, out, "")

    def scripts(self) -> list[str]:
        """The script or command run by each ``exec`` call, in order."""
        return [c[-1] for c in self.calls if c[6] == "exec"]


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


def compose_with(fake: FakeDocker, env_file: Path) -> Compose:
    return Compose(compose_file=Path("compose.yaml"), env_file=env_file, run=fake)


def run_backup(settings: Settings, fake: FakeDocker, env_file: Path, *, verify=False) -> int:
    compose = compose_with(fake, env_file)
    return backup.run(settings, verify_only=verify, compose=compose, now=lambda: NOW)


def touch(directory: Path, name: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_bytes(b"old")
    return path


# --- profile and readiness ---------------------------------------------------------------


def test_restricted_profile_is_refused(monkeypatch, backup_dir, compose_env, capsys):
    settings = settings_for(monkeypatch, backup_dir, profile="restricted")
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


def test_backup_writes_one_file_of_each_kind_named_by_utc_time(
    monkeypatch, backup_dir, compose_env, capsys
):
    settings = settings_for(monkeypatch, backup_dir)
    assert run_backup(settings, FakeDocker(), compose_env) == 0
    names = sorted(p.name for p in backup_dir.iterdir())
    assert names == [f"market_intel-mongo-{STAMP}.archive.gz", f"market_intel-pg-{STAMP}.dump"]
    out = capsys.readouterr().out
    assert "result: OK" in out


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
    scripts = fake.scripts()
    assert any("pg_dump" in s and "--format=custom" in s for s in scripts)
    assert any("mongodump" in s and "--gzip --archive" in s for s in scripts)
    copies = [c[7] for c in fake.calls if c[6] == "cp"]
    assert copies == [f"postgres:/tmp/{PG.name(STAMP)}", f"mongo:/tmp/{MONGO.name(STAMP)}"]
    # the temporary dump files inside the containers are removed again
    assert ["rm", "-f", f"/tmp/{PG.name(STAMP)}"] in [c[-3:] for c in fake.calls]
    assert ["rm", "-f", f"/tmp/{MONGO.name(STAMP)}"] in [c[-3:] for c in fake.calls]


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
        assert "--password" not in joined  # MongoDB tools read it from a config file
    # passwords are only ever read from the container's own environment, inside the script
    mongo_tools = [s for s in fake.scripts() if "mongodump" in s or "mongorestore" in s]
    assert mongo_tools and all('--config="$cfg"' in s for s in mongo_tools)


def test_keeps_the_newest_seven_of_each_kind(monkeypatch, backup_dir, compose_env, capsys):
    old = [f"2026110{d}T000000Z" for d in range(1, 10)]  # 9 older backups of each kind
    for ts in old:
        touch(backup_dir, PG.name(ts))
        touch(backup_dir, MONGO.name(ts))
    unrelated = [touch(backup_dir, "notes.txt"), touch(backup_dir, "market_intel-pg-latest.dump")]
    settings = settings_for(monkeypatch, backup_dir)
    assert run_backup(settings, FakeDocker(), compose_env) == 0
    for kind in (PG, MONGO):
        kept = [p.name for p in backup.backups(backup_dir, kind)]
        assert kept == [kind.name(ts) for ts in [STAMP, *reversed(old)][:7]]
    assert all(p.exists() for p in unrelated)
    assert "removed 6 older file(s)" in capsys.readouterr().out


def test_failed_dump_leaves_no_half_backup(monkeypatch, backup_dir, compose_env, capsys):
    settings = settings_for(monkeypatch, backup_dir)
    fake = FakeDocker(fail="mongodump")
    assert run_backup(settings, fake, compose_env) == 1
    assert list(backup_dir.iterdir()) == []  # the Postgres dump was removed again
    assert "Backup error" in capsys.readouterr().err
    assert ["rm", "-f", f"/tmp/{MONGO.name(STAMP)}"] in [c[-3:] for c in fake.calls]


def test_error_output_is_masked(monkeypatch, backup_dir, compose_env, capsys):
    settings = settings_for(monkeypatch, backup_dir)
    fake = FakeDocker(fail="pg_dump")
    fake.error_text = f"connection to postgresql://owner:{FAKE}@db/x failed"
    assert run_backup(settings, fake, compose_env) == 1
    err = capsys.readouterr().err
    assert FAKE not in err and "connection to" in err


def test_timestamp_names_and_matching():
    assert backup.stamp(NOW) == STAMP
    assert PG.matches(f"market_intel-pg-{STAMP}.dump")
    assert not PG.matches(f"market_intel-pg-{STAMP}.dump.partial")
    assert not MONGO.matches(f"market_intel-pg-{STAMP}.dump")
    assert backup.stamp(datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)) == "20260102T030405Z"


# --- restore test ------------------------------------------------------------------------


def test_verify_passes_when_counts_match(monkeypatch, backup_dir, compose_env, capsys):
    settings = settings_for(monkeypatch, backup_dir)
    fake = FakeDocker()
    run_backup(settings, fake, compose_env)
    capsys.readouterr()
    assert run_backup(settings, fake, compose_env, verify=True) == 0
    out = capsys.readouterr().out
    assert "public.instruments" in out and "news_items" in out
    assert "MISMATCH" not in out and "result: OK" in out
    copies_in = [
        c[8] for c in fake.calls if c[6] == "cp" and c[8].startswith(("postgres:", "mongo:"))
    ]
    assert copies_in == [
        f"postgres:/tmp/restore-{PG.name(STAMP)}",
        f"mongo:/tmp/restore-{MONGO.name(STAMP)}",
    ]


def test_verify_restores_into_throwaway_databases_and_drops_them(
    monkeypatch, backup_dir, compose_env
):
    settings = settings_for(monkeypatch, backup_dir)
    fake = FakeDocker()
    run_backup(settings, fake, compose_env)
    fake.calls.clear()
    run_backup(settings, fake, compose_env, verify=True)
    restores = [c for c in fake.calls if "pg_restore" in c[-1] or "mongorestore" in c[-1]]
    assert len(restores) == 2
    assert all(f"--env=MI_DB={RESTORE_DB}" in c for c in restores)
    assert "--env=MI_SRC_DB=market_intel" in restores[1]
    # the last steps drop both throwaway databases and confirm they are gone
    last = fake.scripts()[-4:]
    assert last == [backup.PG_DROP, backup.PG_QUERY, backup.MONGO_DROP_JS, backup.MONGO_EXISTS_JS]


def test_verify_fails_on_count_mismatch_and_still_drops(
    monkeypatch, backup_dir, compose_env, capsys
):
    settings = settings_for(monkeypatch, backup_dir)
    fake = FakeDocker()
    run_backup(settings, fake, compose_env)
    capsys.readouterr()
    fake.pg_restored = {"public.alembic_version": 1}  # a table went missing in the restore
    fake.mongo_restored = {**SOURCE_MONGO, "news_items": 1}
    assert run_backup(settings, fake, compose_env, verify=True) == 1
    out = capsys.readouterr().out
    assert out.count("MISMATCH") == 2 and "missing" in out
    assert "result: FAILED" in out
    assert fake.scripts()[-4:-2] == [backup.PG_DROP, backup.PG_QUERY]


def test_verify_drops_throwaway_databases_when_restore_fails(
    monkeypatch, backup_dir, compose_env, capsys
):
    settings = settings_for(monkeypatch, backup_dir)
    fake = FakeDocker()
    run_backup(settings, fake, compose_env)
    fake.calls.clear()
    fake.fail = "pg_restore"
    assert run_backup(settings, fake, compose_env, verify=True) == 1
    assert "Backup error" in capsys.readouterr().err
    assert fake.scripts()[-4:] == [
        backup.PG_DROP,
        backup.PG_QUERY,
        backup.MONGO_DROP_JS,
        backup.MONGO_EXISTS_JS,
    ]


def test_verify_without_backups(monkeypatch, backup_dir, compose_env, capsys):
    settings = settings_for(monkeypatch, backup_dir)
    assert run_backup(settings, FakeDocker(), compose_env, verify=True) == 1
    assert "no postgres backup" in capsys.readouterr().err


def test_verify_uses_the_newest_backup(monkeypatch, backup_dir, compose_env):
    for ts in ("20261101T000000Z", "20261115T000000Z", "20261110T000000Z"):
        touch(backup_dir, PG.name(ts))
        touch(backup_dir, MONGO.name(ts))
    settings = settings_for(monkeypatch, backup_dir)
    fake = FakeDocker()
    assert run_backup(settings, fake, compose_env, verify=True) == 0
    copied = [c[7] for c in fake.calls if c[6] == "cp"]
    assert [Path(p).name for p in copied] == [
        PG.name("20261115T000000Z"),
        MONGO.name("20261115T000000Z"),
    ]


def test_parse_counts():
    assert backup.parse_pg_counts("public.a|3\npublic.b|0\n") == {"public.a": 3, "public.b": 0}
    assert backup.parse_mongo_counts('noise\n{"x": 2}\n') == {"x": 2}
    with pytest.raises(backup.BackupError):
        backup.parse_mongo_counts("")
