"""Direct-mode configuration (app/persistence/config.py) and the migration CLI's
--env-file: explicit env files only, the environment wins, URLs and their
escaping are preserved, TLS is enforced for remote servers, malformed or
unsafe settings fail with secret-free messages, missing optional packages are
named, and importing the package performs no I/O."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import quote

import pytest

from app.persistence import open_direct_backend
from app.persistence.config import (
    DirectDatabaseSettings,
    effective_url,
    engine_options,
    load_direct_settings,
    read_env_file,
)
from app.persistence.direct import DirectBackend
from app.persistence.errors import DirectConfigError, SchemaNotCurrentError
from backend.migrate import upgrade

ROOT = Path(__file__).resolve().parents[2]
SECRET = "s3cr3t-P@ss:w/rd%$HOME"
REMOTE = f"postgresql://planner:{quote(SECRET, safe='')}@db.example.com:5432/schedule?application_name=x&connect_timeout=5"


def write_env(path: Path, **values: str) -> Path:
    path.write_text("".join(f"{key}='{value}'\n" for key, value in values.items()), encoding="utf-8")
    return path


# -----------------------------------------------------------------------------
# Env file and environment
# -----------------------------------------------------------------------------


def test_the_environment_overrides_the_explicit_env_file(tmp_path) -> None:
    env_file = write_env(tmp_path / "direct.env", DATABASE_URL=REMOTE)
    from_file = load_direct_settings(env_file=env_file, environ={})
    assert from_file.source == "env file" and from_file.effective_url().host == "db.example.com"

    other = "postgresql://someone@other.example.com/elsewhere"
    from_environment = load_direct_settings(env_file=env_file, environ={"DATABASE_URL": other})
    assert from_environment.source == "environment" and from_environment.effective_url().host == "other.example.com"


def test_env_file_values_are_taken_verbatim_and_only_from_the_named_file(tmp_path, monkeypatch) -> None:
    env_file = write_env(tmp_path / "direct.env", DATABASE_URL=REMOTE, OTHER="${DATABASE_URL}")
    values = read_env_file(env_file)
    assert values["DATABASE_URL"] == REMOTE and values["OTHER"] == "${DATABASE_URL}"  # no interpolation

    # No search for .env files: a .env next to the working directory is ignored unless named.
    write_env(tmp_path / ".env", DATABASE_URL=REMOTE)
    monkeypatch.chdir(tmp_path)
    with pytest.raises(DirectConfigError, match="DATABASE_URL is required"):
        load_direct_settings(environ={})
    with pytest.raises(DirectConfigError, match="does not exist"):
        load_direct_settings(env_file=tmp_path / "missing.env", environ={})


def test_importing_the_package_reads_no_env_file_and_connects_nowhere(tmp_path) -> None:
    write_env(tmp_path / ".env", DATABASE_URL=REMOTE)
    code = ("import os, sys, app.persistence, app.persistence.config; "
            "print('DATABASE_URL' in os.environ, 'sqlalchemy' in sys.modules, 'dotenv' in sys.modules)")
    env = {key: value for key, value in os.environ.items() if key not in ("DATABASE_URL", "TEST_DATABASE_URL")}
    env["PYTHONPATH"] = str(ROOT)
    result = subprocess.run([sys.executable, "-c", code], cwd=tmp_path, env=env, capture_output=True, text=True,
                            timeout=60)
    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == ["False", "False", "False"]


# -----------------------------------------------------------------------------
# URLs and TLS
# -----------------------------------------------------------------------------


def test_the_url_keeps_host_database_credentials_query_and_escaping() -> None:
    url = effective_url(DirectDatabaseSettings(database_url=REMOTE.replace("postgresql://", "postgresql+psycopg://"))
                        .database_url)
    assert (url.drivername, url.host, url.port, url.database, url.username) == (
        "postgresql+psycopg", "db.example.com", 5432, "schedule", "planner")
    assert url.password == SECRET  # decoded exactly; the URL object is passed to the driver unrendered
    assert url.query["application_name"] == "x" and url.query["connect_timeout"] == "5"
    assert url.query["sslmode"] == "require"  # added in memory for a remote server


@pytest.mark.parametrize("raw", ["postgres://u:p@db.example.com/app", "postgresql://u:p@db.example.com/app"])
def test_render_style_urls_use_psycopg_3(raw) -> None:
    assert load_direct_settings(environ={"DATABASE_URL": raw}).effective_url().drivername == "postgresql+psycopg"


@pytest.mark.parametrize("mode", ["require", "verify-ca", "verify-full"])
def test_secure_tls_modes_are_kept(mode) -> None:
    url = load_direct_settings(environ={"DATABASE_URL": f"{REMOTE}&sslmode={mode}"}).effective_url()
    assert url.query["sslmode"] == mode


@pytest.mark.parametrize("mode", ["disable", "allow", "prefer", "bogus"])
def test_weak_tls_modes_are_refused_for_a_remote_server_without_echoing_the_url(mode) -> None:
    with pytest.raises(DirectConfigError) as raised:
        load_direct_settings(environ={"DATABASE_URL": f"{REMOTE}&sslmode={mode}"})
    message = str(raised.value)
    assert "sslmode" in message
    for secret_part in (SECRET, quote(SECRET, safe=""), "db.example.com", "planner"):
        assert secret_part not in message


def test_a_local_server_does_not_require_tls() -> None:
    for host in ("localhost", "127.0.0.1", "[::1]"):
        url = load_direct_settings(environ={"DATABASE_URL": f"postgresql://u@{host}:5432/app_test"}).effective_url()
        assert "sslmode" not in url.query


@pytest.mark.parametrize("raw", [
    "not a url", "http://example.com/x", "sqlite:///file.db", "postgresql+asyncpg://u:p@db.example.com/app",
    f"postgresql://planner:{quote(SECRET, safe='')}@db.example.com",  # no database
])
def test_malformed_or_unsupported_urls_fail_without_echoing_them(raw) -> None:
    with pytest.raises(DirectConfigError) as raised:
        load_direct_settings(environ={"DATABASE_URL": raw})
    assert SECRET not in str(raised.value) and raw not in str(raised.value)


def test_settings_never_show_the_url() -> None:
    settings = load_direct_settings(environ={"DATABASE_URL": REMOTE})
    assert SECRET not in repr(settings) and "db.example.com" not in repr(settings)
    assert settings.describe() == {"source": "environment", "tls": "require", "local_server": "False"}


def test_the_pool_is_small_bounded_and_hides_parameters() -> None:
    options = engine_options(load_direct_settings(environ={"DATABASE_URL": REMOTE}))
    assert options["pool_pre_ping"] and options["hide_parameters"] and options["echo"] is False
    assert options["pool_size"] <= 5 and options["max_overflow"] <= 5 and 0 < options["pool_timeout"] <= 30
    assert 0 < options["connect_args"]["connect_timeout"] <= 30
    with pytest.raises(DirectConfigError):
        DirectDatabaseSettings(database_url=REMOTE, pool_size=100)


# -----------------------------------------------------------------------------
# Schema revision and optional packages
# -----------------------------------------------------------------------------


def test_the_schema_revision_is_checked_and_never_migrated(blank_engine) -> None:
    backend = DirectBackend(blank_engine)
    with pytest.raises(SchemaNotCurrentError, match="has no schema yet") as raised:
        backend.check_schema()
    assert "backend.migrate --env-file .env upgrade" in str(raised.value)
    assert backend.schema_revision()[0] is None  # nothing was created by the check

    upgrade(blank_engine, "0005")
    with pytest.raises(SchemaNotCurrentError, match="revision 0005"):
        backend.check_schema()
    upgrade(blank_engine)
    assert backend.check_schema() == backend.schema_revision()[1]


def _run_blocked(tmp_path: Path, blocked: str, code: str) -> subprocess.CompletedProcess:
    probe = (
        "import sys\n"
        "class Block:\n"
        "    def find_spec(self, name, path=None, target=None):\n"
        f"        if name.split('.')[0] == {blocked!r}:\n"
        "            raise ModuleNotFoundError(name, name=name)\n"
        "sys.meta_path.insert(0, Block())\n" + code
    )
    env = {key: value for key, value in os.environ.items() if key not in ("DATABASE_URL", "TEST_DATABASE_URL")}
    env["PYTHONPATH"] = str(ROOT)
    return subprocess.run([sys.executable, "-c", probe], cwd=tmp_path, env=env, capture_output=True, text=True,
                          timeout=120)


@pytest.mark.parametrize("blocked", ["sqlalchemy", "psycopg", "argon2", "alembic"])
def test_a_missing_optional_package_is_named(tmp_path, blocked) -> None:
    code = (
        "from app.persistence import load_direct_settings, open_direct_backend\n"
        "from app.persistence.errors import DirectModeUnavailableError\n"
        "try:\n"
        "    open_direct_backend(load_direct_settings(environ={'DATABASE_URL': 'postgresql://u@127.0.0.1:1/x_test'}))\n"
        "except DirectModeUnavailableError as error:\n"
        "    print('UNAVAILABLE', error)\n"
    )
    result = _run_blocked(tmp_path, blocked, code)
    assert result.returncode == 0, result.stderr[-2000:]
    assert "UNAVAILABLE" in result.stdout and blocked in result.stdout.lower() and "requirements-direct.txt" in result.stdout


def test_a_missing_dotenv_package_is_named(tmp_path) -> None:
    write_env(tmp_path / "x.env", DATABASE_URL=REMOTE)
    code = (
        "from app.persistence.config import read_env_file\n"
        "from app.persistence.errors import DirectModeUnavailableError\n"
        "try:\n    read_env_file('x.env')\nexcept DirectModeUnavailableError as error:\n    print('UNAVAILABLE', error)\n"
    )
    result = _run_blocked(tmp_path, "dotenv", code)
    assert "UNAVAILABLE" in result.stdout and "python-dotenv" in result.stdout


def test_an_unreachable_server_is_a_safe_error(tmp_path) -> None:
    settings = load_direct_settings(environ={"DATABASE_URL": f"postgresql://planner:{quote(SECRET, safe='')}"
                                                             "@127.0.0.1:1/schedule_test"}, connect_timeout_seconds=3)
    from app.persistence.errors import DatabaseUnavailableError

    with pytest.raises(DatabaseUnavailableError) as raised:
        open_direct_backend(settings)
    assert SECRET not in str(raised.value) and "planner" not in str(raised.value)
    assert raised.value.__cause__ is None and raised.value.__suppress_context__


# -----------------------------------------------------------------------------
# The migration CLI
# -----------------------------------------------------------------------------


def _cli(tmp_path: Path, *args: str, env: dict | None = None) -> subprocess.CompletedProcess:
    base = {key: value for key, value in os.environ.items() if key not in ("DATABASE_URL", "TEST_DATABASE_URL")}
    return subprocess.run([sys.executable, "-m", "backend.migrate", *args], cwd=ROOT, capture_output=True, text=True,
                          timeout=120, env={**base, **(env or {})})


def test_the_migration_cli_reads_an_explicit_env_file_and_fails_safely(tmp_path) -> None:
    weak = write_env(tmp_path / "weak.env", DATABASE_URL=f"{REMOTE}&sslmode=disable")
    result = _cli(tmp_path, "--env-file", str(weak), "check")
    assert result.returncode == 2 and "sslmode=disable" in result.stderr and "Traceback" not in result.stderr
    unreachable = write_env(tmp_path / "down.env", DATABASE_URL=f"postgresql://planner:{quote(SECRET, safe='')}"
                                                                "@127.0.0.1:1/schedule_test")
    result = _cli(tmp_path, "--env-file", str(unreachable), "current")
    assert result.returncode == 2 and "could not be reached" in result.stderr and "Traceback" not in result.stderr
    for output in (result.stdout, result.stderr):
        assert SECRET not in output and quote(SECRET, safe="") not in output and "planner" not in output

    missing = _cli(tmp_path, "--env-file", str(tmp_path / "nope.env"), "upgrade")
    assert missing.returncode == 2 and "does not exist" in missing.stderr


def test_the_migration_cli_still_works_from_the_environment_alone(tmp_path) -> None:
    url = f"sqlite:///{(tmp_path / 'env-only.db').as_posix()}"
    assert _cli(tmp_path, "upgrade", env={"DATABASE_URL": url}).returncode == 0
    assert _cli(tmp_path, "check", env={"DATABASE_URL": url}).returncode == 0
    missing = _cli(tmp_path, "upgrade")
    assert missing.returncode == 2 and "DATABASE_URL is required" in missing.stderr
