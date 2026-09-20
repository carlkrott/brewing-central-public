from __future__ import annotations

import stat
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ANDROID = ROOT / "ops" / "android"


def test_android_phone_scripts_are_valid_bash() -> None:
    scripts = sorted(ANDROID.glob("*.sh"))
    assert scripts
    for script in scripts:
        assert script.stat().st_mode & stat.S_IXUSR
        subprocess.run(["bash", "-n", str(script)], check=True)


def test_phone_stack_startup_is_serialized() -> None:
    script = (ANDROID / "start-phone-stack.sh").read_text()

    assert 'exec 9>"$ROOT/run/start-phone-stack.lock"' in script
    assert "flock -w 90 9" in script
    assert "start_lock=timeout" in script
    assert script.count("9>&-") == 3


def test_phone_stack_pid_files_are_command_bound() -> None:
    start = (ANDROID / "start-phone-stack.sh").read_text()
    stop = (ANDROID / "stop-phone-stack.sh").read_text()

    assert '/proc/$pid/cmdline' in start
    assert "local expected=$2" in start
    assert "command.startswith(prefix)" in stop
    assert "os.geteuid()" in stop
    assert "signal.SIGKILL" in stop


def test_phone_stack_requires_tool_free_structured_model_route() -> None:
    start = (ANDROID / "start-phone-stack.sh").read_text()
    example = (ANDROID / "phone.env.example").read_text()

    assert 'ASSISTANT_STRUCTURED_MODEL_URL:?ASSISTANT_STRUCTURED_MODEL_URL is required' in start
    assert "ASSISTANT_STRUCTURED_MODEL_URL=http://gemma.example.test:8095" in example
    assert "ASSISTANT_STRUCTURED_MODEL_TIMEOUT_S=120" in example


def test_termux_boot_dispatches_into_the_main_termux_context() -> None:
    script = (ANDROID / "termux-boot-start.sh").read_text()

    assert "set -uo pipefail" in script
    assert "umask 077" in script
    assert "com.termux/com.termux.app.RunCommandService" in script
    assert "com.termux.RUN_COMMAND" in script
    assert "com.termux.RUN_COMMAND_PATH" in script
    assert "termux-main-start.sh" in script
    assert "com.termux.RUN_COMMAND_BACKGROUND" in script
    assert "'true'" in script
    assert "dispatch=accepted" in script
    assert "termux-job-scheduler" not in script

    # Wave 1 independent control plane dispatch before app dispatch
    ctrl_dispatch = script.find('--es com.termux.RUN_COMMAND_PATH "$CONTROL_LOOP"')
    main_dispatch = script.find('--es com.termux.RUN_COMMAND_PATH "$ROOT/current/ops/android/termux-main-start.sh"')
    assert ctrl_dispatch != -1
    assert main_dispatch != -1
    assert ctrl_dispatch < main_dispatch
    assert "$ROOT/control/phone-sshd-loop.sh" in script


def test_main_termux_launcher_is_bounded_and_reboot_safe() -> None:
    script = (ANDROID / "termux-main-start.sh").read_text()

    assert "set -uo pipefail" in script
    assert "umask 077" in script
    assert 'rm -f "$ROOT/run/zeroclaw.pid"' in script
    assert '"$ROOT/run/dashboard.pid"' in script
    assert '"$ROOT/run/camera-observer.pid"' in script
    assert "for attempt in $(seq 1 60)" in script
    assert "sleep 10" in script
    assert '"$ROOT/current/ops/android/start-phone-stack.sh"' in script
    assert "boot=ready" in script
    assert "boot=failed attempts=60" in script

    # Non-fatal control-plane preflight
    assert "boot=sshd-preflight" in script
    assert "phone-management-dispatch.py" in script
    assert "verify" in script


def test_phone_health_loop_records_redacted_control_plane_evidence() -> None:
    script = (ANDROID / "phone-health-loop.sh").read_text()

    assert "control-plane-health.json" in script
    assert "phone-management-dispatch.py" in script
    # Listener readiness must use the app-UID-safe socket probe rather than
    # privileged proc/netlink state.
    assert "connect_ex" in script
    assert '"/proc/net/tcp"' not in script
    assert "PHONE_SSHD_BIN" in script
    # No secret/fingerprint/username leakage
    assert "authorized_keys" not in script
    assert "id_ed25519" not in script
    # phone.env mode 0600 is enforced before sourcing
    assert "phone-env-mode-bad" in script
    assert "expected=0600" in script


# ---- W1 control-plane continuation pins ---------------------------------
def test_sshd_config_template_has_authorized_keys_and_no_insecure_bind() -> None:
    template = (ANDROID / "sshd_config.example").read_text()

    # Single AuthorizedKeysFile at the stable control path.
    assert (
        "AuthorizedKeysFile <TERMUX_APP_ROOT>/brewing-central/control/sshd/automation/authorized_keys"
        in template
    )
    # No ListenAddress directive referencing 0.0.0.0 (Tailnet binding is
    # supplied by the loop). Comments referencing 0.0.0.0 to disclaim it
    # are fine; we only forbid actual ListenAddress lines.
    for line in template.splitlines():
        stripped = line.strip()
        if stripped.startswith("#") or not stripped:
            continue
        assert not stripped.startswith("ListenAddress"), (
            f"template must not set ListenAddress directly: {line!r}"
        )
    # Bounded handshake.
    assert "MaxStartups 3:50:10" in template


def test_provision_requires_automation_pubkey_and_validates_ed25519() -> None:
    script = (ANDROID / "provision-phone-control-plane.sh").read_text()

    # Explicit --automation-pubkey-file flag.
    assert "--automation-pubkey-file" in script
    # Fail closed when missing under --apply.
    assert "automation-pubkey-required" in script
    # Validates as an ssh-ed25519 public key without printing bytes.
    assert "ssh-ed25519" in script
    # 0700 on ancestors, 0600 on keys.
    assert "chmod 0700" in script
    assert "chmod 0600" in script
    # restrict / no-agent / no-port / no-X11 / no-pty / no-user-rc options.
    for opt in ("restrict", "no-agent-forwarding", "no-port-forwarding",
                "no-X11-forwarding", "no-pty", "no-user-rc"):
        assert opt in script
    # Explicit command= pointing at the stable dispatcher.
    assert "command=" in script
    assert "phone-management-dispatch.py" in script
    # Host keys must exist before sshd -t validates HostKey paths.
    assert script.index("ssh-keygen -q") < script.index("sshd -t -f")


def test_sshd_loop_resolves_bind_uses_socket_readiness_and_excludes_self() -> None:
    script = (ANDROID / "phone-sshd-loop.sh").read_text()

    # PHONE_SSHD_BIND_IP=auto default; socket route resolution accepts only
    # Tailnet-range addresses and does not require Android netlink access.
    assert "PHONE_SSHD_BIND_IP" in script
    assert "UDP route probe" in script
    assert "python3" in script
    assert "auto" in script
    # Listener readiness must work from the Termux app UID, which cannot read
    # /proc/net/tcp or use Android netlink.
    assert "socket.SOCK_STREAM" in script
    assert "connect_ex" in script
    assert '"/proc/net/tcp"' not in script
    # Supervisor PID excluded from existing-daemon detection.
    assert '"$pid" == "$$"' in script
    # A sanitized effective config owns the single bind and port.
    assert "render_effective_config" in script
    assert '"$SSHD_BIN" -t -f "$EFFECTIVE_CONFIG"' in script
    assert '"$SSHD_BIN" -D -f "$EFFECTIVE_CONFIG"' in script
    assert "ListenAddress %s" in script
    assert "Port %s" in script
    assert "listener_ready" in script
    assert "sshd-pid-not-found" in script
    assert "invalid-tailnet-bind" in script
    assert "cleanup_started_sshd" in script
    assert 'kill -TERM "$launch_pid"' in script
    # No deprecated "phone-sshd-loop.sh|sshd" exact-substring PID match.
    assert "phone-sshd-loop.sh|sshd" not in script
    # Config-test failure path logs bind + config.
    assert "config-test-failed" in script
    assert "bind=$BIND_IP" in script


def test_dispatcher_rejects_symlink_logs_and_exact_sshd_match() -> None:
    src = (ANDROID / "phone-management-dispatch.py").read_text()

    # Logs verb rejects symlink targets before I/O.
    assert 'symlink' in src
    # cmd_health uses exact sshd binary matching and TCP state 0A.
    assert "PHONE_SSHD_BIN" in src
    assert "_sshd_cmdline_matches" in src
    assert "_tcp_port_listening" in src


def test_phone_health_loop_enforces_env_mode_0600() -> None:
    script = (ANDROID / "phone-health-loop.sh").read_text()

    # phone.env mode 0600 enforced at initial source AND every iteration.
    assert "PHONE_ENV_MODE" in script
    assert "loop=phone-env-mode-bad" in script
    assert "expected=0600" in script
