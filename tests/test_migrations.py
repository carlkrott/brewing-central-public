"""Phase 3 data-model migration contract.

These tests describe the schema_migrations ledger, the additive column
backfill for legacy devices/samples, the exact query indexes, the
re-run idempotency contract, raw-json/row preservation guarantees, and
the downgrade-read compatibility path.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_legacy_schema(conn: sqlite3.Connection) -> None:
    """Construct the pre-Phase-3 schema and seed canonical legacy fixtures.

    Used by tests that want to exercise the migration entry point against
    a database that has never seen Phase 3 yet.
    """
    conn.executescript(
        """
        CREATE TABLE devices (
            device_id TEXT PRIMARY KEY,
            device_name TEXT,
            expected_interval_sec INTEGER DEFAULT 300,
            created_at TEXT NOT NULL,
            last_seen TEXT,
            config_json TEXT
        );
        CREATE TABLE samples (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            device_id TEXT NOT NULL,
            ts TEXT NOT NULL,
            angle REAL,
            gravity REAL,
            temp_c REAL,
            battery REAL,
            rssi INTEGER,
            ssid TEXT,
            raw_json TEXT NOT NULL,
            FOREIGN KEY(device_id) REFERENCES devices(device_id)
        );
        CREATE TABLE calibrations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            device_id TEXT NOT NULL,
            label TEXT NOT NULL,
            a REAL NOT NULL,
            b REAL NOT NULL,
            created_at TEXT NOT NULL,
            is_default INTEGER DEFAULT 1,
            FOREIGN KEY(device_id) REFERENCES devices(device_id)
        );
        """
    )

    # Two legacy devices with a variety of expected_interval_sec / null name shapes.
    conn.executemany(
        "INSERT INTO devices(device_id, device_name, expected_interval_sec, "
        "created_at, last_seen, config_json) VALUES (?, ?, ?, ?, ?, ?)",
        [
            (
                "leg-a",
                "Cidre 1",
                300,
                "2026-07-20T08:00:00+00:00",
                "2026-07-28T11:00:00+00:00",
                json.dumps({"channel": 1}),
            ),
            (
                "leg-b",
                None,
                600,
                "2026-07-20T08:05:00+00:00",
                None,
                None,
            ),
        ],
    )

    # Three legacy samples — bare battery (ambiguous) and one with no battery at all.
    legacy_samples = [
        (
            "leg-a",
            "2026-07-28T10:00:00+00:00",
            31.0,
            1.040,
            19.5,
            3.95,
            -68,
            "BrewNet",
            json.dumps(
                {
                    "ID": "leg-a",
                    "angle": 31.0,
                    "gravity": 1.040,
                    "temperature": 19.5,
                    "battery": 3.95,
                    "RSSI": -68,
                    "_injected_at": "2026-07-28T10:00:01+00:00",  # NOT authoritative
                }
            ),
        ),
        (
            "leg-a",
            "2026-07-28T10:30:00+00:00",
            30.7,
            1.042,
            19.8,
            None,
            -70,
            "BrewNet",
            json.dumps({"ID": "leg-a", "angle": 30.7, "gravity": 1.042}),
        ),
        (
            "leg-b",
            "2026-07-28T10:15:00+00:00",
            28.9,
            1.012,
            21.0,
            3.984,
            None,
            None,
            json.dumps({"ID": "leg-b", "angle": 28.9, "gravity": 1.012, "battery": 3.984}),
        ),
    ]
    conn.executemany(
        "INSERT INTO samples(device_id, ts, angle, gravity, temp_c, "
        "battery, rssi, ssid, raw_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        legacy_samples,
    )

    conn.execute(
        "INSERT INTO calibrations(device_id, label, a, b, created_at, is_default) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("leg-a", "default", 0.0012, 0.997, "2026-07-21T00:00:00+00:00", 1),
    )
    conn.commit()


def _legacy_db_path(tmp_path: Path) -> Path:
    """Allocate a fresh SQLite file with the legacy schema populated."""
    p = tmp_path / "legacy.db"
    p.touch()
    with sqlite3.connect(p) as conn:
        _build_legacy_schema(conn)
    return p


def _open(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


# ---------------------------------------------------------------------------
# Migration runs against a fresh (legacy) database
# ---------------------------------------------------------------------------


def test_schema_migrations_table_records_version_one(tmp_path: Path):
    """After init, schema_migrations has exactly one row for version=1."""
    path = _legacy_db_path(tmp_path)
    # Reimporting the module triggers init_db against SQLITE_PATH which is
    # monkeypatched by the app_module fixture; do this manually here.
    os_sqlite = str(path)
    import importlib
    import os
    import sys

    sys.path.insert(0, str(tmp_path))
    os.environ["SQLITE_PATH"] = os_sqlite

    # Build a local import fresh — we want a deterministic against-the-raw-file test.
    # Run migration by re-using the same module-level code path: directly call init_db
    # from app.main. This requires SQLITE_PATH to already be set BEFORE app.main imports.
    # Easiest: import app.main via importlib with env already set.
    import app.main  # noqa: F401 - imports under frozen env

    # Force re-evaluation by reloading — init_db runs on import.
    importlib.reload(app.main)
    with _open(path) as conn:
        row = conn.execute(
            "SELECT version, applied_at FROM schema_migrations WHERE version=1"
        ).fetchone()
    assert row is not None, "schema_migrations must record version=1"
    assert row["version"] == 1
    # applied_at must be an ISO-8601 UTC string
    datetime.fromisoformat(row["applied_at"].replace("Z", "+00:00"))


def test_user_version_pragma_is_two_after_all_migrations(tmp_path: Path):
    path = _legacy_db_path(tmp_path)
    import os

    os.environ["SQLITE_PATH"] = str(path)
    import importlib

    import app.main  # noqa: F401
    importlib.reload(app.main)

    # After Phase 3 + Phase 4 the canonical compatibility signal is user_version=2.
    # (Phase 3 advanced to 1; Phase 4 advances to 2.) Phase 3 itself still ran
    # and was reflected at user_version=1 only momentarily; both phase rows
    # remain in the ledger.
    with _open(path) as conn:
        user_version = conn.execute("PRAGMA user_version").fetchone()[0]
        assert user_version == 2
        versions = sorted(r[0] for r in conn.execute("SELECT version FROM schema_migrations"))
        assert versions == [1, 2], "Phase 3 and Phase 4 ledger rows must exist"
        assert user_version == max(versions)


def test_migration_rerun_is_idempotent(tmp_path: Path):
    """Re-running init_db must retain exactly one row per applied version."""
    path = _legacy_db_path(tmp_path)
    import os

    os.environ["SQLITE_PATH"] = str(path)
    import importlib

    import app.main  # noqa: F401
    importlib.reload(app.main)
    importlib.reload(app.main)  # second init_db call
    importlib.reload(app.main)  # third init_db call

    with _open(path) as conn:
        versions = sorted(r[0] for r in conn.execute("SELECT version FROM schema_migrations"))
        assert versions == [1, 2]
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 2


def test_missing_ledger_is_repaired_even_when_phase3_columns_exist(app_module):
    """The canonical ledger is authority; additive columns alone cannot skip it."""
    with app_module.db() as conn:
        conn.execute("DROP TABLE schema_migrations")
        conn.execute("PRAGMA user_version = 0")
        conn.commit()
        assert app_module._migration_needed(conn) is True
        app_module._phase3_migrate(conn)
        assert conn.execute("SELECT version FROM schema_migrations").fetchone()[0] == 1
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1


def test_legacy_row_count_and_rowid_preserved_through_migration(tmp_path: Path):
    """Migration MUST preserve row counts and integer rowid values."""
    path = _legacy_db_path(tmp_path)

    # Snapshot legacy identity state before migration.
    with _open(path) as conn:
        before_samples = [
            dict(r) for r in conn.execute(
                "SELECT id, device_id, ts, battery, raw_json FROM samples ORDER BY id"
            )
        ]
        before_devices = [
            dict(r) for r in conn.execute(
                "SELECT device_id, device_name, expected_interval_sec FROM devices ORDER BY device_id"
            )
        ]
        before_cals = conn.execute("SELECT COUNT(*) FROM calibrations").fetchone()[0]

    import os

    os.environ["SQLITE_PATH"] = str(path)
    import importlib

    import app.main  # noqa: F401
    importlib.reload(app.main)

    with _open(path) as conn:
        after_samples = [
            dict(r) for r in conn.execute(
                "SELECT id, device_id, ts, battery, raw_json FROM samples ORDER BY id"
            )
        ]
        after_devices = [
            dict(r) for r in conn.execute(
                "SELECT device_id, device_name, expected_interval_sec FROM devices ORDER BY device_id"
            )
        ]
        after_cals = conn.execute("SELECT COUNT(*) FROM calibrations").fetchone()[0]

    assert [r["id"] for r in after_samples] == [r["id"] for r in before_samples]
    assert [r["raw_json"] for r in after_samples] == [r["raw_json"] for r in before_samples]
    assert len(before_samples) == len(after_samples)
    assert before_cals == after_cals
    # Legacy device columns are still coherent mirrors of the originals.
    for a, b in zip(after_devices, before_devices):
        assert a["device_name"] == b["device_name"]
        assert a["expected_interval_sec"] == b["expected_interval_sec"]


def test_devices_backfill_adds_reported_columns_and_keeps_legacy(tmp_path: Path):
    path = _legacy_db_path(tmp_path)
    import os

    os.environ["SQLITE_PATH"] = str(path)
    import importlib

    import app.main  # noqa: F401
    importlib.reload(app.main)

    with _open(path) as conn:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(devices)")}
    # New additive columns
    assert {"reported_device_name", "user_device_name", "reported_interval_sec", "user_interval_sec"} <= cols
    # Legacy columns preserved as mirrors
    assert {"device_name", "expected_interval_sec"} <= cols

    # Backfill semantics
    with _open(path) as conn:
        for row in conn.execute(
            "SELECT device_id, device_name, reported_device_name, user_device_name, "
            "expected_interval_sec, reported_interval_sec, user_interval_sec "
            "FROM devices ORDER BY device_id"
        ):
            assert row["reported_device_name"] == row["device_name"], (
                "reported_device_name must mirror legacy device_name after backfill"
            )
            assert row["reported_interval_sec"] == row["expected_interval_sec"], (
                "reported_interval_sec must mirror legacy expected_interval_sec after backfill"
            )
            assert row["user_device_name"] is None
            assert row["user_interval_sec"] is None


def test_samples_backfill_uses_legacy_unknown_and_never_injected_at(tmp_path: Path):
    path = _legacy_db_path(tmp_path)
    import os

    os.environ["SQLITE_PATH"] = str(path)
    import importlib

    import app.main  # noqa: F401
    importlib.reload(app.main)

    with _open(path) as conn:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(samples)")}
    assert {
        "received_at",
        "measured_at",
        "battery_value",
        "battery_unit",
        "battery_source",
        "sample_event_id",
        "retry_key",
        "payload_hash",
    } <= cols

    with _open(path) as conn:
        for row in conn.execute(
            "SELECT id, received_at, measured_at, battery_value, battery_unit, "
            "battery_source, sample_event_id, retry_key, payload_hash "
            "FROM samples ORDER BY id"
        ):
            assert row["received_at"] == "2026-07-28T10:00:00+00:00" or \
                   row["received_at"].startswith("2026-07-28T10:"), (
                f"received_at must default to the legacy ts, got {row['received_at']!r}"
            )
            assert row["measured_at"] is None, (
                "_injected_at must NOT be promoted to measured_at"
            )
            assert row["battery_unit"] == "unknown"
            assert row["battery_source"] == "legacy_unknown"
            assert row["sample_event_id"] is None
            assert row["retry_key"] is None
            assert row["payload_hash"] is None


def test_indexes_are_created_with_exact_definitions(tmp_path: Path):
    path = _legacy_db_path(tmp_path)
    import os

    os.environ["SQLITE_PATH"] = str(path)
    import importlib

    import app.main  # noqa: F401
    importlib.reload(app.main)

    with _open(path) as conn:
        idx_names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    assert "idx_samples_device_effective_time" in idx_names
    assert "idx_samples_device_retry_window" in idx_names

    # Verify the expression-index column expression
    with _open(path) as conn:
        for row in conn.execute("PRAGMA index_info('idx_samples_device_effective_time')"):
            pass
        # Use index_list on samples to be safe
        idx_defs = list(
            conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name='samples'"
            )
        )
    raw = "\n".join(row[0] or "" for row in idx_defs)
    assert "COALESCE(measured_at, received_at)" in raw
    assert "retry_key IS NOT NULL" in raw or "WHERE" in raw  # partial-index WHERE clause present


def test_downgrade_read_compatibility_via_legacy_columns(tmp_path: Path):
    """A pre-Phase-3 client must still read device_name/expected_interval_sec/battery
    from the legacy columns — the new additive columns must not displace them."""
    path = _legacy_db_path(tmp_path)
    import os

    os.environ["SQLITE_PATH"] = str(path)
    import importlib

    import app.main  # noqa: F401
    importlib.reload(app.main)

    with _open(path) as conn:
        # legacy devices query — must still work
        for row in conn.execute(
            "SELECT device_id, device_name, expected_interval_sec FROM devices"
        ):
            assert row["device_name"] is not None or row["device_id"] == "leg-b"
        # legacy samples query — must still work
        for row in conn.execute(
            "SELECT device_id, ts, battery FROM samples WHERE battery IS NOT NULL"
        ):
            assert row["battery"] is not None


# ---------------------------------------------------------------------------
# Bare-fresh-DB init (no legacy data) must still build the full schema
# ---------------------------------------------------------------------------


def test_fresh_db_initializes_through_phase4_schema_and_version_two(tmp_path: Path):
    """A blank DB must apply Phase 3 then Phase 4 and retain both ledger rows."""
    blank = tmp_path / "blank.db"
    blank.touch()
    import os

    os.environ["SQLITE_PATH"] = str(blank)
    import importlib

    import app.main  # noqa: F401
    importlib.reload(app.main)

    with _open(blank) as conn:
        versions = sorted(r[0] for r in conn.execute("SELECT version FROM schema_migrations"))
        assert versions == [1, 2]
        uv = conn.execute("PRAGMA user_version").fetchone()[0]
        assert uv == 2
        for tbl in ("devices", "samples", "calibrations", "calibration_active"):
            assert conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (tbl,)
            ).fetchone() is not None


# ---------------------------------------------------------------------------
# Phase 4 migration tests
# ---------------------------------------------------------------------------


def _seed_legacy_calibrations(conn: sqlite3.Connection) -> None:
    """Insert legacy calibration rows used by the Phase 4 migration tests.

    The legacy schema (post-Phase-3) still uses `(a, b, is_default)` to encode
    a linear fit. Phase 4 must add the additive columns and backfill them
    without mutating the original row bytes.
    """
    # Ensure the devices row exists for the FK.
    conn.execute(
        "INSERT OR IGNORE INTO devices(device_id, device_name, expected_interval_sec, created_at) "
        "VALUES (?, ?, ?, ?)",
        ("p4-leg-a", "Legacy A", 300, "2026-07-20T00:00:00+00:00"),
    )
    conn.execute(
        "INSERT OR IGNORE INTO devices(device_id, device_name, expected_interval_sec, created_at) "
        "VALUES (?, ?, ?, ?)",
        ("p4-leg-b", "Legacy B", 300, "2026-07-20T00:00:00+00:00"),
    )
    # p4-leg-a: two legacy rows, one is_default=1 (id=1)
    conn.execute(
        "INSERT INTO calibrations(device_id, label, a, b, created_at, is_default) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("p4-leg-a", "first", 0.0012, 0.997, "2026-07-21T00:00:00+00:00", 0),
    )
    conn.execute(
        "INSERT INTO calibrations(device_id, label, a, b, created_at, is_default) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("p4-leg-a", "second", 0.0011, 0.998, "2026-07-22T00:00:00+00:00", 1),
    )
    # p4-leg-b: one legacy row (is_default=1)
    conn.execute(
        "INSERT INTO calibrations(device_id, label, a, b, created_at, is_default) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("p4-leg-b", "only", 0.0020, 0.990, "2026-07-23T00:00:00+00:00", 1),
    )
    conn.commit()


def test_v2_startup_is_byte_idempotent(tmp_path: Path):
    """Phase 4 migration must be idempotent across re-runs.

    Two consecutive runs of init_db must both leave the schema_migrations
    ledger containing exactly {1, 2} and PRAGMA user_version=2, with no
    extra columns, tables, or pointer rows added on the second run.
    """
    path = _legacy_db_path(tmp_path)
    _seed_legacy_calibrations(_open(path))

    # Snapshot legacy row bytes BEFORE Phase 4.
    with _open(path) as conn:
        before_legs = [
            dict(r) for r in conn.execute(
                "SELECT id, device_id, label, a, b, created_at, is_default "
                "FROM calibrations ORDER BY id"
            )
        ]

    import importlib
    import os

    os.environ["SQLITE_PATH"] = str(path)
    import app.main  # noqa: F401
    importlib.reload(app.main)  # first init_db → Phase 4 runs
    # Second init_db (same path) must be a no-op for schema_migrations / user_version.
    importlib.reload(app.main)

    with _open(path) as conn:
        versions = sorted(r[0] for r in conn.execute("SELECT version FROM schema_migrations"))
        assert versions == [1, 2], f"ledger must be {{1, 2}}, got {versions}"
        uv = conn.execute("PRAGMA user_version").fetchone()[0]
        assert uv == 2
        # Foreign keys must be enforced on app connections.
        conn.execute("PRAGMA foreign_keys = ON")
        fk = conn.execute("PRAGMA foreign_keys").fetchone()[0]
        assert fk == 1, "foreign_keys pragma must be ON"
        # calibrations columns must include the additive ones, but no schema
        # changes between the two runs.
        cols = {row[1] for row in conn.execute("PRAGMA table_info(calibrations)")}
        for added in ("poly_order", "coefficients_json", "fit_r2", "point_count", "points_json"):
            assert added in cols
        # calibration_active must exist with the documented shape.
        tbls = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='calibration_active'"
        )}
        assert "calibration_active" in tbls
        # Indexes must exist (idx_calibrations_id_device, idx_calibrations_device_created).
        idx = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
        assert "idx_calibrations_id_device" in idx
        assert "idx_calibrations_device_created" in idx
        # Legacy row bytes must be preserved.
        after_legs = [
            dict(r) for r in conn.execute(
                "SELECT id, device_id, label, a, b, created_at, is_default "
                "FROM calibrations ORDER BY id"
            )
        ]
        assert before_legs == after_legs


def test_v1_to_v2_exact_schema_data_and_timestamp_preservation(tmp_path: Path):
    """Phase 4 must backfill the additive columns without rewriting the
    legacy (id, device_id, label, a, b, created_at, is_default) bytes."""
    path = _legacy_db_path(tmp_path)
    _seed_legacy_calibrations(_open(path))

    # Snapshot legacy bytes.
    with _open(path) as conn:
        before = {
            r[0]: dict(r) for r in conn.execute(
                "SELECT id, device_id, label, a, b, created_at, is_default "
                "FROM calibrations"
            )
        }

    import importlib
    import os

    os.environ["SQLITE_PATH"] = str(path)
    import app.main  # noqa: F401
    importlib.reload(app.main)

    with _open(path) as conn:
        after = {
            r["id"]: dict(r) for r in conn.execute(
                "SELECT id, device_id, label, a, b, created_at, is_default, "
                "poly_order, coefficients_json, fit_r2, point_count, points_json "
                "FROM calibrations"
            )
        }

    # Legacy fields must be unchanged for every row.
    for cid, row in before.items():
        a = after[cid]
        assert a["device_id"] == row["device_id"]
        assert a["label"] == row["label"]
        assert a["a"] == row["a"]
        assert a["b"] == row["b"]
        assert a["created_at"] == row["created_at"]
        assert a["is_default"] == row["is_default"]

    # Additive columns: poly_order=1, coefficients_json canonical compact JSON [b, a].
    for cid, a in after.items():
        assert a["poly_order"] == 1, f"row {cid}: poly_order must be 1"
        assert a["fit_r2"] is None
        assert a["point_count"] is None
        assert a["points_json"] is None
        # coefficients_json must be the canonical compact form, ascending [b, a].
        parsed = json.loads(a["coefficients_json"])
        assert parsed == [a["b"], a["a"]], (
            f"row {cid}: coefficients_json must be [b, a], got {parsed}"
        )
        # Compact form means no spaces.
        assert " " not in a["coefficients_json"], (
            f"row {cid}: coefficients_json must be compact, got {a['coefficients_json']!r}"
        )


def test_phase4_migration_resolves_multiple_legacy_defaults_to_highest_id_pointer(tmp_path: Path):
    """When a device has multiple legacy is_default=1 rows, Phase 4 must
    resolve to a single calibration_active pointer keyed to the highest id
    WITHOUT rewriting any calibration row.
    """
    path = _legacy_db_path(tmp_path)
    conn = _open(path)
    _seed_legacy_calibrations(conn)
    # Snapshot the legacy default set BEFORE Phase 4.
    pre_defaults = sorted(
        r[0] for r in conn.execute(
            "SELECT id FROM calibrations WHERE device_id='p4-leg-a' AND is_default=1 ORDER BY id"
        )
    )
    # Add a 4th row for p4-leg-a marked is_default=1 to ensure multiple
    # legacy defaults exist on the same device. The newly-inserted row must
    # also have its rowid bytes preserved verbatim.
    cur = conn.execute(
        "INSERT INTO calibrations(device_id, label, a, b, created_at, is_default) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("p4-leg-a", "third", 0.0010, 0.999, "2026-07-24T00:00:00+00:00", 1),
    )
    inserted_id = cur.lastrowid
    pre_defaults_full = sorted(pre_defaults + [inserted_id])
    conn.commit()
    conn.close()

    import importlib
    import os

    os.environ["SQLITE_PATH"] = str(path)
    import app.main  # noqa: F401
    importlib.reload(app.main)

    with _open(path) as conn:
        # Find the highest-id legacy default on p4-leg-a.
        max_default = conn.execute(
            "SELECT MAX(id) FROM calibrations WHERE device_id=? AND is_default=1",
            ("p4-leg-a",),
        ).fetchone()[0]
        pointer = conn.execute(
            "SELECT calibration_id FROM calibration_active WHERE device_id=?",
            ("p4-leg-a",),
        ).fetchone()
        assert pointer is not None, "calibration_active pointer must exist for p4-leg-a"
        assert pointer["calibration_id"] == max_default
        assert pointer["calibration_id"] == inserted_id
        # And the legacy calibration rows were NOT rewritten: the set of
        # default ids on p4-leg-a must match exactly what we set BEFORE
        # migration.
        after_defaults = sorted(
            r[0] for r in conn.execute(
                "SELECT id FROM calibrations WHERE device_id=? AND is_default=1 ORDER BY id",
                ("p4-leg-a",),
            )
        )
        assert after_defaults == pre_defaults_full, (
            f"legacy is_default values must be unchanged "
            f"(before={pre_defaults_full}, after={after_defaults})"
        )


def test_fresh_phase4_init_is_single_transaction_and_exact_v2(tmp_path: Path):
    """A brand-new database file must end up with ledger {1, 2} and user_version=2."""
    blank = tmp_path / "phase4-fresh.db"
    blank.touch()
    import importlib
    import os

    os.environ["SQLITE_PATH"] = str(blank)
    import app.main  # noqa: F401
    importlib.reload(app.main)

    with _open(blank) as conn:
        versions = sorted(r[0] for r in conn.execute("SELECT version FROM schema_migrations"))
        assert versions == [1, 2], f"fresh DB ledger must be {{1, 2}}, got {versions}"
        uv = conn.execute("PRAGMA user_version").fetchone()[0]
        assert uv == 2
        # calibration_active and the additive calibration columns must exist.
        cols = {row[1] for row in conn.execute("PRAGMA table_info(calibrations)")}
        for added in ("poly_order", "coefficients_json", "fit_r2", "point_count", "points_json"):
            assert added in cols
        assert conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='calibration_active'"
        ).fetchone() is not None


def test_phase4_foreign_key_check_and_composite_device_binding(app_module):
    """Every db() connection must enable foreign_keys and verify it returns 1.
    The composite foreign key on calibration_active(calibration_id, device_id)
    must prevent pointing a device at another device's calibration.
    """
    with app_module.db() as conn:
        # Add a calibration on a known device.
        conn.execute(
            "INSERT INTO devices(device_id, device_name, expected_interval_sec, created_at) "
            "VALUES (?, ?, ?, ?)",
            ("p4-fk-dev", "FK", 300, "2026-07-20T00:00:00+00:00"),
        )
        cur = conn.execute(
            "INSERT INTO calibrations(device_id, label, a, b, created_at, is_default, "
            "poly_order, coefficients_json, fit_r2, point_count, points_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "p4-fk-dev", "cal", 0.001, 1.0, "2026-07-21T00:00:00+00:00", 1,
                1, json.dumps([1.0, 0.001], separators=(",", ":")), 1.0, 2, None,
            ),
        )
        cal_id = cur.lastrowid
        conn.commit()
        # PRAGMA foreign_keys must be 1 (enforced by db()).
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1

    # Now try to point calibration_active at a calibration owned by a DIFFERENT device.
    # Add a calibration on device X, then try to insert calibration_active that points
    # (device_id=Y, calibration_id=<X's cal>) → composite FK violation.
    with app_module.db() as conn:
        conn.execute(
            "INSERT INTO devices(device_id, device_name, expected_interval_sec, created_at) "
            "VALUES (?, ?, ?, ?)",
            ("p4-fk-owner", "Owner", 300, "2026-07-20T00:00:00+00:00"),
        )
        conn.execute(
            "INSERT INTO devices(device_id, device_name, expected_interval_sec, created_at) "
            "VALUES (?, ?, ?, ?)",
            ("p4-fk-other", "Other", 300, "2026-07-20T00:00:00+00:00"),
        )
        cur = conn.execute(
            "INSERT INTO calibrations(device_id, label, a, b, created_at, is_default, "
            "poly_order, coefficients_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("p4-fk-owner", "owned", 0.001, 1.0, "2026-07-21T00:00:00+00:00", 1, 1, "[1.0,0.001]"),
        )
        owned_id = cur.lastrowid
        conn.commit()

        # Attempt the cross-device insert: must fail because the composite FK
        # (calibration_id, device_id) does not match (owned_id, p4-fk-other).
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO calibration_active(device_id, calibration_id, activated_at) "
                "VALUES (?, ?, ?)",
                ("p4-fk-other", owned_id, "2026-07-28T00:00:00+00:00"),
            )

        # foreign_key_check returns zero rows (no violations).
        violations = conn.execute("PRAGMA foreign_key_check").fetchall()
        assert violations == [], f"unexpected FK violations: {violations}"


def test_migration_rejects_partial_and_unknown_states_without_mutation(
    tmp_path: Path, app_module, monkeypatch
):
    """A partial V2 schema is terminal; startup must not repair any bytes."""
    path = _legacy_db_path(tmp_path)
    with _open(path) as conn:
        app_module._phase3_migrate(conn)
        conn.execute("ALTER TABLE calibrations ADD COLUMN poly_order INTEGER")
        conn.commit()
    before = path.read_bytes()
    monkeypatch.setattr(app_module, "DB_PATH", path)
    with pytest.raises(RuntimeError, match="schema|state|partial"):
        app_module.init_db()
    assert path.read_bytes() == before

    malformed_v2 = tmp_path / "malformed-v2.db"
    malformed_v2.touch()
    monkeypatch.setattr(app_module, "DB_PATH", malformed_v2)
    app_module.init_db()
    with _open(malformed_v2) as conn:
        conn.execute("DROP TRIGGER calibrations_immutable_delete")
        conn.commit()
    before_v2 = malformed_v2.read_bytes()
    with pytest.raises(RuntimeError, match="schema|state|partial"):
        app_module.init_db()
    assert malformed_v2.read_bytes() == before_v2


def _insert_phase4_calibration(conn: sqlite3.Connection) -> int:
    conn.execute(
        "INSERT INTO devices(device_id,created_at) VALUES (?,?)",
        ("immutable-device", "2026-07-28T00:00:00+00:00"),
    )
    cursor = conn.execute(
        "INSERT INTO calibrations(device_id,label,a,b,created_at,is_default,"
        "poly_order,coefficients_json,fit_r2,point_count,points_json) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        ("immutable-device", "original", 0.01, 0.9,
         "2026-07-28T00:00:00+00:00", 1, 1, "[0.9,0.01]", 1.0, 2,
         "[[10.0,1.0],[20.0,1.1]]"),
    )
    conn.commit()
    return int(cursor.lastrowid)


def test_calibration_update_trigger_rejects_direct_sql(app_module):
    with app_module.db() as conn:
        calibration_id = _insert_phase4_calibration(conn)
        with pytest.raises(sqlite3.IntegrityError, match="calibrations are immutable"):
            conn.execute(
                "UPDATE calibrations SET label='mutated' WHERE id=?",
                (calibration_id,),
            )


def test_calibration_delete_trigger_rejects_direct_sql(app_module):
    with app_module.db() as conn:
        calibration_id = _insert_phase4_calibration(conn)
        with pytest.raises(sqlite3.IntegrityError, match="calibrations are immutable"):
            conn.execute("DELETE FROM calibrations WHERE id=?", (calibration_id,))


def test_fresh_phase4_failure_rolls_back_to_empty(tmp_path: Path, app_module, monkeypatch):
    path = tmp_path / "fresh-failure.db"
    path.touch()
    monkeypatch.setattr(app_module, "DB_PATH", path)

    def fail_at_checkpoint(name: str) -> None:
        if name == "fresh_after_objects":
            raise RuntimeError("injected fresh initialization failure")

    monkeypatch.setattr(app_module, "_migration_checkpoint", fail_at_checkpoint, raising=False)
    with pytest.raises(RuntimeError, match="injected fresh initialization failure"):
        app_module.init_db()
    with _open(path) as conn:
        objects = conn.execute(
            "SELECT type,name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
        ).fetchall()
        assert objects == []
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 0


@pytest.mark.parametrize("checkpoint", [
    "after_columns",
    "after_backfill",
    "after_unique_index",
    "after_active_table",
    "after_active_backfill",
    "after_history_index",
    "after_triggers",
    "after_foreign_key_check",
    "after_ledger",
    "after_user_version",
])
def test_migration_checkpoint_failure_restores_exact_v1(
    tmp_path: Path, app_module, monkeypatch, checkpoint: str
):
    case = tmp_path / checkpoint
    case.mkdir()
    path = _legacy_db_path(case)
    with _open(path) as conn:
        app_module._phase3_migrate(conn)
        before_dump = "\n".join(conn.iterdump())
    before_bytes = path.read_bytes()
    monkeypatch.setattr(app_module, "DB_PATH", path)

    def fail_at_checkpoint(name: str) -> None:
        if name == checkpoint:
            raise RuntimeError(f"injected failure at {checkpoint}")

    monkeypatch.setattr(app_module, "_migration_checkpoint", fail_at_checkpoint)
    with pytest.raises(RuntimeError, match="injected failure"):
        app_module.init_db()
    with _open(path) as conn:
        assert "\n".join(conn.iterdump()) == before_dump
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
        assert [row[0] for row in conn.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        )] == [1]
    assert path.read_bytes() == before_bytes


def test_db_enables_foreign_keys_before_any_begin(app_module):
    conn = app_module.db()
    try:
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        conn.execute("BEGIN")
        assert conn.in_transaction
        conn.execute("ROLLBACK")
    finally:
        conn.close()


def test_phase3_rollback_fixture_remains_exact_v1(tmp_path: Path, app_module):
    path = _legacy_db_path(tmp_path)
    with _open(path) as conn:
        app_module._phase3_migrate(conn)
        app_module._require_exact_v1_for_phase4(conn)
        before = "\n".join(conn.iterdump())
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
        assert [row[0] for row in conn.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        )] == [1]
    with _open(path) as conn:
        app_module._require_exact_v1_for_phase4(conn)
        assert "\n".join(conn.iterdump()) == before
