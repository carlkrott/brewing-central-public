"""Deterministic System Health evidence contracts."""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path


NOW = datetime(2026, 7, 28, 12, 0, tzinfo=timezone.utc)


def _battery(percent: float, observed_at: str = "2026-07-28T11:59:00Z") -> dict:
    return {
        "schema_version": "health-evidence-v1", "kind": "battery",
        "observed_at": observed_at, "state": "ok", "detail": "battery sampled",
        "percent": percent, "charging": "unknown", "source": "linux", "host": "test",
    }


def _heartbeat(state: str = "ok", *, observed_at: str = "2026-07-28T11:59:00Z",
               poll_failed: bool = False, attempted: bool = False,
               delivered: bool = False) -> dict:
    return {
        "schema_version": "health-evidence-v1", "kind": "heartbeat",
        "observed_at": observed_at, "state": state, "detail": "poll completed",
        "poll_failed": poll_failed, "alert_attempted": attempted,
        "alert_delivered": delivered,
    }


def test_battery_parser_ok_warning_critical(app_module, tmp_path):
    path = tmp_path / "battery.json"
    for percent, expected in [(90, "ok"), (21, "ok"), (20, "warning"), (10, "critical")]:
        path.write_text(json.dumps(_battery(percent)))
        assert app_module.parse_battery_evidence(path, now=NOW)["status"] == expected


def test_battery_parser_stale_missing_parse_error(app_module, tmp_path):
    assert app_module.parse_battery_evidence(tmp_path / "missing", now=NOW)["status"] == "missing"
    bad = tmp_path / "bad"; bad.write_text("{")
    assert app_module.parse_battery_evidence(bad, now=NOW)["status"] == "parse_error"
    bad.write_text(json.dumps(_battery(101)))
    assert app_module.parse_battery_evidence(bad, now=NOW)["status"] == "parse_error"
    stale = tmp_path / "stale"; stale.write_text(json.dumps(_battery(90, "2026-07-28T11:00:00Z")))
    assert app_module.parse_battery_evidence(stale, now=NOW)["status"] == "stale"


def test_heartbeat_parser_ok_warning_critical_and_poll_failed(app_module, tmp_path):
    path = tmp_path / "heartbeat"
    for value, expected in [(_heartbeat("ok"), "ok"), (_heartbeat("warning"), "warning"),
                            (_heartbeat("ok", poll_failed=True), "critical")]:
        path.write_text(json.dumps(value))
        assert app_module.parse_heartbeat_evidence(path, now=NOW)["status"] == expected


def test_heartbeat_parser_stale_missing_parse_error(app_module, tmp_path):
    assert app_module.parse_heartbeat_evidence(tmp_path / "missing", now=NOW)["status"] == "missing"
    bad = tmp_path / "bad"; bad.write_text("not evidence")
    assert app_module.parse_heartbeat_evidence(bad, now=NOW)["status"] == "parse_error"
    stale = tmp_path / "stale"; stale.write_text(json.dumps(_heartbeat(observed_at="2026-07-28T10:00:00Z")))
    assert app_module.parse_heartbeat_evidence(stale, now=NOW)["status"] == "stale"


def test_battery_and_heartbeat_v1_round_trip(app_module, tmp_path):
    battery = tmp_path / "battery.json"; battery.write_text(json.dumps(_battery(90)))
    heartbeat = tmp_path / "heartbeat.json"; heartbeat.write_text(json.dumps(_heartbeat()))
    assert app_module.parse_battery_evidence(battery, now=NOW)["raw"]["kind"] == "battery"
    assert app_module.parse_heartbeat_evidence(heartbeat, now=NOW)["raw"]["kind"] == "heartbeat"


def test_stale_and_parse_error_never_green(app_module, tmp_path):
    evidence = tmp_path / "evidence.json"
    evidence.write_text(json.dumps(_heartbeat(observed_at="2026-07-28T10:00:00Z")))
    assert app_module.parse_heartbeat_evidence(evidence, now=NOW)["status"] != "ok"
    wrong = _heartbeat(); wrong["schema_version"] = "health-evidence-v2"
    evidence.write_text(json.dumps(wrong))
    assert app_module.parse_heartbeat_evidence(evidence, now=NOW)["status"] == "parse_error"
    wrong = _heartbeat(); wrong["observed_at"] = "2026-07-28T11:59:00+00:00"
    evidence.write_text(json.dumps(wrong))
    assert app_module.parse_heartbeat_evidence(evidence, now=NOW)["status"] == "parse_error"


def test_system_health_api_uses_configured_paths_and_worst_status(app_module, test_client, tmp_path, monkeypatch):
    current = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    battery = tmp_path / "battery"; battery.write_text(json.dumps(_battery(90, current)))
    heartbeat = tmp_path / "heartbeat"; heartbeat.write_text(json.dumps(_heartbeat(observed_at="2020-01-01T00:00:00Z")))
    monkeypatch.setattr(app_module, "BATTERY_PATH", battery); monkeypatch.setattr(app_module, "HEARTBEAT_PATH", heartbeat)
    body = test_client.get("/api/system-health").json()
    assert body["battery"]["status"] == "ok" and body["heartbeat"]["status"] == "stale" and body["status"] == "stale"


def test_missing_host_battery_falls_back_to_latest_device_telemetry(
    app_module, test_client, tmp_path, monkeypatch
):
    current = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    missing_battery = tmp_path / "missing-battery.json"
    heartbeat = tmp_path / "heartbeat.json"
    heartbeat.write_text(json.dumps(_heartbeat(observed_at=current)))
    monkeypatch.setattr(app_module, "BATTERY_PATH", missing_battery)
    monkeypatch.setattr(app_module, "HEARTBEAT_PATH", heartbeat)

    response = test_client.post("/api/ingest", json={
        "ID": "battery-device", "name": "Brew iSpindel",
        "battery": 4.7185, "interval": 300,
    })
    assert response.status_code == 200

    battery = test_client.get("/api/system-health").json()["battery"]
    assert battery["status"] == "ok"
    assert battery["scope"] == "device_telemetry"
    assert battery["device_id"] == "battery-device"
    assert battery["value"] == 4.7185
    assert battery["unit"] == "unknown"
    assert battery["charging"] == "unknown"
    assert "unit not declared" in battery["detail"]
    assert "charging state unavailable" in battery["detail"]


def test_compose_health_mount_is_directory_scoped_and_read_only():
    compose = Path("docker-compose.yml").read_text()
    assert "source: ${ISPINDEL_EVIDENCE_DIR:?" in compose
    assert "target: /health-evidence" in compose
    assert "read_only: true" in compose
    assert "battery-state.json:ro" not in compose
    assert "heartbeat.json:ro" not in compose


def test_health_responses_have_non_tls_security_headers(test_client):
    response = test_client.get("/health/live")
    assert response.status_code == 200
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["x-frame-options"] == "DENY"
    assert "strict-transport-security" not in response.headers


SUCCESS_BODY = {"status": "ok", "service": "ispindel-dashboard"}
DATABASE_UNAVAILABLE = {
    "status": "error",
    "service": "ispindel-dashboard",
    "detail": "database unavailable",
}
SCHEMA_INVALID = {
    "status": "error",
    "service": "ispindel-dashboard",
    "detail": "schema validation failed",
}


def _db_read_snapshot(path: Path) -> dict:
    with sqlite3.connect(path) as conn:
        return {
            "journal_mode": conn.execute("PRAGMA journal_mode").fetchone()[0],
            "user_version": conn.execute("PRAGMA user_version").fetchone()[0],
            "ledger": conn.execute(
                "SELECT version,applied_at FROM schema_migrations ORDER BY version"
            ).fetchall(),
            "schema": conn.execute(
                "SELECT type,name,sql FROM sqlite_master "
                "WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name"
            ).fetchall(),
            "counts": {
                table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in (
                    "devices",
                    "samples",
                    "calibrations",
                    "calibration_active",
                )
            },
        }


def test_compatibility_health_and_liveness_are_exact_and_process_only(
    app_module, test_client, tmp_path, monkeypatch
):
    assert test_client.get("/health").status_code == 200
    assert test_client.get("/health").json() == SUCCESS_BODY
    monkeypatch.setattr(app_module, "DB_PATH", tmp_path / "missing" / "ispindel.db")
    response = test_client.get("/health/live")
    assert response.status_code == 200
    assert response.json() == SUCCESS_BODY


def test_readiness_accepts_exact_v2_without_main_database_mutation(
    app_module, test_client, sqlite_tmp_path
):
    before = _db_read_snapshot(sqlite_tmp_path)
    before_sha = hashlib.sha256(sqlite_tmp_path.read_bytes()).hexdigest()
    response = test_client.get("/health/ready")
    after_sha = hashlib.sha256(sqlite_tmp_path.read_bytes()).hexdigest()
    after = _db_read_snapshot(sqlite_tmp_path)
    assert response.status_code == 200
    assert response.json() == SUCCESS_BODY
    assert after_sha == before_sha
    assert after == before


def test_readiness_reports_missing_database_without_leaking_path(
    app_module, test_client, tmp_path, monkeypatch
):
    missing = tmp_path / "not-created" / "ispindel.db"
    monkeypatch.setattr(app_module, "DB_PATH", missing)
    response = test_client.get("/health/ready")
    assert response.status_code == 503
    assert response.json() == DATABASE_UNAVAILABLE
    assert str(missing) not in response.text
    assert not missing.exists()


def test_readiness_reports_readable_non_v2_schema(
    app_module, test_client, tmp_path, monkeypatch
):
    malformed = tmp_path / "malformed.db"
    with sqlite3.connect(malformed) as conn:
        conn.execute("CREATE TABLE not_v2(value TEXT)")
    before = malformed.read_bytes()
    monkeypatch.setattr(app_module, "DB_PATH", malformed)
    response = test_client.get("/health/ready")
    assert response.status_code == 503
    assert response.json() == SCHEMA_INVALID
    assert malformed.read_bytes() == before


def test_readiness_closes_healthy_and_schema_rejected_connections(
    app_module, test_client, sqlite_tmp_path, tmp_path, monkeypatch
):
    real_connect = sqlite3.connect
    closed = []

    class TrackedConnection:
        def __init__(self, wrapped):
            self.wrapped = wrapped

        def __getattr__(self, name):
            return getattr(self.wrapped, name)

        def close(self):
            closed.append(True)
            self.wrapped.close()

    def tracking_connect(*args, **kwargs):
        return TrackedConnection(real_connect(*args, **kwargs))

    monkeypatch.setattr(app_module.sqlite3, "connect", tracking_connect)
    assert test_client.get("/health/ready").status_code == 200
    assert len(closed) == 1

    malformed = tmp_path / "tracked-malformed.db"
    with real_connect(malformed) as conn:
        conn.execute("CREATE TABLE not_v2(value TEXT)")
    monkeypatch.setattr(app_module, "DB_PATH", malformed)
    assert test_client.get("/health/ready").status_code == 503
    assert len(closed) == 2


def test_dockerfile_is_digest_pinned_nonroot_and_has_native_liveness():
    root = Path(__file__).resolve().parents[1]
    dockerfile = (root / "Dockerfile").read_text()
    assert dockerfile.splitlines()[0] == (
        "FROM python:3.12-slim@sha256:"
        "cab2dbf575e971934a81e4622f5aba17aa7929719bd7e31033a3a83b97fd0464"
    )
    assert "COPY ." not in dockerfile
    assert "USER 10001:10001" in dockerfile
    assert "EXPOSE 8098" in dockerfile
    assert "HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3" in dockerfile
    assert "http://127.0.0.1:8098/health/live" in dockerfile
    assert '"--port", "8098"' in dockerfile


def test_compose_parses_to_exact_runtime_hardening(tmp_path: Path):
    root = Path(__file__).resolve().parents[1]
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
        cwd=root,
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )
    service = json.loads(proc.stdout)["services"]["ispindel-dashboard"]
    assert service["user"] == "10001:10001"
    assert service["read_only"] is True
    assert service["tmpfs"] == ["/tmp:size=64m,mode=1777"]
    assert service["cap_drop"] == ["ALL"]
    assert service["security_opt"] == ["no-new-privileges:true"]
    assert service["command"] is None  # inherited from the reviewed Dockerfile
    assert service["ports"] == [
        {
            "mode": "ingress",
            "host_ip": "127.0.0.1",
            "target": 8098,
            "published": "18098",
            "protocol": "tcp",
        }
    ]
    mounts = {item["target"]: item for item in service["volumes"]}
    assert mounts["/data"]["type"] == "volume"
    assert mounts["/data"].get("read_only", False) is False
    assert mounts["/health-evidence"]["source"] == str(evidence)
    assert mounts["/health-evidence"]["read_only"] is True
    assert mounts["/run/secrets"]["source"] == str(secrets)
    assert mounts["/run/secrets"]["read_only"] is True
