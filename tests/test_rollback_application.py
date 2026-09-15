from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import subprocess
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest


ROOT = Path(__file__).resolve().parents[1]
DEPLOY_PATH = ROOT / "scripts" / "deploy" / "deploy.py"
RELEASE_ID = "20260804T212433Z-8e9c36107489"
CONTAINER = "ispindel-dashboard"
PREDECESSOR_ID = "a" * 64
CANDIDATE_ID = "c" * 64
IDENTITY_FORMAT = "{{.Id}}|{{.Name}}|{{.State.Running}}"
STANDBY_NAME = f"{CONTAINER}-predecessor-{RELEASE_ID.lower()}"
CANDIDATE_QUARANTINE_NAME = f"{CONTAINER}-candidate-{RELEASE_ID.lower()}"


def load_deploy() -> ModuleType:
    spec = importlib.util.spec_from_file_location("ispindel_deploy_rollback_behavior", DEPLOY_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def completed(returncode: int = 0, stdout: bytes = b"", stderr: bytes = b"") -> subprocess.CompletedProcess[bytes]:
    return subprocess.CompletedProcess(["fake"], returncode, stdout, stderr)


class FakeRunner:
    def __init__(self, *, candidate: subprocess.CompletedProcess[bytes], compatibility: subprocess.CompletedProcess[bytes] = completed(),
                 stop: subprocess.CompletedProcess[bytes] = completed(), verify: subprocess.CompletedProcess[bytes] = completed(stdout=b"false\n"),
                 standby_exists: bool = False, preserve_rename: subprocess.CompletedProcess[bytes] = completed()) -> None:
        if candidate.returncode == 0 and not candidate.stdout:
            candidate = completed(stdout=f"{CANDIDATE_ID}|/{CONTAINER}|true\n".encode())
        self.candidate = candidate
        self.compatibility = compatibility
        self.stop = stop
        self.verify = verify
        self.predecessor_restored = False
        self.quarantine_created = False
        self.standby_exists = standby_exists
        self.preserve_rename = preserve_rename
        self.endpoint_calls: list[tuple[str, list[str]]] = []
        self.other_calls: list[tuple[str, Any]] = []

    def remote_endpoint(self, _host: str, _root: str, _endpoint: str, argv: list[str], *, required: bool = True,
                        label: str = "endpoint") -> subprocess.CompletedProcess[bytes]:
        del required, label
        self.endpoint_calls.append((_endpoint, list(argv)))
        if argv == ["docker", "inspect", "--format", IDENTITY_FORMAT, CONTAINER]:
            if self.predecessor_restored:
                return completed(stdout=f"{PREDECESSOR_ID}|/{CONTAINER}|true\n".encode())
            return self.candidate
        if argv == ["docker", "inspect", "--format", IDENTITY_FORMAT, PREDECESSOR_ID]:
            return completed(stdout=f"{PREDECESSOR_ID}|/{STANDBY_NAME}|false\n".encode())
        if argv == ["docker", "inspect", "--format", IDENTITY_FORMAT, CANDIDATE_QUARANTINE_NAME]:
            if self.quarantine_created:
                return completed(stdout=f"{CANDIDATE_ID}|/{CANDIDATE_QUARANTINE_NAME}|false\n".encode())
            return completed(1, b"", f"error: no such object: {CANDIDATE_QUARANTINE_NAME}\n".encode())
        if argv == ["docker", "inspect", "--format", IDENTITY_FORMAT, STANDBY_NAME]:
            if self.standby_exists:
                return completed(stdout=f"{'d' * 64}|/{STANDBY_NAME}|false\n".encode())
            return completed(1, b"", f"error: no such object: {STANDBY_NAME}\n".encode())
        if argv[:3] == ["docker", "exec", CONTAINER]:
            return self.compatibility
        if argv == ["docker", "stop", "--time", "30", CONTAINER]:
            return self.stop
        if argv == ["docker", "inspect", "--format", "{{.State.Running}}", CONTAINER]:
            return self.verify
        if argv == ["docker", "rename", CONTAINER, CANDIDATE_QUARANTINE_NAME]:
            self.quarantine_created = True
            return completed()
        if argv == ["docker", "rename", CANDIDATE_ID, CANDIDATE_QUARANTINE_NAME]:
            self.quarantine_created = True
            return completed()
        if argv == ["docker", "rename", PREDECESSOR_ID, CONTAINER]:
            self.predecessor_restored = True
            return completed()
        if argv == ["docker", "rename", PREDECESSOR_ID, STANDBY_NAME]:
            return self.preserve_rename
        if argv == ["docker", "start", PREDECESSOR_ID]:
            return completed()
        raise AssertionError(f"unexpected endpoint command: {argv}")

    def remote_script_endpoint(self, *_args: Any, **_kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        self.other_calls.append(("remote_script_endpoint", _args))
        return completed(stdout=(PREDECESSOR_ID + "\n").encode())

    def remote_script(self, *_args: Any, **_kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        self.other_calls.append(("remote_script", _args))
        return completed()

    def remote_stdin(self, *_args: Any, **_kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        self.other_calls.append(("remote_stdin", _args))
        return completed()

    def remote(self, *_args: Any, **_kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        self.other_calls.append(("remote", _args))
        return completed()

    def emit_plan(self, *_args: Any, **_kwargs: Any) -> None:
        self.other_calls.append(("emit_plan", _args))


def make_stage05(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, deploy: ModuleType, runner: FakeRunner) -> argparse.Namespace:
    release_manifest = tmp_path / "RELEASE.json"
    release_manifest.write_text("release-bytes\n")
    descriptor = tmp_path / "predecessor.json"
    descriptor.write_text(json.dumps({"database": {"user_version": 7}}))
    system_snapshot = tmp_path / "predecessor-system.json"
    system_snapshot.write_text("{}\n")
    compose_snapshot = tmp_path / "predecessor-compose.json"
    compose_snapshot.write_text("{}\n")
    receipt_path = tmp_path / "01-backup-predecessor.json"
    receipt: dict[str, object] = {
        "release_manifest_sha256": hashlib.sha256(release_manifest.read_bytes()).hexdigest(),
        "predecessor_descriptor": str(descriptor),
        "predecessor_container_id": PREDECESSOR_ID,
        "predecessor_image_id": "sha256:" + "b" * 64,
        "predecessor_system_snapshot": str(system_snapshot),
        "predecessor_compose_snapshot": str(compose_snapshot),
        "predecessor_compose_paths": ["/opt/ispindel-dashboard/docker-compose.yml"],
        "predecessor_systemd_states": {
            unit: {"Id": unit, "LoadState": "not-found", "UnitFileState": "", "ActiveState": "inactive"}
            for unit in deploy.SYSTEMD_UNITS
        },
    }
    receipt_path.write_text(json.dumps(receipt))
    release = {
        "release_id": RELEASE_ID,
        "release_root": f"dist/releases/{RELEASE_ID}/payload",
        "image": {"reference": f"ispindel-release:{RELEASE_ID}", "archive_path": "image.tar"},
    }
    monkeypatch.setattr(deploy, "load_release", lambda _path: (release_manifest, release))
    monkeypatch.setattr(deploy, "require_execution", lambda _args, _release_id: None)
    monkeypatch.setattr(deploy, "load_stage01_receipt", lambda *_args: (receipt_path, receipt))
    monkeypatch.setattr(deploy, "Runner", lambda _dry_run: runner)
    return argparse.Namespace(
        release_manifest=release_manifest,
        remote="user@example",
        remote_root="/srv/ispindel",
        evidence_dir=tmp_path / "evidence",
        confirm_release_id=RELEASE_ID,
        execute=True,
        dry_run=False,
        stage01_receipt=receipt_path,
        stage01_receipt_sha256="0" * 64,
        env_file="/etc/ispindel/production.env",
        production_container=CONTAINER,
        ingress_opened=False,
    )


def test_exact_absence_requires_named_container_and_complete_docker_response() -> None:
    deploy = load_deploy()
    assert deploy.exact_docker_absence_verdict(
        completed(1, b"", b"error: no such object: ispindel-dashboard\n"), CONTAINER,
    )
    assert deploy.exact_docker_absence_verdict(
        completed(1, b"\n", b"error: no such object: ispindel-dashboard\n"), CONTAINER,
    )
    for result in (
        completed(1, b"", b"error: no such object: other\n"),
        completed(1, b"", b"error: no such object: ispindel-dashboard\nextra\n"),
        completed(1, b"noise", b"error: no such object: ispindel-dashboard\n"),
        completed(2, b"", b"error: no such object: ispindel-dashboard\n"),
    ):
        assert not deploy.exact_docker_absence_verdict(result, CONTAINER)


def test_stopped_verdict_requires_zero_exit_and_exact_false_output() -> None:
    deploy = load_deploy()
    assert deploy.exact_docker_stopped_verdict(completed(stdout=b"false\n"))
    assert deploy.exact_docker_stopped_verdict(completed(stdout=b"false"))
    for result in (
        completed(1, b"false\n", b"inspect failed\n"),
        completed(0, b"true\n"),
        completed(0, b"false\n", b"warning\n"),
        completed(0, b"false\nextra\n"),
    ):
        assert not deploy.exact_docker_stopped_verdict(result)


def test_absent_candidate_skips_candidate_operations_and_restarts_predecessor(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    deploy = load_deploy()
    runner = FakeRunner(candidate=completed(1, b"", b"error: no such object: ispindel-dashboard\n"))
    args = make_stage05(monkeypatch, tmp_path, deploy, runner)
    assert deploy.stage05(args) == 0
    commands = [argv for _endpoint, argv in runner.endpoint_calls]
    assert commands[:2] == [["docker", "inspect", "--format", IDENTITY_FORMAT, CONTAINER],
                             ["docker", "inspect", "--format", IDENTITY_FORMAT, PREDECESSOR_ID]]
    assert ["docker", "rename", PREDECESSOR_ID, CONTAINER] in commands
    assert ["docker", "start", PREDECESSOR_ID] in commands
    assert commands[-1] == ["docker", "inspect", "--format", IDENTITY_FORMAT, CONTAINER]


def test_non_exact_absence_fails_closed_before_predecessor_restart(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    deploy = load_deploy()
    runner = FakeRunner(candidate=completed(1, b"", b"error: no such object: other\n"))
    args = make_stage05(monkeypatch, tmp_path, deploy, runner)
    with pytest.raises(deploy.DeploymentError, match="exact Docker absence"):
        deploy.stage05(args)
    assert len(runner.endpoint_calls) == 1


def test_schema_probe_failure_fails_closed_before_stop_or_predecessor_restart(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    deploy = load_deploy()
    runner = FakeRunner(candidate=completed(), compatibility=completed(1, b"", b"probe failed\n"))
    args = make_stage05(monkeypatch, tmp_path, deploy, runner)
    with pytest.raises(deploy.DeploymentError, match="schema verdict"):
        deploy.stage05(args)
    commands = [argv for _endpoint, argv in runner.endpoint_calls]
    assert commands[0] == ["docker", "inspect", "--format", IDENTITY_FORMAT, CONTAINER]
    assert len(commands) == 2
    assert commands[1][:4] == ["docker", "exec", CONTAINER, "python3"]


def test_candidate_stop_failure_prevents_predecessor_restart(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    deploy = load_deploy()
    runner = FakeRunner(candidate=completed(), stop=completed(1, b"", b"stop failed\n"))
    args = make_stage05(monkeypatch, tmp_path, deploy, runner)
    with pytest.raises(deploy.DeploymentError, match="failed to stop"):
        deploy.stage05(args)
    assert not any(argv == ["docker", "start", PREDECESSOR_ID] for _endpoint, argv in runner.endpoint_calls)


def test_candidate_stop_verification_requires_zero_exit_before_predecessor_restart(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    deploy = load_deploy()
    runner = FakeRunner(candidate=completed(), verify=completed(1, b"false\n", b"inspect failed\n"))
    args = make_stage05(monkeypatch, tmp_path, deploy, runner)
    with pytest.raises(deploy.DeploymentError, match="did not stop"):
        deploy.stage05(args)
    assert not any(argv == ["docker", "start", PREDECESSOR_ID] for _endpoint, argv in runner.endpoint_calls)


def test_candidate_stop_and_verification_precede_predecessor_restart(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    deploy = load_deploy()
    runner = FakeRunner(candidate=completed())
    args = make_stage05(monkeypatch, tmp_path, deploy, runner)
    assert deploy.stage05(args) == 0
    commands = [argv for _endpoint, argv in runner.endpoint_calls]
    assert commands.index(["docker", "stop", "--time", "30", CONTAINER]) < commands.index(["docker", "rename", PREDECESSOR_ID, CONTAINER])
    assert commands.index(["docker", "inspect", "--format", "{{.State.Running}}", CONTAINER]) < commands.index(["docker", "start", PREDECESSOR_ID])


def test_stage03_preserves_predecessor_by_release_bound_standby_name() -> None:
    deploy = load_deploy()
    runner = FakeRunner(candidate=completed())
    plan = deploy.slice2_source_preserve_predecessor(
        runner, "user@example", "/srv/ispindel", PREDECESSOR_ID,
        RELEASE_ID, CONTAINER, execute=True,
    )
    commands = [argv for _endpoint, argv in runner.endpoint_calls]
    assert commands == [
        ["docker", "inspect", "--format", IDENTITY_FORMAT, STANDBY_NAME],
        ["docker", "rename", PREDECESSOR_ID, STANDBY_NAME],
        ["docker", "inspect", "--format", IDENTITY_FORMAT, PREDECESSOR_ID],
    ]
    assert plan["standby_name"] == STANDBY_NAME
    assert plan["verified"] == f"{PREDECESSOR_ID}|/{STANDBY_NAME}|false"


def test_stage03_standby_name_collision_fails_before_rename() -> None:
    deploy = load_deploy()
    runner = FakeRunner(candidate=completed(), standby_exists=True)
    with pytest.raises(deploy.DeterministicGateFailure, match="standby name already exists"):
        deploy.slice2_source_preserve_predecessor(
            runner, "user@example", "/srv/ispindel", PREDECESSOR_ID,
            RELEASE_ID, CONTAINER, execute=True,
        )
    commands = [argv for _endpoint, argv in runner.endpoint_calls]
    assert commands == [["docker", "inspect", "--format", IDENTITY_FORMAT, STANDBY_NAME]]


def test_stage03_standby_rename_failure_fails_before_verification() -> None:
    deploy = load_deploy()
    runner = FakeRunner(candidate=completed(), preserve_rename=completed(1, b"", b"rename failed\\n"))
    with pytest.raises(deploy.DeterministicGateFailure, match="failed to preserve predecessor"):
        deploy.slice2_source_preserve_predecessor(
            runner, "user@example", "/srv/ispindel", PREDECESSOR_ID,
            RELEASE_ID, CONTAINER, execute=True,
        )
    commands = [argv for _endpoint, argv in runner.endpoint_calls]
    assert commands == [
        ["docker", "inspect", "--format", IDENTITY_FORMAT, STANDBY_NAME],
        ["docker", "rename", PREDECESSOR_ID, STANDBY_NAME],
    ]
