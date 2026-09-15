"""Deterministic lifecycle, evidence, and alert ownership contracts."""
from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import os
import shutil
import stat
import subprocess
import sys
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
ALERT = ROOT / "ops" / "alerts" / "send-alert.sh"


def _load(name: str, path: Path):
    sys.path.insert(0, str(SCRIPTS))
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _device(now: datetime, age: int, interval: int = 60) -> dict:
    return {
        "device_id": "device-1",
        "last_seen": (now - timedelta(seconds=age)).isoformat(),
        "effective_interval_sec": interval,
        "expected_interval_sec": 999999,
    }


def _state(now: datetime, device: dict | None = None) -> dict:
    return {
        "schema_version": "alert-state-v1",
        "started_at": (now - timedelta(hours=1)).isoformat(),
        "devices": {} if device is None else {"device-1": device},
    }


def test_atomic_writer_modes_and_schema(tmp_path: Path):
    common = _load("ops_common_test", SCRIPTS / "ops_common.py")
    target = tmp_path / "evidence" / "heartbeat.json"
    value = {
        "schema_version": common.SCHEMA_VERSION, "kind": "heartbeat",
        "observed_at": "2026-08-02T12:00:00Z", "state": "ok", "detail": "ok",
        "poll_failed": False, "alert_attempted": False, "alert_delivered": False,
    }
    common.atomic_write_json(target, value)
    assert json.loads(target.read_text()) == value
    assert stat.S_IMODE(target.stat().st_mode) == 0o640
    assert list(target.parent.glob("*.tmp")) == []
    schema = json.loads((ROOT / "schemas" / "health-evidence-v1.schema.json").read_text())
    assert schema["properties"]["schema_version"]["const"] == common.SCHEMA_VERSION


def test_container_group_can_read_but_not_write_evidence(tmp_path: Path):
    evidence = tmp_path / "evidence"; evidence.mkdir(mode=0o750)
    secrets = tmp_path / "secrets"; secrets.mkdir()
    env = os.environ | {
        "ISPINDEL_EVIDENCE_DIR": str(evidence),
        "ISPINDEL_SECRETS_DIR": str(secrets),
        "ISPINDEL_GID": "4242",
    }
    result = subprocess.run(
        ["docker", "compose", "config", "--format", "json"], cwd=ROOT, env=env,
        text=True, capture_output=True, check=True,
    )
    service = json.loads(result.stdout)["services"]["ispindel-dashboard"]
    assert service["group_add"] == ["4242"]
    mount = next(item for item in service["volumes"] if item.get("target") == "/health-evidence")
    assert mount["read_only"] is True
    assert (0o640 & 0o040) and not (0o640 & 0o020)


def test_alert_success_exit_zero():
    received: list[dict] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            received.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            body = b'{"ok":true}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            del format, args

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        result = subprocess.run(
            [str(ALERT), "synthetic test"],
            env=os.environ | {"ISPINDEL_ALERT_URL": f"http://127.0.0.1:{server.server_port}/send"},
            text=True, capture_output=True, check=False,
        )
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=2)
    assert result.returncode == 0
    event = json.loads(result.stdout)
    assert event["event"] == "alert_sent" and event["outcome"] == "ok"
    assert received == [{"message": "synthetic test"}]


def test_failed_delivery_remains_retryable_and_not_sent():
    poll = _load("poll_alert_retry", SCRIPTS / "poll-alert.py")
    now = datetime(2026, 8, 2, 12, 0, tzinfo=timezone.utc)
    state = _state(now)
    snapshots: list[dict] = []
    calls: list[str] = []

    def persist():
        snapshots.append(copy.deepcopy(state))

    def fail(message: str) -> bool:
        assert snapshots[-1]["devices"]["device-1"]["delivery"] == "pending"
        calls.append(message)
        return False

    for _ in range(2):
        transitions, failed = poll.apply_transitions(
            [_device(now, 200)], state, now=now, startup_grace=0,
            sender=fail, persist=persist,
        )
        assert failed is True and transitions[0]["outcome"] == "failed"
        assert state["devices"]["device-1"]["delivery"] == "pending"
    assert len(calls) == 2


def test_stale_threshold_uses_effective_device_interval():
    poll = _load("poll_alert_interval", SCRIPTS / "poll-alert.py")
    now = datetime(2026, 8, 2, 12, 0, tzinfo=timezone.utc)
    state, age, interval = poll.classify_device(_device(now, 200, interval=60), now=now)
    assert (state, round(age), interval) == ("warning", 200, 60)


def test_stale_alert_hysteresis_dedup_and_single_recovery():
    poll = _load("poll_alert_hysteresis", SCRIPTS / "poll-alert.py")
    now = datetime(2026, 8, 2, 12, 0, tzinfo=timezone.utc)
    state = _state(now)
    messages: list[str] = []

    def send(message: str) -> bool:
        messages.append(message)
        return True

    outcomes = []
    for age in (200, 200, 400, 60, 60):
        result, failed = poll.apply_transitions(
            [_device(now, age)], state, now=now, startup_grace=0, sender=send,
        )
        assert failed is False
        outcomes.append(result[0]["outcome"])
    assert outcomes == ["sent", "suppressed", "sent", "sent", "suppressed"]
    assert len(messages) == 3 and messages[-1].endswith("telemetry recovered")


def test_source_and_deployed_alert_script_hash_match(tmp_path: Path):
    deployed = tmp_path / "usr" / "local" / "lib" / "ispindel" / "send-alert.sh"
    deployed.parent.mkdir(parents=True)
    shutil.copyfile(ALERT, deployed)
    digest = lambda path: hashlib.sha256(path.read_bytes()).hexdigest()
    assert digest(ALERT) == digest(deployed)


def test_terminal_events_are_structured_and_secret_free():
    secret = "do-not-log-this-token"
    result = subprocess.run(
        [str(ALERT), secret], env=os.environ | {"ISPINDEL_DRY_RUN": "1"},
        text=True, capture_output=True, check=False,
    )
    event = json.loads(result.stdout)
    assert {"event", "run_id", "duration_ms", "outcome"} <= event.keys()
    assert secret not in result.stdout and "token" not in result.stdout.lower()


def test_systemd_units_use_absolute_execstart_without_shell():
    units = list((ROOT / "ops" / "systemd").glob("ispindel-*.service"))
    assert units
    for path in units:
        starts = [line.split("=", 1)[1] for line in path.read_text().splitlines() if line.startswith("ExecStart=")]
        assert starts and all(value.startswith("/") for value in starts)
        assert all("/bin/sh" not in value and "/bin/bash" not in value for value in starts)


def test_boot_stack_is_secret_and_health_gated():
    stack = (ROOT / "ops/systemd/ispindel-stack.service").read_text()
    verifier = (ROOT / "ops/systemd/ispindel-secrets-verify.service").read_text()
    launcher = (ROOT / "scripts/stack-start.py").read_text()
    preflight = (ROOT / "scripts/verify-production-secrets.py").read_text()
    assert "Requires=docker.service ispindel-secrets-verify.service" in stack
    assert "ExecStartPre=/usr/bin/python3 /usr/local/libexec/ispindel/verify-production-secrets.py" in stack
    assert "ExecStart=/usr/bin/python3 /usr/local/libexec/ispindel/stack-start.py" in stack
    assert "TimeoutStartSec=4min" in stack and "StartLimitBurst=3" in stack
    assert "Before=ispindel-stack.service" in verifier
    assert "ISPINDEL_SECRETS_DIR must be the persistent /etc/ispindel/secrets path" in preflight
    assert "PRAGMA journal_mode" in launcher and "PRAGMA integrity_check" in launcher
    assert '"stop"' in launcher and "--no-build" in launcher
    assert "--project-name" in launcher and "COMPOSE_PROJECT_NAME" in launcher


def test_boot_stack_uses_persisted_compose_project(monkeypatch: pytest.MonkeyPatch):
    launcher = _load("stack_start_project", SCRIPTS / "stack-start.py")
    calls: list[list[str]] = []

    def fake_run(argv):
        calls.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(launcher, "_run", fake_run)
    monkeypatch.setenv("COMPOSE_PROJECT_NAME", "ispindel-promote-release-abc")
    launcher._compose("up", "--detach")
    assert calls == [[
        "/usr/bin/docker", "compose", "--project-name", "ispindel-promote-release-abc",
        "--env-file", "/etc/ispindel/production.env",
        "--file", "/opt/ispindel-dashboard/docker-compose.yml", "up", "--detach",
    ]]
    monkeypatch.setenv("COMPOSE_PROJECT_NAME", "unsafe/project")
    with pytest.raises(launcher.StackStartError, match="invalid"):
        launcher._compose("up")


def test_secret_preflight_token_schema_is_fail_closed():
    preflight = _load("production_secret_preflight", SCRIPTS / "verify-production-secrets.py")
    valid = {
        "schema_version": "ingest-tokens-v1",
        "devices": {
            "device-1": {
                "verifiers": [{
                    "salt_hex": "a" * 32,
                    "verifier_hex": "b" * 64,
                    "not_after": None,
                }],
            },
        },
    }
    assert preflight.validate_tokens(valid) == 1
    invalid = {"schema_version": "ingest-tokens-v1", "devices": {}}
    with pytest.raises(preflight.SecretPreflightError):
        preflight.validate_tokens(invalid)


def test_dependant_jobs_require_the_healthy_stack():
    for name in ("ispindel-poll.service", "ispindel-heartbeat.service",
                 "ispindel-backup.service", "ispindel-restore-drill.service"):
        text = (ROOT / "ops/systemd" / name).read_text()
        assert "Requires=ispindel-stack.service" in text
        assert "After=" in text and "ispindel-stack.service" in text


def test_no_duplicate_zeroclaw_scheduler_ownership():
    units = "\n".join(path.read_text() for path in (ROOT / "ops" / "systemd").glob("ispindel-*"))
    assert "zeroclaw" not in units.lower()
    timers = {path.name for path in (ROOT / "ops" / "systemd").glob("*.timer")}
    assert "ispindel-poll.timer" in timers and "ispindel-heartbeat.timer" in timers
    assert "cron" not in units.lower()
