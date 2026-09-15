"""Phase 3 samples/query contract tests.

Covers effective-timestamp filtering and ordering (COALESCE expression),
backward-compatible `ts` aliasing, provenance fields, and same-time
changed-payload retention through the read API.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import inspect

import pytest


def _firmware_payload(**extra):
    payload = {"ID": "phase3-device", "angle": 30.2, "gravity": 1.042,
               "temperature": 20.1, "battery": 4.1, "RSSI": -70,
               "SSID": "Brew WiFi", "sleep": 300}
    payload.update(extra)
    return payload


def test_effective_timestamp_uses_measured_at_when_present(test_client):
    """`ts` field in the response equals measured_at when it exists."""
    measured = "2026-07-28T10:00:00Z"
    test_client.post("/api/ingest", json=_firmware_payload(measured_at=measured, angle=30.2))
    samples = test_client.get("/api/device/phase3-device/samples?hours=100000").json()["samples"]
    assert len(samples) == 1
    s = samples[-1]
    expected = "2026-07-28T10:00:00+00:00"
    assert s["ts"] == expected
    assert s["measured_at"] == expected
    assert s["received_at"] is not None


def test_effective_timestamp_falls_back_to_received_at(test_client):
    """When measured_at is null, ts equals received_at."""
    test_client.post("/api/ingest", json=_firmware_payload(angle=30.2))
    samples = test_client.get("/api/device/phase3-device/samples").json()["samples"]
    s = samples[-1]
    assert s["measured_at"] is None
    # `ts` is the effective timestamp = COALESCE(measured_at, received_at)
    assert s["ts"] == s["received_at"]


def test_sample_response_includes_provenance_fields(test_client):
    test_client.post("/api/ingest", json=_firmware_payload(
        measured_at="2026-07-28T10:00:00Z", battery=3.95
    ))
    samples = test_client.get("/api/device/phase3-device/samples?hours=100000").json()["samples"]
    s = samples[-1]
    for key in ("received_at", "measured_at", "battery_value", "battery_unit",
                "battery_source", "ts", "battery"):
        assert key in s, f"missing key {key!r} in sample response"


def test_window_filter_uses_effective_timestamp(test_client):
    """Window filter is COALESCE(measured_at, received_at) >= cutoff."""
    # Two recent samples (no measured_at)
    test_client.post("/api/ingest", json=_firmware_payload(angle=30.0))
    test_client.post("/api/ingest", json=_firmware_payload(angle=30.5))
    # hours=24 default cutoff should include both
    samples = test_client.get("/api/device/phase3-device/samples?hours=24").json()["samples"]
    assert len(samples) == 2


def test_samples_ordered_by_effective_timestamp(test_client):
    """Multiple samples should be returned in effective-time ascending order."""
    test_client.post("/api/ingest", json=_firmware_payload(measured_at="2026-07-28T10:00:00Z", angle=10.0))
    test_client.post("/api/ingest", json=_firmware_payload(measured_at="2026-07-28T11:00:00Z", angle=20.0))
    test_client.post("/api/ingest", json=_firmware_payload(measured_at="2026-07-28T09:00:00Z", angle=30.0))

    samples = test_client.get("/api/device/phase3-device/samples?hours=100000").json()["samples"]
    angles = [s["angle"] for s in samples]
    # 09:00 → angle=30.0 ; 10:00 → 10.0 ; 11:00 → 20.0
    assert angles == [30.0, 10.0, 20.0]


def test_legacy_query_alias_device_name_still_effective(test_client):
    """`device_name` alias in the samples API response equals effective name."""
    test_client.post("/api/ingest", json=_firmware_payload(name="Reported"))
    test_client.patch("/api/device/phase3-device", json={"device_name": "Override"})
    body = test_client.get("/api/device/phase3-device/samples").json()
    assert body["device_name"] == "Override"


def test_device_id_and_window_count(test_client):
    test_client.post("/api/ingest", json=_firmware_payload(angle=10.0))
    test_client.post("/api/ingest", json=_firmware_payload(angle=20.0))
    body = test_client.get("/api/device/phase3-device/samples?hours=24").json()
    assert body["device_id"] == "phase3-device"
    assert body["count"] == 2
    assert body["window_hours"] == 24


def test_fractional_sample_windows_and_short_range_controls(test_client):
    test_client.post("/api/ingest", json=_firmware_payload(angle=10.0))
    response = test_client.get("/api/device/phase3-device/samples?hours=0.1666666667")
    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 1
    assert body["window_hours"] == pytest.approx(1 / 6)

    html = test_client.get("/").text
    assert '<option value="0.1666666667">Last 10 Minutes</option>' in html
    assert '<option value="0.5">Last 30 Minutes</option>' in html
    assert '<option value="1">Last 1 Hour</option>' in html


def test_same_measured_time_changed_payload_visible(test_client):
    """Same measured_at but changed telemetry → both rows surface in the read API."""
    ts = "2026-07-28T10:00:00Z"
    test_client.post("/api/ingest", json=_firmware_payload(measured_at=ts, angle=30.2, gravity=1.042))
    test_client.post("/api/ingest", json=_firmware_payload(measured_at=ts, angle=31.5, gravity=1.05))

    body = test_client.get("/api/device/phase3-device/samples?hours=100000").json()
    angles = sorted(s["angle"] for s in body["samples"])
    assert angles == [30.2, 31.5]
    # Both rows share the same measured_at
    measured = {s["measured_at"] for s in body["samples"]}
    assert measured == {"2026-07-28T10:00:00+00:00"}


def test_status_uses_effective_override_interval(test_client, app_module):
    """Status staleness must honor user_interval_sec, not a stale legacy mirror."""
    assert test_client.post("/api/ingest", json=_firmware_payload()).status_code == 200
    old = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    with app_module.db() as conn:
        conn.execute(
            "UPDATE devices SET last_seen=?, expected_interval_sec=5, "
            "reported_interval_sec=5, user_interval_sec=86400 WHERE device_id=?",
            (old, "phase3-device"),
        )
        conn.commit()
    body = test_client.get("/api/status").json()
    assert body["stale_devices"] == 0


def test_status_query_uses_effective_latest_sample_timestamp(app_module):
    """The status aggregate must not regress to MAX(ts) after Phase 3."""
    source = inspect.getsource(app_module.status)
    assert "MAX(COALESCE(measured_at, received_at))" in source
    assert "MAX(ts)" not in source


def test_dashboard_battery_units_derive_from_sample_provenance(test_client):
    """Unknown or percent readings must never be mislabeled as volts."""
    html = test_client.get("/").text
    js = (Path(__file__).resolve().parents[1] / "app/static/dashboard.js").read_text()
    assert "function metricUnit" in js
    assert "point.battery_unit" in js
    assert "chart.data.datasets[i].label = m.label" in js
    assert "chart.options.scales['y_' + m.id].title.text = m.unit" in js
    assert "unitEl.textContent = m === 'battery' ? '?'" in js
    assert "label: 'Battery (V)'" not in js
    assert 'data-metric="battery"' in html
    battery_pill = html.split('data-metric="battery"', 1)[1].split('</div></div>', 1)[0]
    assert '<span class="unit">?</span>' in battery_pill


def test_chart_timestamp_parser_accepts_api_utc_offset() -> None:
    """The API emits +00:00; the browser must not append a second Z."""
    js = (Path(__file__).resolve().parents[1] / "app/static/dashboard.js").read_text()
    assert r"/[+-]\d{2}:?\d{2}$/" in js
    assert r"/[+-]\\d{2}:?\\d{2}$/" not in js


def _save_linear_calibration(test_client, device_id="phase3-device", activate=True):
    response = test_client.post(f"/api/device/{device_id}/calibration", json={
        "label": "sample calibration", "activate": activate,
        "points": [{"angle": 10.0, "value": 1.0}, {"angle": 20.0, "value": 1.1}],
    })
    assert response.status_code == 200, response.text
    return response.json()


def test_samples_preserve_legacy_shape_and_raw_gravity(test_client):
    test_client.post("/api/ingest", json=_firmware_payload(angle=20.0, gravity=1.042))
    calibration = _save_linear_calibration(test_client)
    body = test_client.get("/api/device/phase3-device/samples").json()
    sample = body["samples"][-1]
    assert sample["gravity"] == 1.042
    assert sample["raw_gravity"] == sample["gravity"]
    assert sample["calibrated_gravity"] == 1.1
    assert sample["calibration_id"] == calibration["id"]
    assert body["active_calibration_id"] == calibration["id"]


def test_samples_without_active_or_angle_return_null_calibrated(test_client):
    test_client.post("/api/ingest", json=_firmware_payload(angle=20.0, gravity=1.042))
    body = test_client.get("/api/device/phase3-device/samples").json()
    assert body["active_calibration_id"] is None
    sample = body["samples"][-1]
    assert sample["gravity"] == sample["raw_gravity"] == 1.042
    assert sample["calibrated_gravity"] is None
    assert sample["calibration_id"] is None


def test_samples_with_null_angle_return_null_calibrated_gravity(test_client):
    test_client.post("/api/ingest", json=_firmware_payload(angle=None, gravity=1.042))
    calibration = _save_linear_calibration(test_client)
    sample = test_client.get("/api/device/phase3-device/samples").json()["samples"][-1]
    assert sample["angle"] is None
    assert sample["calibrated_gravity"] is None
    assert sample["calibration_id"] == calibration["id"]


def test_samples_active_calibration_uses_one_snapshot(test_client):
    test_client.post("/api/ingest", json=_firmware_payload(angle=10.0, gravity=1.01))
    test_client.post("/api/ingest", json=_firmware_payload(angle=20.0, gravity=1.02))
    first = _save_linear_calibration(test_client)
    second = test_client.post("/api/device/phase3-device/calibration", json={
        "label": "second", "points": [
            {"angle": 10.0, "value": 1.05}, {"angle": 20.0, "value": 1.15}
        ]}).json()
    assert first["id"] != second["id"]
    body = test_client.get("/api/device/phase3-device/samples").json()
    assert body["active_calibration_id"] == second["id"]
    assert {sample["calibration_id"] for sample in body["samples"]} == {second["id"]}


def test_calibration_does_not_mutate_persisted_samples_or_raw_json(test_client, app_module):
    test_client.post("/api/ingest", json=_firmware_payload(angle=20.0, gravity=1.042))
    with app_module.db() as conn:
        before = [tuple(row) for row in conn.execute("SELECT * FROM samples ORDER BY id")]
    _save_linear_calibration(test_client)
    test_client.get("/api/device/phase3-device/samples")
    with app_module.db() as conn:
        after = [tuple(row) for row in conn.execute("SELECT * FROM samples ORDER BY id")]
    assert after == before


def test_samples_quadratic_horner_exact_fixture(test_client):
    assert test_client.post(
        "/api/ingest", json=_firmware_payload(angle=3.0, gravity=1.08)
    ).status_code == 200
    calibration = test_client.post(
        "/api/device/phase3-device/calibration",
        json={
            "label": "quadratic fixture",
            "order": 2,
            "points": [
                {"angle": 0.0, "value": 1.0},
                {"angle": 1.0, "value": 1.03},
                {"angle": 2.0, "value": 1.08},
                {"angle": 3.0, "value": 1.15},
            ],
        },
    )
    assert calibration.status_code == 200, calibration.text
    body = test_client.get("/api/device/phase3-device/samples").json()
    sample = body["samples"][-1]
    assert sample["calibration_id"] == calibration.json()["id"]
    assert sample["calibrated_gravity"] == 1.1499999999999997
