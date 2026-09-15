"""Focused tests for Slice-2C exact target-volume seeding and byte verification."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import subprocess
import sys
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[1]
DEPLOY_PATH = ROOT / "scripts/deploy/deploy.py"
HOST = "user@example"
REMOTE_ROOT = "/opt/ispindel-dashboard"
SOURCE = "unix:///var/run/docker.sock"
TARGET = "unix:///var/run/docker.sock"
WRONG = "unix:///run/user/1000/docker.sock"
TARGET_VOLUME = "ispindel-dashboard_ispindel-data"
IMAGE = "sha256:" + "2" * 64
RUN_ID = "20260804T000000Z-deadbeef"
BACKUP_ROOT = "/var/backups/ispindel-dashboard"
BASENAME = f"ispindel-{RUN_ID}.db"
MANIFEST_HASH = "3" * 64
FILE_HASH = "4" * 64
DATABASE = {"user_version": 3, "migration_versions": [1, 2, 3],
            "counts": {"devices": 1, "samples": 2,
                       "calibrations": 3, "calibration_active": 1}}


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("ispindel_deploy_seed", DEPLOY_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault(spec.name, module)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def deploy() -> ModuleType:
    return _load()


def receipt(**changes: object) -> dict[str, object]:
    value: dict[str, object] = {"target_endpoint": TARGET, "predecessor_image_id": IMAGE}
    value.update(changes)
    return value


def backup(**changes: object) -> dict[str, object]:
    evidence: dict[str, object] = {
        "source_endpoint": SOURCE, "backup_root": BACKUP_ROOT, "run_id": RUN_ID,
        "evidence": {
            "manifest": f"{BACKUP_ROOT}/{RUN_ID}/manifest.json",
            "manifest_sha256": MANIFEST_HASH,
            "file": {"basename": BASENAME, "size": 42, "sha256": FILE_HASH},
            "database": DATABASE, "run_id": RUN_ID,
        },
    }
    evidence.update(changes)
    return evidence


def verify_line(**changes: object) -> bytes:
    payload: dict[str, object] = {
        "target_endpoint": TARGET, "target_volume": TARGET_VOLUME,
        "source_manifest_sha256": MANIFEST_HASH, "source_run_id": RUN_ID,
        "journal_mode": "wal",
        "file": {"basename": "ispindel.db", "size": 42, "sha256": FILE_HASH},
        "database": DATABASE,
    }
    payload.update(changes)
    return json.dumps(payload, sort_keys=True).encode() + b"\n"


class Runner:
    def __init__(self, *, dry_run: bool = False, returncodes: list[int] | None = None,
                 verify_stdout: bytes | None = None) -> None:
        self.dry_run = dry_run
        self.returncodes = list(returncodes or [0, 0, 0])
        self.verify_stdout = verify_stdout if verify_stdout is not None else verify_line()
        self.calls: list[tuple[str, object]] = []

    def remote(self, _host: str, _root: str, argv: list[str], *,
               required: bool = True) -> subprocess.CompletedProcess[bytes]:
        self.calls.append(("remote", argv))
        rc = self.returncodes.pop(0) if self.returncodes else 0
        return subprocess.CompletedProcess(argv, rc, b"inspect\n", b"")

    def remote_script_endpoint(self, _host: str, _root: str, endpoint: str,
                               script: str, *, label: str = "endpoint",
                               required: bool = True) -> subprocess.CompletedProcess[bytes]:
        self.calls.append(("script", (endpoint, label, script)))
        return subprocess.CompletedProcess([], 0, self.verify_stdout, b"")


def call(deploy: ModuleType, runner: Runner, *, stage01: dict[str, object] | None = None,
         quiesced: dict[str, object] | None = None, seed_image_id: str = IMAGE,
         execute: bool = True) -> dict[str, object]:
    return deploy.slice2_target_volume_seed(
        runner, HOST, REMOTE_ROOT, stage01 or receipt(), quiesced or backup(),
        seed_image_id=seed_image_id, execute=execute,
    )


def test_exact_four_operations_and_closed_helper_order(deploy: ModuleType) -> None:
    runner = Runner()
    plan = call(deploy, runner)
    helper = "/usr/local/sbin/ispindel-root-helper"
    source = f"{BACKUP_ROOT}/{RUN_ID}/{BASENAME}"
    assert runner.calls[:3] == [
        ("remote", ["sudo", helper, "volume-create", TARGET_VOLUME]),
        ("remote", ["sudo", helper, "volume-seed", source, TARGET_VOLUME, IMAGE]),
        ("remote", ["sudo", helper, "volume-inspect", TARGET_VOLUME]),
    ]
    assert runner.calls[3][0] == "script"
    endpoint, label, script = runner.calls[3][1]
    assert (endpoint, label) == (TARGET, "target")
    assert "docker run --rm --network none --pull never --user 0:0" in script
    assert f"src={TARGET_VOLUME},dst=/target" in script
    assert f"src={TARGET_VOLUME},dst=/target,readonly" not in script
    assert f"--entrypoint python3 {IMAGE} -c" in script
    assert plan["evidence"]["source_manifest_sha256"] == MANIFEST_HASH


@pytest.mark.parametrize("stage01,quiesced", [
    (receipt(target_endpoint=WRONG), backup()),
    (receipt(), backup(source_endpoint=WRONG)),
    (receipt(), backup(backup_root="relative")),
    (receipt(), backup(evidence={"run_id": RUN_ID})),
])
def test_invalid_bindings_refuse_before_calls(deploy: ModuleType, stage01: dict[str, object],
                                               quiesced: dict[str, object]) -> None:
    runner = Runner()
    with pytest.raises(deploy.DeterministicGateFailure):
        call(deploy, runner, stage01=stage01, quiesced=quiesced)
    assert runner.calls == []


def test_invalid_release_seed_image_refuses_before_calls(deploy: ModuleType) -> None:
    runner = Runner()
    with pytest.raises(deploy.DeterministicGateFailure, match="release-bound"):
        call(deploy, runner, seed_image_id="sha256:bad")
    assert runner.calls == []


def test_seed_failure_prevents_inspect_and_verifier(deploy: ModuleType) -> None:
    runner = Runner(returncodes=[0, 1])
    with pytest.raises(deploy.DeterministicGateFailure, match="volume-seed"):
        call(deploy, runner)
    assert len(runner.calls) == 2


def test_verifier_is_independent_and_checks_all_database_evidence(deploy: ModuleType) -> None:
    runner = Runner()
    call(deploy, runner)
    script = runner.calls[3][1][2]
    assert "/target/ispindel.db" in script and "is_symlink" in script
    assert "PRAGMA integrity_check" in script and "PRAGMA foreign_key_check" in script
    assert "PRAGMA journal_mode" in script and "journal_mode must be wal" in script
    assert "PRAGMA user_version" in script and "schema_migrations" in script
    for table in DATABASE["counts"]:
        assert table in script
    assert json.dumps(DATABASE, sort_keys=True, separators=(",", ":")) in script


def test_mismatched_verifier_binding_fails_closed(deploy: ModuleType) -> None:
    runner = Runner(verify_stdout=verify_line(source_run_id="wrong"))
    with pytest.raises(deploy.DeterministicGateFailure, match="binding mismatch"):
        call(deploy, runner)
    assert len(runner.calls) == 4


def test_verifier_rejects_target_in_delete_mode(deploy: ModuleType) -> None:
    runner = Runner(verify_stdout=verify_line(journal_mode="delete"))
    with pytest.raises(deploy.DeterministicGateFailure, match="binding mismatch"):
        call(deploy, runner)


def test_verifier_accepts_target_hash_changed_by_wal_transition(deploy: ModuleType) -> None:
    runner = Runner(verify_stdout=verify_line(file={"basename": "ispindel.db", "size": 42,
                                                     "sha256": "5" * 64}))
    result = call(deploy, runner)
    evidence = result["evidence"]
    assert isinstance(evidence, dict)
    assert evidence["file"]["sha256"] == "5" * 64


def test_dry_run_records_exact_four_operations_without_evidence(deploy: ModuleType) -> None:
    runner = Runner(dry_run=True)
    plan = call(deploy, runner, execute=False)
    assert len(runner.calls) == 4
    assert [step["label"] for step in plan["steps"]] == [
        "volume_create", "volume_seed", "volume_inspect", "target_byte_verify",
    ]
    assert "evidence" not in plan
