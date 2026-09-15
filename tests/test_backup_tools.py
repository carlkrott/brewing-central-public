"""T7 verified backup, retention, and restore-rehearsal contracts."""
from __future__ import annotations

import datetime as dt
import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path


import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

from backup_common import (  # pyright: ignore[reportMissingImports]  # noqa: E402
    BackupError,
    apply_retention,
    atomic_json,
    bind_basename,
    bind_brew_basename,
    bind_run_id,
    build_dual_manifest,
    build_manifest,
    require_capacity,
    resolve_manifest_index,
    validate_dual_generation,
    validate_generation,
)


def load_script(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / filename)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


backup_production = load_script("backup_production_script", "backup-production.py")
rehearse_restore = load_script("rehearse_restore_script", "rehearse-restore.py")
restore_production = load_script("restore_production_script", "restore-production.py")


def fixture_db(path: Path) -> Path:
    conn = sqlite3.connect(path)
    try:
        conn.executescript(
            """
            CREATE TABLE devices (device_id TEXT PRIMARY KEY);
            CREATE TABLE samples (id INTEGER PRIMARY KEY);
            CREATE TABLE calibrations (id INTEGER PRIMARY KEY);
            CREATE TABLE calibration_active (id INTEGER PRIMARY KEY);
            CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY);
            INSERT INTO schema_migrations(version) VALUES (1),(2);
            INSERT INTO devices(device_id) VALUES ('fixture');
            PRAGMA user_version=2;
            """
        )
        conn.commit()
    finally:
        conn.close()
    return path


def generation(root: Path, moment: dt.datetime, nonce: int) -> Path:
    run_id = f"{moment:%Y%m%dT%H%M%SZ}-{nonce:08x}"
    directory = root / run_id
    directory.mkdir(parents=True)
    db = fixture_db(directory / bind_basename(run_id))
    manifest = build_manifest(
        run_id,
        db,
        moment.strftime("%Y-%m-%dT%H:%M:%SZ"),
        {"mode": "fixture"},
    )
    atomic_json(directory / "manifest.json", manifest)
    return directory


def test_capacity_accepts_dynamic_inode_filesystem(tmp_path: Path, monkeypatch):
    actual = os.statvfs(tmp_path)
    dynamic = os.statvfs_result((*actual[:5], 0, 0, 0, *actual[8:]))
    monkeypatch.setattr(
        "backup_common.os.statvfs",
        lambda _path: dynamic,
    )
    require_capacity(tmp_path, 0, 1024)


def test_capacity_rejects_reported_inode_exhaustion(tmp_path: Path, monkeypatch):
    actual = os.statvfs(tmp_path)
    exhausted = os.statvfs_result((*actual[:5], 100, 0, 0, *actual[8:]))
    monkeypatch.setattr(
        "backup_common.os.statvfs",
        lambda _path: exhausted,
    )
    with pytest.raises(BackupError, match="insufficient free inodes"):
        require_capacity(tmp_path, 0, 1)


def test_container_snapshot_streams_tmpfs_bytes_without_docker_cp(tmp_path: Path, monkeypatch):
    destination = tmp_path / "snapshot.db"
    run_calls = []
    subprocess_calls = []
    monkeypatch.setattr(backup_production, "discover_volume", lambda _container: "source-volume")
    monkeypatch.setattr(backup_production, "run", lambda command: run_calls.append(command))

    def fake_subprocess_run(command, **kwargs):
        subprocess_calls.append(command)
        if kwargs.get("stdout") is not None:
            kwargs["stdout"].write(b"sqlite-backup-bytes")
        return subprocess.CompletedProcess(command, 0, b"", b"")

    monkeypatch.setattr(backup_production.subprocess, "run", fake_subprocess_run)
    volume = backup_production.snapshot_container("ispindel-dashboard", "backup.db", destination)

    assert volume == "source-volume"
    assert destination.read_bytes() == b"sqlite-backup-bytes"
    assert destination.stat().st_mode & 0o777 == 0o600
    assert all(call[:2] != ["docker", "cp"] for call in run_calls)
    assert len(subprocess_calls) == 2
    assert subprocess_calls[0][:3] == ["docker", "exec", "ispindel-dashboard"]


def test_container_snapshot_removes_partial_file_when_stream_fails(tmp_path: Path, monkeypatch):
    destination = tmp_path / "snapshot.db"
    monkeypatch.setattr(backup_production, "discover_volume", lambda _container: "source-volume")
    monkeypatch.setattr(backup_production, "run", lambda _command: None)
    calls = 0

    def fake_subprocess_run(command, **kwargs):
        nonlocal calls
        calls += 1
        if kwargs.get("stdout") is not None:
            kwargs["stdout"].write(b"partial")
            return subprocess.CompletedProcess(command, 9, b"", b"stream error")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(backup_production.subprocess, "run", fake_subprocess_run)
    with pytest.raises(BackupError, match="container backup stream failed"):
        backup_production.snapshot_container("ispindel-dashboard", "backup.db", destination)

    assert calls == 2
    assert not destination.exists()
    assert not list(tmp_path.iterdir())


def test_snapshot_sqlite_copies_wal_sidecars_before_snapshot(tmp_path: Path):
    source = tmp_path / "source.db"
    connection = sqlite3.connect(source)
    try:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("CREATE TABLE samples(value TEXT)")
        connection.execute("INSERT INTO samples(value) VALUES ('wal-value')")
        connection.commit()
        assert (tmp_path / "source.db-wal").exists()
        destination = tmp_path / "nested" / "destination.db"
        backup_production.snapshot_sqlite(source, destination)
    finally:
        connection.close()
    with sqlite3.connect(destination) as copied:
        assert copied.execute("SELECT value FROM samples").fetchone() == ("wal-value",)
    assert not list((tmp_path / "nested").glob("*.sourcecopy-*"))


def test_snapshot_sqlite_uses_immutable_read_only_source(tmp_path: Path):
    source = fixture_db(tmp_path / "source.db")
    destination = tmp_path / "nested" / "destination.db"
    backup_production.snapshot_sqlite(source, destination)
    assert destination.is_file()
    assert destination.stat().st_mode & 0o777 == 0o600
    with sqlite3.connect(destination) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)


def test_backup_binds_one_filename_end_to_end(tmp_path: Path):
    source = fixture_db(tmp_path / "source.db")
    local = tmp_path / "local"
    offhost = tmp_path / "offhost"
    run_id = "20260803T000000Z-00000001"
    args = backup_production.parser().parse_args(
        ["--execute", "--source-db", str(source), "--run-id", run_id,
         "--backup-root", str(local), "--offhost", str(offhost),
         "--min-free-bytes", "0", "--min-free-inodes", "0"]
    )
    result = backup_production.execute_backup(args)
    basename = bind_basename(run_id)
    assert result["basename"] == basename
    manifest = validate_generation(local / run_id / "manifest.json")
    assert manifest["basename"] == manifest["file"]["name"] == basename
    assert (offhost / run_id / basename).is_file()
    index = json.loads((local / "index.json").read_text())
    assert index["latest_verified_manifest"] == str((local / run_id / "manifest.json").resolve())


def test_backup_manifest_hash_integrity_and_counts(tmp_path: Path):
    directory = generation(tmp_path, dt.datetime(2026, 8, 3, tzinfo=dt.timezone.utc), 2)
    manifest = validate_generation(directory / "manifest.json")
    assert manifest["database"] == {
        "integrity": "ok", "user_version": 2,
        "migration_versions": [1, 2],
        "counts": {"devices": 1, "samples": 0, "calibrations": 0, "calibration_active": 0},
    }
    db = directory / str(manifest["basename"])
    with db.open("ab") as handle:
        handle.write(b"tamper")
    with pytest.raises(BackupError, match="size mismatch|sha256 mismatch"):
        validate_generation(directory / "manifest.json")


def test_offhost_partial_is_not_promoted_on_failure(tmp_path: Path):
    directory = generation(tmp_path / "source", dt.datetime(2026, 8, 3, tzinfo=dt.timezone.utc), 3)
    manifest_path = directory / "manifest.json"
    data = json.loads(manifest_path.read_text())
    data["file"]["sha256"] = "0" * 64
    atomic_json(manifest_path, data)
    run_id = directory.name
    with pytest.raises(BackupError):
        backup_production.copy_local(directory, tmp_path / "remote", run_id)
    assert (tmp_path / "remote" / f"{run_id}.partial").is_dir()
    assert not (tmp_path / "remote" / run_id).exists()


def test_retention_preserves_28_local_56_remote_and_12_weekly_generations(tmp_path: Path):
    now = dt.datetime(2026, 8, 3, tzinfo=dt.timezone.utc)
    local, remote = tmp_path / "local", tmp_path / "remote"
    for index in range(70):
        moment = now - dt.timedelta(days=69 - index)
        generation(local, moment, index + 10)
        generation(remote, moment, index + 1000)
    local_result = apply_retention(local, 28, now=now)
    remote_result = apply_retention(remote, 56, 12, now=now)
    assert (local_result.kept, local_result.deleted) == (28, 42)
    assert remote_result.kept >= 56
    assert remote_result.weekly_kept == 11
    assert len([p for p in remote.iterdir() if p.is_dir()]) == remote_result.kept


def test_retention_ignores_partial_or_invalid_manifests(tmp_path: Path):
    root = tmp_path / "root"
    good = generation(root, dt.datetime(2026, 8, 3, tzinfo=dt.timezone.utc), 4)
    (root / "bad.partial").mkdir()
    (root / "bad").mkdir()
    (root / "bad" / "manifest.json").write_text("not-json")
    result = apply_retention(root, 1)
    assert result.verified == 1 and good.exists()
    assert (root / "bad.partial").exists() and (root / "bad").exists()


def test_low_space_or_offhost_failure_suppresses_deletion(tmp_path: Path):
    root = tmp_path / "root"
    for index in range(4):
        generation(root, dt.datetime(2026, 8, index + 1, tzinfo=dt.timezone.utc), index + 20)
    result = apply_retention(root, 1, suppress=True)
    assert result.deleted == 0 and len(list(root.iterdir())) == 4


def test_remote_retention_runs_verified_fail_closed_program(monkeypatch: pytest.MonkeyPatch):
    observed: dict[str, object] = {}

    def fake_ssh(host: str, arguments: list[str]) -> subprocess.CompletedProcess[str]:
        observed.update(host=host, arguments=arguments)
        return subprocess.CompletedProcess(arguments, 0, json.dumps({
            "result": "REMOTE_RETENTION_OK", "verified": 70,
            "kept": 58, "deleted": 12, "weekly": 11,
        }), "")

    monkeypatch.setattr(backup_production, "ssh_command", fake_ssh)
    result = backup_production.apply_remote_retention(
        "backup@backup.example.test:/srv/ispindel", 56, 12
    )
    assert result["result"] == "REMOTE_RETENTION_OK"
    assert observed["host"] == "backup@backup.example.test"
    arguments = observed["arguments"]
    assert isinstance(arguments, list)
    assert arguments[:2] == ["python3", "-c"]
    assert arguments[-3:] == ["/srv/ispindel", "56", "12"]
    assert "integrity_check" in arguments[2]
    assert ".partial" in arguments[2]


def test_rehearsal_refuses_production_names(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    directory = generation(tmp_path, dt.datetime(2026, 8, 3, tzinfo=dt.timezone.utc), 30)
    args = rehearse_restore.parser().parse_args([
        "--manifest", str(directory / "manifest.json"), "--image", "fixture-image",
        "--evidence-dir", str(tmp_path / "evidence"), "--nonce", "fixed",
        "--production-volume", "ispindel-rehearsal-volume-fixed",
    ])
    monkeypatch.setattr(rehearse_restore, "docker", lambda *a, **k: subprocess.CompletedProcess(a, 0, "[]", ""))
    with pytest.raises(BackupError, match="collides"):
        rehearse_restore.execute(args)


def test_rehearsal_uses_pull_never_and_disposable_volume():
    source = (SCRIPTS / "rehearse-restore.py").read_text()
    assert '"--pull", "never"' in source
    assert "ispindel-rehearsal-volume-" in source
    assert "--network\", \"none" in source
    assert "PRODUCTION_VOLUME" in source


def test_rehearsal_fingerprint_ignores_runtime_state(monkeypatch: pytest.MonkeyPatch):
    base = {
        "Id": "container-id",
        "Image": "sha256:image-id",
        "Name": "/ispindel-dashboard",
        "Config": {"Image": "candidate", "Env": ["SQLITE_PATH=/data/ispindel.db"]},
        "HostConfig": {"Binds": ["ispindel-dashboard_ispindel-data:/data"]},
        "Mounts": [{"Name": "ispindel-dashboard_ispindel-data", "Destination": "/data"}],
        "State": {"Status": "running", "Health": {"Status": "healthy", "Log": [{"ExitCode": 0}]}},
        "NetworkSettings": {"Ports": {"8098/tcp": [{"HostPort": "8098"}]}},
    }
    changed = json.loads(json.dumps(base))
    changed["State"]["Health"]["Log"].append({"ExitCode": 0})
    config_changed = json.loads(json.dumps(base))
    config_changed["Config"]["Image"] = "different-image"
    responses = iter((base, changed, config_changed))

    def fake_docker(*args: str, **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args, 0, json.dumps([next(responses)]), "")

    monkeypatch.setattr(rehearse_restore, "docker", fake_docker)
    first = rehearse_restore.inspect_fingerprint("container", "ispindel-dashboard")
    second = rehearse_restore.inspect_fingerprint("container", "ispindel-dashboard")
    assert first == second
    third = rehearse_restore.inspect_fingerprint("container", "ispindel-dashboard")
    assert first != third


def test_rehearsal_cleanup_verified(tmp_path: Path, app_module):
    image = os.getenv("ISPINDEL_REHEARSAL_IMAGE")
    if not image:
        pytest.skip("set ISPINDEL_REHEARSAL_IMAGE to run Docker restore smoke")
    source = Path(app_module.DB_PATH)
    local, offhost = tmp_path / "local", tmp_path / "offhost"
    run_id = "20260803T010000Z-00000031"
    backup = subprocess.run(
        [sys.executable, str(SCRIPTS / "backup-production.py"), "--execute",
         "--source-db", str(source), "--run-id", run_id, "--backup-root", str(local),
         "--offhost", str(offhost), "--min-free-bytes", "0", "--min-free-inodes", "0"],
        text=True, capture_output=True, check=False,
    )
    assert backup.returncode == 0, backup.stderr
    drill = subprocess.run(
        [sys.executable, str(SCRIPTS / "rehearse-restore.py"),
         "--manifest", str(local / run_id / "manifest.json"), "--image", image,
         "--evidence-dir", str(tmp_path / "evidence"), "--nonce", "pytest-t7"],
        text=True, capture_output=True, check=False,
    )
    assert drill.returncode == 0, drill.stderr
    payload = json.loads(drill.stdout)
    assert payload["result"] == "PASS"
    assert payload["production_unchanged"] is True
    assert all(payload["cleanup"].values())


def test_restore_drill_uses_indexed_exact_manifest_not_glob_or_mtime(tmp_path: Path):
    directory = generation(tmp_path / "backup", dt.datetime(2026, 8, 3, tzinfo=dt.timezone.utc), 40)
    index = tmp_path / "backup" / "index.json"
    atomic_json(index, {"latest_verified_manifest": str((directory / "manifest.json").resolve())})
    assert resolve_manifest_index(index) == (directory / "manifest.json").resolve()
    text = (SCRIPTS / "rehearse-restore.py").read_text()
    assert "glob(" not in text and "getmtime" not in text


def test_backup_and_restore_drill_units_verify():
    backup_timer = (ROOT / "ops/systemd/ispindel-backup.timer").read_text()
    drill_timer = (ROOT / "ops/systemd/ispindel-restore-drill.timer").read_text()
    backup_service = (ROOT / "ops/systemd/ispindel-backup.service").read_text()
    drill_service = (ROOT / "ops/systemd/ispindel-restore-drill.service").read_text()
    assert "OnUnitActiveSec=6h" in backup_timer and "Persistent=true" in backup_timer
    assert "OnCalendar=monthly" in drill_timer and "Persistent=false" in drill_timer
    # The backup service invokes dual mode explicitly (W6 contract).
    assert "--production --include-brew --execute" in backup_service
    assert "--production --execute" not in backup_service or "--include-brew" in backup_service
    assert "--paired" in drill_service
    assert "--manifest-index /var/backups/ispindel-dashboard/index.json" in drill_service
    assert "Environment=DOCKER_HOST=unix:///var/run/docker.sock" in backup_service
    assert "Environment=DOCKER_HOST=unix:///var/run/docker.sock" in drill_service
    assert "restore-production.py" not in backup_service + drill_service + backup_timer + drill_timer


def test_production_restore_requires_exact_confirmation(tmp_path: Path):
    script = SCRIPTS / "restore-production.py"
    proc = subprocess.run([sys.executable, str(script), "--help"], text=True, capture_output=True, check=False)
    assert proc.returncode == 0
    assert "--confirm-production-restore" in proc.stdout
    assert "--pre-restore-manifest" in proc.stdout
    assert "--quiescence-proof" in proc.stdout
    source = script.read_text()
    assert "args.confirm_production_restore != run_id" in source
    assert "--pull\", \"never" in source
    assert "quiescence proof is stale or future-dated" in source
    fixture_manifest = {
        "source": {"mode": "fixture-online-backup", "container": "ispindel-dashboard"}
    }
    with pytest.raises(BackupError, match="not a production-container online backup"):
        restore_production.require_production_manifest(
            fixture_manifest, "ispindel-dashboard", "target"
        )


# ----------------------------------------------------------------------
# W6 dual-mode backup/restore contract tests
# ----------------------------------------------------------------------


def fixture_dual_db(path: Path, *, prefix: str, label: str) -> Path:
    """Build a small regular SQLite fixture whose basename respects a prefix."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    conn = sqlite3.connect(path)
    try:
        conn.executescript(
            f"""
            CREATE TABLE {prefix}_items (id INTEGER PRIMARY KEY, label TEXT NOT NULL);
            INSERT INTO {prefix}_items(label) VALUES ('{label}');
            CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY);
            INSERT INTO schema_migrations(version) VALUES (1);
            PRAGMA user_version=1;
            """
        )
        conn.commit()
    finally:
        conn.close()
    return path


def dual_generation(root: Path, run_id: str, *, ispindel_label: str = "a", brew_label: str = "b") -> Path:
    """Build a fully verified paired generation directory (dual mode).

    Layout:
        <run_id>/ispindel-<run_id>.db
        <run_id>/brew-<run_id>.db
        <run_id>/manifest.json
        <run_id>/brew-manifest.json
    """
    directory = root / run_id
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    ispindel_db = fixture_dual_db(
        directory / bind_basename(run_id), prefix="isp", label=ispindel_label,
    )
    brew_db = fixture_dual_db(
        directory / bind_brew_basename(run_id), prefix="brew", label=brew_label,
    )
    manifest = build_dual_manifest(
        run_id,
        ispindel_db,
        brew_db,
        "2026-09-14T00:00:00Z",
        {
            "mode": "container-online-dual-backup",
            "container": "ispindel-dashboard",
            "databases": {"ispindel": "/data/ispindel.db", "brew": "/data/brew.db"},
        },
    )
    atomic_json(directory / "manifest.json", manifest["primary"])
    atomic_json(directory / "brew-manifest.json", manifest["brew"])
    return directory


def test_bind_brew_basename_matches_run_id():
    """The brew prefix must bind the same run identifier as the primary."""
    run_id = "20260914T000000Z-deadbeef"
    assert bind_brew_basename(run_id) == f"brew-{run_id}.db"


def test_dual_generation_paired_layout_validates(tmp_path: Path):
    """A paired generation must validate end-to-end with both manifests."""
    run_id = "20260914T000000Z-deadbeef"
    directory = dual_generation(tmp_path, run_id)
    payload = validate_dual_generation(directory)
    assert payload["primary"]["run_id"] == run_id
    assert payload["brew"]["run_id"] == run_id
    assert payload["primary"]["basename"] == bind_basename(run_id)
    assert payload["brew"]["basename"] == bind_brew_basename(run_id)
    assert payload["shared_run_id"] == run_id


def test_dual_validation_rejects_mismatched_run_id(tmp_path: Path):
    """The primary and brew manifest must share the canonical run identifier."""
    run_id = "20260914T000000Z-cafef00d"
    directory = dual_generation(tmp_path, run_id)
    brew_manifest = directory / "brew-manifest.json"
    payload = json.loads(brew_manifest.read_text())
    payload["run_id"] = "20260914T000000Z-99999999"
    atomic_json(brew_manifest, payload)
    with pytest.raises(BackupError, match="run_id|mismatch|binding"):
        validate_dual_generation(directory)


def test_dual_validation_rejects_missing_sibling(tmp_path: Path):
    """A partial directory missing either sibling must fail closed."""
    run_id = "20260914T000000Z-12345678"
    directory = dual_generation(tmp_path, run_id)
    (directory / bind_basename(run_id)).unlink()
    with pytest.raises(BackupError, match="missing|sibling|partial"):
        validate_dual_generation(directory)


def test_dual_validation_rejects_sidecars_or_symlinks(tmp_path: Path):
    """A dual generation must not contain -wal/-shm/-journal or symlinks."""
    run_id = "20260914T000000Z-abcdef01"
    directory = dual_generation(tmp_path, run_id)
    (directory / f"{bind_basename(run_id)}-wal").write_bytes(b"")
    with pytest.raises(BackupError, match="sidecar|wal|shm|journal"):
        validate_dual_generation(directory)


def test_dual_validation_rejects_only_one_database_verified(tmp_path: Path):
    """A promoted generation where only one database was verified must be rejected."""
    run_id = "20260914T000000Z-0fedcba9"
    directory = dual_generation(tmp_path, run_id)
    brew_db = directory / bind_brew_basename(run_id)
    with brew_db.open("ab") as handle:
        handle.write(b"tamper")
    with pytest.raises(BackupError, match="integrity|drift|mismatch"):
        validate_dual_generation(directory)


def test_old_single_manifest_remains_valid(tmp_path: Path):
    """Legacy single-mode generations must still validate via validate_generation."""
    run_id = "20260914T000000Z-11223344"
    directory = dual_generation(tmp_path, run_id)
    payload = validate_generation(directory / "manifest.json")
    assert payload["run_id"] == run_id
    assert payload["basename"] == bind_basename(run_id)


def test_offhost_index_retains_legacy_field_and_adds_databases_map(tmp_path: Path):
    """Off-host index must keep legacy latest_verified_manifest AND add databases map."""
    from backup_common import update_index_for_dual_generation
    run_id = "20260914T000000Z-55667788"
    directory = dual_generation(tmp_path, run_id)
    index_path = tmp_path / "index.json"
    atomic_json(
        index_path,
        {
            "schema": "ispindel-backup-index/v1",
            "latest_verified_manifest": str((directory / "manifest.json").resolve()),
            "updated_at": "2026-09-14T00:00:00Z",
        },
    )
    update_index_for_dual_generation(
        index_path,
        run_id,
        primary_manifest_path=directory / "manifest.json",
        brew_manifest_path=directory / "brew-manifest.json",
        offhost_pair_path=str(directory),
    )
    payload = json.loads(index_path.read_text())
    assert payload["latest_verified_manifest"] == str((directory / "manifest.json").resolve())
    assert payload["latest_verified_run_id"] == run_id
    databases = payload["databases"]
    assert databases["ispindel"]["manifest_path"] == str((directory / "manifest.json").resolve())
    assert databases["ispindel"]["run_id"] == run_id
    assert databases["brew"]["manifest_path"] == str((directory / "brew-manifest.json").resolve())
    assert databases["brew"]["run_id"] == run_id
    assert databases["shared_run_id"] == run_id


def test_backup_health_only_written_after_offhost_verification_and_promotion(tmp_path: Path):
    """backup_health.json must NOT exist when off-host verification fails."""
    from backup_common import write_backup_health_if_verified, BackupHealthNotWritten
    class _State:
        verified = False
        promoted = False
    state = _State()

    def verifier() -> bool:
        return state.verified and state.promoted

    health_path = tmp_path / "backup_health.json"
    with pytest.raises(BackupHealthNotWritten):
        write_backup_health_if_verified(health_path, lambda: verifier(), payload={"ok": True})
    assert not health_path.exists()

    state.verified = True
    state.promoted = True
    write_backup_health_if_verified(health_path, lambda: verifier(), payload={"ok": True})
    payload = json.loads(health_path.read_text())
    assert payload["ok"] is True


def test_backup_production_source_writes_backup_health_only_after_offhost_verification():
    """backup-production.py must write backup_health.json only after off-host promotion."""
    src = (SCRIPTS / "backup-production.py").read_text()
    # Identify the off-host copy/verify/promote block and the backup_health
    # write call. The write call invokes ``write_backup_health_if_verified``;
    # that function name appears ONLY in the actual write call (it is not
    # imported in this module's imports), so its first source-position is
    # the write site itself.
    copy_idx = src.find("offhost_manifest")
    health_idx = src.find("write_backup_health_if_verified(")
    assert copy_idx != -1, "offhost_manifest block missing"
    assert health_idx != -1, "backup_health write call missing"
    assert health_idx > copy_idx, "backup_health must be written AFTER off-host verification"


def test_backup_service_invokes_dual_mode():
    """The scheduled backup service must invoke the dual mode explicitly."""
    src = (ROOT / "ops/systemd/ispindel-backup.service").read_text()
    assert "--include-brew" in src, "backup service must include --include-brew to invoke dual mode"


def test_restore_production_source_paired_layout_and_pull_never():
    """restore-production.py must support a paired restore contract and keep old single path."""
    src = (SCRIPTS / "restore-production.py").read_text()
    assert ("--target-manifest" in src) or ("paired" in src), "paired restore flag missing"
    assert "ispindel-" in src and "brew-" in src, "restore must reference both prefixes"
    assert '"--pull", "never"' in src, "restore must use --pull never"
    assert "confirm_production_restore" in src, "exact run-id confirmation required"
    assert "quiescence" in src, "quiescence proof required"
    assert ("atomic" in src.lower()) or ("os.replace" in src), "atomic per-database replacement required"
    assert ("sidecar" in src.lower()) or ("-wal" in src), "sidecar cleanup required"
    for forbidden in ("except Exception:", "except BaseException:"):
        assert forbidden not in src, f"restore must not swallow {forbidden}"


def test_rehearse_restore_source_paired_layout_and_probe_endpoints():
    """rehearse-restore.py must restore paired generations and probe the new endpoints."""
    src = (SCRIPTS / "rehearse-restore.py").read_text()
    assert "--paired" in src or "paired" in src, "paired rehearsal flag missing"
    assert "/api/devices" in src, "must probe /api/devices"
    assert "/api/recipes" in src, "must probe /api/recipes"
    assert "research_documents" in src, "must probe FTS-driven search route"
    assert '"--pull", "never"' in src, "must use --pull never"
    assert "rehearsal-volume-" in src, "must use nonce-only isolated volume"
    assert "rehearsal-network-" in src, "must use nonce-only isolated network"
    assert '"--network", "none"' in src, "helper container must be network=none"
    assert "BREW_TABLES" in src, "must cross-check the canonical brew-table allowlist"


def test_rehearsal_cross_checks_every_manifest_brew_table(monkeypatch):
    """A mismatch outside the old four-table subset must fail the rehearsal."""
    expected = {
        "archive_evidence_bundles": 1,
        "recipe_scheduled_additions": 2,
    }
    observed = {
        "archive_evidence_bundles": 1,
        "recipe_scheduled_additions": 1,
    }

    def fake_docker(*_args, **_kwargs):
        return subprocess.CompletedProcess(
            args=["docker"],
            returncode=0,
            stdout="BREW_COUNTS_JSON=" + json.dumps(observed) + "\n",
            stderr="",
        )

    monkeypatch.setattr(rehearse_restore, "docker", fake_docker)
    with pytest.raises(BackupError, match="recipe_scheduled_additions"):
        rehearse_restore._cross_check_brew_counts("candidate", "volume", expected, "nonce")


def test_no_termux_paths_introduced_by_w6_slice():
    """This W6 slice must not introduce Termux systemd/cron/job-scheduler paths."""
    paths_to_check = [
        SCRIPTS / "backup_common.py",
        SCRIPTS / "backup-production.py",
        SCRIPTS / "restore-production.py",
        SCRIPTS / "rehearse-restore.py",
        ROOT / "ops/systemd/ispindel-backup.service",
        ROOT / "ops/systemd/ispindel-restore-drill.service",
    ]
    forbidden_terms = ("termux-job-scheduler", "termux.cron", "termux-cron", "termux-boot-start")
    for path in paths_to_check:
        text = path.read_text().lower()
        for forbidden in forbidden_terms:
            assert forbidden not in text, f"{path.name} introduced forbidden term {forbidden!r}"
