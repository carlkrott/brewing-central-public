"""Operating-intent, camera-policy, and honest alert contracts."""
from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app import camera_observer
from app.brewing import BrewingStore

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"


def _load_script(name: str, path: Path):
    sys.path.insert(0, str(SCRIPTS))
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _ingest(client, device_id: str) -> None:
    response = client.post(
        "/api/ingest",
        json={"ID": device_id, "angle": 24.0, "gravity": 1.0, "temperature": 20.0},
    )
    assert response.status_code == 200, response.text


def _set_last_seen(app_module, device_id: str, value: str) -> None:
    with sqlite3.connect(app_module.DB_PATH) as conn:
        conn.execute("UPDATE devices SET last_seen=? WHERE device_id=?", (value, device_id))
        conn.commit()


def test_operating_intent_defaults_and_round_trip(test_client):
    _ingest(test_client, "intent-device")

    device = test_client.get("/api/devices").json()["devices"][0]
    assert device["operating_mode"] == "brewing"
    assert device["camera_policy"] == "active_brew_structural"
    assert device["operating_intent_configured"] is False

    default = test_client.get("/api/device/intent-device/operating-intent")
    assert default.status_code == 200
    assert default.json()["mode"] == "brewing"
    assert default.json()["configured"] is False

    changed = test_client.put(
        "/api/device/intent-device/operating-intent",
        json={"mode": "stored", "camera_policy": "off"},
    )
    assert changed.status_code == 200
    assert changed.json()["mode"] == "stored"
    assert changed.json()["camera_policy"] == "off"
    assert changed.json()["configured"] is True

    listed = test_client.get("/api/devices").json()["devices"][0]
    assert listed["operating_mode"] == "stored"
    assert listed["camera_policy"] == "off"
    assert listed["operating_intent_configured"] is True


def test_operating_intent_rejects_unknown_values_and_unknown_device(test_client):
    _ingest(test_client, "intent-validation-device")

    bad_mode = test_client.put(
        "/api/device/intent-validation-device/operating-intent",
        json={"mode": "sleeping", "camera_policy": "off"},
    )
    assert bad_mode.status_code == 422

    bad_camera = test_client.put(
        "/api/device/intent-validation-device/operating-intent",
        json={"mode": "stored", "camera_policy": "always_stream"},
    )
    assert bad_camera.status_code == 422

    missing = test_client.get("/api/device/no-such-device/operating-intent")
    assert missing.status_code == 404


def test_operating_intent_persists_when_brewing_store_is_reopened(tmp_path):
    path = tmp_path / "brew.db"
    first = BrewingStore(path)
    first.initialize()
    first.set_operating_intent("persist-device", "stored", "off")

    second = BrewingStore(path)
    second.initialize()
    value = second.get_operating_intent("persist-device")
    assert value["mode"] == "stored"
    assert value["camera_policy"] == "off"
    assert value["configured"] is True

    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT version FROM brew_schema").fetchone() == (6,)
        assert conn.execute(
            "SELECT mode,camera_policy FROM device_operating_intent WHERE device_id=?",
            ("persist-device",),
        ).fetchone() == ("stored", "off")


def test_status_excludes_stored_devices_from_stale_count(app_module, test_client):
    for device_id in ("stored-stale", "preparing-fresh", "brewing-stale"):
        _ingest(test_client, device_id)
    old = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    _set_last_seen(app_module, "stored-stale", old)
    _set_last_seen(app_module, "brewing-stale", old)
    test_client.put(
        "/api/device/stored-stale/operating-intent",
        json={"mode": "stored", "camera_policy": "off"},
    )
    test_client.put(
        "/api/device/preparing-fresh/operating-intent",
        json={"mode": "preparing", "camera_policy": "active_brew_structural"},
    )

    status = test_client.get("/api/status")
    assert status.status_code == 200
    body = status.json()
    assert body["devices"] == 3
    assert body["stored_devices"] == 1
    assert body["preparing_devices"] == 1
    assert body["brewing_devices"] == 1
    assert body["stale_devices"] == 1


def test_poll_alert_suppresses_stored_device_without_sender_call():
    poll = _load_script("poll_alert_operating_intent", SCRIPTS / "poll-alert.py")
    now = datetime(2026, 9, 13, 12, 0, tzinfo=timezone.utc)
    device = {
        "device_id": "stored-device",
        "last_seen": None,
        "effective_interval_sec": 300,
        "operating_mode": "stored",
        "camera_policy": "off",
    }
    state = {
        "schema_version": "alert-state-v1",
        "started_at": (now - timedelta(hours=1)).isoformat(),
        "devices": {},
    }

    def sender(_message: str) -> bool:
        pytest.fail("stored device must not send an alert")

    transitions, failed = poll.apply_transitions(
        [device], state, now=now, startup_grace=0, sender=sender
    )
    assert failed is False
    assert transitions == [{
        "device_id": "stored-device",
        "state": "stored",
        "outcome": "suppressed_intent",
    }]
    assert state["devices"] == {}


def test_ispindel_check_suppresses_stored_device_but_checks_brewing_device(monkeypatch):
    check = _load_script("ispindel_check_operating_intent", SCRIPTS / "ispindel-check.py")
    args = check.parse_args(["--url", "http://dashboard.invalid", "--json"])
    old = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    devices = [
        {
            "device_id": "stored-device",
            "last_seen": old,
            "operating_mode": "stored",
            "camera_policy": "off",
        },
        {
            "device_id": "brewing-device",
            "last_seen": old,
            "operating_mode": "brewing",
            "camera_policy": "active_brew_structural",
        },
    ]

    def fake_fetch(_base: str, path: str, _timeout: float):
        if path == "/api/status":
            return {"devices": 2, "stale_devices": 1, "total_samples": 2}
        if path == "/api/devices":
            return {"devices": devices}
        if path.endswith("/samples?hours=1"):
            return {"samples": [{"battery": 3.0, "rssi": -90, "ssid": "brew-wifi"}]}
        raise AssertionError(path)

    monkeypatch.setattr(check, "fetch", fake_fetch)
    result = check.evaluate(args)

    assert result["state"] == "unhealthy"
    assert result["suppressed_devices"] == ["stored-device"]
    assert all("stored-device" not in note for note in result["notes"])
    assert any("brewing-device" in note for note in result["notes"])


def _camera_config(tmp_path: Path) -> camera_observer.ObserverConfig:
    return camera_observer.ObserverConfig(
        dashboard_url="http://dashboard.invalid",
        dashboard_host="dashboard.invalid",
        camera_url="http://camera.invalid/shot.jpg",
        model_url="http://model.invalid/v1/chat/completions",
        model="gemma",
        image_dir=tmp_path / "camera",
        device_id=None,
        retention_days=30,
    )


def test_camera_policy_off_prevents_capture_even_with_active_brew(monkeypatch, tmp_path):
    brew = {"id": 7, "device_id": "camera-device", "status": "active"}
    calls: list[str] = []

    def fake_get(_config, path):
        calls.append(path)
        if path == "/api/brews?status=active":
            return {"brews": [brew]}
        if path == "/api/device/camera-device/operating-intent":
            return {"mode": "brewing", "camera_policy": "off"}
        raise AssertionError(path)

    monkeypatch.setattr(camera_observer, "_get_json", fake_get)
    monkeypatch.setattr(
        camera_observer,
        "fetch_snapshot",
        lambda *_: pytest.fail("disabled camera must not capture"),
    )

    result = camera_observer.run_once(_camera_config(tmp_path))
    assert result["status"] == "disabled_by_operator"
    assert calls == [
        "/api/brews?status=active",
        "/api/device/camera-device/operating-intent",
    ]
    assert not (tmp_path / "camera").exists()


def test_camera_no_active_brew_is_not_required_and_does_not_fetch_policy(monkeypatch, tmp_path):
    calls: list[str] = []

    def fake_get(_config, path):
        calls.append(path)
        assert path == "/api/brews?status=active"
        return {"brews": []}

    monkeypatch.setattr(camera_observer, "_get_json", fake_get)
    monkeypatch.setattr(
        camera_observer,
        "fetch_snapshot",
        lambda *_: pytest.fail("no active brew must not capture"),
    )

    result = camera_observer.run_once(_camera_config(tmp_path))
    assert result["status"] == "not_required_no_active_brew"
    assert calls == ["/api/brews?status=active"]
    assert not (tmp_path / "camera").exists()


def test_dashboard_and_brewing_ui_expose_operating_intent_controls():
    main_source = (ROOT / "app" / "main.py").read_text()
    dashboard_source = (ROOT / "app" / "static" / "dashboard.js").read_text()
    brewing_source = (ROOT / "app" / "static" / "brewing.js").read_text()
    assert "operating-intent-mode" in main_source
    assert "operating-intent-camera" in main_source
    assert "/operating-intent" in brewing_source
    assert "Stored / intentionally off" in brewing_source
    assert "stored_devices" in dashboard_source
    assert "no active brew" in dashboard_source.lower()
