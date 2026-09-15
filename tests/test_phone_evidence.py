"""RED-then-GREEN tests for the W6 phone-health evidence slice.

Scope is intentionally narrow: phone battery/heartbeat evidence + the single
Termux:Boot -> termux-main-start.sh -> phone-health-loop.sh scheduler chain.

Out of scope (must NOT be exercised here): dual-database backup/restore,
/api/backup-health, production activation, deployment, service restart, live
phone commands, or unrelated W1-W5 work.
"""
from __future__ import annotations

import contextlib
import http.server
import json
import os
import shutil
import socket
import socketserver
import stat
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
ANDROID = REPO_ROOT / "ops" / "android"
PRODUCER = ANDROID / "write-phone-evidence.py"
LOOP = ANDROID / "phone-health-loop.sh"
SCHEMA = REPO_ROOT / "schemas" / "health-evidence-v1.schema.json"


# --- helpers ---------------------------------------------------------------


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _RecordingHandler(http.server.BaseHTTPRequestHandler):
    """Records the request host header and replies 200 to anything."""

    requests: list[tuple[str, str, str | None]] = []

    def log_message(self, format: str, *args: object) -> None:  # pragma: no cover
        return

    def do_GET(self) -> None:  # noqa: N802
        host = self.headers.get("Host")
        type(self).requests.append((self.command, self.path, host))
        body = b'{"status":"ok"}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class _ReusableServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True


@contextlib.contextmanager
def _local_http_server():
    handler = type(
        "_H",
        (_RecordingHandler,),
        {"requests": []},
    )
    server = _ReusableServer(("127.0.0.1", 0), handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield port, handler.requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _run_producer(
    tmp_path: Path,
    *,
    battery_command: list[str] | None,
    sysfs_root: Path | None,
    zeroclaw_url: str,
    dashboard_url: str,
    host_header: str | None,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess:
    battery_path = tmp_path / "battery-state.json"
    heartbeat_path = tmp_path / "heartbeat.json"
    env = os.environ.copy()
    env["BATTERY_EVIDENCE_PATH"] = str(battery_path)
    env["HEARTBEAT_EVIDENCE_PATH"] = str(heartbeat_path)
    if battery_command is not None:
        env["PHONE_BATTERY_COMMAND"] = " ".join(
            __import__("shlex").quote(part) for part in battery_command
        )
    if sysfs_root is not None:
        env["PHONE_BATTERY_SYSFS_ROOT"] = str(sysfs_root)
    env["ZEROCLAW_URL"] = zeroclaw_url
    env["DASHBOARD_URL"] = dashboard_url
    if host_header is not None:
        env["DASHBOARD_HOST_HEADER"] = host_header
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [sys.executable, str(PRODUCER)],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(REPO_ROOT / "scripts"),
        check=False,
    )


def _fake_termux_battery(tmp_path: Path, percent: int, status: str) -> Path:
    script = tmp_path / "fake_termux_battery.sh"
    script.write_text(
        "#!/usr/bin/env bash\n"
        f"printf '%s' '{{\"percentage\":{percent},\"status\":\"{status}\"}}'\n"
    )
    script.chmod(0o755)
    return script


def _fake_sysfs_battery(root: Path, percent: int, status: str) -> None:
    bat = root / "BAT0"
    bat.mkdir(parents=True, exist_ok=True)
    bat.joinpath("capacity").write_text(str(percent))
    bat.joinpath("status").write_text(status)


# --- producer tests --------------------------------------------------------


def test_producer_writes_termux_battery_evidence_atomically(tmp_path: Path) -> None:
    """When termux-battery-status is available, percent and source are taken from it."""
    script = _fake_termux_battery(tmp_path, 73, "DISCHARGING")
    battery_path = tmp_path / "battery-state.json"
    heartbeat_path = tmp_path / "heartbeat.json"

    with _local_http_server() as (zc_port, _zc), _local_http_server() as (db_port, _db):
        proc = _run_producer(
            tmp_path,
            battery_command=[str(script)],
            sysfs_root=None,
            zeroclaw_url=f"http://127.0.0.1:{zc_port}/health",
            dashboard_url=f"http://127.0.0.1:{db_port}/health",
            host_header=None,
        )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert battery_path.exists()
    assert heartbeat_path.exists()

    raw = json.loads(battery_path.read_text())
    assert raw["schema_version"] == "health-evidence-v1"
    assert raw["kind"] == "battery"
    assert raw["state"] in {"ok", "warning", "critical"}
    assert raw["percent"] == 73
    assert raw["charging"] is False
    assert raw["source"] == "termux-battery-status"
    assert isinstance(raw["detail"], str) and raw["detail"]

    # Atomic replacement: no leftover .tmp files in the output directory.
    leftovers = [p for p in tmp_path.iterdir() if p.name.startswith(".")]
    assert leftovers == []


def test_producer_falls_back_to_sysfs_battery(tmp_path: Path) -> None:
    """Without termux-battery-status, read /sys/class/power_supply/BAT*/capacity + status."""
    sysfs_root = tmp_path / "sysfs"
    _fake_sysfs_battery(sysfs_root, 42, "Charging")

    with _local_http_server() as (zc_port, _zc), _local_http_server() as (db_port, _db):
        proc = _run_producer(
            tmp_path,
            battery_command=None,
            sysfs_root=sysfs_root,
            zeroclaw_url=f"http://127.0.0.1:{zc_port}/health",
            dashboard_url=f"http://127.0.0.1:{db_port}/health",
            host_header=None,
        )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    raw = json.loads((tmp_path / "battery-state.json").read_text())
    assert raw["percent"] == 42
    assert raw["charging"] is True
    assert raw["source"] == "sysfs"


def test_producer_marks_unavailable_battery_as_parse_error(tmp_path: Path) -> None:
    """No battery source available -> write an explicit parse-error marker and still write heartbeat."""
    battery_path = tmp_path / "battery-state.json"
    heartbeat_path = tmp_path / "heartbeat.json"
    empty_sysfs = tmp_path / "empty_sysfs"
    empty_sysfs.mkdir()

    with _local_http_server() as (zc_port, _zc), _local_http_server() as (db_port, _db):
        proc = _run_producer(
            tmp_path,
            battery_command=["/nonexistent/termux-battery-status"],
            sysfs_root=empty_sysfs,
            zeroclaw_url=f"http://127.0.0.1:{zc_port}/health",
            dashboard_url=f"http://127.0.0.1:{db_port}/health",
            host_header=None,
        )

    # Producer must still write heartbeat even on battery failure.
    assert heartbeat_path.exists()
    assert proc.returncode != 0  # explicit failure

    raw = json.loads(battery_path.read_text())
    # The marker must NOT be a normal green battery reading; the existing parser
    # in app/main.py returns parse_error when percent is missing/invalid or
    # outside 0..100. Either route is acceptable as long as the producer does
    # not fabricate a usable percent.
    assert raw["schema_version"] == "health-evidence-v1"
    assert raw["kind"] == "battery"
    assert raw["source"] == "unavailable"
    percent = raw.get("percent")
    assert percent is None or not (isinstance(percent, (int, float)) and 0 <= percent <= 100)


def test_producer_writes_heartbeat_with_probe_results(tmp_path: Path) -> None:
    """Heartbeat must include poll_failed/alert fields exactly as schema requires."""
    script = _fake_termux_battery(tmp_path, 88, "DISCHARGING")

    with _local_http_server() as (zc_port, _zc_reqs), _local_http_server() as (db_port, _db_reqs):
        proc = _run_producer(
            tmp_path,
            battery_command=[str(script)],
            sysfs_root=None,
            zeroclaw_url=f"http://127.0.0.1:{zc_port}/health",
            dashboard_url=f"http://127.0.0.1:{db_port}/health",
            host_header="phone.example.test",
        )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    raw = json.loads((tmp_path / "heartbeat.json").read_text())
    assert raw["schema_version"] == "health-evidence-v1"
    assert raw["kind"] == "heartbeat"
    assert isinstance(raw["poll_failed"], bool)
    assert isinstance(raw["alert_attempted"], bool)
    assert isinstance(raw["alert_delivered"], bool)
    assert raw["poll_failed"] is False
    assert raw["state"] == "ok"
    # dashboard probe must use the configured Host header.
    assert any(host == "phone.example.test" for _, _, host in _db_reqs)


def test_producer_derives_zeroclaw_health_path_from_base_url(tmp_path: Path) -> None:
    """The production ZEROCLAW_URL is a base URL, not the health endpoint."""
    script = _fake_termux_battery(tmp_path, 88, "DISCHARGING")
    with _local_http_server() as (zc_port, zc_reqs), _local_http_server() as (db_port, _db):
        proc = _run_producer(
            tmp_path,
            battery_command=[str(script)],
            sysfs_root=None,
            zeroclaw_url=f"http://127.0.0.1:{zc_port}",
            dashboard_url=f"http://127.0.0.1:{db_port}/health",
            host_header=None,
        )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert any(path == "/health" for _, path, _ in zc_reqs)
    heartbeat = json.loads((tmp_path / "heartbeat.json").read_text())
    assert heartbeat["state"] == "ok"


def test_producer_makes_heartbeat_critical_when_probe_fails(tmp_path: Path) -> None:
    """When loopback ZeroClaw or dashboard is unreachable, heartbeat.state must be critical."""
    script = _fake_termux_battery(tmp_path, 88, "DISCHARGING")
    dead_zc_port = _free_port()  # nothing listening
    dead_db_port = _free_port()

    proc = _run_producer(
        tmp_path,
        battery_command=[str(script)],
        sysfs_root=None,
        zeroclaw_url=f"http://127.0.0.1:{dead_zc_port}/health",
        dashboard_url=f"http://127.0.0.1:{dead_db_port}/health",
        host_header=None,
    )

    # Battery evidence still written even when probes fail.
    assert (tmp_path / "battery-state.json").exists()
    raw = json.loads((tmp_path / "heartbeat.json").read_text())
    assert raw["poll_failed"] is True
    assert raw["state"] == "critical"


def test_producer_does_not_leak_credentials_or_tokens(tmp_path: Path) -> None:
    """Stdout/stderr must not echo the configured secrets or paths."""
    secret_token = "supersecret-token-must-not-leak"
    env = {"ZEROCLAW_TOKEN": secret_token, "DASHBOARD_TOKEN": secret_token}
    proc = _run_producer(
        tmp_path,
        battery_command=["/nonexistent/termux-battery-status"],
        sysfs_root=tmp_path / "empty_sysfs",
        zeroclaw_url="http://127.0.0.1:1/health",
        dashboard_url="http://127.0.0.1:1/health",
        host_header=None,
        extra_env=env,
    )
    combined = proc.stdout + proc.stderr
    assert secret_token not in combined
    assert "ZEROCLAW_TOKEN" not in combined
    assert "DASHBOARD_TOKEN" not in combined


def test_producer_atomic_writes_leave_no_temp_files(tmp_path: Path) -> None:
    """Even after multiple invocations, no .tmp siblings remain next to evidence."""
    script = _fake_termux_battery(tmp_path, 50, "DISCHARGING")

    with _local_http_server() as (zc_port, _zc), _local_http_server() as (db_port, _db):
        for _ in range(3):
            proc = _run_producer(
                tmp_path,
                battery_command=[str(script)],
                sysfs_root=None,
                zeroclaw_url=f"http://127.0.0.1:{zc_port}/health",
                dashboard_url=f"http://127.0.0.1:{db_port}/health",
                host_header=None,
            )
            assert proc.returncode == 0, proc.stdout + proc.stderr

    leftovers = [p for p in tmp_path.iterdir() if p.name.startswith(".")]
    assert leftovers == []


def test_producer_observes_bounded_timeouts(tmp_path: Path) -> None:
    """Bounded subprocess/HTTP timeouts: a hanging battery command must not stall forever."""
    hanger = tmp_path / "hang.sh"
    hanger.write_text("#!/usr/bin/env bash\nsleep 30\n")
    hanger.chmod(0o755)

    started = time.monotonic()
    proc = _run_producer(
        tmp_path,
        battery_command=[str(hanger)],
        sysfs_root=tmp_path / "empty",
        zeroclaw_url="http://127.0.0.1:1/health",
        dashboard_url="http://127.0.0.1:1/health",
        host_header=None,
    )
    elapsed = time.monotonic() - started
    assert elapsed < 20, f"producer did not bound subprocess timeout: {elapsed:.1f}s"


# --- loop / scheduling ownership tests ------------------------------------


def test_loop_script_is_bash_and_valid() -> None:
    assert LOOP.exists()
    assert LOOP.stat().st_mode & stat.S_IXUSR
    subprocess.run(["bash", "-n", str(LOOP)], check=True)


def test_loop_is_command_bound_and_single_instance(tmp_path: Path) -> None:
    """phone-health-loop.sh must own a flock and a command-bound PID file."""
    body = LOOP.read_text()
    assert "flock" in body
    assert "phone-health-loop" in body  # cmdline tag for command-bound match
    assert "trap" in body
    assert "TERM" in body
    assert "INT" in body


def test_loop_bounds_interval_with_upper_cap(tmp_path: Path) -> None:
    """PHONE_EVIDENCE_INTERVAL_SECONDS must be positive and capped by the loop."""
    body = LOOP.read_text()
    # default and an explicit cap
    assert "PHONE_EVIDENCE_INTERVAL_SECONDS" in body
    assert "3600" in body  # upper bound = 1 hour
    assert "300" in body  # default 300 seconds


def test_loop_invokes_the_producer(tmp_path: Path) -> None:
    body = LOOP.read_text()
    assert "write-phone-evidence.py" in body


def test_termux_main_start_starts_loop_after_stack_readiness() -> None:
    """termux-main-start.sh must start the phone-health loop exactly once after readiness."""
    body = (ANDROID / "termux-main-start.sh").read_text()
    assert "phone-health-loop.sh" in body
    # After the readiness retry loop only, not via systemd/cron/job-scheduler.
    assert "systemd" not in body
    assert "termux-job-scheduler" not in body
    assert "crond" not in body
    # The launcher must invoke the loop exactly once (single nohup call) and
    # only after the readiness gate succeeds.
    assert body.count("nohup \"$ROOT/current/ops/android/phone-health-loop.sh\"") == 1
    launch_idx = body.find("nohup \"$ROOT/current/ops/android/phone-health-loop.sh\"")
    readiness_idx = body.find("boot=ready")
    assert 0 <= readiness_idx < launch_idx


def test_stop_phone_stack_terminates_loop_and_removes_pid() -> None:
    """stop-phone-stack.sh must command-match the loop and remove its PID file."""
    body = (ANDROID / "stop-phone-stack.sh").read_text()
    assert "phone-health-loop.sh" in body
    assert "phone-health-loop.pid" in body
    assert ".split(b\"\\0\")" in body
    assert "path.resolve().relative_to(root)" in body


def test_no_termux_systemd_cron_or_job_scheduler_added() -> None:
    """Defensive: no new daemon or scheduler was introduced for this slice."""
    for path in sorted(ANDROID.glob("*.sh")):
        body = path.read_text()
        # Strip comments to allow contract documentation in headers.
        code = "\n".join(
            line for line in body.splitlines() if not line.lstrip().startswith("#")
        )
        for forbidden in (
            "termux-job-scheduler",
            "crond",
            "/system/bin/systemd",
            "systemctl ",
        ):
            assert forbidden not in code, f"{path.name} introduced scheduler: {forbidden}"


def test_phone_env_example_adds_only_non_secret_interval_setting() -> None:
    """phone.env.example must add PHONE_EVIDENCE_INTERVAL_SECONDS and not leak secrets."""
    body = (ANDROID / "phone.env.example").read_text()
    assert "PHONE_EVIDENCE_INTERVAL_SECONDS=300" in body
    # no token-style secrets added
    assert "TOKEN" not in body or "ZEROCLAW_TOKEN_FILE" in body  # existing non-secret path is allowed


def test_docs_name_single_owner_and_call_out_unqualified_gate() -> None:
    runbook = (REPO_ROOT / "docs" / "PHONE-RUNBOOK.md").read_text()
    operations = (REPO_ROOT / "docs" / "OPERATIONS.md").read_text()
    assert "phone-health-loop.sh" in runbook
    assert "Termux:Boot" in runbook
    assert "off-host" in runbook.lower() and "backup" in runbook.lower()
    # The docs must NOT claim phone evidence is automatically off-host backed up.
    combined = (runbook + "\n" + operations).lower()
    assert "unqualified" in combined or "separate" in combined


# --- regression tests for the W6 source-contract repair -----------------


def test_producer_derives_dashboard_url_and_host_from_phone_bind_ip(
    tmp_path: Path,
) -> None:
    """When DASHBOARD_URL/DASHBOARD_HOST_HEADER are unset, the producer must
    derive the dashboard probe URL from PHONE_BIND_IP (port 8098) and the
    Host header from TAILNET_FQDN. A local HTTP server confirms the
    derived URL and Host actually reach the producer and back.
    """
    # _run_producer unconditionally sets DASHBOARD_URL; we need a sibling
    # invocation path that OMITS that override so the producer's derivation
    # can take effect.
    battery_path = tmp_path / "battery-state.json"
    heartbeat_path = tmp_path / "heartbeat.json"
    script = _fake_termux_battery(tmp_path, 73, "DISCHARGING")

    with _local_http_server() as (db_port, db_reqs):
        env = os.environ.copy()
        env["BATTERY_EVIDENCE_PATH"] = str(battery_path)
        env["HEARTBEAT_EVIDENCE_PATH"] = str(heartbeat_path)
        env["PHONE_BATTERY_COMMAND"] = " ".join(
            __import__("shlex").quote(part) for part in [str(script)]
        )
        env["ZEROCLAW_URL"] = f"http://127.0.0.1:{db_port}/health"
        # Intentionally do NOT set DASHBOARD_URL or DASHBOARD_HOST_HEADER;
        # set PHONE_BIND_IP and TAILNET_FQDN so the producer derives them.
        env.pop("DASHBOARD_URL", None)
        env.pop("DASHBOARD_HOST_HEADER", None)
        env["PHONE_BIND_IP"] = "127.0.0.1"
        env["PHONE_DASHBOARD_PORT"] = str(db_port)
        env["TAILNET_FQDN"] = "phone.example.test"

        proc = subprocess.run(
            [sys.executable, str(PRODUCER)],
            capture_output=True,
            text=True,
            env=env,
            cwd=str(REPO_ROOT / "scripts"),
            check=False,
        )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    raw = json.loads(heartbeat_path.read_text())
    assert raw["kind"] == "heartbeat"
    assert raw["poll_failed"] is False
    assert raw["state"] == "ok"
    # The derived Host header must have reached the local server.
    assert any(
        host == "phone.example.test" for _, _, host in db_reqs
    ), db_reqs


def test_producer_explicit_overrides_win_over_derivation(tmp_path: Path) -> None:
    """Explicit DASHBOARD_URL/DASHBOARD_HOST_HEADER must beat the
    PHONE_BIND_IP/TAILNET_FQDN derivation.
    """
    battery_path = tmp_path / "battery-state.json"
    heartbeat_path = tmp_path / "heartbeat.json"
    script = _fake_termux_battery(tmp_path, 73, "DISCHARGING")

    with _local_http_server() as (db_port, db_reqs):
        env = os.environ.copy()
        env["BATTERY_EVIDENCE_PATH"] = str(battery_path)
        env["HEARTBEAT_EVIDENCE_PATH"] = str(heartbeat_path)
        env["PHONE_BATTERY_COMMAND"] = " ".join(
            __import__("shlex").quote(part) for part in [str(script)]
        )
        env["ZEROCLAW_URL"] = f"http://127.0.0.1:{db_port}/health"
        # Explicit override points at the local server with an explicit Host.
        env["DASHBOARD_URL"] = f"http://127.0.0.1:{db_port}/health"
        env["DASHBOARD_HOST_HEADER"] = "explicit-host.example"
        # Derivation inputs would point elsewhere; they must NOT win.
        env["PHONE_BIND_IP"] = "198.51.100.99"
        env["PHONE_DASHBOARD_PORT"] = "1"
        env["TAILNET_FQDN"] = "wrong-host.example"

        proc = subprocess.run(
            [sys.executable, str(PRODUCER)],
            capture_output=True,
            text=True,
            env=env,
            cwd=str(REPO_ROOT / "scripts"),
            check=False,
        )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    raw = json.loads(heartbeat_path.read_text())
    assert raw["poll_failed"] is False
    # Only the explicit Host reached the server.
    hosts = [host for _, _, host in db_reqs if host is not None]
    assert "explicit-host.example" in hosts
    assert "wrong-host.example" not in hosts
    assert "phone.example.test" not in hosts


def test_loop_sources_phone_env_before_invoking_producer(tmp_path: Path) -> None:
    """phone-health-loop.sh must source config/phone.env before launching
    write-phone-evidence.py so BATTERY_EVIDENCE_PATH, HEARTBEAT_EVIDENCE_PATH,
    ZEROCLAW_URL, and dashboard settings are not absent in the Termux:Boot
    environment. The source must happen before the producer is invoked and
    must use export semantics. The loop must still refuse to introduce
    systemd/cron/termux-job-scheduler.
    """
    body = LOOP.read_text()
    assert (
        'PHONE_ENV="${PHONE_ENV_FILE:-$ROOT/config/phone.env}"' in body
    ), "loop must use the protected root-level phone.env by default"
    # export semantics — the sourced file must be marked for export so
    # the producer subprocess inherits every variable.
    assert "export" in body.lower() or ". " in body
    # The source line must precede the producer invocation in the script
    # text (source-before-invoke ordering).
    source_idx = body.find("config/phone.env")
    producer_idx = body.find("write-phone-evidence.py")
    assert 0 <= source_idx < producer_idx, (
        "loop must source config/phone.env before invoking the producer"
    )
    # No new scheduler introduced.
    code = "\n".join(
        line for line in body.splitlines() if not line.lstrip().startswith("#")
    )
    for forbidden in (
        "termux-job-scheduler",
        "crond",
        "/system/bin/systemd",
        "systemctl ",
    ):
        assert forbidden not in code, f"loop introduced scheduler: {forbidden}"


def test_loop_sources_real_phone_env_into_producer_subprocess(
    tmp_path: Path,
) -> None:
    """Behavior contract: when the loop launches the producer, the
    producer must see values defined in the sourced phone.env. We
    simulate by writing a phone.env with custom BATTERY_EVIDENCE_PATH and
    HEARTBEAT_EVIDENCE_PATH, then asserting the producer writes to those
    paths when invoked via the same sourcing idiom the loop uses.

    The test does NOT run the actual loop (avoids Termux commands and
    sleep loops); it asserts that a bash fragment using the loop's
    sourcing idiom exports the variables before the producer runs.
    """
    phone_env = tmp_path / "phone.env"
    custom_battery = tmp_path / "from_phone_env_battery.json"
    custom_heartbeat = tmp_path / "from_phone_env_heartbeat.json"
    phone_env.write_text(
        "\n".join(
            [
                f'BATTERY_EVIDENCE_PATH="{custom_battery}"',
                f'HEARTBEAT_EVIDENCE_PATH="{custom_heartbeat}"',
                # point ZEROCLAW_URL at an unused port to isolate the
                # dashboard contract test below
                "ZEROCLAW_URL=http://127.0.0.1:1/health",
                # explicit dashboard URL + Host so derivation is not needed
                'DASHBOARD_URL="http://127.0.0.1:1/health"',
                "DASHBOARD_HOST_HEADER=ignored",
            ]
        )
        + "\n"
    )

    producer_path = PRODUCER.resolve()
    # Use the same sourcing idiom the loop uses (`.` + export).
    bash_fragment = (
        f"set -a\n"
        f". '{phone_env}'\n"
        f"set +a\n"
        f"'{producer_path}'\n"
    )
    script = tmp_path / "run_with_phone_env.sh"
    script.write_text(f"#!/usr/bin/env bash\n{bash_fragment}")
    script.chmod(0o755)

    proc = subprocess.run(
        [str(script)],
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": str(REPO_ROOT / "scripts")},
        check=False,
    )
    # The producer must write evidence to the paths that came from phone.env.
    assert custom_battery.exists(), proc.stdout + proc.stderr
    assert custom_heartbeat.exists(), proc.stdout + proc.stderr


def _make_isolated_phone_env_root(tmp_path: Path, *, interval: str) -> Path:
    """Build a self-contained BREWING_CENTRAL_ROOT tree matching the loop's
    layout: config/phone.env, current/ops/android/, run/, logs/, data/.
    Tests install any producer fixture they need after creating the tree.
    """
    root = tmp_path / "fake_root"
    (root / "config").mkdir(parents=True)
    (root / "current" / "ops" / "android").mkdir(parents=True)
    (root / "run").mkdir()
    (root / "logs").mkdir()
    (root / "data").mkdir()
    # Copy the real loop into the isolated tree so it sees its own sibling
    # files (ops_common, schemas, etc.) via its current/ relative paths.
    shutil.copy(LOOP, root / "current" / "ops" / "android" / "phone-health-loop.sh")
    (root / "current" / "ops" / "android" / "phone-health-loop.sh").chmod(0o755)
    # python3 is fine for the placeholder producer; the real producer lives
    # elsewhere and is irrelevant to these two tests.
    phone_env = root / "config" / "phone.env"
    phone_env.write_text(
        "\n".join(
            [
                "# RED-test phone.env: short safe interval that must dominate",
                f"PHONE_EVIDENCE_INTERVAL_SECONDS={interval}",
                # Secret that must NEVER appear in any log line if the loop
                # mishandles the source block.
                "ZEROCLAW_TOKEN=supersecret-token-must-not-leak",
                "DASHBOARD_TOKEN=supersecret-dashboard-token-must-not-leak",
            ]
        )
        + "\n"
    )
    phone_env.chmod(0o600)
    return root


def test_producer_runs_from_phone_bundle_without_scripts_tree(tmp_path: Path) -> None:
    """The deployed Android bundle must not depend on an omitted scripts/ tree."""
    deployed = tmp_path / "release" / "ops" / "android" / PRODUCER.name
    deployed.parent.mkdir(parents=True)
    shutil.copy(PRODUCER, deployed)
    deployed.chmod(0o755)
    battery = _fake_termux_battery(tmp_path, 75, "DISCHARGING")
    battery_path = tmp_path / "battery.json"
    heartbeat_path = tmp_path / "heartbeat.json"
    env = os.environ.copy()
    env.update(
        {
            "PYTHONPATH": "",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PHONE_BATTERY_COMMAND": str(battery),
            "BATTERY_EVIDENCE_PATH": str(battery_path),
            "HEARTBEAT_EVIDENCE_PATH": str(heartbeat_path),
            "ZEROCLAW_URL": "http://127.0.0.1:1/health",
            "DASHBOARD_URL": "http://127.0.0.1:1/health",
        }
    )
    proc = subprocess.run(
        [sys.executable, str(deployed)],
        capture_output=True,
        text=True,
        cwd=tmp_path / "release",
        env=env,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert battery_path.is_file()
    assert heartbeat_path.is_file()


def _make_marker_producer(tmp_path: Path) -> tuple[Path, Path]:
    """Producer that writes a marker file when invoked. The marker is the
    observable proof that the loop launched the producer; absence of the
    marker after the loop exits is the RED/GREEN contract for the
    source-failure test.
    """
    marker = tmp_path / "producer_marker"
    producer = tmp_path / "write-phone-evidence.py"
    producer.write_text(
        "#!/usr/bin/env python3\n"
        "from pathlib import Path\n"
        f"Path({str(marker)!r}).write_text('invoked')\n"
    )
    producer.chmod(0o755)
    return marker, producer


def test_loop_uses_configured_interval_from_phone_env(tmp_path: Path) -> None:
    """Behavior contract: when phone.env defines a safe positive
    PHONE_EVIDENCE_INTERVAL_SECONDS, the loop's start-of-iteration log
    MUST use that value. The current implementation computes RAW_INTERVAL
    before sourcing phone.env, so the log will show the default (300) and
    this test must fail RED.

    The loop is launched with an isolated BREWING_CENTRAL_ROOT so it
    sources our temp phone.env and runs no real producers (no Termux
    battery, no dashboard, no flock contention). We give it a short
    interval (2s) so the test bounds itself in well under 30 seconds.
    """
    # 2s is safe (well inside the 1..3600 cap) and keeps the test bounded.
    interval = "2"
    root = _make_isolated_phone_env_root(tmp_path, interval=interval)
    log_file = root / "logs" / "phone-health-loop.log"
    env = os.environ.copy()
    env["BREWING_CENTRAL_ROOT"] = str(root)
    # Use the host python3 so a missing venv does not stall the loop.
    env["PHONE_EVIDENCE_PYTHON"] = sys.executable
    # The loop re-exports PATH as "$HOME/bin:$PREFIX/bin:/system/bin". On
    # the test host that hides /usr/bin (where `date`, `flock`, etc.
    # live), so point HOME/PREFIX at /usr so the rewritten PATH still
    # resolves those commands.
    env["HOME"] = "/usr"
    env["PREFIX"] = "/usr"
    env["PATH"] = "/usr/bin:/bin:" + env.get("PATH", "")

    # Launch the loop with a generous overall timeout. The loop will:
    #   - log loop=start with whatever interval is in effect
    #   - run the producer
    #   - sleep 2s
    #   - repeat until we send SIGTERM, then trap-cleanup runs.
    # The script's hard-coded shebang targets the Termux bash path which
    # does not exist on the test host; invoke it explicitly via bash.
    loop_script = root / "current" / "ops" / "android" / "phone-health-loop.sh"
    proc = subprocess.Popen(
        ["bash", str(loop_script)],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        # Wait for the loop to start and produce at least one log line.
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            if log_file.exists() and log_file.stat().st_size > 0:
                break
            time.sleep(0.1)
        # Give the first iteration one more beat so loop=start is fully
        # flushed before we terminate the loop.
        time.sleep(0.5)
        proc.terminate()
        try:
            stdout, stderr = proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            stdout, stderr = proc.communicate()
    finally:
        if proc.poll() is None:
            proc.kill()

    assert log_file.exists(), "loop never wrote its log file"
    log_text = log_file.read_text()
    # The loop must announce its start with the CONFIGURED interval (2),
    # NOT the default (300) and NOT a parsed-from-old-position value.
    assert f"loop=start interval={interval}" in log_text, (
        "loop logged wrong interval; expected configured "
        f"{interval}, got log:\n{log_text}"
    )
    assert "loop=start interval=300" not in log_text, (
        "loop still defaults to 300 even when phone.env overrides it; "
        f"log:\n{log_text}"
    )
    # Sourced values/credentials must never appear in the log.
    assert "supersecret-token-must-not-leak" not in log_text
    assert "supersecret-dashboard-token-must-not-leak" not in log_text
    assert "ZEROCLAW_TOKEN" not in log_text
    assert "DASHBOARD_TOKEN" not in log_text


def test_loop_restarts_immediately_after_parent_is_killed(tmp_path: Path) -> None:
    """A child sleep must not retain the singleton flock after parent death."""
    root = _make_isolated_phone_env_root(tmp_path, interval="2")
    loop_script = root / "current" / "ops" / "android" / "phone-health-loop.sh"
    env = os.environ.copy()
    env.update(
        {
            "BREWING_CENTRAL_ROOT": str(root),
            "PHONE_EVIDENCE_PYTHON": sys.executable,
            "HOME": "/usr",
            "PREFIX": "/usr",
            "PATH": "/usr/bin:/bin:" + env.get("PATH", ""),
        }
    )

    first = subprocess.Popen(
        ["bash", str(loop_script)],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    second: subprocess.Popen[bytes] | None = None
    try:
        time.sleep(0.3)
        first.kill()
        first.wait(timeout=5)

        second = subprocess.Popen(
            ["bash", str(loop_script)],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        time.sleep(0.3)
        assert second is not None and second.poll() is None, (
            "restarted loop could not acquire its flock"
        )
    finally:
        if first.poll() is None:
            first.kill()
            first.wait(timeout=5)
        if second is not None and second.poll() is None:
            second.terminate()
            second.wait(timeout=5)


def test_loop_does_not_invoke_producer_when_phone_env_source_fails(
    tmp_path: Path,
) -> None:
    """Behavior contract: when phone.env has a syntax error, the `.` source
    returns nonzero. The current implementation does NOT check the source
    status, so the next line still launches the producer with partial /
    stale variables. After the fix, the loop must fail-closed for that
    iteration: log the source failure and skip the producer entirely.

    A marker producer (writes a sentinel file when invoked) is installed
    at the path the loop expects so we can observe whether it ran.
    """
    root = _make_isolated_phone_env_root(tmp_path, interval="2")
    marker, producer = _make_marker_producer(tmp_path)
    # Install the marker producer at the path the loop will look for.
    target = root / "current" / "ops" / "android" / "write-phone-evidence.py"
    shutil.copy(producer, target)
    target.chmod(0o755)

    # Replace phone.env with a syntactically invalid one (unterminated
    # quote). Bash `.` returns nonzero on parse errors; the deployed loop
    # currently ignores that status and proceeds to invoke the producer.
    phone_env = root / "config" / "phone.env"
    phone_env.write_text(
        "\n".join(
            [
                "PHONE_EVIDENCE_INTERVAL_SECONDS=2",
                # Bash will report a syntax error when sourcing this file
                # (unterminated double quote) and return nonzero.
                'ZEROCLAW_URL="http://127.0.0.1:1/health',
            ]
        )
        + "\n"
    )
    phone_env.chmod(0o600)

    log_file = root / "logs" / "phone-health-loop.log"
    env = os.environ.copy()
    env["BREWING_CENTRAL_ROOT"] = str(root)
    env["PHONE_EVIDENCE_PYTHON"] = sys.executable
    env["HOME"] = "/usr"
    env["PREFIX"] = "/usr"
    env["PATH"] = "/usr/bin:/bin:" + env.get("PATH", "")

    proc = subprocess.Popen(
        ["bash", str(root / "current" / "ops" / "android" / "phone-health-loop.sh")],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 12.0
        while time.monotonic() < deadline:
            if log_file.exists() and "loop=start" in log_file.read_text():
                break
            time.sleep(0.2)
        # Allow enough time for the loop to attempt (and fail) the source.
        time.sleep(1.5)
        proc.terminate()
        try:
            stdout, stderr = proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            stdout, stderr = proc.communicate()
    finally:
        if proc.poll() is None:
            proc.kill()

    log_text = log_file.read_text() if log_file.exists() else ""
    # The producer must NOT have been invoked after the failed source.
    assert not marker.exists(), (
        "producer was invoked after a source failure; loop must fail-closed "
        f"for that iteration. Log:\n{log_text}"
    )
    # The source failure should be visible in the log (without echoing the
    # sourced values).
    assert "source" in log_text.lower(), (
        "loop did not log the source failure; log:\n" + log_text
    )
    # The broken phone.env value must NOT leak into the log.
    assert "127.0.0.1:1/health" not in log_text
    # Sourced credentials must never be echoed.
    assert "ZEROCLAW_TOKEN" not in log_text
    assert "DASHBOARD_TOKEN" not in log_text


def test_runbook_stop_and_logs_cover_phone_health_loop() -> None:
    """The runbook must reflect that stop-phone-stack.sh owns the loop too,
    and must list phone-health-loop.log as a documented phone log. The
    runbook must not claim off-host backup, restore, installation,
    scheduling qualification, or production readiness for this slice.
    """
    runbook = (REPO_ROOT / "docs" / "PHONE-RUNBOOK.md").read_text()
    # stop section must reference the loop, not just the original three services
    assert "phone-health-loop" in runbook
    lower = runbook.lower()
    # The stop wording must NOT claim only three services are stopped.
    assert "stop only the three" not in lower, (
        "runbook still claims stop covers only the three services"
    )
    # The loop log must be in the documented logs list.
    assert "phone-health-loop.log" in runbook
    # Negative claims: nothing in this slice should claim production-grade.
    forbidden_phrases = (
        "production ready",
        "off-host backup qualified",
        "off-host restore qualified",
        "scheduling qualified",
        "automatically backed up off-host",
    )
    for phrase in forbidden_phrases:
        assert phrase not in lower, f"runbook overclaimed: {phrase}"
