"""Phase 5B SQLite connection-policy and WAL persistence contracts."""
from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest


EXPECTED_OBJECTS = {
    ("table", "schema_migrations"),
    ("table", "devices"),
    ("table", "samples"),
    ("table", "calibrations"),
    ("table", "calibration_active"),
    ("index", "idx_samples_device_effective_time"),
    ("index", "idx_samples_device_retry_window"),
    ("index", "idx_calibrations_id_device"),
    ("index", "idx_calibrations_device_created"),
    ("trigger", "calibrations_immutable_update"),
    ("trigger", "calibrations_immutable_delete"),
}
EXPECTED_INDEX_SQL = {
    "idx_samples_device_effective_time": (
        "CREATE INDEX idx_samples_device_effective_time "
        "ON samples (device_id, COALESCE(measured_at, received_at))"
    ),
    "idx_samples_device_retry_window": (
        "CREATE INDEX idx_samples_device_retry_window "
        "ON samples (device_id, retry_key, received_at) WHERE retry_key IS NOT NULL"
    ),
    "idx_calibrations_id_device": (
        "CREATE UNIQUE INDEX idx_calibrations_id_device ON calibrations(id, device_id)"
    ),
    "idx_calibrations_device_created": (
        "CREATE INDEX idx_calibrations_device_created "
        "ON calibrations(device_id, created_at DESC, id DESC)"
    ),
}


def _pragma(conn: sqlite3.Connection, name: str):
    return conn.execute(f"PRAGMA {name}").fetchone()[0]


def _schema_state(path: Path) -> tuple:
    with sqlite3.connect(path) as conn:
        objects = {
            (row[0], row[1])
            for row in conn.execute(
                "SELECT type,name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
            )
        }
        versions = [
            row[0]
            for row in conn.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            )
        ]
        indexes = dict(
            conn.execute(
                "SELECT name,sql FROM sqlite_master "
                "WHERE type='index' AND sql IS NOT NULL ORDER BY name"
            )
        )
        return objects, versions, _pragma(conn, "user_version"), indexes


def test_module_import_transitions_fresh_database_to_wal_and_exact_v2(
    app_module, sqlite_tmp_path: Path
):
    with sqlite3.connect(sqlite_tmp_path) as conn:
        assert _pragma(conn, "journal_mode").lower() == "wal"
    objects, versions, user_version, indexes = _schema_state(sqlite_tmp_path)
    assert objects == EXPECTED_OBJECTS
    assert versions == [1, 2]
    assert user_version == 2
    assert indexes == EXPECTED_INDEX_SQL


def test_init_db_rerun_preserves_schema_and_data(app_module, sqlite_tmp_path: Path):
    with app_module.db() as conn:
        conn.execute(
            "INSERT INTO devices(device_id,created_at) VALUES (?,?)",
            ("idempotent-device", "2026-07-29T00:00:00+00:00"),
        )
        conn.commit()
    before = _schema_state(sqlite_tmp_path)
    app_module.init_db()
    after = _schema_state(sqlite_tmp_path)
    assert after == before
    with sqlite3.connect(sqlite_tmp_path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM devices WHERE device_id='idempotent-device'"
        ).fetchone()[0] == 1
        assert _pragma(conn, "journal_mode").lower() == "wal"


def test_db_applies_and_verifies_every_connection_policy(app_module):
    conn = app_module.db()
    try:
        assert _pragma(conn, "foreign_keys") == 1
        assert _pragma(conn, "busy_timeout") == 5000
        assert _pragma(conn, "journal_mode").lower() == "wal"
        assert _pragma(conn, "synchronous") == 1
    finally:
        conn.close()


def test_db_connect_uses_explicit_five_second_timeout(app_module, monkeypatch):
    real_connect = sqlite3.connect
    observed: list[float | None] = []

    def recording_connect(*args, **kwargs):
        observed.append(kwargs.get("timeout"))
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(app_module.sqlite3, "connect", recording_connect)
    with app_module.db():
        pass
    assert observed == [5.0]


def test_raw_reopen_reports_full_until_that_connection_sets_normal(
    app_module, sqlite_tmp_path: Path
):
    del app_module
    with sqlite3.connect(sqlite_tmp_path) as conn:
        assert _pragma(conn, "journal_mode").lower() == "wal"
        assert _pragma(conn, "synchronous") == 2
        conn.execute("PRAGMA synchronous=NORMAL")
        assert _pragma(conn, "synchronous") == 1


def test_ordinary_db_fails_closed_if_quiesced_database_reverted_to_delete(
    app_module, sqlite_tmp_path: Path
):
    with sqlite3.connect(sqlite_tmp_path) as raw:
        assert raw.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0] == 0
        assert raw.execute("PRAGMA journal_mode=DELETE").fetchone()[0].lower() == "delete"
    with pytest.raises(sqlite3.OperationalError, match="journal_mode"):
        app_module.db()
    with sqlite3.connect(sqlite_tmp_path) as raw:
        assert _pragma(raw, "journal_mode").lower() == "delete"


def test_policy_query_back_mismatch_closes_connection_before_error(
    app_module, monkeypatch
):
    real_connect = sqlite3.connect
    closed = False

    class MismatchCursor:
        @staticmethod
        def fetchone():
            return (4999,)

    class Proxy:
        def __init__(self, inner):
            object.__setattr__(self, "inner", inner)

        def __getattr__(self, name):
            return getattr(self.inner, name)

        def __setattr__(self, name, value):
            setattr(self.inner, name, value)

        def execute(self, sql, *args):
            normalized = " ".join(sql.strip().lower().split())
            if normalized == "pragma busy_timeout":
                return MismatchCursor()
            return self.inner.execute(sql, *args)

        def close(self):
            nonlocal closed
            closed = True
            self.inner.close()

    def proxied_connect(*args, **kwargs):
        return Proxy(real_connect(*args, **kwargs))

    monkeypatch.setattr(app_module.sqlite3, "connect", proxied_connect)
    with pytest.raises(sqlite3.OperationalError, match="busy_timeout"):
        app_module.db()
    assert closed is True


def test_startup_wal_lock_failure_is_bounded_and_creates_no_schema(
    tmp_path: Path
):
    locked = tmp_path / "startup-locked.db"
    locked.touch()
    holder = sqlite3.connect(locked)
    holder.execute("BEGIN EXCLUSIVE")
    try:
        env = os.environ.copy()
        env["SQLITE_PATH"] = str(locked)
        result = subprocess.run(
            [sys.executable, "-c", "import app.main"],
            cwd=Path(__file__).resolve().parents[1],
            env=env,
            text=True,
            capture_output=True,
            timeout=12,
            check=False,
        )
    finally:
        holder.rollback()
        holder.close()
    assert result.returncode != 0
    assert "locked" in (result.stdout + result.stderr).lower()
    with sqlite3.connect(locked) as conn:
        assert conn.execute(
            "SELECT type,name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
        ).fetchall() == []
        assert _pragma(conn, "user_version") == 0


def test_exact_v2_schema_indexes_query_plan_and_checks(app_module):
    with app_module.db() as conn:
        objects = {
            (row[0], row[1])
            for row in conn.execute(
                "SELECT type,name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
            )
        }
        indexes = dict(
            conn.execute(
                "SELECT name,sql FROM sqlite_master "
                "WHERE type='index' AND sql IS NOT NULL ORDER BY name"
            )
        )
        plan = " ".join(
            str(column)
            for row in conn.execute(
                "EXPLAIN QUERY PLAN SELECT id,ts,received_at,measured_at,angle,gravity,"
                "temp_c,battery_value,battery_unit,battery_source,battery,rssi,ssid "
                "FROM samples WHERE device_id=? "
                "AND COALESCE(measured_at, received_at) >= ? "
                "ORDER BY COALESCE(measured_at, received_at) ASC",
                ("plan-device", "2000-01-01T00:00:00+00:00"),
            )
            for column in row
        )
        assert objects == EXPECTED_OBJECTS
        assert indexes == EXPECTED_INDEX_SQL
        assert [row[0] for row in conn.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        )] == [1, 2]
        assert _pragma(conn, "user_version") == 2
        assert "USING INDEX idx_samples_device_effective_time" in plan
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_wal_persists_across_close_and_reopen(app_module, sqlite_tmp_path: Path):
    with app_module.db() as conn:
        conn.execute(
            "INSERT INTO devices(device_id,created_at) VALUES (?,?)",
            ("wal-persist", "2026-07-29T00:00:00+00:00"),
        )
        conn.commit()
    with sqlite3.connect(sqlite_tmp_path) as reopened:
        assert _pragma(reopened, "journal_mode").lower() == "wal"
        assert reopened.execute(
            "SELECT COUNT(*) FROM devices WHERE device_id='wal-persist'"
        ).fetchone()[0] == 1


def test_wal_sidecars_exist_while_write_capable_connection_is_held(
    app_module, sqlite_tmp_path: Path
):
    conn = app_module.db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT INTO devices(device_id,created_at) VALUES (?,?)",
            ("sidecar-device", "2026-07-29T00:00:00+00:00"),
        )
        assert Path(str(sqlite_tmp_path) + "-wal").is_file()
        assert Path(str(sqlite_tmp_path) + "-shm").is_file()
        conn.rollback()
    finally:
        conn.close()
    # SQLite may remove either sidecar after the last connection closes; their
    # post-close presence is intentionally not an acceptance condition.
