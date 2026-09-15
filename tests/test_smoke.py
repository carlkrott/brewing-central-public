"""Smoke tests - verify the harness itself is wired up correctly.

These are deliberately cheap, dependency-free tests that:
  * Confirm the app module reloaded with the per-test SQLITE_PATH.
  * Confirm /health returns 200 (sanity for both TestClient and live_server).
  * Confirm the Phase 0 requirements (TestClient + live Uvicorn) both reach
    the same FastAPI app instance via its routes.

Each test gets a unique SQLite file (auto-cleaned by tmp_path) so DB state
never bleeds across tests.
"""

from __future__ import annotations

from pathlib import Path

import httpx


def test_app_module_uses_temp_sqlite(app_module, sqlite_tmp_path: Path) -> None:
    """The module-level DB_PATH must reflect the SQLITE_PATH env var we set."""
    assert Path(app_module.DB_PATH) == sqlite_tmp_path, (
        f"DB_PATH {app_module.DB_PATH!r} does not match temp sqlite path "
        f"{sqlite_tmp_path!r}"
    )


def test_health_via_testclient(test_client) -> None:
    r = test_client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body == {"status": "ok", "service": "ispindel-dashboard"}


def test_health_via_live_server(live_server: str) -> None:
    """Phase 0 requires a live Uvicorn fixture - this proves it's wired up."""
    with httpx.Client(timeout=2.0) as client:
        r = client.get(f"{live_server}/health")
    assert r.status_code == 200
    body = r.json()
    assert body == {"status": "ok", "service": "ispindel-dashboard"}


def test_dashboard_html_served(test_client) -> None:
    r = test_client.get("/")
    assert r.status_code == 200
    # The chart container exists in the HTML (id matches what JS expects)
    assert 'id="chartContainer"' in r.text
    assert 'id="chartCanvas"' in r.text


def test_ingest_then_list_samples(test_client) -> None:
    """Ingest a single sample and verify it round-trips through the API."""
    payload = {
        "ID": "test-device-001",
        "angle": 30.5,
        "gravity": 1.045,
        "temperature": 19.2,
        "battery": 4.1,
        "rssi": -67,
        "SSID": "MyBrew",
        "interval": 300,
    }
    r = test_client.post("/api/ingest", json=payload)
    assert r.status_code == 200
    assert r.json()["device_id"] == "test-device-001"

    r2 = test_client.get("/api/devices")
    assert r2.status_code == 200
    devices = r2.json()["devices"]
    assert any(d["device_id"] == "test-device-001" for d in devices)

    r3 = test_client.get("/api/device/test-device-001/samples?hours=24")
    assert r3.status_code == 200
    samples = r3.json()["samples"]
    assert len(samples) == 1
    assert samples[0]["gravity"] == 1.045
