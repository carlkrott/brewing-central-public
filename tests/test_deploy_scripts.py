from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
from types import ModuleType
from typing import cast

import pytest


ROOT = Path(__file__).resolve().parents[1]
DEPLOY_PATH = ROOT / "scripts" / "deploy" / "deploy.py"
RELEASE_MANIFEST = ROOT / "descriptors" / "RELEASE.json"
PREDECESSOR = ROOT / "descriptors" / "predecessor-production.json"
CONTRACT = ROOT / "contracts" / "ispindel-predecessor-expected-missing-amendment-v3.json"
CONTRACT_VERSION = "ispindel-predecessor-expected-missing/2026-08-06-v3"
EXPECTED_MISSING_PATH = "/tmp/ispindel-phase06a1-exact-build-20260730T023218Z-114bd2aa2a0dfe0943295766b177da0f/compose.override.yml"
# Deterministic identifiers for synthetic-only fixtures and tests. The generated
# ``descriptors/RELEASE.json`` carries a private commit SHA in its ``release_id``
# and is therefore removed from the public source export; these constants let
# receipt fixtures and tampered-descriptor tests stay stable across releases
# without ever reading the generated descriptor.
SYNTHETIC_RELEASE_ID = "20260803T011203Z-3607db5a9611"
SYNTHETIC_RELEASE_MANIFEST_SHA256 = (
    "0000000000000000000000000000000000000000000000000000000000000000"
)


def _load_release_id() -> str:
    """Return ``RELEASE.json``'s ``release_id`` or the synthetic fallback.

    The public source export intentionally omits ``descriptors/RELEASE.json``
    because the generated ``release_id`` embeds a private commit SHA. Tests
    that genuinely require the descriptor (and therefore the real release id)
    guard themselves with :func:`_require_release_manifest`; everyone else uses
    :data:`SYNTHETIC_RELEASE_ID` so the module can import cleanly on a public
    clone.
    """
    if RELEASE_MANIFEST.is_file():
        return json.loads(RELEASE_MANIFEST.read_text())["release_id"]
    return SYNTHETIC_RELEASE_ID


RELEASE_ID = _load_release_id()


def _release_payload_root() -> Path:
    """Resolve the generated release payload directory declared by RELEASE.json.

    Public CI exports intentionally omit ``dist/`` (per ``.gitignore``), so this
    directory is only present when the release has been built locally. Tests that
    require the generated payload skip themselves when the directory is absent.
    """
    return ROOT / "dist" / "releases" / RELEASE_ID / "payload"


def _require_release_manifest() -> None:
    """Skip the calling test when ``descriptors/RELEASE.json`` is absent.

    The public source export deliberately omits the generated descriptor because
    its ``release_id`` embeds a private commit SHA. Tests that read the
    descriptor to verify the live release (load the manifest, cross-check
    artefact hashes, etc.) must skip rather than fail when the file is missing
    on a clean public clone.
    """
    if not RELEASE_MANIFEST.is_file():
        pytest.skip(
            f"generated release descriptor is absent at {RELEASE_MANIFEST} "
            f"(the descriptor is intentionally removed from public source "
            f"exports because its release_id embeds a private commit SHA)"
        )


def _require_release_payload() -> None:
    """Skip the calling test when the generated release payload is absent.

    Five deployment tests load ``descriptors/RELEASE.json`` through
    ``deploy.load_release`` (directly or via the deploy entrypoints), which
    shells out to ``scripts/verify-release.py``. The verifier rejects the
    manifest when the generated ``dist/releases/<release_id>/payload/`` tree is
    absent, because that tree is the source of truth for the SHA-256 bindings.
    Public CI exports deliberately omit ``dist/`` from the tracked source, so on
    a clean public clone those tests must be skipped rather than failed.

    These tests already read ``descriptors/RELEASE.json`` and therefore also
    require the manifest itself; we reuse :func:`_require_release_manifest` so
    the skip reason stays accurate on a public clone that ships the descriptor
    but no ``dist/`` payload tree.
    """
    _require_release_manifest()
    payload_root = _release_payload_root()
    if not payload_root.is_dir():
        pytest.skip(
            f"generated release payload is absent at {payload_root} (dist/ is "
            f"intentionally ignored in public source exports); release_id={RELEASE_ID}"
        )


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture(scope="module")
def deploy() -> ModuleType:
    spec = importlib.util.spec_from_file_location("ispindel_deploy", DEPLOY_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def stage01_receipt(tmp_path: Path, deploy: ModuleType) -> tuple[Path, str, dict[str, object]]:
    compose_paths = ["/opt/ispindel-dashboard/docker-compose.yml"]
    present_value = b"a"
    compose_rows = {
        compose_paths[0]: {
            "present": True,
            "mode": 0o640,
            "uid": os.getuid(),
            "gid": os.getgid(),
            "size": len(present_value),
            "sha256": hashlib.sha256(present_value).hexdigest(),
            "data_b64": __import__("base64").b64encode(present_value).decode(),
        },

    }
    artifacts: dict[str, Path] = {}
    values = {
        "predecessor_source_archive": b"source-tar",
        "predecessor_image_archive": b"image-tar",
        "predecessor_image_inspect": b"[]\n",
        "predecessor_volume_inspect": json.dumps([{
            "Name": "ispindel-dashboard_ispindel-data",
            "Driver": "local",
            "Mountpoint": "/var/lib/docker/volumes/ispindel-dashboard_ispindel-data/_data",
            "Labels": None,
        }], sort_keys=True).encode() + b"\n",
        "predecessor_system_snapshot": json.dumps(
            {path: {"present": False} for path in deploy.SYSTEM_PATHS}, sort_keys=True
        ).encode() + b"\n",
        "predecessor_compose_snapshot": json.dumps(compose_rows, sort_keys=True).encode() + b"\n",
    }
    for key, value in values.items():
        target = tmp_path / key
        target.write_bytes(value)
        artifacts[key] = target
    # Synthetic fixture: pin a deterministic release id and manifest sha256 so
    # the receipt stays stable across releases and never reads the generated
    # descriptor (which is intentionally removed from the public source export).
    release_id = SYNTHETIC_RELEASE_ID
    systemd_states = {
        unit: {"Id": unit, "LoadState": "not-found", "UnitFileState": "", "ActiveState": "inactive"}
        for unit in deploy.SYSTEMD_UNITS
    }
    expected_missing: list[str] = []
    presence = {path: bool(row["present"]) for path, row in compose_rows.items()}
    stable_fingerprint = "1" * 64
    receipt: dict[str, object] = {
        "schema": "ispindel-deploy-stage-receipt/v1",
        "stage": "01-backup-predecessor",
        "release_id": release_id,
        "result": "PASS",
        "database_restored": False,
        "source_endpoint": deploy.SOURCE_DOCKER_HOST,
        "target_endpoint": deploy.TARGET_DOCKER_HOST,
        "predecessor_container_id": "a" * 64,
        "predecessor_volume_name": "ispindel-dashboard_ispindel-data",
        "release_manifest_sha256": SYNTHETIC_RELEASE_MANIFEST_SHA256,
        "predecessor_descriptor": str(PREDECESSOR),
        "predecessor_descriptor_sha256": digest(PREDECESSOR),
        "expected_missing_amendment": str(CONTRACT),
        "expected_missing_amendment_sha256": digest(CONTRACT),
        "expected_missing_contract_version": CONTRACT_VERSION,
        "predecessor_expected_missing_paths": expected_missing,
        "predecessor_expected_missing_paths_sha256": deploy.canonical_object_sha256(expected_missing),
        "predecessor_compose_presence": presence,
        "predecessor_image_id": "sha256:" + "0" * 64,
        "predecessor_compose_paths": compose_paths,
        "predecessor_compose_active_paths": [compose_paths[0]],
        "predecessor_container_stable_fingerprint_before": stable_fingerprint,
        "predecessor_container_stable_fingerprint_after": stable_fingerprint,
        "predecessor_systemd_states": systemd_states,
        "predecessor_systemd_states_sha256": deploy.canonical_object_sha256(systemd_states),
        "backup_manifest": "/var/backups/ispindel-dashboard/test/manifest.json",
    }
    for key, target in artifacts.items():
        receipt[key] = str(target)
        receipt[f"{key}_sha256"] = digest(target)
    receipt["predecessor_source_tree_full_sha256"] = receipt["predecessor_source_archive_sha256"]
    path = tmp_path / "01-backup-predecessor.json"
    path.write_text(json.dumps(receipt, sort_keys=True) + "\n")
    return path, digest(path), receipt


def test_stable_container_fingerprint_ignores_mount_order(deploy: ModuleType) -> None:
    container = {
        "Id": "container-id",
        "Image": "sha256:image-id",
        "Name": "/ispindel-dashboard",
        "Config": {"Image": "candidate"},
        "HostConfig": {"NetworkMode": "default"},
        "Mounts": [
            {"Destination": "/data", "Type": "volume", "Name": "data"},
            {"Destination": "/health", "Type": "bind", "Source": "/health"},
        ],
    }
    reordered = json.loads(json.dumps(container))
    reordered["Mounts"].reverse()
    assert deploy.stable_container_fingerprint(container) == deploy.stable_container_fingerprint(reordered)


def test_current_release_passes_strict_local_verification(deploy: ModuleType) -> None:
    _require_release_payload()
    path, manifest = deploy.load_release(RELEASE_MANIFEST)
    assert path == RELEASE_MANIFEST
    assert manifest["release_id"] == RELEASE_ID


def test_path_guards_reject_destructive_roots_and_env_redirection(deploy: ModuleType) -> None:
    with pytest.raises(deploy.DeploymentError, match="non-root"):
        deploy.validate_scoped_absolute("/", "remote root")
    with pytest.raises(deploy.DeploymentError, match="non-root"):
        deploy.validate_scoped_absolute("/srv", "remote root")
    with pytest.raises(deploy.DeploymentError, match="fixed"):
        deploy.validate_production_env("/etc/passwd")
    assert deploy.validate_scoped_absolute("/srv/ispindel", "remote root") == "/srv/ispindel"
    assert deploy.validate_production_env("/etc/ispindel/production.env") == "/etc/ispindel/production.env"


def test_tampered_release_descriptor_is_rejected_before_ssh(tmp_path: Path) -> None:
    _require_release_manifest()
    manifest = json.loads(RELEASE_MANIFEST.read_text())
    manifest["release_id"] = "20260803T011203Z-000000000000"
    target = tmp_path / "RELEASE.json"
    target.write_text(json.dumps(manifest))
    result = subprocess.run(
        [sys.executable, str(DEPLOY_PATH), "stage-04", "--release-manifest", str(target),
         "--remote", "user@example", "--remote-root", "/srv/ispindel",
         "--evidence-dir", str(tmp_path / "evidence"), "--dry-run",
         "--confirm-release-id", manifest["release_id"], "--browser-command", "true",
         "--security-command", "true", "--alert-command", "true",
         "--test-ingest-command", "true", "--test-ingest-policy", "remove",
         "--lan-url", "http://lan.example.test:8098", "--tailnet-url", "https://tailnet.example.test"],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 1
    assert "strict local release verification failed" in result.stderr
    assert "ssh" not in result.stderr


def test_predecessor_descriptor_is_bound_to_release_inventory(tmp_path: Path) -> None:
    _require_release_payload()
    altered = json.loads(PREDECESSOR.read_text())
    altered["hostname"] = "tampered"
    target = tmp_path / "predecessor.json"
    target.write_text(json.dumps(altered, sort_keys=True) + "\n")
    release_id = json.loads(RELEASE_MANIFEST.read_text())["release_id"]
    result = subprocess.run(
        [sys.executable, str(DEPLOY_PATH), "stage-01", "--release-manifest", str(RELEASE_MANIFEST),
         "--predecessor-descriptor", str(target), "--remote", "user@example",
         "--remote-root", "/srv/ispindel", "--evidence-dir", str(tmp_path / "evidence"),
         "--confirm-release-id", release_id, "--dry-run", "--offhost", "backup@example:/archive"],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 1
    assert "does not match the frozen release inventory" in result.stderr


def test_rootful_production_compose_path_is_approved_and_other_absolute_paths_are_not(deploy: ModuleType) -> None:
    assert deploy.validate_predecessor_compose_path(
        "/opt/ispindel-dashboard/docker-compose.yml",
        "/opt/ispindel-dashboard",
    ) == "/opt/ispindel-dashboard/docker-compose.yml"
    with pytest.raises(deploy.DeploymentError, match="outside approved roots"):
        deploy.validate_predecessor_compose_path(
            "/etc/ispindel/docker-compose.yml",
            "/opt/ispindel-dashboard",
        )


@pytest.mark.parametrize("value", ["root@example;id", "example", "u@h host", "u@host\ncmd"])
def test_unsafe_remote_is_rejected(deploy: ModuleType, value: str) -> None:
    with pytest.raises(deploy.DeploymentError):
        deploy.validate_remote(value)


@pytest.mark.parametrize("value", ["relative/path", "/tmp/x\ncmd", "/tmp/../etc", "/tmp/x\x00y"])
def test_unsafe_absolute_path_is_rejected(deploy: ModuleType, value: str) -> None:
    with pytest.raises(deploy.DeploymentError):
        deploy.validate_absolute(value, "test path")


def test_remote_commands_force_bash_and_required_cd(deploy: ModuleType) -> None:
    command = deploy.remote_command("/opt/ispindel-dashboard", ["docker", "compose", "up", "-d"])
    assert command.startswith("/bin/bash -lc ")
    assert "cd /opt/ispindel-dashboard && docker compose up -d" in command
    script = deploy.remote_script_command("/opt/ispindel-dashboard", "docker compose ps")
    assert script.startswith("/bin/bash -lc ")
    assert "cd /opt/ispindel-dashboard && docker compose ps" in script


def test_dry_runner_never_opens_ssh(deploy: ModuleType) -> None:
    runner = deploy.Runner(dry_run=True)
    result = runner.remote("user@example", "/srv/ispindel", ["docker", "compose", "ps"])
    assert result.returncode == 0
    assert runner.plan[0][0] == "ssh"
    assert "/bin/bash -lc" in runner.plan[0][-1]
    assert "cd /srv/ispindel && docker compose ps" in runner.plan[0][-1]


def test_endpoint_prefix_precedes_subcommand_and_handles_env_compose(deploy: ModuleType) -> None:
    endpoint = deploy.TARGET_DOCKER_HOST
    assert deploy.prefix_endpoint(["docker", "inspect", "candidate"], endpoint) == [
        "docker", "--host", endpoint, "inspect", "candidate",
    ]
    assert deploy.prefix_endpoint(
        ["env", "ISPINDEL_IMAGE_REF=exact", "docker", "compose", "up", "-d"], endpoint,
    ) == [
        "env", "ISPINDEL_IMAGE_REF=exact", "docker", "--host", endpoint, "compose", "up", "-d",
    ]
    assert deploy.prefix_endpoint(["docker", "context", "use", "unsafe"], endpoint) == [
        "docker", "context", "use", "unsafe",
    ]
    assert deploy.docker_endpoint_env(endpoint) == f"export DOCKER_HOST={endpoint}; "


def test_release_verifier_invocations_disable_bytecode_writes(deploy: ModuleType) -> None:
    assert deploy.remote_python_no_bytecode(["/tmp/verify-release.py", "--help"]) == [
        "env", "PYTHONDONTWRITEBYTECODE=1", "python3", "/tmp/verify-release.py", "--help",
    ]
    args = deploy.network_none_verifier("/tmp/release", "image:exact")
    assert args[:10] == [
        "docker", "run", "--rm", "--network", "none", "--pull", "never",
        "--env", "PYTHONDONTWRITEBYTECODE=1", "--entrypoint",
    ]
    assert "--env" in args
    assert "PYTHONDONTWRITEBYTECODE=1" in args
    assert "image:exact" in args
    assert "/release/RELEASE.json" in args
    assert any("readonly" in value for value in args)




def test_stage02_rehearsal_command_disables_bytecode_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, deploy: ModuleType,
) -> None:
    _require_release_payload()
    calls: list[tuple[str, list[str] | str]] = []
    image_id = json.loads(RELEASE_MANIFEST.read_text())["image"]["id"]

    class FakeRunner:
        def __init__(self, _dry_run: bool) -> None:
            pass

        def remote(self, _host: str, _root: str, command: list[str], *, required: bool = True):
            del required
            calls.append(("remote", command))
            return subprocess.CompletedProcess(command, 0, b"", b"")

        def remote_endpoint(self, _host: str, _root: str, _endpoint: str, command: list[str], *, required: bool = True, label: str = "endpoint"):
            del required, label
            calls.append(("endpoint", command))
            stdout = (str(image_id) + "\n").encode() if "inspect" in command else b""
            return subprocess.CompletedProcess(command, 0, stdout, b"")

        def remote_script_endpoint(self, _host: str, _root: str, _endpoint: str, command: str, *, required: bool = True, label: str = "endpoint"):
            del required, label
            calls.append(("script", command))
            return subprocess.CompletedProcess(command, 0, b"rehearsal-output", b"")

    monkeypatch.setattr(deploy, "Runner", FakeRunner)
    args = argparse.Namespace(
        release_manifest=RELEASE_MANIFEST,
        remote="user@example",
        remote_root="/srv/ispindel",
        evidence_dir=tmp_path / "evidence",
        confirm_release_id=RELEASE_ID,
        execute=True,
        dry_run=False,
        remote_release_root="/tmp/release/payload",
        backup_manifest="/var/backups/ispindel-dashboard/run/manifest.json",
    )
    assert deploy.stage02(args) == 0
    remote_command = calls[0][1]
    assert remote_command[:3] == ["env", "PYTHONDONTWRITEBYTECODE=1", "python3"]
    rehearsal_commands = [str(command) for kind, command in calls if kind == "script" and "rehearse-restore.py" in str(command)]
    assert len(rehearsal_commands) == 1
    assert rehearsal_commands[0].startswith("env PYTHONDONTWRITEBYTECODE=1 python3 ")

    present = tmp_path / "nested" / "present"
    missing = tmp_path / "nested" / "missing"
    present.parent.mkdir()
    present.write_bytes(b"exact predecessor bytes\x00\xff")
    present.chmod(0o640)
    paths = [str(present), str(missing)]
    snapshot = subprocess.run(
        [sys.executable, "-c", deploy.system_snapshot_code(), json.dumps(paths)],
        capture_output=True, check=True,
    )
    rows = json.loads(snapshot.stdout)
    assert rows[str(present)]["sha256"] == hashlib.sha256(present.read_bytes()).hexdigest()
    assert rows[str(present)]["mode"] == 0o640
    present.write_bytes(b"mutated")
    present.chmod(0o600)
    missing.write_bytes(b"must be removed")
    restored = subprocess.run(
        [sys.executable, "-c", deploy.system_restore_code(), json.dumps(paths)],
        input=snapshot.stdout, capture_output=True, check=True,
    )
    assert restored.stdout == b"SYSTEM_RESTORE_OK\n"
    assert present.read_bytes() == b"exact predecessor bytes\x00\xff"
    assert stat.S_IMODE(present.stat().st_mode) == 0o640
    assert not missing.exists()


def test_stage03_prepares_rootful_backup_root_with_sudo() -> None:
    source = DEPLOY_PATH.read_text()
    assert "sudo test -L {path}" in source
    assert "sudo test -d {path}" in source
    assert "sudo install -d -m 700 -- {path}" in source
    assert 'f"install -d -m 700 -- {shlex.quote(backup_root)}"' not in source


def test_snapshot_rejects_symlink(tmp_path: Path, deploy: ModuleType) -> None:
    target = tmp_path / "target"
    target.write_text("secret")
    link = tmp_path / "link"
    link.symlink_to(target)
    result = subprocess.run(
        [sys.executable, "-c", deploy.system_snapshot_code(), json.dumps([str(link)])],
        capture_output=True, check=False,
    )
    assert result.returncode != 0


def test_backup_output_binds_one_verified_absolute_manifest(deploy: ModuleType) -> None:
    event = {
        "event": "backup_complete", "result": "BACKUP_VERIFIED",
        "manifest": "/var/backups/ispindel/run/manifest.json",
        "offhost_manifest": "backup@example:/archive/run/manifest.json",
    }
    parsed = deploy.parse_backup_output((json.dumps({"event": "progress"}) + "\n" + json.dumps(event) + "\n").encode())
    assert parsed == event
    with pytest.raises(deploy.DeploymentError, match="one verified"):
        deploy.parse_backup_output((json.dumps(event) + "\n" + json.dumps(event) + "\n").encode())
    event["manifest"] = "relative/manifest.json"
    with pytest.raises(deploy.DeploymentError, match="absolute"):
        deploy.parse_backup_output((json.dumps(event) + "\n").encode())


def test_stage04_receipt_preserves_inspectable_database_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, deploy: ModuleType,
) -> None:
    _require_release_payload()
    class FakeRunner:
        dry_run = False

        def remote(self, _host: str, _root: str, command: object, *, required: bool = True) -> subprocess.CompletedProcess[bytes]:
            del required
            words = cast(list[str], command)
            stdout = b"ok\n"
            if words[:2] == ["docker", "exec"]:
                stdout = b'{"counts":{"devices":2,"samples":18},"user_version":2}\n'
            elif words[:4] == ["curl", "-fsS", "--max-time", "5"]:
                stdout = b'{"status":"ok"}\n'
            elif "is-active" in words:
                stdout = b"active\n"
            elif "is-enabled" in words:
                stdout = b"enabled\n"
            return subprocess.CompletedProcess(words, 0, stdout, b"")

        def remote_script(self, _host: str, _root: str, command: str, *, required: bool = True) -> subprocess.CompletedProcess[bytes]:
            del required
            return subprocess.CompletedProcess(command, 0, b"", b"")

        def remote_endpoint(self, host: str, root: str, _endpoint: str, command: object, *, required: bool = True, label: str = "endpoint") -> subprocess.CompletedProcess[bytes]:
            del label
            return self.remote(host, root, command, required=required)

        def remote_script_endpoint(self, host: str, root: str, _endpoint: str, command: str, *, required: bool = True, label: str = "endpoint") -> subprocess.CompletedProcess[bytes]:
            del label
            return self.remote_script(host, root, command, required=required)

    monkeypatch.setattr(deploy, "Runner", lambda _dry_run: FakeRunner())
    evidence = tmp_path / "evidence"
    args = argparse.Namespace(
        release_manifest=RELEASE_MANIFEST, remote="user@example", remote_root="/srv/ispindel",
        evidence_dir=evidence, confirm_release_id=RELEASE_ID, execute=True, dry_run=False,
        production_container="ispindel-dashboard", browser_command="true", security_command="true",
        alert_command="true", test_ingest_command="true", test_ingest_policy="remove",
        lan_url="http://lan.example.test:8098", tailnet_url="https://tailnet.example.test",
    )
    assert deploy.stage04(args) == 0
    receipt = json.loads((evidence / RELEASE_ID / "04-validate-live.json").read_text())
    assert receipt["database_evidence"] == {"counts": {"devices": 2, "samples": 18}, "user_version": 2}
    assert json.loads(receipt["checks"]["database"]["stdout"])["user_version"] == 2
    assert "stdout" not in receipt["checks"]["devices"]
    static_assets = json.loads(RELEASE_MANIFEST.read_text())["source"]["static_assets"]
    expected_served_assets = {
        path: static_assets[path] for path in deploy.SERVED_STATIC_ASSET_PATHS
    }
    assert receipt["served_static_assets"] == expected_served_assets


def test_stage04_rejects_non_object_database_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, deploy: ModuleType,
) -> None:
    _require_release_payload()
    class BadRunner:
        dry_run = False

        def remote(self, _host: str, _root: str, command: object, *, required: bool = True) -> subprocess.CompletedProcess[bytes]:
            del required
            words = cast(list[str], command)
            if words[:2] == ["docker", "exec"]:
                output = b"[]\n"
            elif "is-active" in words:
                output = b"active\n"
            elif "is-enabled" in words:
                output = b"enabled\n"
            else:
                output = b"{}\n"
            return subprocess.CompletedProcess(words, 0, output, b"")

        def remote_script(self, _host: str, _root: str, _command: str, *, required: bool = True) -> subprocess.CompletedProcess[bytes]:
            del required
            return subprocess.CompletedProcess([], 0, b"", b"")

        def remote_endpoint(self, host: str, root: str, _endpoint: str, command: object, *, required: bool = True, label: str = "endpoint") -> subprocess.CompletedProcess[bytes]:
            del label
            return self.remote(host, root, command, required=required)

        def remote_script_endpoint(self, host: str, root: str, _endpoint: str, command: str, *, required: bool = True, label: str = "endpoint") -> subprocess.CompletedProcess[bytes]:
            del label
            return self.remote_script(host, root, command, required=required)

    monkeypatch.setattr(deploy, "Runner", lambda _dry_run: BadRunner())
    args = argparse.Namespace(
        release_manifest=RELEASE_MANIFEST, remote="user@example", remote_root="/srv/ispindel",
        evidence_dir=tmp_path / "evidence", confirm_release_id=RELEASE_ID, execute=True, dry_run=False,
        production_container="ispindel-dashboard", browser_command="true", security_command="true",
        alert_command="true", test_ingest_command="true", test_ingest_policy="remove",
        lan_url="http://lan.example.test:8098", tailnet_url="https://tailnet.example.test",
    )
    with pytest.raises(deploy.DeploymentError, match="JSON object"):
        deploy.stage04(args)


def test_image_ref_env_update_is_atomic_and_preserves_other_bytes_and_mode(
    tmp_path: Path, deploy: ModuleType,
) -> None:
    target = tmp_path / "production.env"
    target.write_bytes(b"SECRET=preserved\nISPINDEL_IMAGE_REF=old:image\nOTHER=value\n")
    target.chmod(0o640)
    result = subprocess.run(
        [sys.executable, "-c", deploy.update_image_env_code(), str(target), "release:exact"],
        capture_output=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert target.read_bytes() == b"SECRET=preserved\nISPINDEL_IMAGE_REF=release:exact\nOTHER=value\n"
    assert stat.S_IMODE(target.stat().st_mode) == 0o640


def test_image_ref_env_update_rejects_duplicate_bindings(tmp_path: Path, deploy: ModuleType) -> None:
    target = tmp_path / "production.env"
    original = b"ISPINDEL_IMAGE_REF=one\nISPINDEL_IMAGE_REF=two\n"
    target.write_bytes(original)
    result = subprocess.run(
        [sys.executable, "-c", deploy.update_image_env_code(), str(target), "release:exact"],
        capture_output=True, check=False,
    )
    assert result.returncode != 0
    assert target.read_bytes() == original


def test_update_production_env_sets_persistent_secret_path_atomically(tmp_path: Path, deploy: ModuleType):
    target = tmp_path / "production.env"
    target.write_bytes(
        b"ISPINDEL_SECRETS_DIR=/run/secrets\n"
        b"ISPINDEL_IMAGE_REF=old:image\n"
        b"COMPOSE_PROJECT_NAME=old-project\n"
        b"ISPINDEL_DATA_VOLUME=old-volume\n"
        b"OTHER=value\n"
    )
    target.chmod(0o640)
    result = subprocess.run(
        [
            sys.executable, "-c", deploy.update_production_env_code(),
            str(target), "release:exact", "release-project", "release-volume",
        ],
        text=True, capture_output=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert target.read_bytes() == (
        b"ISPINDEL_SECRETS_DIR=/etc/ispindel/secrets\n"
        b"ISPINDEL_IMAGE_REF=release:exact\n"
        b"COMPOSE_PROJECT_NAME=release-project\n"
        b"ISPINDEL_DATA_VOLUME=release-volume\n"
        b"OTHER=value\n"
    )
    assert stat.S_IMODE(target.stat().st_mode) == 0o640


def test_update_production_env_rejects_duplicate_contract_keys(tmp_path: Path, deploy: ModuleType):
    target = tmp_path / "production.env"
    target.write_bytes(b"ISPINDEL_SECRETS_DIR=/run/secrets\nISPINDEL_SECRETS_DIR=/tmp\n")
    result = subprocess.run(
        [
            sys.executable, "-c", deploy.update_production_env_code(),
            str(target), "release:exact", "release-project", "release-volume",
        ],
        text=True, capture_output=True, check=False,
    )
    assert result.returncode != 0


def test_systemd_state_binding_requires_exact_unit_coverage(deploy: ModuleType) -> None:
    states = {
        unit: {"Id": unit, "LoadState": "not-found", "UnitFileState": "", "ActiveState": "inactive"}
        for unit in deploy.SYSTEMD_UNITS
    }
    assert deploy.require_systemd_states(states) == states
    states.pop(next(iter(states)))
    with pytest.raises(deploy.DeploymentError, match="coverage"):
        deploy.require_systemd_states(states)


def test_restore_rejects_tampered_snapshot_without_replacing_target(tmp_path: Path, deploy: ModuleType) -> None:
    target = tmp_path / "target"
    target.write_bytes(b"current")
    rows = {
        str(target): {
            "present": True, "mode": 0o600, "uid": os.getuid(), "gid": os.getgid(),
            "size": 4, "sha256": "0" * 64, "data_b64": "dGFtcA==",
        }
    }
    result = subprocess.run(
        [sys.executable, "-c", deploy.system_restore_code(), json.dumps([str(target)])],
        input=(json.dumps(rows) + "\n").encode(), capture_output=True, check=False,
    )
    assert result.returncode != 0
    assert target.read_bytes() == b"current"


def test_stage01_receipt_and_all_artifact_hashes_are_accepted(
    stage01_receipt: tuple[Path, str, dict[str, object]], deploy: ModuleType,
) -> None:
    path, expected_hash, receipt = stage01_receipt
    loaded_path, loaded = deploy.load_stage01_receipt(path, str(receipt["release_id"]), expected_hash)
    assert loaded_path == path
    assert loaded == receipt


def test_tampered_stage01_receipt_is_rejected(
    stage01_receipt: tuple[Path, str, dict[str, object]], deploy: ModuleType,
) -> None:
    path, expected_hash, receipt = stage01_receipt
    receipt["database_restored"] = True
    path.write_text(json.dumps(receipt, sort_keys=True) + "\n")
    with pytest.raises(deploy.DeploymentError, match="receipt SHA-256 mismatch"):
        deploy.load_stage01_receipt(path, str(receipt["release_id"]), expected_hash)


@pytest.mark.parametrize("artifact_key", [
    "predecessor_source_archive", "predecessor_image_archive", "predecessor_image_inspect",
    "predecessor_volume_inspect", "predecessor_system_snapshot", "predecessor_compose_snapshot",
])
def test_tampered_stage01_artifact_is_rejected(
    stage01_receipt: tuple[Path, str, dict[str, object]], deploy: ModuleType, artifact_key: str,
) -> None:
    path, expected_hash, receipt = stage01_receipt
    Path(str(receipt[artifact_key])).write_bytes(b"tampered")
    with pytest.raises(deploy.DeploymentError, match="hash mismatch"):
        deploy.load_stage01_receipt(path, str(receipt["release_id"]), expected_hash)


def test_tampered_predecessor_descriptor_is_rejected_by_receipt(
    tmp_path: Path, stage01_receipt: tuple[Path, str, dict[str, object]], deploy: ModuleType,
) -> None:
    path, _, receipt = stage01_receipt
    descriptor = tmp_path / "predecessor.json"
    descriptor.write_bytes(PREDECESSOR.read_bytes() + b" ")
    receipt["predecessor_descriptor"] = str(descriptor)
    path.write_text(json.dumps(receipt, sort_keys=True) + "\n")
    expected_hash = digest(path)
    with pytest.raises(deploy.DeploymentError, match="descriptor hash mismatch"):
        deploy.load_stage01_receipt(path, str(receipt["release_id"]), expected_hash)


def test_receipt_semantics_reject_expected_missing_hash_drift(
    stage01_receipt: tuple[Path, str, dict[str, object]], deploy: ModuleType,
) -> None:
    path, _, receipt = stage01_receipt
    receipt["predecessor_expected_missing_paths_sha256"] = "0" * 64
    path.write_text(json.dumps(receipt, sort_keys=True) + "\n")
    with pytest.raises(deploy.DeploymentError, match="expected-missing path hash"):
        deploy.load_stage01_receipt(path, str(receipt["release_id"]), digest(path))


def test_receipt_semantics_reject_container_fingerprint_drift(
    stage01_receipt: tuple[Path, str, dict[str, object]], deploy: ModuleType,
) -> None:
    path, _, receipt = stage01_receipt
    receipt["predecessor_container_stable_fingerprint_after"] = "2" * 64
    path.write_text(json.dumps(receipt, sort_keys=True) + "\n")
    with pytest.raises(deploy.DeploymentError, match="stable fingerprint"):
        deploy.load_stage01_receipt(path, str(receipt["release_id"]), digest(path))


def test_receipt_semantics_reject_systemd_state_hash_drift(
    stage01_receipt: tuple[Path, str, dict[str, object]], deploy: ModuleType,
) -> None:
    path, _, receipt = stage01_receipt
    receipt["predecessor_systemd_states_sha256"] = "0" * 64
    path.write_text(json.dumps(receipt, sort_keys=True) + "\n")
    with pytest.raises(deploy.DeploymentError, match="systemd state hash"):
        deploy.load_stage01_receipt(path, str(receipt["release_id"]), digest(path))


def test_receipt_semantics_reject_volume_inspect_descriptor_drift(
    stage01_receipt: tuple[Path, str, dict[str, object]], deploy: ModuleType,
) -> None:
    path, _, receipt = stage01_receipt
    artifact = Path(str(receipt["predecessor_volume_inspect"]))
    value = json.loads(artifact.read_text())
    value[0]["Driver"] = "tampered"
    artifact.write_text(json.dumps(value, sort_keys=True) + "\n")
    receipt["predecessor_volume_inspect_sha256"] = digest(artifact)
    path.write_text(json.dumps(receipt, sort_keys=True) + "\n")
    with pytest.raises(deploy.DeploymentError, match="volume inspect"):
        deploy.load_stage01_receipt(path, str(receipt["release_id"]), digest(path))


def test_receipt_semantics_reject_compose_observation_drift(
    stage01_receipt: tuple[Path, str, dict[str, object]], deploy: ModuleType,
) -> None:
    path, _, receipt = stage01_receipt
    artifact = Path(str(receipt["predecessor_compose_snapshot"]))
    value = json.loads(artifact.read_text())
    value[EXPECTED_MISSING_PATH] = dict(value["/opt/ispindel-dashboard/docker-compose.yml"])
    artifact.write_text(json.dumps(value, sort_keys=True) + "\n")
    receipt["predecessor_compose_snapshot_sha256"] = digest(artifact)
    path.write_text(json.dumps(receipt, sort_keys=True) + "\n")
    with pytest.raises(deploy.DeploymentError, match="coverage is invalid|exactly match"):
        deploy.load_stage01_receipt(path, str(receipt["release_id"]), digest(path))


def test_stable_container_fingerprint_ignores_state_but_not_mounts(deploy: ModuleType) -> None:
    base: dict[str, object] = {
        "Id": "id", "Image": "sha256:" + "1" * 64, "Name": "/ispindel-dashboard",
        "Config": {"Image": "exact"}, "HostConfig": {"ReadonlyRootfs": True},
        "Mounts": [{"Type": "volume", "Name": "data"}], "State": {"Health": {"Status": "healthy"}},
    }
    changed_state = json.loads(json.dumps(base))
    changed_state["State"] = {"Health": {"Status": "starting"}}
    assert deploy.stable_container_fingerprint(base) == deploy.stable_container_fingerprint(changed_state)
    changed_mount = json.loads(json.dumps(base))
    changed_mount["Mounts"] = []
    assert deploy.stable_container_fingerprint(base) != deploy.stable_container_fingerprint(changed_mount)


def test_wrong_release_confirmation_is_rejected(deploy: ModuleType) -> None:
    args = type("Args", (), {"confirm_release_id": "wrong", "execute": True, "dry_run": False})()
    with pytest.raises(deploy.DeploymentError, match="must exactly match"):
        deploy.require_execution(args, "20260803T011203Z-3607db5a9611")


def test_execute_and_dry_run_are_mutually_exclusive(deploy: ModuleType) -> None:
    args = type("Args", (), {
        "confirm_release_id": "20260803T011203Z-3607db5a9611", "execute": True, "dry_run": True,
    })()
    with pytest.raises(deploy.DeploymentError, match="exactly one"):
        deploy.require_execution(args, "20260803T011203Z-3607db5a9611")


def test_stage05_never_invokes_database_restore() -> None:
    source = DEPLOY_PATH.read_text()
    assert "restore-production.py" not in source
    assert "database_restored\": True" not in source
    assert "database_restored\": False" in source
    assert "file:/data/ispindel.db?mode=ro" in source
    assert "docker inspect" in source
    assert "docker stop" in source
    assert "no such object:" in source
    assert "else 42" in source
    assert ".restore.sqlitecopy" not in source


def test_database_incompatibility_emits_stop_and_returns_20(
    deploy: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path,
) -> None:
    def incompatible(_args: object) -> int:
        raise deploy.DatabaseIncompatibleError(
            "STOP AND REQUIRE PRODUCTION-RESTORE PROCEDURE: incompatible"
        )

    monkeypatch.setattr(deploy, "stage05", incompatible)
    result = deploy.main([
        "stage-05", "--remote", "user@example", "--evidence-dir", str(tmp_path),
        "--confirm-release-id", "20260803T011203Z-3607db5a9611", "--dry-run",
        "--stage01-receipt", str(tmp_path / "receipt"),
        "--stage01-receipt-sha256", "0" * 64,
    ])
    captured = capsys.readouterr()
    assert result == 20
    assert "STOP AND REQUIRE PRODUCTION-RESTORE PROCEDURE" in captured.err
    assert "rollback_db_incompatible" in captured.err


def _make_fake_authorized_runner(tmp_path: Path) -> tuple[Path, dict[str, str], Path]:
    root = tmp_path / "fake-repo"
    deploy_dir = root / "scripts" / "deploy"
    deploy_dir.mkdir(parents=True)
    (root / "descriptors").mkdir()
    shutil.copy2(ROOT / "scripts" / "deploy" / "run-authorized-release.sh", deploy_dir)
    (root / "descriptors" / "RELEASE.json").write_text(json.dumps({"release_id": "20260803T011203Z-3607db5a9611"}))
    stage_script = """#!/usr/bin/env bash
set -euo pipefail
name=$(basename "$0")
case "$name" in
  01-*) mkdir -p "$ISPINDEL_DEPLOY_EVIDENCE_DIR/20260803T011203Z-3607db5a9611"; printf '{"backup_manifest":"/var/backups/ispindel-dashboard/test/manifest.json"}\\n' > "$ISPINDEL_DEPLOY_EVIDENCE_DIR/20260803T011203Z-3607db5a9611/01-backup-predecessor.json" ;;
  02-*) mkdir -p "$ISPINDEL_DEPLOY_EVIDENCE_DIR/20260803T011203Z-3607db5a9611"; printf '{}\\n' > "$ISPINDEL_DEPLOY_EVIDENCE_DIR/20260803T011203Z-3607db5a9611/02-rehearse-release.json" ;;
  03-*) exit "${FAKE_STAGE03_RC:-0}" ;;
  04-*)
    count_file=${FAKE_STAGE04_COUNT_FILE:?}
    count=0
    [[ ! -f $count_file ]] || count=$(<"$count_file")
    IFS=, read -r -a values <<< "${FAKE_STAGE04_SEQUENCE:-0}"
    last=$((${#values[@]} - 1))
    ((count <= last)) || count=$last
    rc=${values[$count]}
    printf '%s\\n' "$((count + 1))" > "$count_file"
    exit "$rc"
    ;;
  05-*) printf 'rollback\\n' >> "$ROLLBACK_LOG"; exit "${FAKE_ROLLBACK_RC:-0}" ;;
esac
"""
    for name in (
        "01-backup-predecessor.sh", "02-rehearse-release.sh", "03-promote-release.sh",
        "04-validate-live.sh", "05-rollback-application.sh",
    ):
        path = deploy_dir / name
        path.write_text(stage_script)
        path.chmod(0o755)
    rollback_log = tmp_path / "rollback.log"
    evidence = tmp_path / "evidence"
    env = os.environ.copy()
    env.update({
        "ISPINDEL_DEPLOY_REMOTE": "user@example", "ISPINDEL_REMOTE_RELEASE_ROOT": "/tmp/release",
        "ISPINDEL_OFFHOST": "backup@example:/archive",
        "ISPINDEL_DEPLOY_EVIDENCE_DIR": str(evidence), "ISPINDEL_BROWSER_COMMAND": "true",
        "ISPINDEL_SECURITY_COMMAND": "true", "ISPINDEL_ALERT_COMMAND": "true",
        "ISPINDEL_LAN_URL": "http://lan.example.test:18098",
        "ISPINDEL_TAILNET_URL": "https://tailnet.example.test",
        "ISPINDEL_TEST_INGEST_COMMAND": "true", "ISPINDEL_TEST_INGEST_POLICY": "remove",
        "ISPINDEL_EXPECTED_MISSING_CONTRACT_VERSION": CONTRACT_VERSION,
        "ROLLBACK_LOG": str(rollback_log),
        "FAKE_STAGE04_COUNT_FILE": str(tmp_path / "stage04-count"),
        "ISPINDEL_DEPLOY_RETRY_DELAY_SECONDS": "0",
    })
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_ssh = fake_bin / "ssh"
    fake_ssh.write_text("#!/usr/bin/env bash\nexit \"${FAKE_DIRECT_RC:-0}\"\n")
    fake_ssh.chmod(0o755)
    env["PATH"] = f"{fake_bin}:{env['PATH']}"
    return deploy_dir / "run-authorized-release.sh", env, rollback_log


@pytest.mark.parametrize(
    ("stage03", "stage04_sequence", "direct_rc", "expected_exit", "rollback"),
    [
        (20, "0", 0, 20, True),
        (7, "0", 0, 7, True),
        (255, "0", 0, 255, True),
        (0, "1,1", 1, 20, True),
        (0, "1,1", 0, 1, False),
        (0, "1,0", 1, 0, False),
        (0, "0", 1, 0, False),
        (0, "1,1", 255, 1, False),
    ],
)
def test_rollback_requires_repeated_failure_and_failed_direct_container_check(
    tmp_path: Path, stage03: int, stage04_sequence: str, direct_rc: int,
    expected_exit: int, rollback: bool,
) -> None:
    runner, env, rollback_log = _make_fake_authorized_runner(tmp_path)
    env["FAKE_STAGE03_RC"] = str(stage03)
    env["FAKE_STAGE04_SEQUENCE"] = stage04_sequence
    env["FAKE_DIRECT_RC"] = str(direct_rc)
    result = subprocess.run(
        [str(runner), "--confirm-release-id", "20260803T011203Z-3607db5a9611", "--execute"],
        env=env, capture_output=True, text=True, check=False,
    )
    assert result.returncode == expected_exit
    assert rollback_log.exists() is rollback
    if rollback:
        assert rollback_log.read_text() == "rollback\n"


def test_stage03_rollback_failure_is_fail_closed(tmp_path: Path) -> None:
    runner, env, rollback_log = _make_fake_authorized_runner(tmp_path)
    env.update({"FAKE_STAGE03_RC": "7", "FAKE_ROLLBACK_RC": "9"})
    result = subprocess.run(
        [str(runner), "--confirm-release-id", "20260803T011203Z-3607db5a9611", "--execute"],
        env=env, capture_output=True, text=True, check=False,
    )
    assert result.returncode == 20
    assert rollback_log.read_text() == "rollback\n"
    assert "stage rc=7 rollback rc=9" in result.stderr


def test_all_five_real_dry_run_plans_are_safe_and_complete(
    tmp_path: Path, stage01_receipt: tuple[Path, str, dict[str, object]], deploy: ModuleType,
) -> None:
    _require_release_manifest()
    receipt_path, receipt_sha, receipt = stage01_receipt
    release_id = str(receipt["release_id"])
    release_manifest = json.loads(RELEASE_MANIFEST.read_text())
    predecessor_artifact = next(
        item for item in release_manifest["artifacts"]
        if item["path"] == "source/descriptors/predecessor-production.json"
    )
    if predecessor_artifact["sha256"] != digest(PREDECESSOR):
        pytest.skip("pre-freeze release manifest does not bind the current predecessor descriptor")
    helper_artifact = next(
        item for item in release_manifest["artifacts"]
        if item["path"] == "source/scripts/deploy/ispindel-root-helper"
    )
    if helper_artifact["sha256"] != digest(ROOT / "scripts/deploy/ispindel-root-helper"):
        pytest.skip("pre-freeze release manifest does not bind the current root helper")
    release_manifest_sha = hashlib.sha256(RELEASE_MANIFEST.read_bytes()).hexdigest()
    release = json.loads(RELEASE_MANIFEST.read_text())
    stage02_receipt_path = tmp_path / "02-rehearse-release.json"
    stage02_receipt_path.write_text(json.dumps({
        "schema": "ispindel-deploy-stage-receipt/v1", "stage": "02-rehearse-release",
        "release_id": release_id, "result": "PASS",
        "source_endpoint": deploy.SOURCE_DOCKER_HOST, "target_endpoint": deploy.TARGET_DOCKER_HOST,
        "production_mutated": False, "release_manifest_sha256": release_manifest_sha,
        "image_id": release["image"]["id"], "remote_release_root": "/tmp/release",
        "target_image_loaded": True, "network_none_verified": True,
    }))
    stage02_receipt_sha = hashlib.sha256(stage02_receipt_path.read_bytes()).hexdigest()
    common = [
        "--release-manifest", str(RELEASE_MANIFEST), "--remote", "user@example",
        "--remote-root", "/opt/ispindel-dashboard",
        "--evidence-dir", str(tmp_path / "evidence"), "--confirm-release-id", release_id, "--dry-run",
    ]
    wrappers = ROOT / "scripts" / "deploy"
    commands = [
        [str(wrappers / "01-backup-predecessor.sh"), *common, "--offhost", "backup@example:/archive"],
        [str(wrappers / "02-rehearse-release.sh"), *common, "--remote-release-root", "/tmp/release",
         "--backup-manifest", "/tmp/backup/index.json"],
        [str(wrappers / "03-promote-release.sh"), *common, "--stage01-receipt", str(receipt_path),
         "--stage01-receipt-sha256", receipt_sha, "--stage02-receipt", str(stage02_receipt_path),
         "--stage02-receipt-sha256", stage02_receipt_sha, "--remote-release-root", "/tmp/release",
         "--lan-url", "http://lan.example.test:18098", "--tailnet-url", "https://tailnet.example.test"],
        [str(wrappers / "04-validate-live.sh"), *common, "--browser-command", "true",
         "--security-command", "true", "--alert-command", "true", "--test-ingest-command", "true",
         "--test-ingest-policy", "remove", "--lan-url", "http://lan.example.test:8098",
         "--tailnet-url", "https://tailnet.example.test"],
        [str(wrappers / "05-rollback-application.sh"), *common, "--stage01-receipt", str(receipt_path),
         "--stage01-receipt-sha256", receipt_sha],
    ]
    plans: list[dict[str, object]] = []
    for index, command in enumerate(commands, start=1):
        result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, check=False)
        assert result.returncode == 0, result.stderr
        plan = json.loads(result.stdout)
        assert plan["stage"].startswith(f"0{index}-")
        assert plan["release_id"] == release_id
        assert plan["commands"]
        for remote in cast(list[str], plan["commands"]):
            assert remote.startswith("ssh ")
            assert "/bin/bash -lc" in remote
            assert "cd /opt/ispindel-dashboard &&" in remote
        plans.append(plan)
    stage01 = "\n".join(cast(list[str], plans[0]["commands"]))
    stage02 = "\n".join(cast(list[str], plans[1]["commands"]))
    stage03 = "\n".join(cast(list[str], plans[2]["commands"]))
    stage04 = "\n".join(cast(list[str], plans[3]["commands"]))
    stage05 = "\n".join(cast(list[str], plans[4]["commands"]))
    assert "tar --exclude=.git --exclude=.venv --exclude=dist --exclude=evidence --exclude=__pycache__ -cf - ." in stage01
    assert f"docker --host {deploy.TARGET_DOCKER_HOST} run --rm --network none --pull never" in stage02
    assert f"DOCKER_HOST={deploy.TARGET_DOCKER_HOST}" in stage02
    assert "docker image load" in stage02
    assert "docker image load" not in stage03
    assert "--evidence-dir /opt/ispindel-dashboard/evidence/rehearsal" in stage02
    assert "--evidence-dir /tmp/release/evidence/rehearsal" not in stage02
    assert f"DOCKER_HOST={deploy.TARGET_DOCKER_HOST}" in stage03
    assert f"docker --host {deploy.SOURCE_DOCKER_HOST} rename" in stage03
    assert "--project-name ispindel-promote-" in stage03
    assert "ISPINDEL_DATA_VOLUME=ispindel-dashboard_ispindel-data" in stage03
    assert "predecessor-202" in stage03
    assert "{{.Id}}|{{.Name}}|{{.State.Running}}" in stage03
    assert "docker run --rm --network none --pull never" in stage03
    assert stage03.count("/usr/local/sbin/ispindel-root-helper") == 4
    assert "volume-create ispindel-dashboard_ispindel-data" in stage03
    assert "volume-seed" in stage03 and "volume-inspect ispindel-dashboard_ispindel-data" in stage03
    assert "type=volume,src=ispindel-dashboard_ispindel-data,dst=/target" in stage03
    assert "type=volume,src=ispindel-dashboard_ispindel-data,dst=/target,readonly" not in stage03
    assert f"ISPINDEL_IMAGE_REF=ispindel-release:{RELEASE_ID}" in stage03
    assert "--no-build --pull never" in stage03
    assert "--build" not in stage03
    assert f"docker --host {deploy.TARGET_DOCKER_HOST} inspect --format" in stage03
    assert "/etc/ispindel/Caddyfile" in stage03
    assert "/opt/ispindel-dashboard" in stage03
    assert "scripts/backup-production.py" in stage03
    assert "/usr/local/libexec/ispindel/" in stage03
    assert "PRODUCTION_ENV_IMAGE_REF_OK" in stage03
    assert "PRODUCTION_ENV_SECRETS_PATH_OK" in stage03
    assert "verify-production-secrets.py" in stage03
    assert "sudo env ISPINDEL_MODE=production ISPINDEL_SECRETS_DIR=/etc/ispindel/secrets ISPINDEL_GID=954 python3 /usr/local/libexec/ispindel/verify-production-secrets.py" in stage03
    assert "sudo chown 0:954 /etc/ispindel/secrets /etc/ispindel/secrets/ingest-tokens.json" in stage03
    assert "sudo chmod 0750 /etc/ispindel/secrets" in stage03
    assert "sudo chmod 0640 /etc/ispindel/secrets/ingest-tokens.json" in stage03
    assert "ispindel-secrets-verify.service" in stage03
    assert "if sudo systemctl cat -- ispindel-caddy.service >/dev/null 2>&1; then" in stage03
    assert "sudo systemctl disable --now -- ispindel-caddy.service" in stage03
    assert "systemctl enable --now ispindel-caddy.service" not in stage03
    assert "rsync -a --delete --exclude=.git/ --exclude=.venv/ --exclude=dist/ --exclude=evidence/" in stage03
    assert "sudo rsync -a --delete --chown=root:root" not in stage03
    assert "sha256sum" in stage04 and "/static/" in stage04
    assert "systemctl enable --now ispindel-caddy.service" in stage04
    assert f"docker --host {deploy.SOURCE_DOCKER_HOST} start {'a' * 64}" in stage05
    assert "docker compose" not in stage05
    assert "tar -xf - -C" not in stage05
    assert "rsync -a --delete" not in stage05
    assert "restore-production.py" not in stage05
    assert ".restore.sqlitecopy" not in stage05
    assert "os.replace('/data" not in stage05


def test_authorized_runner_derives_backup_manifest_from_stage01_receipt() -> None:
    source = (ROOT / "scripts/deploy/run-authorized-release.sh").read_text()
    assert "ISPINDEL_BACKUP_MANIFEST" not in source
    assert "[\"backup_manifest\"]" in source
    assert '--backup-manifest "$backup_manifest"' in source


def test_authorized_runner_defaults_stage03_backup_under_helper_allowlist() -> None:
    source = (ROOT / "scripts/deploy/run-authorized-release.sh").read_text()
    assert 'stage03_backup_root=${ISPINDEL_STAGE03_BACKUP_ROOT:-$remote_root/evidence/$release_id/backup}' in source
    assert '/path/to/staging/$release_id/backup' not in source


def test_authorized_runner_requires_and_passes_two_key_amendment_activation() -> None:
    source = (ROOT / "scripts/deploy/run-authorized-release.sh").read_text()
    assert '${ISPINDEL_EXPECTED_MISSING_CONTRACT_VERSION:?' in source
    assert "--allow-expected-missing" in source
    assert '--expected-missing-contract-version "$ISPINDEL_EXPECTED_MISSING_CONTRACT_VERSION"' in source
    stage01_block = source.split('"$SCRIPT_DIR/01-backup-predecessor.sh"', 1)[1].split('stage01_sha256=', 1)[0]
    assert stage01_block.count("--allow-expected-missing") == 1
    assert stage01_block.count("--expected-missing-contract-version") == 1


def test_authorized_runner_stops_before_stages_when_contract_version_is_missing(tmp_path: Path) -> None:
    runner, env, rollback_log = _make_fake_authorized_runner(tmp_path)
    env.pop("ISPINDEL_EXPECTED_MISSING_CONTRACT_VERSION")
    result = subprocess.run(
        [str(runner), "--confirm-release-id", "20260803T011203Z-3607db5a9611", "--execute"],
        env=env, capture_output=True, text=True, check=False,
    )
    assert result.returncode != 0
    assert "ISPINDEL_EXPECTED_MISSING_CONTRACT_VERSION" in result.stderr
    assert not rollback_log.exists()


def test_shell_wrappers_are_thin_and_database_restore_free() -> None:
    deploy_dir = ROOT / "scripts" / "deploy"
    for path in sorted(deploy_dir.glob("0[1-5]-*.sh")):
        source = path.read_text()
        assert "exec python3" in source
        assert "restore-production" not in source
    runner = (deploy_dir / "run-authorized-release.sh").read_text()
    assert "if [[ $rc -ne 0 ]]" in runner
    assert "stage01_sha256=$(sha256sum" in runner
    assert "stage02_sha256=$(sha256sum" in runner
