"""Phase 3 ingest contract tests.

Covers:
  * override persistence / explicit clear / omitted semantics
  * explicit & ambiguous battery provenance, conflicting representations
  * timestamp alias acceptance, naive/malformed/pre-2000/future rejections
  * retry duplicate / conflict / one-hour horizon / no-dedup paths
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
import sqlite3


def _firmware_payload(**extra):
    payload = {"ID": "phase3-device", "angle": 30.2, "gravity": 1.042,
               "temperature": 20.1, "battery": 4.1, "RSSI": -70,
               "SSID": "Brew WiFi", "sleep": 300}
    payload.update(extra)
    return payload


def test_stock_numeric_id_is_normalized_to_string(test_client):
    response = test_client.post(
        "/api/ingest", json=_firmware_payload(ID=12345678)
    )
    assert response.status_code == 200, response.text
    devices = test_client.get("/api/devices").json()["devices"]
    assert any(device["device_id"] == "12345678" for device in devices)


@pytest.mark.parametrize(
    "invalid_id",
    [True, False, 0, -1, 0x1_0000_0000, 10**60, 42.0, 1.5],
)
def test_invalid_numeric_device_ids_return_422(test_client, invalid_id):
    response = test_client.post(
        "/api/ingest", json=_firmware_payload(ID=invalid_id)
    )
    assert response.status_code == 422, response.text


# ---------------------------------------------------------------------------
# Override / PATCH semantics
# ---------------------------------------------------------------------------


def test_ingest_writes_reported_name_and_does_not_overwrite_user_override(test_client):
    test_client.post("/api/ingest", json=_firmware_payload(name="Reported Name"))
    test_client.patch(
        "/api/device/phase3-device",
        json={"device_name": "My Override"},
    )
    test_client.post("/api/ingest", json=_firmware_payload(name="Fresh Reported"))
    device = next(
        d for d in test_client.get("/api/devices").json()["devices"]
        if d["device_id"] == "phase3-device"
    )
    assert device["user_device_name"] == "My Override"
    assert device["reported_device_name"] == "Fresh Reported"
    assert device["device_name"] == "My Override"
    assert device["effective_device_name"] == "My Override"


def test_patch_with_null_device_name_clears_user_override(test_client):
    test_client.post("/api/ingest", json=_firmware_payload(name="Reported"))
    test_client.patch("/api/device/phase3-device", json={"device_name": "Override"})
    test_client.patch("/api/device/phase3-device", json={"device_name": None})
    device = next(
        d for d in test_client.get("/api/devices").json()["devices"]
        if d["device_id"] == "phase3-device"
    )
    assert device["user_device_name"] is None
    assert device["device_name"] == "Reported"
    assert device["effective_device_name"] == "Reported"


def test_patch_with_empty_string_clears_user_override(test_client):
    test_client.post("/api/ingest", json=_firmware_payload(name="Reported"))
    test_client.patch("/api/device/phase3-device", json={"device_name": "Override"})
    test_client.patch("/api/device/phase3-device", json={"device_name": ""})
    device = next(
        d for d in test_client.get("/api/devices").json()["devices"]
        if d["device_id"] == "phase3-device"
    )
    assert device["user_device_name"] is None
    assert device["device_name"] == "Reported"


def test_patch_omitted_field_does_not_clear_existing_override(test_client):
    test_client.post("/api/ingest", json=_firmware_payload(name="Reported"))
    test_client.patch("/api/device/phase3-device", json={"device_name": "Override"})
    test_client.patch("/api/device/phase3-device", json={"expected_interval_sec": 600})
    device = next(
        d for d in test_client.get("/api/devices").json()["devices"]
        if d["device_id"] == "phase3-device"
    )
    assert device["user_device_name"] == "Override"
    assert device["user_interval_sec"] == 600


def test_patch_null_expected_interval_clears_user_override(test_client):
    test_client.post("/api/ingest", json=_firmware_payload())
    test_client.patch("/api/device/phase3-device", json={"expected_interval_sec": 600})
    test_client.patch("/api/device/phase3-device", json={"expected_interval_sec": None})
    device = next(
        d for d in test_client.get("/api/devices").json()["devices"]
        if d["device_id"] == "phase3-device"
    )
    assert device["user_interval_sec"] is None


# ---------------------------------------------------------------------------
# Battery semantics
# ---------------------------------------------------------------------------


def test_explicit_battery_voltage_validates_and_stores_unit(test_client):
    response = test_client.post("/api/ingest", json=_firmware_payload(
        battery_voltage=3.91, battery=3.91, battery_unit="V"
    ))
    assert response.status_code == 200
    samples = test_client.get("/api/device/phase3-device/samples").json()["samples"]
    assert samples
    s = samples[-1]
    assert s["battery_value"] == pytest.approx(3.91)
    assert s["battery_unit"] == "V"
    assert s["battery_source"] == "explicit_voltage"
    assert s["battery"] == pytest.approx(3.91)


def test_explicit_battery_percent_validates_and_stores_unit(test_client):
    response = test_client.post("/api/ingest", json=_firmware_payload(
        battery_percent=42, battery_unit="%"
    ))
    assert response.status_code == 200
    samples = test_client.get("/api/device/phase3-device/samples").json()["samples"]
    s = samples[-1]
    assert s["battery_value"] == pytest.approx(42)
    assert s["battery_unit"] == "%"
    assert s["battery_source"] == "explicit_percent"


def test_explicit_voltage_out_of_range_is_422(test_client):
    assert test_client.post(
        "/api/ingest",
        json=_firmware_payload(battery_voltage=7.0, battery_unit="V"),
    ).status_code == 422
    assert test_client.post(
        "/api/ingest",
        json=_firmware_payload(battery_voltage=-0.1, battery_unit="V"),
    ).status_code == 422


def test_explicit_percent_out_of_range_is_422(test_client):
    assert test_client.post(
        "/api/ingest",
        json=_firmware_payload(battery_percent=120, battery_unit="%"),
    ).status_code == 422


def test_bare_battery_without_unit_is_ambiguous_unknown(test_client):
    test_client.post("/api/ingest", json=_firmware_payload(battery=3.95))
    samples = test_client.get("/api/device/phase3-device/samples").json()["samples"]
    s = samples[-1]
    assert s["battery_unit"] == "unknown"
    assert s["battery_source"] == "ambiguous_unknown"
    assert s["battery_value"] == pytest.approx(3.95)


def test_bare_battery_with_explicit_unit_uses_explicit_unit_source(test_client):
    test_client.post("/api/ingest", json=_firmware_payload(battery=3.95, battery_unit="V"))
    samples = test_client.get("/api/device/phase3-device/samples").json()["samples"]
    s = samples[-1]
    assert s["battery_unit"] == "V"
    assert s["battery_source"] == "explicit_unit"


def test_batt_alias_follows_same_rules(test_client):
    test_client.post("/api/ingest", json=_firmware_payload(battery=None, batt=3.97))
    samples = test_client.get("/api/device/phase3-device/samples").json()["samples"]
    s = samples[-1]
    assert s["battery_unit"] == "unknown"
    assert s["battery_source"] == "ambiguous_unknown"
    assert s["battery_value"] == pytest.approx(3.97)


def test_conflicting_battery_representations_is_422(test_client):
    assert test_client.post(
        "/api/ingest",
        json=_firmware_payload(
            battery_voltage=3.91,
            battery_percent=42,
            battery_unit="V",
        ),
    ).status_code == 422


def test_omitted_battery_is_null_unknown(test_client):
    test_client.post("/api/ingest", json=_firmware_payload(battery=None))
    samples = test_client.get("/api/device/phase3-device/samples").json()["samples"]
    s = samples[-1]
    assert s["battery_value"] is None
    assert s["battery_unit"] == "unknown"
    assert s["battery_source"] in {"omitted", None}


# ---------------------------------------------------------------------------
# Timestamp semantics
# ---------------------------------------------------------------------------


def test_iso_with_z_is_accepted_and_normalised(test_client):
    ts = "2026-07-28T10:00:00Z"
    test_client.post("/api/ingest", json=_firmware_payload(measured_at=ts))
    samples = test_client.get("/api/device/phase3-device/samples?hours=100000").json()["samples"]
    s = samples[-1]
    assert s["measured_at"] is not None
    parsed = datetime.fromisoformat(s["measured_at"].replace("Z", "+00:00"))
    assert parsed == datetime(2026, 7, 28, 10, 0, 0, tzinfo=timezone.utc)


def test_iso_with_offset_is_accepted(test_client):
    ts = "2026-07-28T12:00:00+02:00"
    test_client.post("/api/ingest", json=_firmware_payload(measured_at=ts))
    samples = test_client.get("/api/device/phase3-device/samples?hours=100000").json()["samples"]
    parsed = datetime.fromisoformat(samples[-1]["measured_at"].replace("Z", "+00:00"))
    assert parsed == datetime(2026, 7, 28, 10, 0, 0, tzinfo=timezone.utc)


def test_timestamp_alias_is_accepted(test_client):
    ts = "2026-07-28T10:00:00Z"
    test_client.post("/api/ingest", json=_firmware_payload(timestamp=ts))
    samples = test_client.get("/api/device/phase3-device/samples?hours=100000").json()["samples"]
    assert samples[-1]["measured_at"] is not None


def test_time_alias_is_accepted(test_client):
    ts = "2026-07-28T10:00:00Z"
    test_client.post("/api/ingest", json=_firmware_payload(time=ts))
    samples = test_client.get("/api/device/phase3-device/samples?hours=100000").json()["samples"]
    assert samples[-1]["measured_at"] is not None


def test_unix_epoch_seconds_are_accepted(test_client):
    epoch = int(datetime(2026, 7, 28, 10, 0, 0, tzinfo=timezone.utc).timestamp())
    test_client.post("/api/ingest", json=_firmware_payload(measured_at=epoch))
    samples = test_client.get("/api/device/phase3-device/samples?hours=100000").json()["samples"]
    parsed = datetime.fromisoformat(samples[-1]["measured_at"].replace("Z", "+00:00"))
    assert parsed == datetime(2026, 7, 28, 10, 0, 0, tzinfo=timezone.utc)


def test_unix_epoch_milliseconds_are_accepted(test_client):
    epoch_ms = int(datetime(2026, 7, 28, 10, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)
    test_client.post("/api/ingest", json=_firmware_payload(measured_at=epoch_ms))
    samples = test_client.get("/api/device/phase3-device/samples?hours=100000").json()["samples"]
    parsed = datetime.fromisoformat(samples[-1]["measured_at"].replace("Z", "+00:00"))
    assert parsed == datetime(2026, 7, 28, 10, 0, 0, tzinfo=timezone.utc)


def test_naive_datetime_is_422(test_client):
    assert test_client.post(
        "/api/ingest",
        json=_firmware_payload(measured_at="2026-07-28T10:00:00"),
    ).status_code == 422


def test_pre_2000_timestamp_is_422(test_client):
    assert test_client.post(
        "/api/ingest",
        json=_firmware_payload(measured_at="1999-12-31T23:59:59Z"),
    ).status_code == 422


def test_far_future_timestamp_is_422(test_client):
    far = datetime.now(timezone.utc) + timedelta(hours=2)
    assert test_client.post(
        "/api/ingest",
        json=_firmware_payload(measured_at=far.isoformat()),
    ).status_code == 422


def test_malformed_timestamp_is_422(test_client):
    assert test_client.post(
        "/api/ingest",
        json=_firmware_payload(measured_at="not-a-date"),
    ).status_code == 422


def test_injected_at_is_ignored(test_client):
    """_injected_at must NEVER be promoted to measured_at."""
    test_client.post(
        "/api/ingest",
        json=_firmware_payload(_injected_at="2026-07-28T10:00:00Z"),
    )
    samples = test_client.get("/api/device/phase3-device/samples").json()["samples"]
    assert samples[-1]["measured_at"] is None


def test_omitted_measured_at_yields_null(test_client):
    test_client.post("/api/ingest", json=_firmware_payload())
    samples = test_client.get("/api/device/phase3-device/samples").json()["samples"]
    assert samples[-1]["measured_at"] is None


# ---------------------------------------------------------------------------
# Retry / dedup semantics
# ---------------------------------------------------------------------------


def _row_received_at(app_module, sample_event_id, new_iso):
    with sqlite3.connect(app_module.DB_PATH) as conn:
        conn.execute(
            "UPDATE samples SET received_at=? WHERE sample_event_id=?",
            (new_iso, sample_event_id),
        )
        conn.commit()


def test_stable_id_same_payload_within_hour_is_duplicate(test_client, app_module):
    payload = _firmware_payload(sample_id="evt-001", angle=30.2, gravity=1.042)
    r1 = test_client.post("/api/ingest", json=payload)
    assert r1.status_code == 200
    body1 = r1.json()
    assert body1.get("status") == "ok"
    assert not body1.get("duplicate", False)

    r2 = test_client.post("/api/ingest", json=payload)
    assert r2.status_code == 200
    body2 = r2.json()
    assert body2.get("duplicate") is True
    assert body2.get("sample_id") == body1.get("sample_id")

    samples = test_client.get("/api/device/phase3-device/samples").json()["samples"]
    assert len(samples) == 1


def test_stable_id_changed_payload_within_hour_is_409(test_client, app_module):
    payload = _firmware_payload(sample_id="evt-002", angle=30.2)
    r1 = test_client.post("/api/ingest", json=payload)
    assert r1.status_code == 200

    changed = _firmware_payload(sample_id="evt-002", angle=31.0, gravity=1.05)
    r2 = test_client.post("/api/ingest", json=changed)
    assert r2.status_code == 409

    samples = test_client.get("/api/device/phase3-device/samples").json()["samples"]
    assert len(samples) == 1
    assert samples[-1]["angle"] == 30.2


def test_stable_id_after_one_hour_window_is_accepted(test_client, app_module):
    payload = _firmware_payload(sample_id="evt-003", angle=30.2)
    r1 = test_client.post("/api/ingest", json=payload)
    assert r1.status_code == 200

    old = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    _row_received_at(app_module, "evt-003", old)

    r2 = test_client.post("/api/ingest", json=payload)
    assert r2.status_code == 200
    assert not (r2.json().get("duplicate", False))

    samples = test_client.get("/api/device/phase3-device/samples").json()["samples"]
    assert len(samples) == 2


def test_measured_time_same_payload_is_duplicate(test_client):
    ts = "2026-07-28T10:00:00Z"
    payload = _firmware_payload(measured_at=ts, angle=30.2, gravity=1.042)
    r1 = test_client.post("/api/ingest", json=payload)
    assert r1.status_code == 200

    r2 = test_client.post("/api/ingest", json=payload)
    assert r2.status_code == 200
    assert r2.json().get("duplicate") is True


def test_same_measured_time_changed_telemetry_is_retained(test_client):
    ts = "2026-07-28T10:00:00Z"
    payload = _firmware_payload(measured_at=ts, angle=30.2, gravity=1.042)
    r1 = test_client.post("/api/ingest", json=payload)
    assert r1.status_code == 200
    body1 = r1.json()
    assert not body1.get("duplicate", False)

    payload2 = _firmware_payload(measured_at=ts, angle=31.5, gravity=1.05)
    r2 = test_client.post("/api/ingest", json=payload2)
    assert r2.status_code == 200
    assert not r2.json().get("duplicate", False)

    samples = test_client.get("/api/device/phase3-device/samples?hours=100000").json()["samples"]
    assert len(samples) == 2
    angles = sorted(s["angle"] for s in samples)
    assert angles == [30.2, 31.5]


def test_no_id_no_time_is_always_append_only(test_client):
    payload = _firmware_payload(angle=30.2, gravity=1.042)
    for _ in range(3):
        r = test_client.post("/api/ingest", json=payload)
        assert r.status_code == 200
        assert not r.json().get("duplicate", False)

    samples = test_client.get("/api/device/phase3-device/samples").json()["samples"]
    assert len(samples) == 3


def test_post_horizon_reuse_of_stable_id_inserts_new_row(test_client, app_module):
    payload = _firmware_payload(sample_id="evt-wrap", angle=30.2)
    r1 = test_client.post("/api/ingest", json=payload)
    assert r1.status_code == 200

    old = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    _row_received_at(app_module, "evt-wrap", old)

    r2 = test_client.post("/api/ingest", json=_firmware_payload(sample_id="evt-wrap", angle=33.3))
    assert r2.status_code == 200
    assert not r2.json().get("duplicate", False)

    samples = test_client.get("/api/device/phase3-device/samples").json()["samples"]
    assert len(samples) == 2


def test_payload_hash_is_deterministic_for_equivalent_telemetry(test_client):
    """Equivalent normalized telemetry (same measured_at) must dedup, even
    when the raw JSON differs in key order or whitespace."""
    payload1 = _firmware_payload(measured_at="2026-07-28T10:00:00Z",
                                angle=30.2, gravity=1.042, battery=3.95)
    payload2 = {"ID": "phase3-device", "angle": 30.2, "gravity": 1.042,
                "temperature": 20.1, "battery": 3.95, "RSSI": -70,
                "SSID": "Brew WiFi", "sleep": 300,
                "measured_at": "2026-07-28T10:00:00Z"}
    r1 = test_client.post("/api/ingest", json=payload1)
    assert r1.status_code == 200
    r2 = test_client.post("/api/ingest", json=payload2)
    assert r2.status_code == 200
    assert r2.json().get("duplicate") is True


def test_token_is_not_a_device_identity_alias(test_client):
    response = test_client.post(
        "/api/ingest",
        json={"token": "test-only-not-an-identity", "angle": 30.2},
    )
    assert response.status_code == 422
    assert response.json() == {"detail": "stable device identity is required"}


def test_device_identity_priority_is_id_then_lower_id_then_device_id_then_name(test_client):
    payload = _firmware_payload(
        ID="priority-ID",
        id="lower-id",
        device_id="device-id",
        name="reported-name",
        token="test-only-credential",
        sample_id="identity-priority",
    )
    assert test_client.post("/api/ingest", json=payload).status_code == 200
    devices = test_client.get("/api/devices").json()["devices"]
    assert any(device["device_id"] == "priority-ID" for device in devices)
    assert all(device["device_id"] != "test-only-credential" for device in devices)