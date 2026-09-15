"""Portability and one-command verification contracts."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
POLLER = ROOT / "scripts" / "ispindel-check.py"
RUNNER = ROOT / "scripts" / "run-tests.sh"
REMOVED_TMP_ROOT = "/tmp/ispindel-" + "phase01-v1-CZxJkx"


def _run_poller(*args: str, env: dict[str, str] | None = None):
    merged = os.environ.copy()
    merged.pop("ISPINDEL_URL", None)
    if env:
        merged.update(env)
    return subprocess.run(
        [sys.executable, str(POLLER), *args],
        cwd=ROOT,
        env=merged,
        text=True,
        capture_output=True,
        check=False,
    )


def test_source_import_has_no_removed_tmp_root():
    assert REMOVED_TMP_ROOT not in (ROOT / "tests" / "conftest.py").read_text()
    assert REMOVED_TMP_ROOT not in "\n".join(
        p.read_text(errors="replace")
        for p in ROOT.rglob("*.py")
        if ".venv" not in p.parts and "__pycache__" not in p.parts
    )


def test_poller_default_targets_8098_and_transport_is_exit_2():
    proc = _run_poller("--json", "--timeout", "1")
    assert proc.returncode == 2
    body = json.loads(proc.stdout)
    assert body["url"] == "http://127.0.0.1:8098"
    assert body["state"] == "transport_error"


def test_poller_cli_url_precedes_environment():
    proc = _run_poller(
        "--json", "--timeout", "1", "--url", "http://127.0.0.1:9",
        env={"ISPINDEL_URL": "http://127.0.0.1:8"},
    )
    assert proc.returncode == 2
    assert json.loads(proc.stdout)["url"] == "http://127.0.0.1:9"


def test_compose_has_no_home_or_tmp_bind_sources(tmp_path: Path):
    evidence = tmp_path / "evidence"
    secrets = tmp_path / "secrets"
    evidence.mkdir()
    secrets.mkdir()
    env = os.environ | {
        "ISPINDEL_EVIDENCE_DIR": str(evidence),
        "ISPINDEL_SECRETS_DIR": str(secrets),
        "ISPINDEL_GID": "10001",
    }
    proc = subprocess.run(
        ["docker", "compose", "config", "--format", "json"],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )
    service = json.loads(proc.stdout)["services"]["ispindel-dashboard"]
    mounts = service["volumes"]
    sources = [m.get("source", "") for m in mounts if m["type"] == "bind"]
    assert str(evidence) in sources
    assert str(secrets) in sources
    production_sources = [source for source in sources if source not in {str(evidence), str(secrets)}]
    assert not any(
        source.startswith("/home/") or source.startswith("/tmp/")
        for source in production_sources
    )
    assert service["ports"] == [
        {
            "mode": "ingress",
            "host_ip": "127.0.0.1",
            "target": 8098,
            "published": "18098",
            "protocol": "tcp",
        }
    ]


def test_runtime_target_is_resolved_by_wrapper():
    proc = subprocess.run(
        [str(RUNNER), "contract"],
        cwd="/",
        text=True,
        capture_output=True,
        check=True,
    )
    body = json.loads(proc.stdout)
    assert body["source_root"] == str(ROOT)
    assert body["runtime_image_env"] == "PHASE05C_RUNTIME_IMAGE"
    assert body["runtime_source_env"] == "PHASE05C_RUNTIME_SOURCE_ROOT"
    assert body["manual_export_required"] is False


def test_network_boundary_verifier_renders_loopback_compose():
    proc = subprocess.run(
        [str(ROOT / "scripts" / "verify-network-boundary.sh")],
        cwd="/", text=True, capture_output=True, check=True,
    )
    assert proc.stdout.strip() == "NETWORK_BOUNDARY_OK fastapi=127.0.0.1:18098"


def test_caddy_systemd_unit_uses_absolute_pinned_runtime_path():
    unit = (ROOT / "ops/systemd/ispindel-caddy.service").read_text()
    assert "ExecStart=/usr/local/bin/caddy run" in unit
    assert "ExecStartPre=/usr/local/bin/caddy validate" in unit
    assert "EnvironmentFile=/etc/ispindel/caddy.env" in unit
    assert "AmbientCapabilities=CAP_NET_BIND_SERVICE" in unit
    assert "ProtectSystem=strict" in unit
    assert "WantedBy=multi-user.target" in unit
