"""Verify database-scoped systemd writer installation and control."""

from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest

_ROOT = Path(__file__).parents[1]
_SCRIPT = _ROOT / "scripts/github-puller-daemon.sh"
_REPOSITORY = "acme/widgets"


def _unit_for(database: Path) -> str:
    canonical = str(database.resolve())
    identity = hashlib.sha256(canonical.encode()).hexdigest()[:12]
    return f"gh-puller-{identity}.service"


def _full_digest_unit_for(database: Path) -> str:
    canonical = str(database.resolve())
    identity = hashlib.sha256(canonical.encode()).hexdigest()
    return f"gh-puller-{identity}.service"


def _bind_archive(
    database: Path,
    repository: str = _REPOSITORY,
    schema: str = "4",
) -> None:
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE archive_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
        )
        connection.executemany(
            "INSERT INTO archive_meta(key, value) VALUES (?, ?)",
            (("schema_version", schema), ("repository", repository)),
        )


def _write_managed_unit(
    units: Path,
    database: Path,
    repository: str = _REPOSITORY,
    *,
    full_digest: bool = False,
) -> Path:
    units.mkdir(exist_ok=True)
    name = _full_digest_unit_for(database) if full_digest else _unit_for(database)
    unit = units / name
    unit.write_text(
        f"# gh-puller-repository={repository}\n# gh-puller-database={database.resolve()}\n[Unit]\n",
    )
    return unit


def _environment(tmp_path: Path) -> tuple[dict[str, str], Path, Path]:
    log = tmp_path / "systemctl.log"
    systemctl = tmp_path / "systemctl"
    systemctl.write_text(
        '#!/usr/bin/env bash\nprintf \'%s\\n\' "$*" >>"$GH_PULLER_TEST_SYSTEMCTL_LOG"\n',
    )
    systemctl.chmod(0o755)
    uv = shutil.which("uv")
    assert uv is not None
    units = tmp_path / "units"
    environment = os.environ | {
        "GH_PULLER_JOURNALCTL": str(systemctl),
        "GH_PULLER_SYSTEMD_ANALYZE": "/usr/bin/true",
        "GH_PULLER_SYSTEMD_DIR": str(units),
        "GH_PULLER_SYSTEMCTL": str(systemctl),
        "GH_PULLER_TEST_SYSTEMCTL_LOG": str(log),
        "GH_PULLER_UV_BIN": uv,
    }
    return environment, units, log


def _run(
    *arguments: str,
    environment: dict[str, str],
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [_SCRIPT, *arguments],
        cwd=_ROOT,
        env=environment,
        check=check,
        capture_output=True,
        text=True,
    )


def test_daemon_script_renders_database_scoped_service_contract(tmp_path: Path) -> None:
    environment, _, _ = _environment(tmp_path)
    database = tmp_path / "nested" / "widgets.sqlite3"

    subprocess.run(["bash", "-n", _SCRIPT], check=True)
    rendered = _run(
        "render",
        _REPOSITORY,
        str(database),
        "--interval",
        "30m",
        "--concurrency",
        "7",
        environment=environment,
    ).stdout

    canonical = database.resolve()
    identity = _unit_for(database).removeprefix("gh-puller-").removesuffix(".service")
    assert "User=root" not in rendered
    assert f"# gh-puller-repository={_REPOSITORY}" in rendered
    assert f"# gh-puller-database={canonical}" in rendered
    assert f"WorkingDirectory={_ROOT}" in rendered
    assert f"SyslogIdentifier=gh-puller-{identity}" in rendered
    assert "run --frozen -m gh_puller.github schedule" in rendered
    assert f'"{_REPOSITORY}" "{canonical}"' in rendered
    assert '"--concurrency" "7"' in rendered
    assert '"--interval" "30m"' in rendered
    assert "Restart=always" in rendered
    assert "WantedBy=multi-user.target" in rendered


def test_install_is_idempotent_and_uninstall_preserves_archive(tmp_path: Path) -> None:
    environment, units, log = _environment(tmp_path)
    database = tmp_path / "widgets.sqlite3"
    _bind_archive(database)
    original = database.read_bytes()
    unit_name = _unit_for(database)
    database_arguments = (str(database), os.path.relpath(database, _ROOT))

    for argument in database_arguments:
        result = _run("install", _REPOSITORY, argument, environment=environment)
        assert f"Installed and started {unit_name}" in result.stdout
        assert f"Status: {_SCRIPT} status {database.resolve()}" in result.stdout
        assert f"Logs:   {_SCRIPT} logs {database.resolve()}" in result.stdout
        assert "Status: sudo" not in result.stdout
        assert "Logs:   sudo" not in result.stdout

    unit = units / unit_name
    assert unit.is_file()
    assert unit.stat().st_mode & 0o777 == 0o644
    assert f'"{database.resolve()}"' in unit.read_text()
    install_calls = log.read_text().splitlines()
    assert install_calls.count("daemon-reload") == 2
    assert install_calls.count(f"enable {unit_name}") == 2
    assert install_calls.count(f"restart {unit_name}") == 2
    assert install_calls.count(f"is-active --quiet {unit_name}") == 2

    for argument in reversed(database_arguments):
        result = _run("uninstall", argument, environment=environment)
        assert "SQLite archives, .env, environments, and source files were preserved." in result.stdout

    assert not unit.exists()
    assert database.read_bytes() == original
    calls = log.read_text().splitlines()
    assert calls.count(f"disable --now {unit_name}") == 2
    assert calls.count(f"reset-failed {unit_name}") == 2


def test_install_converges_full_digest_unit_to_canonical_short_name(tmp_path: Path) -> None:
    environment, units, log = _environment(tmp_path)
    database = tmp_path / "facts.sqlite3"
    legacy = _write_managed_unit(units, database, full_digest=True)
    canonical = units / _unit_for(database)

    result = _run("install", _REPOSITORY, str(database), environment=environment)

    assert f"Installed and started {canonical.name}" in result.stdout
    assert canonical.is_file()
    assert not legacy.exists()
    calls = log.read_text().splitlines()
    assert f"disable --now {legacy.name}" in calls
    assert f"enable {canonical.name}" in calls
    assert f"restart {canonical.name}" in calls


def test_same_repository_can_have_independent_database_writers(tmp_path: Path) -> None:
    environment, units, log = _environment(tmp_path)
    databases = (tmp_path / "a" / "facts.sqlite3", tmp_path / "b" / "facts.sqlite3")

    for database in databases:
        _run("install", _REPOSITORY, str(database), environment=environment)

    unit_names = {_unit_for(database) for database in databases}
    assert len(unit_names) == 2
    assert {path.name for path in units.iterdir()} == unit_names
    for database in databases:
        assert f'"{database.resolve()}"' in (units / _unit_for(database)).read_text()
    calls = log.read_text().splitlines()
    assert {call.removeprefix("enable ") for call in calls if call.startswith("enable ")} == unit_names


def test_existing_writer_cannot_be_rebound_to_another_repository(tmp_path: Path) -> None:
    environment, units, log = _environment(tmp_path)
    database = tmp_path / "facts.sqlite3"
    unit_name = _unit_for(database)
    _run("install", _REPOSITORY, str(database), environment=environment)
    unit_before = (units / unit_name).read_bytes()
    calls_before = log.read_text()

    result = _run(
        "install",
        "acme/other",
        str(database),
        environment=environment,
        check=False,
    )

    assert result.returncode == 2
    assert f"database writer is configured for {_REPOSITORY}" in result.stderr
    assert (units / unit_name).read_bytes() == unit_before
    assert log.read_text() == calls_before


def test_archive_repository_binding_rejects_wrong_writer(tmp_path: Path) -> None:
    environment, units, log = _environment(tmp_path)
    database = tmp_path / "facts.sqlite3"
    _bind_archive(database, "acme/original")

    result = _run(
        "install",
        _REPOSITORY,
        str(database),
        environment=environment,
        check=False,
    )

    assert result.returncode == 2
    assert "database belongs to acme/original" in result.stderr
    assert not units.exists()
    assert not log.exists()


def test_install_rejects_incompatible_archive_schema(tmp_path: Path) -> None:
    environment, units, log = _environment(tmp_path)
    database = tmp_path / "facts.sqlite3"
    _bind_archive(database, schema="3")

    result = _run(
        "install",
        _REPOSITORY,
        str(database),
        environment=environment,
        check=False,
    )

    assert result.returncode == 2
    assert "database is not a valid gh-puller archive" in result.stderr
    assert not units.exists()
    assert not log.exists()


def test_install_preserves_and_rejects_non_archive_database(tmp_path: Path) -> None:
    environment, units, log = _environment(tmp_path)
    database = tmp_path / "unrelated.sqlite3"
    original = b"not a gh-puller archive"
    database.write_bytes(original)

    result = _run(
        "install",
        _REPOSITORY,
        str(database),
        environment=environment,
        check=False,
    )

    assert result.returncode == 2
    assert "database is not a valid gh-puller archive" in result.stderr
    assert database.read_bytes() == original
    assert not units.exists()
    assert not log.exists()


def test_uninstall_refuses_unmanaged_identity_collision(tmp_path: Path) -> None:
    environment, units, log = _environment(tmp_path)
    database = tmp_path / "facts.sqlite3"
    unit = units / _unit_for(database)
    units.mkdir()
    unit.write_text("[Service]\nExecStart=/usr/bin/false\n")

    result = _run("uninstall", str(database), environment=environment, check=False)

    assert result.returncode == 2
    assert "unit exists but is not managed by this installer" in result.stderr
    assert unit.is_file()
    assert not log.exists()


def test_install_rejects_short_identity_collision(tmp_path: Path) -> None:
    environment, units, log = _environment(tmp_path)
    database = tmp_path / "facts.sqlite3"
    other = tmp_path / "other.sqlite3"
    unit = units / _unit_for(database)
    units.mkdir()
    unit.write_text(
        f"# gh-puller-repository={_REPOSITORY}\n"
        f"# gh-puller-database={other.resolve()}\n[Unit]\n",
    )
    original = unit.read_bytes()

    result = _run(
        "install",
        _REPOSITORY,
        str(database),
        environment=environment,
        check=False,
    )

    assert result.returncode == 2
    assert f"writer identity collision for {database.resolve()}" in result.stderr
    assert unit.read_bytes() == original
    assert not log.exists()


@pytest.mark.parametrize("action", ["status", "start", "stop", "restart", "logs"])
def test_daemon_control_actions_address_database_writer(
    tmp_path: Path,
    action: str,
) -> None:
    environment, units, log = _environment(tmp_path)
    database = tmp_path / "facts.sqlite3"
    unit_name = _unit_for(database)
    _write_managed_unit(units, database)

    result = _run(action, str(database), environment=environment)

    calls = log.read_text().splitlines()
    if action == "status":
        assert f"DATABASE    {database.resolve()}" in result.stdout
        assert calls[0].startswith(f"show {unit_name} --property=ActiveState")
        assert calls[1] == f"--unit {unit_name} --output=cat --lines=512 --no-pager"
    elif action in {"start", "restart"}:
        assert calls == [f"{action} {unit_name}", f"is-active --quiet {unit_name}"]
    elif action == "stop":
        assert calls == [f"stop {unit_name}"]
    else:
        assert calls == [f"--unit {unit_name} --output=cat --lines=100 --follow"]


def test_control_action_resolves_existing_full_digest_unit(tmp_path: Path) -> None:
    environment, units, log = _environment(tmp_path)
    database = tmp_path / "facts.sqlite3"
    legacy = _write_managed_unit(units, database, full_digest=True)

    _run("stop", str(database), environment=environment)

    assert log.read_text().splitlines() == [f"stop {legacy.name}"]


def test_status_without_database_lists_all_managed_writers(tmp_path: Path) -> None:
    environment, units, _ = _environment(tmp_path)
    first = tmp_path / "a.sqlite3"
    second = tmp_path / "b.sqlite3"
    _write_managed_unit(units, first)
    _write_managed_unit(units, second, "acme/other")

    result = _run("status", environment=environment)

    assert "WRITER" in result.stdout
    assert "DATABASE" in result.stdout
    assert str(first.resolve()) in result.stdout
    assert str(second.resolve()) in result.stdout
    assert "acme/widgets" in result.stdout
    assert "acme/other" in result.stdout


@pytest.mark.parametrize("action", ["status", "start", "stop", "restart", "logs"])
def test_control_action_rejects_repository_in_database_position(
    tmp_path: Path,
    action: str,
) -> None:
    environment, _, log = _environment(tmp_path)

    result = _run(action, _REPOSITORY, environment=environment, check=False)

    assert result.returncode == 2
    assert f"{action} expects DATABASE" in result.stderr
    assert f"'{_REPOSITORY}' looks like OWNER/REPO" in result.stderr
    assert not log.exists()


def test_status_rejects_database_without_managed_writer(tmp_path: Path) -> None:
    environment, _, log = _environment(tmp_path)
    database = tmp_path / "missing.sqlite3"

    result = _run("status", str(database), environment=environment, check=False)

    assert result.returncode == 2
    assert f"no managed writer for database: {database.resolve()}" in result.stderr
    assert not log.exists()
