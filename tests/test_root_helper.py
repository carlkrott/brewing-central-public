from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[1]
HELPER_PATH = ROOT / "scripts" / "deploy" / "ispindel-root-helper"
SUDOERS_PATH = ROOT / "ops" / "sudoers" / "ispindel-root-helper"
ENDPOINT = "unix:///var/run/docker.sock"
PRODUCTION_VOLUME = "ispindel-dashboard_ispindel-data"
IMAGE = "sha256:" + "a" * 64


def load_helper() -> ModuleType:
    loader = importlib.machinery.SourceFileLoader("ispindel_root_helper", str(HELPER_PATH))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def helper() -> ModuleType:
    return load_helper()


def plan(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(HELPER_PATH), "--plan", *arguments],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def test_closed_verbs_reject_arbitrary_command_endpoint_and_extra_arguments() -> None:
    for arguments in (
        ("shell",),
        ("volume-inspect", PRODUCTION_VOLUME, "--host", "tcp://attacker:2375"),
        ("systemctl-start-caddy", "ispindel-caddy.service", ";id"),
        ("install-release-files", "/tmp/attacker"),
    ):
        result = plan(*arguments)
        assert result.returncode == 2, (arguments, result.stdout, result.stderr)


def test_volume_names_are_exactly_constrained(helper: ModuleType) -> None:
    allowed = {
        PRODUCTION_VOLUME,
        "ispindel-rehearsal-volume-deadbeef",
    }
    for name in allowed:
        assert helper.validate_volume_name(name, allow_production=True) == name
    for name in (
        "ispindel-dashboard_ispindel-data-copy",
        "ispindel-rehearsal-volume-deadbee",
        "ispindel-rehearsal-container-deadbeef",
        "ispindel-rehearsal-volume-deadbeef;id",
        "../ispindel-rehearsal-volume-deadbeef",
    ):
        with pytest.raises(helper.HelperError):
            helper.validate_volume_name(name, allow_production=True)


def test_destructive_nonce_verbs_cannot_target_production_or_wrong_resource() -> None:
    assert plan("volume-remove-nonce", "ispindel-rehearsal-volume-deadbeef").returncode == 0
    assert plan("container-remove-nonce", "ispindel-rehearsal-container-deadbeef").returncode == 0
    for arguments in (
        ("volume-remove-nonce", PRODUCTION_VOLUME),
        ("volume-remove-nonce", "ispindel-rehearsal-container-deadbeef"),
        ("container-remove-nonce", "ispindel-rehearsal-volume-deadbeef"),
        ("container-remove-nonce", "ispindel-rehearsal-container-DEADBEEF"),
    ):
        assert plan(*arguments).returncode == 2


def test_all_docker_plans_pin_the_internal_unix_socket() -> None:
    commands = (
        ("volume-inspect", PRODUCTION_VOLUME),
        ("volume-create", PRODUCTION_VOLUME),
        ("volume-remove-nonce", "ispindel-rehearsal-volume-deadbeef"),
        ("container-remove-nonce", "ispindel-rehearsal-container-deadbeef"),
        (
            "volume-seed",
            "/var/backups/ispindel-dashboard/20260804T010203Z-deadbeef/ispindel-20260804T010203Z-deadbeef.db",
            PRODUCTION_VOLUME,
            IMAGE,
        ),
    )
    for arguments in commands:
        result = plan(*arguments)
        assert result.returncode == 0, (arguments, result.stderr)
        payload = json.loads(result.stdout)
        assert payload["commands"]
        for command in payload["commands"]:
            assert command[:3] == ["/usr/bin/docker", "--host", ENDPOINT]
        rendered = json.dumps(payload)
        assert "tcp://" not in rendered
        assert "DOCKER_HOST" not in rendered


def test_seed_rejects_bad_path_image_target_and_endpoint_input() -> None:
    good_source = "/var/backups/ispindel-dashboard/20260804T010203Z-deadbeef/ispindel-20260804T010203Z-deadbeef.db"
    rejected = (
        ("volume-seed", "relative/ispindel.db", PRODUCTION_VOLUME, IMAGE),
        ("volume-seed", "/var/backups/ispindel-dashboard/../shadow/ispindel.db", PRODUCTION_VOLUME, IMAGE),
        ("volume-seed", "/tmp/ispindel.db", PRODUCTION_VOLUME, IMAGE),
        ("volume-seed", "/var/backups/ispindel-dashboard/generation/not-the-db", PRODUCTION_VOLUME, IMAGE),
        ("volume-seed", good_source, PRODUCTION_VOLUME, "candidate:latest"),
        ("volume-seed", good_source, PRODUCTION_VOLUME, "sha256:" + "A" * 64),
        ("volume-seed", good_source, PRODUCTION_VOLUME, IMAGE, "tcp://attacker:2375"),
    )
    for arguments in rejected:
        assert plan(*arguments).returncode == 2, arguments


def test_seed_plan_never_forms_source_chown_remove_or_prune() -> None:
    source = "/opt/ispindel-dashboard/evidence/generation/ispindel.db"
    result = plan("volume-seed", source, PRODUCTION_VOLUME, IMAGE)
    assert result.returncode == 0, result.stderr
    command = json.loads(result.stdout)["commands"][0]
    rendered = " ".join(command)
    assert f"type=bind,src={source},dst=/source/ispindel.db,readonly" in command
    assert f"type=volume,src={PRODUCTION_VOLUME},dst=/target" in command
    assert "/target/ispindel.db" in rendered
    assert "chown /source" not in rendered
    assert "chmod /source" not in rendered
    assert "unlink('/source" not in rendered
    assert "rmtree" not in rendered
    assert "prune" not in rendered
    assert "/source/ispindel.db" in rendered
    assert "os.chown(dst,10001,10001)" in rendered
    assert "os.chmod(dst,0o600)" in rendered
    assert "PRAGMA journal_mode=WAL" in rendered
    assert "PRAGMA integrity_check" in rendered
    assert "os.unlink(side)" in rendered
    assert "mode=ro" in rendered
    assert "ispindel.db-wal" in rendered and "ispindel.db-shm" in rendered
    assert "os.chown('/target',10001,10001)" in rendered
    assert "os.chmod('/target',0o700)" in rendered


def test_seed_code_is_valid_python() -> None:
    helper = load_helper()
    compile(helper.SEED_CODE, "<ispindel-root-helper-seed>", "exec")


def test_systemctl_unit_is_exact_and_no_shell_is_used() -> None:
    for verb, action in (("systemctl-start-caddy", "start"), ("systemctl-stop-caddy", "stop")):
        result = plan(verb, "ispindel-caddy.service")
        assert result.returncode == 0, result.stderr
        command = json.loads(result.stdout)["commands"][0]
        assert command == ["/usr/bin/systemctl", action, "--", "ispindel-caddy.service"]
    for unit in ("caddy.service", "ispindel-caddy.service;id", "ispindel-caddy.service another.service"):
        assert plan("systemctl-start-caddy", unit).returncode == 2


def test_permanent_helper_cannot_install_files_or_modify_sudo_authority() -> None:
    assert plan("install-release-files").returncode == 2
    assert plan("remove-temporary-authority").returncode == 2


def test_sudoers_grants_only_installed_helper() -> None:
    text = SUDOERS_PATH.read_text(encoding="utf-8")
    active = [line.strip() for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")]
    assert active == [
        "ispindel ALL=(root) NOPASSWD: /usr/local/sbin/ispindel-root-helper *",
    ]
    assert "/bin/sh" not in text
    assert "/bin/bash" not in text
    assert " ALL" not in active[0].split("NOPASSWD:", 1)[1]
