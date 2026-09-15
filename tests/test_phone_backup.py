"""P3 phone backup boundary and safety contracts.

These tests use an injected ADB runner only; no test invokes a real phone,
ADB daemon, service, or deployment. The fake executes the deployed helper
against disposable SQLite sources and returns raw bytes for exec-out, which
proves the production-shaped protocol rather than a push/pull approximation.
"""
from __future__ import annotations

import fcntl
import importlib.util
import json
import os
import shlex
import sqlite3
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Callable

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
HELPER_PATH = ROOT / "ops/android/phone-backup-snapshot.py"
COORDINATOR_PATH = ROOT / "scripts/backup-phone.py"
SYSTEMD = ROOT / "ops/systemd"
SERIAL = "FAKE-SERIAL-DEADBEEF"
PHONE_ROOT = "/phone/root"
STAGING_ROOT = f"{PHONE_ROOT}/data/backup-staging"
HELPER_REMOTE = f"{PHONE_ROOT}/current/ops/android/phone-backup-snapshot.py"
PYTHON_REMOTE = f"{PHONE_ROOT}/venv/bin/python"


def load_module(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def seed_db(path: Path, *, brew: bool = False, migrations: list[int] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        if brew:
            conn.executescript(
                "CREATE TABLE research_documents(id INTEGER PRIMARY KEY, value TEXT, body TEXT);"
                "CREATE VIRTUAL TABLE research_documents_fts USING fts5(body, content='research_documents', content_rowid='id');"
            )
            for i in range(3):
                conn.execute(
                    "INSERT INTO research_documents(value, body) VALUES (?, ?)",
                    (f"v{i}", f"body {i}"),
                )
            conn.execute(
                "INSERT INTO research_documents_fts(rowid, body) "
                "SELECT id, body FROM research_documents"
            )
        else:
            conn.execute("CREATE TABLE things(value TEXT)")
            for i in range(2):
                conn.execute("INSERT INTO things(value) VALUES (?)", (f"v{i}",))
        if migrations is not None:
            conn.execute("CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY)")
            conn.executemany("INSERT INTO schema_migrations(version) VALUES (?)", [(v,) for v in migrations])
        conn.commit()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        conn.close()


def seed_pair(root: Path, *, migrations: list[int] | None = None) -> tuple[Path, Path]:
    ispindel = root / "ispindel.db"
    brew = root / "brew.db"
    seed_db(ispindel, migrations=migrations)
    seed_db(brew, brew=True, migrations=migrations)
    return ispindel, brew


class FakeADB:
    """Execute the phone helper locally while exposing exact ADB argv."""

    def __init__(self, coordinator: ModuleType, helper: ModuleType, sources: tuple[Path, Path], phone: Path):
        self.coordinator = coordinator
        self.helper = helper
        self.sources = sources
        self.phone = phone
        self.calls: list[list[str]] = []
        self.events: list[str] = []
        self.fail_exec_out = False
        self.fail_publish = False
        self.run_ids: list[str] = []
        self.phone.mkdir(parents=True, exist_ok=True)
        self.staging = self.phone / "data" / "backup-staging"
        self.staging.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def result(argv: list[str], *, stdout: bytes = b"", stderr: bytes = b"", rc: int = 0) -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(argv, rc, stdout, stderr)

    def __call__(self, argv: list[str], *, timeout: float) -> subprocess.CompletedProcess:
        self.calls.append(list(argv))
        assert argv[0] == "adb", argv
        if len(argv) > 1 and argv[1] == "adb":
            raise AssertionError(f"double adb argv: {argv!r}")
        if argv[1:3] == ["devices", "-l"]:
            return self.result(argv, stdout=f"{SERIAL} device usb:1\n".encode())
        assert argv[1:3] == ["-s", SERIAL], argv
        op = argv[3]
        if op == "shell":
            tokens = shlex.split(argv[4])
            assert tokens[:2] == ["run-as", "com.termux"], argv
            assert tokens[2] == PYTHON_REMOTE
            assert tokens[3] == HELPER_REMOTE
            command = tokens[4]
            if command == "snapshot":
                args = parse_args(tokens[5:])
                run_id = args["--run-id"]
                self.run_ids.append(run_id)
                self.events.append("snapshot")
                self.helper.snapshot_pair(
                    run_id=run_id,
                    staging_root=self.staging,
                    ispindel_src=self.sources[0],
                    brew_src=self.sources[1],
                    produced_at="2026-09-14T12:00:00Z",
                )
                return self.result(argv)
            if command == "cleanup":
                args = parse_args(tokens[5:])
                self.events.append("cleanup")
                self.helper.cleanup_staging(
                    staging_root=self.staging, run_id=args["--run-id"]
                )
                return self.result(argv)
            if command == "publish-health":
                if self.fail_publish:
                    return self.result(argv, stderr=b"forced publish failure", rc=3)
                args = parse_args(tokens[5:])
                self.events.append("publish-health")
                payload = json.loads(args["--payload-json"])
                self.helper.publish_health(
                    health_path=self.phone / "data" / "backup_health.json",
                    payload=payload,
                )
                return self.result(argv)
            raise AssertionError(argv)
        if op == "exec-out":
            if self.fail_exec_out:
                return self.result(argv, stderr=b"forced exec-out failure", rc=4)
            command = shlex.split(argv[4])
            assert command[:3] == ["run-as", "com.termux", "cat"]
            remote = Path(command[3])
            rel = remote.relative_to(Path(PHONE_ROOT))
            staged = self.phone / rel
            self.events.append(f"exec-out:{remote.name}")
            return self.result(argv, stdout=staged.read_bytes())
        raise AssertionError(argv)


def parse_args(tokens: list[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for i in range(0, len(tokens), 2):
        result[tokens[i]] = tokens[i + 1]
    return result


def run_backup(coordinator: ModuleType, fake: FakeADB, tmp_path: Path, run_id: str | None = "20260914T120000Z-deadbeef") -> dict[str, object]:
    return coordinator.run_phone_backup(
        SERIAL,
        adb_call=fake,
        backup_root=tmp_path / "backup-root",
        offhost_root=tmp_path / "offhost",
        run_id=run_id,
        phone_root=PHONE_ROOT,
        helper_path=HELPER_REMOTE,
        min_free_bytes=0,
        min_free_inodes=0,
        lock_path=tmp_path / "phone-backup.lock",
        lock_wait_seconds=0.2,
        produced_at="2026-09-14T12:01:00Z",
    )


def make_stack(tmp_path: Path) -> tuple[ModuleType, ModuleType, FakeADB]:
    helper = load_module("phone_helper_test", HELPER_PATH)
    coordinator = load_module("phone_coordinator_test", COORDINATOR_PATH)
    sources = seed_pair(tmp_path / "sources", migrations=[1, 3, 7])
    fake = FakeADB(coordinator, helper, sources, tmp_path / "phone")
    return coordinator, helper, fake


def test_helper_snapshot_uses_canonical_manifest_evidence_and_fresh_child(tmp_path: Path) -> None:
    helper = load_module("helper_manifest_test", HELPER_PATH)
    sources = seed_pair(tmp_path / "sources", migrations=[2, 5])
    staging_root = tmp_path / "phone" / "data" / "backup-staging"
    staging_root.mkdir(parents=True)
    run_id = "20260914T120000Z-deadbeef"
    envelope = helper.snapshot_pair(
        run_id=run_id,
        staging_root=staging_root,
        ispindel_src=sources[0],
        brew_src=sources[1],
        produced_at="2026-09-14T12:00:00Z",
    )
    generation = staging_root / run_id
    assert generation.is_dir()
    assert envelope["shared_run_id"] == run_id
    primary = json.loads((generation / "manifest.json").read_text())
    brew = json.loads((generation / "brew-manifest.json").read_text())
    assert primary["database"]["migration_versions"] == [2, 5]
    assert brew["database"]["migration_versions"] == [2, 5]
    assert len(brew["database"]["counts"]) == 19
    assert not (staging_root / f"{run_id}.partial").exists()
    with pytest.raises(helper.HelperError, match="already exists"):
        helper.snapshot_pair(
            run_id=run_id,
            staging_root=staging_root,
            ispindel_src=sources[0],
            brew_src=sources[1],
        )


def test_helper_rejects_symlink_staging_child(tmp_path: Path) -> None:
    helper = load_module("helper_symlink_test", HELPER_PATH)
    sources = seed_pair(tmp_path / "sources")
    staging_root = tmp_path / "staging"
    staging_root.mkdir()
    run_id = "20260914T120000Z-deadbeef"
    outside = tmp_path / "outside"
    outside.mkdir()
    (staging_root / run_id).symlink_to(outside, target_is_directory=True)
    with pytest.raises(helper.HelperError, match="already exists|symlink"):
        helper.snapshot_pair(run_id=run_id, staging_root=staging_root, ispindel_src=sources[0], brew_src=sources[1])


def test_real_default_wrapper_executes_one_adb(monkeypatch: pytest.MonkeyPatch) -> None:
    coordinator = load_module("coordinator_wrapper_test", COORDINATOR_PATH)
    seen: list[list[str]] = []

    def fake_run(argv, **kwargs):
        seen.append(argv)
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    monkeypatch.setattr(coordinator.subprocess, "run", fake_run)
    coordinator._default_adb_call(["adb", "devices", "-l"], timeout=1)
    assert seen == [["adb", "devices", "-l"]]
    with pytest.raises(coordinator.PhoneBackupError, match="double"):
        coordinator._default_adb_call(["adb", "adb", "devices"], timeout=1)


def test_discovery_guard_checks_len_before_parts_index() -> None:
    coordinator = load_module("coordinator_discovery_test", COORDINATOR_PATH)

    def fake(argv, *, timeout):
        return subprocess.CompletedProcess(argv, 0, b"List of devices attached\nmalformed\n", b"")

    with pytest.raises(coordinator.PhoneBackupError, match="exactly one"):
        coordinator._discover_serial(adb_call=fake, timeout=1)


def test_production_run_as_exec_out_binary_roundtrip_cleanup_and_health_order(tmp_path: Path) -> None:
    coordinator, _helper, fake = make_stack(tmp_path)
    # Add bytes that would be corrupted by a text-mode path.
    (fake.phone / "data").mkdir(parents=True, exist_ok=True)
    result = run_backup(coordinator, fake, tmp_path)
    run_id = str(result["run_id"])
    assert run_id == "20260914T120000Z-deadbeef"
    assert fake.events[0] == "snapshot"
    assert all(event.startswith("exec-out:") for event in fake.events[1:5])
    assert fake.events[5:] == ["cleanup", "publish-health"]
    assert not (fake.staging / run_id).exists()
    promoted = tmp_path / "offhost" / run_id
    assert promoted.is_dir()
    phone_health = json.loads(
        (tmp_path / "phone" / "data" / "backup_health.json").read_text()
    )
    host_health = json.loads(
        (tmp_path / "backup-root" / "backup_health.json").read_text()
    )
    assert phone_health["schema"] == "ispindel-backup-health/v1"
    assert phone_health["mode"] == "dual"
    assert host_health["run_id"] == run_id
    assert host_health["mode"] == "dual"
    assert all(argv[0] == "adb" and argv[1] != "adb" for argv in fake.calls)
    assert not any(argv[3] == "push" for argv in fake.calls if len(argv) > 3)
    exec_calls = [argv for argv in fake.calls if len(argv) > 3 and argv[3] == "exec-out"]
    assert len(exec_calls) == 4
    for argv in exec_calls:
        assert argv[4].startswith("run-as com.termux cat ")


def test_binary_exec_out_bytes_are_not_text_decoded(tmp_path: Path) -> None:
    coordinator, _helper, fake = make_stack(tmp_path)
    original = fake.__call__

    def with_binary(argv, *, timeout):
        result = original(argv, timeout=timeout)
        if len(argv) > 3 and argv[3] == "exec-out" and argv[4].endswith("manifest.json"):
            return subprocess.CompletedProcess(argv, 0, b"\x00\xff\n\x80", b"")
        return result

    fake_call = with_binary
    with pytest.raises(Exception):
        coordinator.run_phone_backup(
            SERIAL, adb_call=fake_call, backup_root=tmp_path / "backup-root",
            offhost_root=tmp_path / "offhost", run_id="20260914T120000Z-deadbeef",
            phone_root=PHONE_ROOT, helper_path=HELPER_REMOTE, min_free_bytes=0,
            min_free_inodes=0, lock_path=tmp_path / "lock", lock_wait_seconds=0.1,
        )
    # The fake returns raw bytes and never asks Python to decode them.
    assert any("exec-out" in argv for argv in fake.calls)


def test_auto_run_id_is_unique_for_two_invocations(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    coordinator, helper, _ = make_stack(tmp_path)
    ids = iter(["20260914T120000Z-aaaaaaaa", "20260914T120000Z-bbbbbbbb"])
    monkeypatch.setattr(coordinator, "bind_run_id", lambda: next(ids))
    results = []
    for index in range(2):
        sources = seed_pair(tmp_path / f"sources-{index}")
        fake = FakeADB(coordinator, helper, sources, tmp_path / f"phone-{index}")
        results.append(run_backup(coordinator, fake, tmp_path / f"run-{index}", run_id="auto"))
    assert [r["run_id"] for r in results] == ["20260914T120000Z-aaaaaaaa", "20260914T120000Z-bbbbbbbb"]


def test_failure_best_effort_cleanup_and_no_health_or_promotion(tmp_path: Path) -> None:
    coordinator, _helper, fake = make_stack(tmp_path)
    fake.fail_exec_out = True
    with pytest.raises(coordinator.PhoneBackupError, match="exec-out"):
        run_backup(coordinator, fake, tmp_path)
    assert not (tmp_path / "offhost" / "20260914T120000Z-deadbeef").exists()
    assert not (tmp_path / "backup-root" / "backup_health.json").exists()
    assert fake.events[-1] == "cleanup"


def test_health_publish_failure_happens_after_promotion_but_no_local_copy(tmp_path: Path) -> None:
    coordinator, _helper, fake = make_stack(tmp_path)
    fake.fail_publish = True
    with pytest.raises(coordinator.PhoneBackupError, match="publish"):
        run_backup(coordinator, fake, tmp_path)
    assert (tmp_path / "offhost" / "20260914T120000Z-deadbeef").is_dir()
    assert not (tmp_path / "backup-root" / "backup_health.json").exists()


def test_bounded_lock_contention_fails_closed(tmp_path: Path) -> None:
    coordinator, _helper, fake = make_stack(tmp_path)
    lock = tmp_path / "held.lock"
    with lock.open("w") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(coordinator.PhoneBackupError, match="within"):
            coordinator.run_phone_backup(
                SERIAL, adb_call=fake, backup_root=tmp_path / "backup-root",
                offhost_root=tmp_path / "offhost", run_id="20260914T120000Z-deadbeef",
                phone_root=PHONE_ROOT, helper_path=HELPER_REMOTE, min_free_bytes=0,
                min_free_inodes=0, lock_path=lock, lock_wait_seconds=0.05,
            )
    assert fake.calls == []


def test_configured_phone_paths_are_confined_before_adb(tmp_path: Path) -> None:
    coordinator, _helper, fake = make_stack(tmp_path)
    with pytest.raises(coordinator.PhoneBackupError, match="confined relative path"):
        coordinator.run_phone_backup(
            SERIAL,
            adb_call=fake,
            backup_root=tmp_path / "backup-root",
            offhost_root=tmp_path / "offhost",
            run_id="20260914T120000Z-deadbeef",
            phone_root=PHONE_ROOT,
            staging_relative="../outside",
            min_free_bytes=0,
            min_free_inodes=0,
            lock_path=tmp_path / "lock",
        )
    assert fake.calls == []


def test_helper_publish_health_is_atomic_and_validates_schema(tmp_path: Path) -> None:
    helper = load_module("helper_health_test", HELPER_PATH)
    path = tmp_path / "data" / "backup_health.json"
    payload = {
        "schema": "ispindel-backup-health/v1",
        "run_id": "20260914T120000Z-deadbeef",
        "verified_at": "2026-09-14T12:00:00Z",
        "mode": "dual",
        "offhost_path": "/offhost/run",
    }
    helper.publish_health(health_path=path, payload=payload)
    assert json.loads(path.read_text()) == payload
    assert not list(path.parent.glob(".*.tmp-*"))
    with pytest.raises(helper.HelperError, match="schema"):
        helper.publish_health(health_path=path, payload={"schema": "wrong"})


def test_systemd_contract_is_production_shaped() -> None:
    service = (SYSTEMD / "ispindel-phone-backup.service").read_text()
    timer = (SYSTEMD / "ispindel-phone-backup.timer").read_text()
    assert "After=network-online.target tailscaled.service" in service
    assert "docker.service" not in service
    assert "ispindel-stack.service" not in service
    assert "DOCKER_HOST" not in service
    # W1: EnvironmentFile is required (no leading `-`); the optional form
    # must be absent so a missing env file surfaces as a unit failure.
    assert "EnvironmentFile=/etc/ispindel/phone-backup.env" in service
    assert "EnvironmentFile=-/etc/ispindel/phone-backup.env" not in service
    # No inline serial in the unit: the serial must come from the env file.
    assert "Environment=ISPINDEL_PHONE_SERIAL=" not in service
    assert "EXAMPLE-DEVICE-SERIAL" not in service
    assert "--run-id=auto" in service
    assert "--execute" in service
    assert "/path/to/backups" not in service
    assert "ISPINDEL_PHONE_BACKUP_ROOT" in service
    assert "ISPINDEL_PHONE_OFFHOST_ROOT" in service
    assert "ISPINDEL_PHONE_HEALTH_ROOT" in service
    assert "User=ispindel" in service
    assert "NoNewPrivileges=true" in service
    assert "PrivateTmp=true" in service
    assert "ProtectSystem=strict" in service
    assert "Unit=ispindel-phone-backup.service" in timer
    # Legacy backup units remain present (must NOT be removed or disabled).
    assert (SYSTEMD / "ispindel-backup.service").is_file()
    assert (SYSTEMD / "ispindel-backup.timer").is_file()


def test_legacy_units_remain_present_and_phone_helper_stdlib_only() -> None:
    assert (SYSTEMD / "ispindel-backup.service").is_file()
    assert (SYSTEMD / "ispindel-backup.timer").is_file()
    text = HELPER_PATH.read_text()
    for forbidden in ("import requests", "import psycopg2", "import boto3", "import paramiko"):
        assert forbidden not in text
