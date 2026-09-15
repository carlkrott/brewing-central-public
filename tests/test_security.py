"""Authentication, request-boundary, host, and response-header contracts."""
from __future__ import annotations

import hashlib
import importlib
import json
import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.security import DeviceTokenBucket, PBKDF2_ITERATIONS

TOKEN = "test-only-phase4-token-with-adequate-entropy"
OTHER_TOKEN = "test-only-other-device-token-with-entropy"
OLD_TOKEN = "test-only-overlap-token-with-adequate-entropy"
DEVICE = "phase4-device"


def _verifier(token: str, salt: bytes, not_after: str | None = None) -> dict[str, object]:
    digest = hashlib.pbkdf2_hmac("sha256", token.encode(), salt, PBKDF2_ITERATIONS)
    return {"salt_hex": salt.hex(), "verifier_hex": digest.hex(), "not_after": not_after}


@pytest.fixture
def secured_app(monkeypatch, tmp_path: Path):
    future = (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat().replace("+00:00", "Z")
    token_file = tmp_path / "ingest-tokens.json"
    token_file.write_text(json.dumps({
        "schema_version": "ingest-tokens-v1",
        "devices": {
            DEVICE: {"verifiers": [
                _verifier(TOKEN, bytes.fromhex("10" * 16)),
                _verifier(OLD_TOKEN, bytes.fromhex("20" * 16), future),
            ]},
            "other-device": {"verifiers": [
                _verifier(OTHER_TOKEN, bytes.fromhex("30" * 16)),
            ]},
        },
    }))
    monkeypatch.setenv("ISPINDEL_MODE", "production")
    monkeypatch.setenv("ISPINDEL_ADMIN_CONFIGURED", "true")
    monkeypatch.setenv("TAILNET_FQDN", "ispindel.example.test")
    monkeypatch.setenv("ISPINDEL_TOKEN_FILE", str(token_file))
    monkeypatch.setenv("ISPINDEL_RATE_CAPACITY", "100")
    monkeypatch.setenv("ISPINDEL_RATE_REFILL_PER_SECOND", "1")
    monkeypatch.setenv("SQLITE_PATH", str(tmp_path / "secured.db"))
    import app.main as app_main
    app_main = importlib.reload(app_main)
    with TestClient(app_main.app) as client:
        yield app_main, client


def _payload(**extra: object) -> dict[str, object]:
    value: dict[str, object] = {
        "ID": DEVICE,
        "token": TOKEN,
        "angle": 30.2,
        "gravity": 1.042,
        "temperature": 20.1,
        "battery": 4.1,
        "RSSI": -70,
        "SSID": "Test WiFi",
        "sleep": 300,
    }
    value.update(extra)
    return value


def test_production_startup_fails_closed_without_security_configuration(tmp_path: Path):
    env = os.environ.copy()
    env.update({"ISPINDEL_MODE": "production", "SQLITE_PATH": str(tmp_path / "closed.db")})
    for key in ("ISPINDEL_ADMIN_CONFIGURED", "TAILNET_FQDN", "ISPINDEL_TOKEN_FILE"):
        env.pop(key, None)
    result = subprocess.run(
        [sys.executable, "-c", "import app.main"],
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode != 0
    assert "production requires ISPINDEL_ADMIN_CONFIGURED=true" in result.stderr


def test_ingest_rejects_missing_token(secured_app):
    _, client = secured_app
    payload = _payload()
    payload.pop("token")
    response = client.post("/api/ingest", json=payload)
    assert response.status_code == 401
    assert response.json() == {"detail": "invalid ingest credentials"}


def test_ingest_rejects_token_for_other_device(secured_app):
    _, client = secured_app
    response = client.post("/api/ingest", json=_payload(token=OTHER_TOKEN))
    assert response.status_code == 401
    assert response.json() == {"detail": "invalid ingest credentials"}


def test_ingest_accepts_stock_firmware_token_field(secured_app):
    _, client = secured_app
    response = client.post("/api/ingest", json=_payload())
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert client.post("/api/ingest", json=_payload(token=OLD_TOKEN, sample_id="old-overlap")).status_code == 200


def test_token_is_not_device_identity_or_persisted(secured_app):
    app_main, client = secured_app
    no_identity = client.post("/api/ingest", json={"token": TOKEN, "angle": 20})
    assert no_identity.status_code == 422
    assert client.post("/api/ingest", json=_payload(sample_id="persistence-check")).status_code == 200
    with sqlite3.connect(app_main.DB_PATH) as connection:
        rows = connection.execute("SELECT device_id,raw_json FROM samples").fetchall()
    assert rows[-1][0] == DEVICE
    assert TOKEN not in rows[-1][1]
    assert "token" not in json.loads(rows[-1][1])


def test_token_never_appears_in_logs_or_response(secured_app, caplog):
    _, client = secured_app
    response = client.post("/api/ingest", json=_payload(sample_id="secret-output"))
    assert response.status_code == 200
    assert TOKEN not in response.text
    assert TOKEN not in caplog.text


def test_ingest_rate_limit_and_retry_after(secured_app):
    app_main, client = secured_app
    app_main.INGEST_BUCKET = DeviceTokenBucket(2, 0.001)
    assert client.post("/api/ingest", json=_payload(sample_id="rate-1")).status_code == 200
    assert client.post("/api/ingest", json=_payload(sample_id="rate-2")).status_code == 200
    limited = client.post("/api/ingest", json=_payload(sample_id="rate-3"))
    assert limited.status_code == 429
    assert int(limited.headers["Retry-After"]) >= 1
    assert TOKEN not in limited.text


def test_ingest_body_limit_before_json_parse(secured_app):
    _, client = secured_app
    response = client.post(
        "/api/ingest",
        content=b'{' + (b'"padding":"' + b'x' * 70_000 + b'"}'),
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 413
    assert response.json() == {"detail": "request body too large"}


def test_ingest_rejects_unsupported_content_type(secured_app):
    _, client = secured_app
    response = client.post("/api/ingest", content=b"ID=x&token=y", headers={"content-type": "application/x-www-form-urlencoded"})
    assert response.status_code == 415


def test_untrusted_host_rejected(secured_app):
    _, client = secured_app
    response = client.get("/health", headers={"host": "attacker.invalid"})
    assert response.status_code == 400
    assert response.text == "Invalid host header"


def test_hsts_only_for_https(secured_app):
    app_main, client = secured_app
    http_response = client.get("http://testserver/health")
    assert "strict-transport-security" not in http_response.headers
    with TestClient(app_main.app, base_url="https://testserver") as https_client:
        https_response = https_client.get("/health")
    assert https_response.headers["strict-transport-security"] == "max-age=31536000"
    for response in (http_response, https_response):
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["referrer-policy"] == "no-referrer"
        assert response.headers["x-frame-options"] == "DENY"
        assert "frame-ancestors 'none'" in response.headers["content-security-policy"]


def test_caddy_split_ingress_contract_is_fail_closed():
    root = Path(__file__).resolve().parents[1]
    caddyfile = (root / "ops/caddy/Caddyfile").read_text()
    assert "http://{$ISPINDEL_LAN_IP}:8098" in caddyfile
    assert "bind {$ISPINDEL_LAN_IP}" in caddyfile
    assert "method POST" in caddyfile and "path /api/ingest" in caddyfile
    assert "max_size 64KB" in caddyfile
    assert "respond 404" in caddyfile
    assert "https://{$TAILNET_FQDN}" in caddyfile
    assert "bind {$ISPINDEL_TAILNET_IP}" in caddyfile
    assert "tls internal" in caddyfile and "basic_auth" in caddyfile
    assert "reverse_proxy 127.0.0.1:18098" in caddyfile


def test_caddy_overwrites_forwarding_headers_and_redacts_access_logs():
    root = Path(__file__).resolve().parents[1]
    caddyfile = (root / "ops/caddy/Caddyfile").read_text()
    for header in ("X-Forwarded-For", "X-Forwarded-Host", "X-Forwarded-Proto"):
        assert f"header_up -{header}" in caddyfile
        assert f"header_up {header}" in caddyfile
    assert "request>uri delete" in caddyfile
    assert "request>headers>Authorization delete" in caddyfile
    assert "request>headers>Cookie delete" in caddyfile


def test_headscale_candidate_is_one_narrow_https_grant():
    root = Path(__file__).resolve().parents[1]
    policy = json.loads((root / "ops/headscale/acl.candidate.json").read_text())
    assert policy["groups"] == {"group:ispindel-viewers": ["operator@example.test"]}
    viewer_rules = [r for r in policy["acls"] if r["src"] == ["group:ispindel-viewers"]]
    assert viewer_rules == [{
        "action": "accept",
        "src": ["group:ispindel-viewers"],
        "dst": ["tag:publisher:443"],
    }]
    assert not any(
        src == "group:ispindel-viewers" and dst != "tag:publisher:443"
        for rule in policy["acls"] for src in rule["src"] for dst in rule["dst"]
    )
