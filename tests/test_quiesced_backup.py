"""Focused tests for the Slice-2B2 source-volume quiesced backup."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import re
import subprocess
import sys
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[1]
DEPLOY_PATH = ROOT / "scripts/deploy/deploy.py"
SOURCE = "unix:///var/run/docker.sock"
HOST = "user@example"
REMOTE_ROOT = "/opt/ispindel-dashboard"
REMOTE_RELEASE = "/path/to/releases/candidate"
BACKUP_ROOT = "/var/backups/ispindel-dashboard"
CID = "1" * 64
IMAGE = "sha256:" + "2" * 64
VOLUME = "ispindel-dashboard_ispindel-data"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("ispindel_deploy_quiesced", DEPLOY_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault(spec.name, module)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def deploy() -> ModuleType:
    return _load()


def receipt() -> dict[str, object]:
    return {"source_endpoint": SOURCE, "predecessor_container_id": CID,
            "predecessor_volume_name": VOLUME, "predecessor_image_id": IMAGE}


def stopped(**changes: object) -> dict[str, object]:
    value: dict[str, object] = {"source_endpoint": SOURCE,
                                "predecessor_container_id": CID,
                                "post_stop_running": "false"}
    value.update(changes)
    return value


def verifier_output(run_id: str = "20260804T000000Z-deadbeef") -> bytes:
    payload = {"manifest": f"{BACKUP_ROOT}/{run_id}/manifest.json",
               "manifest_sha256": "3" * 64,
               "file": {"basename": f"ispindel-{run_id}.db", "size": 42,
                        "sha256": "4" * 64},
               "database": {"user_version": 3, "migration_versions": [1, 2, 3],
                            "counts": {"devices": 1, "samples": 2,
                                       "calibrations": 3, "calibration_active": 1}},
               "run_id": run_id}
    return b"SLICE2B2_VERIFIER_RESULT=" + json.dumps(payload).encode() + b"\n"


class Runner:
    def __init__(self, *, dry_run: bool = False, backup_rc: int = 0,
                 verify_stdout: bytes | None = None) -> None:
        self.dry_run = dry_run
        self.backup_rc = backup_rc
        self.verify_stdout = verify_stdout
        self.calls: list[tuple[str, str, str]] = []

    def remote_script_endpoint(self, _host: str, _root: str, endpoint: str,
                               script: str, *, label: str = "endpoint",
                               required: bool = True) -> subprocess.CompletedProcess[bytes]:
        self.calls.append((endpoint, label, script))
        index = len(self.calls)
        if index == 1:
            return subprocess.CompletedProcess([], self.backup_rc, b"backup\n", b"")
        match = re.search(r"[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}", script)
        assert match is not None
        stdout = (self.verify_stdout if self.verify_stdout is not None
                  else verifier_output(match.group(0)))
        return subprocess.CompletedProcess([], 0, stdout, b"")


def call(deploy: ModuleType, runner: Runner, *, stop: dict[str, object] | None = None,
         execute: bool = True) -> dict[str, object]:
    return deploy.slice2_source_quiesced_backup(
        runner, HOST, REMOTE_ROOT, REMOTE_RELEASE, receipt(), stop or stopped(),
        backup_root=BACKUP_ROOT, execute=execute,
    )


def test_stop_evidence_mismatch_refuses_before_calls(deploy: ModuleType) -> None:
    runner = Runner()
    with pytest.raises(deploy.DeterministicGateFailure, match="stop evidence"):
        call(deploy, runner, stop=stopped(predecessor_container_id="9" * 64))
    assert runner.calls == []


def test_exact_backup_then_verify_order_and_source_endpoint(deploy: ModuleType) -> None:
    runner = Runner()
    plan = call(deploy, runner)
    assert len(runner.calls) == 2
    assert [item[0] for item in runner.calls] == [SOURCE, SOURCE]
    backup, verify = runner.calls[0][2], runner.calls[1][2]
    assert "docker run --rm" in backup
    assert "--network none --pull never --user 0:0" in backup
    assert f"src={VOLUME},dst=/source,readonly" in backup
    assert f"src={REMOTE_RELEASE},dst=/release,readonly" in backup
    assert f"src={BACKUP_ROOT},dst=/backup" in backup
    assert IMAGE in backup and "--source-db /source/ispindel.db" in backup
    assert "docker exec" not in backup and "--production" not in backup
    assert "PRAGMA foreign_key_check" in verify
    assert "sudo -n env" in verify
    assert plan["evidence"]["database"]["counts"]["samples"] == 2


def test_verifier_covers_authoritative_counts_and_manifest_comparison(deploy: ModuleType) -> None:
    script = deploy._slice2b2_render_verifier(BACKUP_ROOT, "20260804T000000Z-deadbeef")
    for table in ("devices", "samples", "calibrations", "calibration_active"):
        assert table in script
    assert "PRAGMA integrity_check" in script
    assert "PRAGMA foreign_key_check" in script
    assert "table count mismatch" in script
    assert "user_version mismatch" in script


def test_malformed_verifier_output_fails_closed(deploy: ModuleType) -> None:
    runner = Runner(verify_stdout=b"not-json\n")
    with pytest.raises(deploy.DeterministicGateFailure, match="did not emit"):
        call(deploy, runner)
    assert len(runner.calls) == 2


def test_dry_run_records_two_endpoint_scripts(deploy: ModuleType) -> None:
    runner = Runner(dry_run=True)
    plan = call(deploy, runner, execute=False)
    assert len(runner.calls) == 2
    assert all(endpoint == SOURCE for endpoint, _label, _script in runner.calls)
    assert [step["label"] for step in plan["steps"]] == ["helper_run", "verifier"]
    assert "evidence" not in plan


def test_backup_failure_prevents_verifier(deploy: ModuleType) -> None:
    runner = Runner(backup_rc=1)
    with pytest.raises(deploy.DeterministicGateFailure, match="helper failed"):
        call(deploy, runner)
    assert len(runner.calls) == 1
