"""Phase 5B deterministic WAL reader/writer and contention contracts."""
from __future__ import annotations

import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import httpx


def _ingest_payload(sample_id: str, *, angle: float = 20.0) -> dict:
    return {
        "ID": "phase5b-device",
        "name": "Phase 5B Device",
        "sample_id": sample_id,
        "angle": angle,
        "gravity": 1.04,
        "temperature": 20.0,
        "battery_voltage": 4.0,
        "interval": 300,
    }


def _calibration_payload() -> dict:
    return {
        "label": "Phase 5B concurrent calibration",
        "activate": True,
        "points": [
            {"angle": 10.0, "value": 1.0},
            {"angle": 20.0, "value": 1.1},
        ],
    }


def _assert_database_clean(app_module) -> None:
    with app_module.db() as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert [row[0] for row in conn.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        )] == [1, 2]
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 2


def test_wal_read_endpoints_succeed_while_writer_transaction_is_held(
    live_server: str, app_module
):
    with httpx.Client(timeout=3.0) as client:
        seeded = client.post(
            f"{live_server}/api/ingest", json=_ingest_payload("reader-seed")
        )
        assert seeded.status_code == 200, seeded.text

    holder = sqlite3.connect(app_module.DB_PATH, timeout=5.0)
    holder.execute("PRAGMA foreign_keys=ON")
    holder.execute("PRAGMA busy_timeout=5000")
    holder.execute("PRAGMA synchronous=NORMAL")
    holder.execute("BEGIN IMMEDIATE")
    holder.execute(
        "UPDATE devices SET config_json=? WHERE device_id=?",
        ('{"held":true}', "phase5b-device"),
    )
    try:
        with httpx.Client(timeout=3.0) as client:
            devices = client.get(f"{live_server}/api/devices")
            status = client.get(f"{live_server}/api/status")
            samples = client.get(
                f"{live_server}/api/device/phase5b-device/samples?hours=100000"
            )
        assert devices.status_code == 200
        assert devices.json()["count"] == 1
        assert devices.json()["devices"][0]["device_id"] == "phase5b-device"
        assert status.status_code == 200
        assert status.json()["devices"] == 1
        assert status.json()["total_samples"] == 1
        assert samples.status_code == 200
        assert samples.json()["device_id"] == "phase5b-device"
        assert samples.json()["count"] == 1
    finally:
        holder.rollback()
        holder.close()
    _assert_database_clean(app_module)


def test_normal_contention_preserves_every_http_write_and_read(
    live_server: str, app_module
):
    with httpx.Client(timeout=3.0) as client:
        seeded = client.post(
            f"{live_server}/api/ingest", json=_ingest_payload("contention-seed")
        )
        assert seeded.status_code == 200, seeded.text

    operations = [
        ("ingest-a", "POST", "/api/ingest", _ingest_payload("contention-a", angle=21.0)),
        ("ingest-b", "POST", "/api/ingest", _ingest_payload("contention-b", angle=22.0)),
        ("devices", "GET", "/api/devices", None),
        ("status", "GET", "/api/status", None),
        ("samples", "GET", "/api/device/phase5b-device/samples?hours=100000", None),
        (
            "calibration",
            "POST",
            "/api/device/phase5b-device/calibration",
            _calibration_payload(),
        ),
    ]
    barrier = threading.Barrier(len(operations))

    def issue(operation):
        name, method, path, payload = operation
        barrier.wait(timeout=3.0)
        with httpx.Client(timeout=9.0) as client:
            response = client.request(method, f"{live_server}{path}", json=payload)
        return name, response.status_code, response.json()

    with ThreadPoolExecutor(max_workers=len(operations)) as pool:
        results = dict(
            (name, (status, body))
            for name, status, body in pool.map(issue, operations)
        )

    assert set(results) == {operation[0] for operation in operations}
    assert all(status == 200 for status, _body in results.values()), results
    assert results["devices"][1]["count"] == 1
    assert results["status"][1]["devices"] == 1
    assert results["samples"][1]["device_id"] == "phase5b-device"
    assert results["calibration"][1]["is_active"] is True

    with app_module.db() as conn:
        sample_ids = [
            row[0]
            for row in conn.execute(
                "SELECT sample_event_id FROM samples "
                "WHERE sample_event_id LIKE 'contention-%' ORDER BY sample_event_id"
            )
        ]
        assert sample_ids == ["contention-a", "contention-b", "contention-seed"]
        assert conn.execute(
            "SELECT COUNT(*) FROM samples WHERE sample_event_id IN (?,?)",
            ("contention-a", "contention-b"),
        ).fetchone()[0] == 2
        history = conn.execute(
            "SELECT id,device_id FROM calibrations WHERE device_id=?",
            ("phase5b-device",),
        ).fetchall()
        pointer = conn.execute(
            "SELECT calibration_id FROM calibration_active WHERE device_id=?",
            ("phase5b-device",),
        ).fetchone()
        assert len(history) == 1
        assert pointer is not None and pointer[0] == history[0][0]
    _assert_database_clean(app_module)


def test_lock_exhaustion_returns_existing_500_without_partial_row_or_hang(
    live_server: str, app_module
):
    with httpx.Client(timeout=3.0) as client:
        seeded = client.post(
            f"{live_server}/api/ingest", json=_ingest_payload("lock-seed")
        )
        assert seeded.status_code == 200, seeded.text

    holder = sqlite3.connect(app_module.DB_PATH, timeout=5.0)
    holder.execute("PRAGMA foreign_keys=ON")
    holder.execute("PRAGMA busy_timeout=5000")
    holder.execute("PRAGMA synchronous=NORMAL")
    holder.execute("BEGIN IMMEDIATE")
    holder.execute(
        "UPDATE devices SET config_json=? WHERE device_id=?",
        ('{"lock":"held"}', "phase5b-device"),
    )
    request_started = threading.Event()

    def competing_write():
        request_started.set()
        started = time.monotonic()
        with httpx.Client(timeout=9.0) as client:
            response = client.post(
                f"{live_server}/api/ingest",
                json=_ingest_payload("must-not-persist", angle=23.0),
            )
        return response, time.monotonic() - started

    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(competing_write)
            assert request_started.wait(timeout=2.0)
            response, elapsed = future.result(timeout=12.0)
    finally:
        holder.rollback()
        holder.close()

    assert response.status_code == 500
    assert 4.0 <= elapsed < 10.0
    with app_module.db() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM samples WHERE sample_event_id='must-not-persist'"
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM calibrations WHERE label='must-not-persist'"
        ).fetchone()[0] == 0
    _assert_database_clean(app_module)
