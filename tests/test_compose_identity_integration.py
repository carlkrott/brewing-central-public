"""Nonce-scoped Compose promotion/rollback identity integration test."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import uuid
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
IMAGE_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


def docker(*argv: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", *argv],
        check=check,
        capture_output=True,
        text=True,
    )


def compose(project: str, compose_file: Path, *argv: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return docker(
        "compose", "--project-name", project, "--file", str(compose_file), *argv,
        check=check,
    )


def inspect(container: str, template: str) -> str:
    return docker("inspect", "--format", template, container).stdout.strip()


def test_compose_project_switch_preserves_exact_predecessor_id(tmp_path: Path) -> None:
    """Prove Compose cannot destroy the standby when promotion changes projects.

    The test is opt-in because it creates disposable Docker containers, network,
    and volume resources. It is exercised in the release validation command with
    both immutable image IDs supplied.
    """
    old_image = os.environ.get("ISPINDEL_IDENTITY_OLD_IMAGE")
    new_image = os.environ.get("ISPINDEL_IDENTITY_NEW_IMAGE")
    if not old_image or not new_image:
        pytest.skip("set ISPINDEL_IDENTITY_OLD_IMAGE and ISPINDEL_IDENTITY_NEW_IMAGE")
    if shutil.which("docker") is None:
        pytest.skip("docker is unavailable")
    assert IMAGE_RE.fullmatch(old_image)
    assert IMAGE_RE.fullmatch(new_image)
    assert old_image != new_image

    nonce = uuid.uuid4().hex[:12]
    historical_project = f"ispindel-identity-history-{nonce}"
    promotion_project = f"ispindel-promote-identity-{nonce}"
    canonical = f"ispindel-identity-drill-{nonce}"
    standby = f"{canonical}-predecessor-{nonce}"
    quarantine = f"{canonical}-candidate-quarantine"
    volume = f"ispindel-identity-volume-{nonce}"
    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(
        """services:
  app:
    image: ${ISPINDEL_IMAGE_REF}
    container_name: ${ISPINDEL_CONTAINER_NAME}
    entrypoint: ["python3"]
    command: ["-c", "import time; time.sleep(300)"]
    volumes:
      - ispindel-data:/data
volumes:
  ispindel-data:
    name: ${ISPINDEL_DATA_VOLUME}
""",
        encoding="utf-8",
    )

    def clean() -> None:
        for name in (canonical, standby, quarantine):
            docker("rm", "-f", name, check=False)
        compose(historical_project, compose_file, "down", "--volumes", "--remove-orphans", check=False)
        compose(promotion_project, compose_file, "down", "--volumes", "--remove-orphans", check=False)
        docker("volume", "rm", volume, check=False)

    clean()
    try:
        docker("volume", "create", volume)
        os.environ.update({
            "ISPINDEL_CONTAINER_NAME": canonical,
            "ISPINDEL_DATA_VOLUME": volume,
            "ISPINDEL_IMAGE_REF": old_image,
        })
        compose(historical_project, compose_file, "up", "-d", "--pull", "never", check=True)
        old_id = inspect(canonical, "{{.Id}}")
        assert inspect(canonical, "{{.Image}}") == old_image
        assert inspect(canonical, "{{(index .Mounts 0).Name}}") == volume
        assert inspect(canonical, "{{index .Config.Labels \"com.docker.compose.project\"}}") == historical_project

        docker("stop", old_id)
        docker("rename", old_id, standby)
        assert inspect(old_id, "{{.Name}}|{{.State.Running}}") == f"/{standby}|false"

        # Compose sees the new project, not the predecessor's historical labels.
        os.environ["ISPINDEL_IMAGE_REF"] = new_image
        compose(promotion_project, compose_file, "up", "-d", "--pull", "never", check=True)
        new_id = inspect(canonical, "{{.Id}}")
        assert new_id != old_id
        assert inspect(canonical, "{{.Image}}") == new_image
        assert inspect(canonical, "{{(index .Mounts 0).Name}}") == volume
        assert inspect(canonical, "{{index .Config.Labels \"com.docker.compose.project\"}}") == promotion_project
        assert inspect(old_id, "{{.Name}}|{{.State.Running}}") == f"/{standby}|false"

        # The persisted production project is the project that owns the candidate.
        os.environ["COMPOSE_PROJECT_NAME"] = promotion_project
        compose(promotion_project, compose_file, "up", "-d", "--pull", "never", check=True)
        assert inspect(canonical, "{{.Id}}") == new_id

        docker("stop", new_id)
        docker("rename", new_id, quarantine)
        docker("rename", old_id, canonical)
        docker("start", old_id)
        assert inspect(canonical, "{{.Id}}|{{.Name}}|{{.State.Running}}|{{.Image}}|{{(index .Mounts 0).Name}}") == (
            f"{old_id}|/{canonical}|true|{old_image}|{volume}"
        )
        assert inspect(canonical, "{{index .Config.Labels \"com.docker.compose.project\"}}") == historical_project

        # Stage 05 restores the predecessor production.env snapshot. A later
        # systemd/Compose reconciliation must therefore use the historical
        # project and leave the exact predecessor ID untouched.
        os.environ["COMPOSE_PROJECT_NAME"] = historical_project
        os.environ["ISPINDEL_IMAGE_REF"] = old_image
        compose(historical_project, compose_file, "up", "-d", "--pull", "never", check=True)
        assert inspect(canonical, "{{.Id}}|{{.State.Running}}|{{.Image}}") == f"{old_id}|true|{old_image}"
    finally:
        os.environ.pop("ISPINDEL_CONTAINER_NAME", None)
        os.environ.pop("ISPINDEL_DATA_VOLUME", None)
        os.environ.pop("ISPINDEL_IMAGE_REF", None)
        os.environ.pop("COMPOSE_PROJECT_NAME", None)
        clean()


def test_compose_default_expansion_is_unset_only(tmp_path: Path):
    """P3 line-45 repair: ``${VAR-default}`` vs ``${VAR:-default}``.

    ``:-`` treats an empty string as set (still substitutes the default);
    ``-`` substitutes the default only when the variable is unset. The
    backup producer's path may legitimately be empty (an override that
    points at a deliberately-empty file), so the bind source must NOT
    be silently overwritten with the production default. The repaired
    line is the v1 contract: default applied only when the env var is
    absent; explicit override preserved.
    """
    compose = (ROOT / "docker-compose.yml").read_text()
    assert "${ISPINDEL_BACKUP_HEALTH_FILE-" in compose
    assert "${ISPINDEL_BACKUP_HEALTH_FILE:-" not in compose, (
        "compose still uses :- default form; that substitutes the production "
        "path even when the env var is set to an empty string"
    )


def test_compose_default_expansion_applies_when_env_var_is_unset(tmp_path: Path):
    """P3 line-45 repair: when the env var is unset, the default path
    must still be the bind source (the default is the production
    fallback when no override is supplied)."""
    evidence = tmp_path / "evidence"; evidence.mkdir()
    secrets = tmp_path / "secrets"; secrets.mkdir()
    env = os.environ | {
        "ISPINDEL_EVIDENCE_DIR": str(evidence),
        "ISPINDEL_SECRETS_DIR": str(secrets),
        "ISPINDEL_GID": "10001",
    }
    # Ensure ISPINDEL_BACKUP_HEALTH_FILE is genuinely unset for this run.
    env.pop("ISPINDEL_BACKUP_HEALTH_FILE", None)
    proc = subprocess.run(
        ["docker", "compose", "config", "--format", "json"],
        cwd=ROOT, env=env, text=True, capture_output=True, check=True,
    )
    service = json.loads(proc.stdout)["services"]["ispindel-dashboard"]
    mounts = {item["target"]: item for item in service["volumes"]}
    backup_mount = mounts["/backup-health/backup_health.json"]
    # The unset form ``${VAR-default}`` falls back to ``default`` ONLY
    # when the variable is absent, which is exactly this case.
    assert backup_mount["source"].endswith(
        "/var/backups/ispindel-dashboard/backup_health.json"
    )
    assert backup_mount["read_only"] is True
