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
