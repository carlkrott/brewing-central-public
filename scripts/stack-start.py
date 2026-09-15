#!/usr/bin/env python3
"""Start the permanent Compose stack and wait for a usable backend.

The systemd unit calls this only after the secret preflight.  All paths and
service names are fixed production values; no shell or caller-supplied command
is accepted.  A readiness failure stops the exact candidate container so
Docker's restart policy cannot leave an uncontrolled boot loop behind.
"""
from __future__ import annotations

import http.client
import json
import os
import re
import subprocess
import sys
import time
from typing import Sequence

DOCKER = "/usr/bin/docker"
COMPOSE_FILE = "/opt/ispindel-dashboard/docker-compose.yml"
ENV_FILE = "/etc/ispindel/production.env"
CONTAINER = "ispindel-dashboard"
SERVICE = "ispindel-dashboard"
READY_HOST = "127.0.0.1"
READY_PORT = 18098
READY_PATH = "/health/ready"
START_TIMEOUT_SECONDS = 180.0
POLL_SECONDS = 2.0
COMPOSE_PROJECT_DEFAULT = "ispindel-dashboard"
COMPOSE_PROJECT_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,62}")

DB_PROBE = (
    "import sqlite3;"
    "c=sqlite3.connect('file:/data/ispindel.db?mode=ro',uri=True);"
    "assert str(c.execute('PRAGMA journal_mode').fetchone()[0]).lower()=='wal';"
    "assert c.execute('PRAGMA integrity_check').fetchone()[0]=='ok';"
    "assert c.execute('PRAGMA foreign_key_check').fetchall()==[];"
    "c.close()"
)


class StackStartError(RuntimeError):
    pass


def _run(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(list(argv), text=True, capture_output=True, check=False)


def _compose_project() -> str:
    project = os.environ.get("COMPOSE_PROJECT_NAME", COMPOSE_PROJECT_DEFAULT)
    if COMPOSE_PROJECT_RE.fullmatch(project) is None:
        raise StackStartError("COMPOSE_PROJECT_NAME is invalid")
    return project


def _compose(*arguments: str) -> subprocess.CompletedProcess[str]:
    return _run([
        DOCKER, "compose", "--project-name", _compose_project(),
        "--env-file", ENV_FILE, "--file", COMPOSE_FILE, *arguments,
    ])


def _state() -> dict[str, object]:
    result = _run([DOCKER, "inspect", "--format", "{{json .State}}", CONTAINER])
    if result.returncode:
        raise StackStartError("permanent container is not inspectable")
    try:
        state = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise StackStartError("permanent container returned invalid state") from exc
    if not isinstance(state, dict):
        raise StackStartError("permanent container state is not an object")
    return state


def _ready_http() -> bool:
    connection = http.client.HTTPConnection(READY_HOST, READY_PORT, timeout=3)
    try:
        connection.request("GET", READY_PATH)
        response = connection.getresponse()
        body = response.read()
        return response.status == 200 and bool(body)
    except OSError:
        return False
    finally:
        connection.close()


def wait_for_ready(timeout: float = START_TIMEOUT_SECONDS) -> dict[str, object]:
    deadline = time.monotonic() + timeout
    last_health = "unknown"
    while time.monotonic() < deadline:
        state = _state()
        health = state.get("Health")
        last_health = str(health.get("Status", "missing")) if isinstance(health, dict) else "missing"
        if state.get("Status") != "running":
            raise StackStartError(f"permanent container stopped during boot (health={last_health})")
        if last_health == "unhealthy":
            raise StackStartError("permanent container healthcheck is unhealthy")
        if last_health == "healthy" and _ready_http():
            return state
        time.sleep(POLL_SECONDS)
    raise StackStartError(f"permanent container did not become ready (health={last_health})")


def verify_database() -> None:
    result = _run([DOCKER, "exec", "--", CONTAINER, "python3", "-c", DB_PROBE])
    if result.returncode:
        raise StackStartError("permanent database failed WAL/integrity verification")


def stop_candidate() -> None:
    _run([DOCKER, "stop", "--time", "30", "--", CONTAINER])


def start() -> None:
    if os.environ.get("ISPINDEL_CONTAINER_NAME", CONTAINER) != CONTAINER:
        raise StackStartError("ISPINDEL_CONTAINER_NAME must remain the fixed production container")
    result = _compose("up", "--detach", "--no-build", "--pull", "never")
    if result.returncode:
        raise StackStartError("permanent Compose start failed")
    try:
        wait_for_ready()
        verify_database()
    except Exception:
        stop_candidate()
        raise


def main() -> int:
    try:
        start()
    except StackStartError as exc:
        print(f"ISPINDEL_STACK_START_FAILED reason={exc}", file=sys.stderr)
        return 2
    print("ISPINDEL_STACK_READY")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
