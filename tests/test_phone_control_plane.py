from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
ANDROID = ROOT / "ops" / "android"

# Dynamically import phone-management-dispatch without modifying sys.path permanently
dispatch_spec = importlib.util.spec_from_file_location(
    "phone_management_dispatch",
    ANDROID / "phone-management-dispatch.py",
)
assert dispatch_spec is not None and dispatch_spec.loader is not None
dispatch_mod = importlib.util.module_from_spec(dispatch_spec)
dispatch_spec.loader.exec_module(dispatch_mod)


# ---- sshd_config.example contracts ---------------------------------------
def test_sshd_config_contract_negative_and_hardening_settings() -> None:
    config_text = (ANDROID / "sshd_config.example").read_text()

    # Port is rendered into the private effective config; the template has no
    # additive network directives.
    assert "Port 8022" not in config_text
    assert "Include " not in config_text
    assert "PasswordAuthentication no" in config_text
    assert "KbdInteractiveAuthentication no" in config_text
    assert "ChallengeResponseAuthentication no" in config_text
    assert "PermitEmptyPasswords no" in config_text
    assert "PubkeyAuthentication yes" in config_text
    assert "AuthenticationMethods publickey" in config_text
    assert "PermitRootLogin no" in config_text
    assert "MaxAuthTries 3" in config_text

    # Bounded client alive
    assert "ClientAliveInterval 60" in config_text
    assert "ClientAliveCountMax 3" in config_text

    # Forwarding / tunneling / PTY / user rc all disabled
    assert "AllowAgentForwarding no" in config_text
    assert "AllowTcpForwarding no" in config_text
    assert "AllowStreamLocalForwarding no" in config_text
    assert "GatewayPorts no" in config_text
    assert "X11Forwarding no" in config_text
    assert "PermitTunnel no" in config_text
    assert "PermitUserRC no" in config_text
    assert "PermitTTY no" in config_text
    assert "DisableForwarding yes" in config_text

    # Forced-command dispatcher entry point
    assert "ForceCommand <TERMUX_APP_ROOT>/brewing-central/control/bin/phone-management-dispatch.py" in config_text

    # No hardcoded secrets, usernames, or live keys
    assert "ssh-rsa" not in config_text
    assert "BEGIN OPENSSH PRIVATE KEY" not in config_text
    assert "100.64." not in config_text  # No live Tailscale IPs


# ---- phone-management-dispatch.py contracts ------------------------------
def test_dispatcher_rejects_unknown_verbs() -> None:
    with pytest.raises(SystemExit) as exc:
        dispatch_mod._parse_argv(["bash"])
    assert exc.value.code == 2

    with pytest.raises(SystemExit) as exc:
        dispatch_mod._parse_argv(["exec", "ls"])
    assert exc.value.code == 2


def test_dispatcher_rejects_shell_metacharacters() -> None:
    dangerous_inputs = [
        "health;reboot",
        "health|ls",
        "health&&rm",
        "`whoami`",
        "$(id)",
        "logs;cat /etc/passwd",
        "test>out",
        "test<in",
    ]
    for bad in dangerous_inputs:
        with pytest.raises(SystemExit) as exc:
            dispatch_mod._parse_argv([bad])
        assert exc.value.code == 2


def test_dispatcher_rejects_traversal() -> None:
    traversal_inputs = [
        ["logs", "../../../etc/passwd", "10"],
        ["snapshot", "../secret.json"],
        ["export", "../../run"],
        ["cleanup", "../logs", "100"],
    ]
    for args in traversal_inputs:
        with pytest.raises(SystemExit) as exc:
            dispatch_mod._parse_argv(args)
        assert exc.value.code == 2


def test_dispatcher_rejects_extra_arbitrary_argv() -> None:
    # health takes no args
    with pytest.raises(SystemExit) as exc:
        dispatch_mod.cmd_health(["extra", "arg"])
    assert exc.value.code == 2

    # verify takes no args
    with pytest.raises(SystemExit) as exc:
        dispatch_mod.cmd_verify(["bogus"])
    assert exc.value.code == 2

    # snapshot takes exactly 1 arg
    with pytest.raises(SystemExit) as exc:
        dispatch_mod.cmd_snapshot(["battery-state.json", "extra"])
    assert exc.value.code == 2

    # export takes exactly 1 arg
    with pytest.raises(SystemExit) as exc:
        dispatch_mod.cmd_export(["camera", "extra"])
    assert exc.value.code == 2

    # cleanup takes exactly 2 args
    with pytest.raises(SystemExit) as exc:
        dispatch_mod.cmd_cleanup(["camera", "1024", "extra"])
    assert exc.value.code == 2

    # release takes exactly 1 arg
    with pytest.raises(SystemExit) as exc:
        dispatch_mod.cmd_release(["stage", "activate"])
    assert exc.value.code == 2


def test_dispatcher_rejects_unbounded_ranges() -> None:
    # logs tail lines bounded in [1, 200]
    with pytest.raises(SystemExit) as exc:
        dispatch_mod.cmd_logs(["phone-health-loop", "0"])
    assert exc.value.code == 2

    with pytest.raises(SystemExit) as exc:
        dispatch_mod.cmd_logs(["phone-health-loop", "500"])
    assert exc.value.code == 2

    with pytest.raises(SystemExit) as exc:
        dispatch_mod.cmd_logs(["phone-health-loop", "-1"])
    assert exc.value.code == 2

    with pytest.raises(SystemExit) as exc:
        dispatch_mod.cmd_logs(["phone-health-loop", "notanint"])
    assert exc.value.code == 2


def test_dispatcher_health_produces_redacted_evidence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dispatch_mod, "ROOT", tmp_path)
    monkeypatch.setattr(dispatch_mod, "RUN_DIR", tmp_path / "run")
    (tmp_path / "run").mkdir(parents=True, exist_ok=True)

    # Capture stdout
    import io
    fake_out = io.StringIO()
    monkeypatch.setattr("sys.stdout", fake_out)

    rc = dispatch_mod.cmd_health([])
    assert rc == 0
    output = json.loads(fake_out.getvalue())
    assert "checks" in output
    assert "ts" in output
    assert "sshd_alive" in output["checks"]
    assert "port_8022_listening" in output["checks"]
    assert "tailnet_bind_ip_present" in output["checks"]

    # Redaction: ensure no private info exists in health summary
    serialized = fake_out.getvalue()
    assert "id_ed25519" not in serialized
    assert "authorized_keys" not in serialized
    assert "password" not in serialized.lower()


def test_dispatcher_empty_forced_command_defaults_to_health(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dispatch_mod, "ROOT", tmp_path)
    monkeypatch.setattr(dispatch_mod, "RUN_DIR", tmp_path / "run")
    (tmp_path / "run").mkdir(parents=True, exist_ok=True)

    import io
    fake_out = io.StringIO()
    monkeypatch.setattr("sys.stdout", fake_out)

    assert dispatch_mod.main([]) == 0
    output = json.loads(fake_out.getvalue())
    assert output["checks"]["sshd_alive"] is False


def test_dispatcher_verify_prefers_effective_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    control_dir = tmp_path / "control"
    sshd_dir = control_dir / "sshd"
    sshd_dir.mkdir(parents=True)
    source_config = sshd_dir / "sshd_config"
    effective_config = sshd_dir / "sshd_config.effective"
    source_config.write_text("source")
    effective_config.write_text("effective")
    effective_config.chmod(0o600)
    monkeypatch.setattr(dispatch_mod, "CONTROL_DIR", control_dir)

    seen: list[list[str]] = []

    class Result:
        returncode = 0
        stderr = b""

    def fake_run(argv: list[str], **_kwargs: Any) -> Result:
        seen.append(argv)
        return Result()

    monkeypatch.setattr(dispatch_mod.subprocess, "run", fake_run)
    assert dispatch_mod.cmd_verify([]) == 0
    capsys.readouterr()
    assert seen == [["/data/data/com.termux/files/usr/bin/sshd", "-t", "-f", str(effective_config)]]


def test_dispatcher_resolves_cgnat_via_socket_route(monkeypatch: pytest.MonkeyPatch) -> None:
    tailnet_ip = ".".join(("100", "64", "0", "12"))
    probe_hosts: list[str] = []

    class FakeSocket:
        def __enter__(self) -> "FakeSocket":
            return self

        def __exit__(self, *_: Any) -> None:
            return None

        def settimeout(self, _seconds: int) -> None:
            return None

        def connect(self, endpoint: tuple[str, int]) -> None:
            probe_hosts.append(endpoint[0])

        def getsockname(self) -> tuple[str, int]:
            return tailnet_ip, 0

    monkeypatch.delenv("PHONE_SSHD_BIND_IP", raising=False)
    monkeypatch.delenv("PHONE_SSHD_TAILNET_PROBE_HOST", raising=False)
    monkeypatch.setattr(dispatch_mod.socket, "socket", lambda *_args: FakeSocket())

    assert dispatch_mod._resolve_tailnet_ipv4() == tailnet_ip
    assert probe_hosts == [".".join(("100", "64", "0", "1"))]


def test_dispatcher_release_verbs_write_markers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dispatch_mod, "ROOT", tmp_path)
    monkeypatch.setattr(dispatch_mod, "RUN_DIR", tmp_path / "run")
    (tmp_path / "run").mkdir(parents=True, exist_ok=True)

    import io
    monkeypatch.setattr("sys.stdout", io.StringIO())

    for verb in ("stage", "activate", "rollback"):
        rc = dispatch_mod.cmd_release([verb])
        assert rc == 0
        marker = tmp_path / "run" / f"release-{verb}.marker"
        assert marker.exists()
        content = json.loads(marker.read_text())
        assert content["verb"] == verb


def test_dispatcher_receipt_publish(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dispatch_mod, "ROOT", tmp_path)
    monkeypatch.setattr(dispatch_mod, "RUN_DIR", tmp_path / "run")
    (tmp_path / "run").mkdir(parents=True, exist_ok=True)

    import io
    monkeypatch.setattr("sys.stdout", io.StringIO())

    payload = json.dumps({"status": "staged", "commit": "132fcb0"})
    rc = dispatch_mod.cmd_receipt_publish(["release-stage", payload])
    assert rc == 0
    receipt = tmp_path / "run" / "release-stage.json"
    assert receipt.exists()
    assert json.loads(receipt.read_text())["commit"] == "132fcb0"


def test_dispatcher_rejects_symlinks_in_snapshot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dispatch_mod, "ROOT", tmp_path)
    monkeypatch.setattr(dispatch_mod, "DATA_DIR", tmp_path / "data")
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)

    outside_file = tmp_path / "outside.json"
    outside_file.write_text("secret")

    symlink_file = data_dir / "battery-state.json"
    symlink_file.symlink_to(outside_file)

    with pytest.raises(SystemExit) as exc:
        dispatch_mod.cmd_snapshot(["battery-state.json"])
    assert exc.value.code == 2


# ---- phone-sshd-loop.sh text contracts -----------------------------------
def test_phone_sshd_loop_text_contract() -> None:
    loop_text = (ANDROID / "phone-sshd-loop.sh").read_text()

    # Stable control path outside current
    assert "$ROOT/current" not in loop_text
    assert "current/" not in loop_text

    # Single-instance flock
    assert "exec 9>\"$LOCK_FILE\"" in loop_text
    assert "flock -n 9" in loop_text

    # Bounded tailnet wait
    assert "PHONE_SSHD_TAILNET_WAIT_MAX" in loop_text
    assert "tailnet-wait-exceeded" in loop_text

    # Effective config is rendered and validated before launch.
    assert '"$SSHD_BIN" -t -f "$EFFECTIVE_CONFIG"' in loop_text
    assert "config-test-failed" in loop_text

    # Command-bound PID detection and idempotence
    assert "/proc/$pid/cmdline" in loop_text
    assert "already-active" in loop_text

    # Exactly one sshd launched with FD 9 closed
    assert '9>&-' in loop_text
    assert "spawn-failed" not in loop_text
    assert "launch_pid=$!" in loop_text
    assert "connect_ex" in loop_text
    assert "sshd_config.effective" in loop_text


# ---- provision-phone-control-plane.sh text contracts ---------------------
def test_provision_control_plane_text_contract() -> None:
    # Contract: Do not execute provision script in tests, verify text contracts only.
    script_text = (ANDROID / "provision-phone-control-plane.sh").read_text()

    # Dry run by default
    assert "DRY_RUN=1" in script_text
    assert "APPLY=0" in script_text

    # --apply plus exact --confirm-target sentinel
    assert "--confirm-target" in script_text
    assert "CANONICAL_TERMUX_ROOT=\"/data/data/com.termux/files/home\"" in script_text
    assert "confirm-target-required" in script_text

    # Generates host keys on-device only
    assert "ssh-keygen -q -N \"\" -t ed25519" in script_text

    # File modes: 0700 ssh dir, 0600 sshd_config and host keys
    assert "chmod 0700" in script_text
    assert "chmod 0600" in script_text

    # Never prints private keys
    assert "cat " not in script_text or "cat <<USAGE" in script_text
    assert "ssh-keygen -lf" in script_text
