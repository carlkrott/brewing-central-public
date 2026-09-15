from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from starlette.middleware.trustedhost import TrustedHostMiddleware
from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    ValidationError,
    field_validator,
)

from app.assistant import (
    AssistantStore,
    CombinedGemmaClient,
    ResearchBroker,
    ZeroClawClient,
    create_assistant_router,
)
from app.brewing import BrewingStore, create_brewing_router
from app.security import DeviceTokenBucket, authenticate, load_security_config

DB_PATH = Path(os.getenv("SQLITE_PATH", str(Path(__file__).resolve().parents[1] / "data" / "ispindel.db")))
BREW_DB_PATH = Path(
    os.getenv(
        "BREW_SQLITE_PATH",
        str(Path(__file__).resolve().parents[1] / "data" / "brew.db"),
    )
)
STATIC_DIR = Path(__file__).resolve().parent / "static"
IMMUTABLE_ASSET_CACHE = "public, max-age=31536000, immutable"
FIRST_PARTY_ASSET_CACHE = "no-cache, max-age=0, must-revalidate"


SECURITY_CONFIG = load_security_config()
INGEST_BUCKET = DeviceTokenBucket(
    SECURITY_CONFIG.rate_capacity,
    SECURITY_CONFIG.rate_refill_per_second,
)

@asynccontextmanager
async def lifespan(application: FastAPI):
    pipeline = getattr(application.state, "assistant_pipeline", None)
    if pipeline is not None:
        pipeline.start()
    try:
        yield
    finally:
        if pipeline is not None:
            pipeline.stop()


app = FastAPI(title="iSpindel Local Dashboard", lifespan=lifespan)
app.add_middleware(
    TrustedHostMiddleware,
    allowed_hosts=SECURITY_CONFIG.allowed_hosts,
)

REQUEST_BODY_LIMIT = 65_536
BATTERY_PATH = Path(os.getenv("BATTERY_EVIDENCE_PATH", "/health-evidence/battery-state.json"))
HEARTBEAT_PATH = Path(os.getenv("HEARTBEAT_EVIDENCE_PATH", "/health-evidence/heartbeat.json"))
BATTERY_TTL_SECONDS = int(os.getenv("BATTERY_TTL_SECONDS", "600"))
HEARTBEAT_TTL_SECONDS = int(os.getenv("HEARTBEAT_TTL_SECONDS", "5400"))
# Backup-health evidence is published by ``scripts/backup-production.py`` at
# ``${ISPINDEL_BACKUP_ROOT:-/var/backups/ispindel-dashboard}/backup_health.json``.
# The container only sees a narrow read-only slice of that tree at
# ``/backup-health`` (see docker-compose.yml), so the default path under the
# mount matches the producer's filename. The TTL default (86400 s = 24 h) is
# chosen to comfortably tolerate the 6-hour backup timer plus a single
# missed run plus retry latency.
BACKUP_HEALTH_PATH = Path(
    os.getenv("BACKUP_HEALTH_PATH", "/backup-health/backup_health.json")
)
BACKUP_HEALTH_TTL_SECONDS = int(os.getenv("BACKUP_HEALTH_TTL_SECONDS", "86400"))
BACKUP_HEALTH_SCHEMA = "ispindel-backup-health/v1"


class RequestBodyLimitMiddleware:
    """A pure ASGI, replaying request-body cap for declared and chunked data."""

    def __init__(self, app: Any, limit: int = REQUEST_BODY_LIMIT) -> None:
        self.app, self.limit = app, limit

    async def __call__(self, scope: Dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = {key.lower(): value for key, value in scope.get("headers", [])}
        declared = headers.get(b"content-length")
        if declared:
            try:
                if int(declared) > self.limit:
                    await self._too_large(send)
                    return
            except ValueError:
                await self._too_large(send)
                return
        # Read and validate every ASGI chunk before route execution, then replay
        # one equivalent message. This makes the streamed limit deterministic
        # and prevents a route from observing a partial oversized body.
        chunks: list[bytes] = []
        received = 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                break
            if message["type"] != "http.request":
                continue
            chunk = message.get("body", b"")
            received += len(chunk)
            if received > self.limit:
                await self._too_large(send)
                return
            chunks.append(chunk)
            if not message.get("more_body", False):
                break
        replayed = False

        async def limited_receive() -> Dict[str, Any]:
            nonlocal replayed
            if replayed:
                return {"type": "http.disconnect"}
            replayed = True
            return {"type": "http.request", "body": b"".join(chunks), "more_body": False}

        await self.app(scope, limited_receive, send)

    @staticmethod
    async def _too_large(send: Any) -> None:
        body = b'{"detail":"request body too large"}'
        await send({"type": "http.response.start", "status": 413,
                    "headers": [(b"content-type", b"application/json"),
                                (b"content-length", str(len(body)).encode())]})
        await send({"type": "http.response.body", "body": body})


app.add_middleware(RequestBodyLimitMiddleware)


@app.middleware("http")
async def security_headers(request: Request, call_next: Any) -> Any:
    response = await call_next(request)
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self'; style-src 'self'; "
        "img-src 'self' data:; connect-src 'self'; object-src 'none'; "
        "base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
    )
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Frame-Options"] = "DENY"
    if request.url.scheme == "https":
        response.headers["Strict-Transport-Security"] = "max-age=31536000"
    return response


@dataclass
class DeviceInfo:
    id: str
    name: str | None


SQLITE_BUSY_TIMEOUT_MS = 5000


def _open_db() -> sqlite3.Connection:
    """Open a connection with policy common to initialization and requests."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(DB_PATH, timeout=5.0)
        conn.row_factory = sqlite3.Row

        conn.execute("PRAGMA foreign_keys=ON")
        foreign_keys = conn.execute("PRAGMA foreign_keys").fetchone()[0]
        if foreign_keys != 1:
            raise sqlite3.OperationalError(
                f"PRAGMA foreign_keys must report 1; got {foreign_keys}"
            )

        conn.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_MS}")
        busy_timeout = conn.execute("PRAGMA busy_timeout").fetchone()[0]
        if busy_timeout != SQLITE_BUSY_TIMEOUT_MS:
            raise sqlite3.OperationalError(
                "PRAGMA busy_timeout must report "
                f"{SQLITE_BUSY_TIMEOUT_MS}; got {busy_timeout}"
            )

        return conn
    except Exception as exc:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
        if isinstance(exc, sqlite3.OperationalError):
            raise
        raise sqlite3.OperationalError(
            f"SQLite connection policy setup failed: {exc}"
        ) from exc


def db() -> sqlite3.Connection:
    """Open an ordinary connection that verifies, but never assigns, WAL."""
    conn = _open_db()
    try:
        journal_mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        if str(journal_mode).lower() != "wal":
            raise sqlite3.OperationalError(
                f"PRAGMA journal_mode must report wal; got {journal_mode}"
            )
        _set_and_require_synchronous_normal(conn)
        return conn
    except Exception as exc:
        try:
            conn.close()
        except Exception:
            pass
        if isinstance(exc, sqlite3.OperationalError):
            raise
        raise sqlite3.OperationalError(
            f"SQLite connection policy setup failed: {exc}"
        ) from exc


def _set_and_require_synchronous_normal(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA synchronous=NORMAL")
    synchronous = conn.execute("PRAGMA synchronous").fetchone()[0]
    if synchronous != 1:
        raise sqlite3.OperationalError(
            f"PRAGMA synchronous must report 1; got {synchronous}"
        )


def _enable_wal_after_successful_init(conn: sqlite3.Connection) -> None:
    """Perform the sole journal-mode assignment after exact V2 succeeds."""
    journal_mode = conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]
    if str(journal_mode).lower() != "wal":
        raise sqlite3.OperationalError(
            f"PRAGMA journal_mode must report wal; got {journal_mode}"
        )
    _set_and_require_synchronous_normal(conn)


# ---------------------------------------------------------------------------
# Module-level helpers used by the migration runner.
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Phase 3 migration: idempotent, additive, transactional.
# ---------------------------------------------------------------------------

# Canonical Phase 3 migration version. New additive column definitions and
# indexes live here; future versions append additional statements.
PHASE3_TARGET_VERSION = 1
PHASE4_TARGET_VERSION = 2
RETRY_HORIZON_SECONDS = 3600  # 1 hour
MIN_VALID_TIMESTAMP = datetime(2000, 1, 1, tzinfo=timezone.utc)
SAMPLE_ID_ALIASES = ("sample_id", "event_id", "record_id")
TIMESTAMP_ALIASES = ("measured_at", "timestamp", "time")
ALLOWED_BATTERY_UNITS = ("V", "%", "unknown")


def _migration_needed(conn: sqlite3.Connection) -> bool:
    """Return True if the schema_migrations ledger is missing or empty
    AND the database does not yet contain a legacy-only Phase 2 schema
    that we still need to migrate forward.

    A pre-Phase-3 database may already have `devices`/`samples` tables but
    no `schema_migrations` row. We detect that by looking for the new
    additive columns on `samples`.
    """
    has_ledger = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
    ).fetchone()
    if not has_ledger:
        # The ledger is canonical authority. A database with additive columns
        # but no ledger is an interrupted/manual state and must be repaired.
        return True
    applied = conn.execute("SELECT version FROM schema_migrations").fetchall()
    return PHASE3_TARGET_VERSION not in {r[0] for r in applied}


def _phase3_migrate(conn: sqlite3.Connection) -> None:
    """Apply the Phase 3 data-model migration forward.

    Idempotent: safe to call repeatedly. Creates the schema_migrations ledger,
    adds additive columns, backfills them from legacy mirrors, and creates
    the two expression/partial indexes. Sets PRAGMA user_version=1 as an
    external compatibility signal after every statement succeeds.

    Preserves row counts, INTEGER PRIMARY KEY rowid values, and raw_json
    bytes byte-for-byte.
    """
    # All work happens inside one transaction. If anything raises, the whole
    # migration rolls back and the next startup retries it cleanly.
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY,
                applied_at TEXT NOT NULL
            )
            """
        )

        # ---- devices: additive override columns ----
        devices_cols = {row[1] for row in conn.execute("PRAGMA table_info(devices)")}
        if "reported_device_name" not in devices_cols:
            conn.execute("ALTER TABLE devices ADD COLUMN reported_device_name TEXT")
        if "user_device_name" not in devices_cols:
            conn.execute("ALTER TABLE devices ADD COLUMN user_device_name TEXT")
        if "reported_interval_sec" not in devices_cols:
            conn.execute("ALTER TABLE devices ADD COLUMN reported_interval_sec INTEGER")
        if "user_interval_sec" not in devices_cols:
            conn.execute("ALTER TABLE devices ADD COLUMN user_interval_sec INTEGER")

        # Backfill from legacy mirror columns. Use IS NOT NULL guards so the
        # backfill is safe to re-run on partial states.
        conn.execute(
            """
            UPDATE devices
               SET reported_device_name = COALESCE(reported_device_name, device_name)
             WHERE reported_device_name IS NULL AND device_name IS NOT NULL
            """
        )
        conn.execute(
            """
            UPDATE devices
               SET reported_interval_sec = COALESCE(reported_interval_sec, expected_interval_sec)
             WHERE reported_interval_sec IS NULL AND expected_interval_sec IS NOT NULL
            """
        )

        # ---- samples: additive telemetry columns ----
        samples_cols = {row[1] for row in conn.execute("PRAGMA table_info(samples)")}
        if "received_at" not in samples_cols:
            conn.execute("ALTER TABLE samples ADD COLUMN received_at TEXT")
        if "measured_at" not in samples_cols:
            conn.execute("ALTER TABLE samples ADD COLUMN measured_at TEXT")
        if "battery_value" not in samples_cols:
            conn.execute("ALTER TABLE samples ADD COLUMN battery_value REAL")
        if "battery_unit" not in samples_cols:
            conn.execute("ALTER TABLE samples ADD COLUMN battery_unit TEXT")
        if "battery_source" not in samples_cols:
            conn.execute("ALTER TABLE samples ADD COLUMN battery_source TEXT")
        if "sample_event_id" not in samples_cols:
            conn.execute("ALTER TABLE samples ADD COLUMN sample_event_id TEXT")
        if "retry_key" not in samples_cols:
            conn.execute("ALTER TABLE samples ADD COLUMN retry_key TEXT")
        if "payload_hash" not in samples_cols:
            conn.execute("ALTER TABLE samples ADD COLUMN payload_hash TEXT")

        # Backfill received_at from the legacy `ts` column. measured_at
        # intentionally stays NULL — `_injected_at` is never authoritative.
        conn.execute(
            """
            UPDATE samples
               SET received_at = COALESCE(received_at, ts)
             WHERE received_at IS NULL OR received_at = ''
            """
        )
        # Backfill battery semantics: keep the numeric value (or NULL), but
        # mark unit as 'unknown' and source as 'legacy_unknown'. Never infer
        # a unit from the numeric range.
        conn.execute(
            """
            UPDATE samples
               SET battery_unit  = COALESCE(battery_unit, 'unknown'),
                   battery_source = COALESCE(battery_source, 'legacy_unknown')
             WHERE battery_unit IS NULL OR battery_unit = ''
            """
        )
        conn.execute(
            """
            UPDATE samples
               SET battery_value = COALESCE(battery_value, battery)
             WHERE battery_value IS NULL
            """
        )

        # ---- expression / partial indexes ----
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_samples_device_effective_time
                ON samples (device_id, COALESCE(measured_at, received_at))
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_samples_device_retry_window
                ON samples (device_id, retry_key, received_at)
                WHERE retry_key IS NOT NULL
            """
        )

        # ---- record the migration as applied ----
        applied_at = _now_iso()
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (?, ?)",
            (PHASE3_TARGET_VERSION, applied_at),
        )

        # External compatibility signal. user_version is NOT the sole ledger
        # (schema_migrations is); it's only set after every statement has
        # succeeded, so an external probe can confirm migration completed.
        conn.execute(f"PRAGMA user_version = {PHASE3_TARGET_VERSION}")

        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def _phase4_migration_needed(conn: sqlite3.Connection) -> bool:
    """Return True if the Phase 4 migration has not yet been applied.

    The ledger is canonical. If the ledger is missing entirely we treat the
    database as pre-Phase-4 (Phase 3 would have created it). If the ledger
    is present, Phase 4 is needed when version=2 is not recorded.
    """
    has_ledger = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
    ).fetchone()
    if not has_ledger:
        return False  # Phase 3 will create the ledger; defer to it.
    applied = conn.execute("SELECT version FROM schema_migrations").fetchall()
    return PHASE4_TARGET_VERSION not in {r[0] for r in applied}


def _require_exact_v1_for_phase4(conn: sqlite3.Connection) -> None:
    """Fail closed unless the connection is on the exact supported V1 shape."""
    expected_columns = {
        "schema_migrations": ["version", "applied_at"],
        "devices": [
            "device_id", "device_name", "expected_interval_sec", "created_at",
            "last_seen", "config_json", "reported_device_name", "user_device_name",
            "reported_interval_sec", "user_interval_sec",
        ],
        "samples": [
            "id", "device_id", "ts", "angle", "gravity", "temp_c", "battery",
            "rssi", "ssid", "raw_json", "received_at", "measured_at",
            "battery_value", "battery_unit", "battery_source", "sample_event_id",
            "retry_key", "payload_hash",
        ],
        "calibrations": [
            "id", "device_id", "label", "a", "b", "created_at", "is_default",
        ],
    }
    objects = {
        (row[0], row[1])
        for row in conn.execute(
            "SELECT type,name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
        )
    }
    expected_objects = {
        ("table", "schema_migrations"), ("table", "devices"),
        ("table", "samples"), ("table", "calibrations"),
        ("index", "idx_samples_device_effective_time"),
        ("index", "idx_samples_device_retry_window"),
    }
    actual_columns = {
        table: [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]
        for table in expected_columns
    }
    versions = [row[0] for row in conn.execute(
        "SELECT version FROM schema_migrations ORDER BY version"
    )]
    user_version = conn.execute("PRAGMA user_version").fetchone()[0]
    if (
        objects != expected_objects
        or actual_columns != expected_columns
        or versions != [PHASE3_TARGET_VERSION]
        or user_version != PHASE3_TARGET_VERSION
        or conn.execute("PRAGMA foreign_key_check").fetchall()
    ):
        raise RuntimeError(
            "unsupported SQLite schema state: Phase 4 requires exact V1; "
            f"user_version={user_version}, ledger={versions}, objects={sorted(objects)}"
        )


def _normalized_schema_sql(conn: sqlite3.Connection, object_type: str) -> dict[str, str]:
    return {
        row[0]: " ".join(row[1].split())
        for row in conn.execute(
            "SELECT name,sql FROM sqlite_master WHERE type=? AND name NOT LIKE 'sqlite_%'",
            (object_type,),
        )
    }


def _foreign_key_signature(conn: sqlite3.Connection, table: str) -> set[tuple[Any, ...]]:
    groups: dict[int, list[sqlite3.Row]] = {}
    for row in conn.execute(f"PRAGMA foreign_key_list({table})"):
        groups.setdefault(int(row[0]), []).append(row)
    return {
        (
            rows[0][2],
            tuple((row[3], row[4]) for row in sorted(rows, key=lambda item: item[1])),
            rows[0][5], rows[0][6], rows[0][7],
        )
        for rows in groups.values()
    }


def _require_exact_v2(conn: sqlite3.Connection) -> None:
    expected_columns = {
        "schema_migrations": ["version", "applied_at"],
        "devices": [
            "device_id", "device_name", "expected_interval_sec", "created_at",
            "last_seen", "config_json", "reported_device_name", "user_device_name",
            "reported_interval_sec", "user_interval_sec",
        ],
        "samples": [
            "id", "device_id", "ts", "angle", "gravity", "temp_c", "battery",
            "rssi", "ssid", "raw_json", "received_at", "measured_at",
            "battery_value", "battery_unit", "battery_source", "sample_event_id",
            "retry_key", "payload_hash",
        ],
        "calibrations": [
            "id", "device_id", "label", "a", "b", "created_at", "is_default",
            "poly_order", "coefficients_json", "fit_r2", "point_count", "points_json",
        ],
        "calibration_active": ["device_id", "calibration_id", "activated_at"],
    }
    expected_objects = {
        ("table", "schema_migrations"), ("table", "devices"),
        ("table", "samples"), ("table", "calibrations"),
        ("table", "calibration_active"),
        ("index", "idx_samples_device_effective_time"),
        ("index", "idx_samples_device_retry_window"),
        ("index", "idx_calibrations_id_device"),
        ("index", "idx_calibrations_device_created"),
        ("trigger", "calibrations_immutable_update"),
        ("trigger", "calibrations_immutable_delete"),
    }
    objects = {
        (row[0], row[1]) for row in conn.execute(
            "SELECT type,name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
        )
    }
    actual_columns = {
        table: [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]
        for table in expected_columns
    }
    versions = [row[0] for row in conn.execute(
        "SELECT version FROM schema_migrations ORDER BY version"
    )]
    expected_indexes = {
        "idx_samples_device_effective_time": "CREATE INDEX idx_samples_device_effective_time ON samples (device_id, COALESCE(measured_at, received_at))",
        "idx_samples_device_retry_window": "CREATE INDEX idx_samples_device_retry_window ON samples (device_id, retry_key, received_at) WHERE retry_key IS NOT NULL",
        "idx_calibrations_id_device": "CREATE UNIQUE INDEX idx_calibrations_id_device ON calibrations(id, device_id)",
        "idx_calibrations_device_created": "CREATE INDEX idx_calibrations_device_created ON calibrations(device_id, created_at DESC, id DESC)",
    }
    expected_triggers = {
        "calibrations_immutable_update": "CREATE TRIGGER calibrations_immutable_update BEFORE UPDATE ON calibrations BEGIN SELECT RAISE(ABORT, 'calibrations are immutable'); END",
        "calibrations_immutable_delete": "CREATE TRIGGER calibrations_immutable_delete BEFORE DELETE ON calibrations BEGIN SELECT RAISE(ABORT, 'calibrations are immutable'); END",
    }
    active_fks = {
        ("devices", (("device_id", "device_id"),), "NO ACTION", "CASCADE", "NONE"),
        ("calibrations", (("calibration_id", "id"), ("device_id", "device_id")),
         "NO ACTION", "RESTRICT", "NONE"),
    }
    valid_rows = True
    for row in conn.execute("SELECT poly_order,coefficients_json FROM calibrations"):
        try:
            coefficients = json.loads(row[1])
            valid_rows = valid_rows and row[0] in (1, 2, 3) and len(coefficients) == row[0] + 1
            valid_rows = valid_rows and all(
                isinstance(value, (int, float)) and not isinstance(value, bool)
                and math.isfinite(float(value)) for value in coefficients
            )
        except (TypeError, ValueError, json.JSONDecodeError):
            valid_rows = False
    if (
        objects != expected_objects
        or actual_columns != expected_columns
        or versions != [PHASE3_TARGET_VERSION, PHASE4_TARGET_VERSION]
        or conn.execute("PRAGMA user_version").fetchone()[0] != PHASE4_TARGET_VERSION
        or _normalized_schema_sql(conn, "index") != expected_indexes
        or _normalized_schema_sql(conn, "trigger") != expected_triggers
        or _foreign_key_signature(conn, "calibration_active") != active_fks
        or not valid_rows
        or conn.execute("PRAGMA foreign_key_check").fetchall()
    ):
        raise RuntimeError("unsupported SQLite schema state: malformed or partial V2")


def _phase4_migrate(conn: sqlite3.Connection) -> None:
    """Apply the Phase 4 calibration migration forward.

    Idempotent: safe to call repeatedly. Adds the additive columns to
    ``calibrations``, backfills them from legacy `(a, b)` rows without
    touching the original row bytes, creates the activation pointer
    table, the supporting indexes, and records the migration in the
    ledger. Sets ``PRAGMA user_version = 2`` as the external
    compatibility signal after every statement succeeds.

    Atomicity: the entire migration runs in one ``BEGIN IMMEDIATE``
    transaction; any exception rolls back completely.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        _require_exact_v1_for_phase4(conn)
        # 1. Require the Phase 3 ledger row version=1; reject unknown/newer.
        applied = {
            r[0] for r in conn.execute("SELECT version FROM schema_migrations").fetchall()
        }
        if PHASE3_TARGET_VERSION not in applied:
            raise RuntimeError(
                "Phase 4 migration requires the Phase 3 ledger row "
                f"(version={PHASE3_TARGET_VERSION}) to be present."
            )
        if any(v > PHASE4_TARGET_VERSION for v in applied):
            raise RuntimeError(
                "Phase 4 migration refuses to run on a database with a "
                "newer schema_migrations row than " + str(PHASE4_TARGET_VERSION)
            )

        # 2. Add additive columns idempotently.
        cols = {row[1] for row in conn.execute("PRAGMA table_info(calibrations)")}
        if "poly_order" not in cols:
            conn.execute("ALTER TABLE calibrations ADD COLUMN poly_order INTEGER")
        if "coefficients_json" not in cols:
            conn.execute("ALTER TABLE calibrations ADD COLUMN coefficients_json TEXT")
        if "fit_r2" not in cols:
            conn.execute("ALTER TABLE calibrations ADD COLUMN fit_r2 REAL")
        if "point_count" not in cols:
            conn.execute("ALTER TABLE calibrations ADD COLUMN point_count INTEGER")
        if "points_json" not in cols:
            conn.execute("ALTER TABLE calibrations ADD COLUMN points_json TEXT")
        _migration_checkpoint("after_columns")

        # 3. Backfill legacy rows. Read `a`/`b` once, write canonical
        # compact JSON ascending coefficients [b, a]. Preserve every other
        # byte exactly. Only update rows where the additive fields are still
        # NULL (idempotent).
        legacy_rows = list(
            conn.execute(
                "SELECT id, a, b FROM calibrations WHERE coefficients_json IS NULL"
            )
        )
        for row in legacy_rows:
            coeffs = [row["b"], row["a"]]
            conn.execute(
                "UPDATE calibrations SET poly_order = ?, coefficients_json = ? "
                "WHERE id = ?",
                (
                    1,
                    json.dumps(coeffs, separators=(",", ":"), allow_nan=False),
                    row["id"],
                ),
            )
        _migration_checkpoint("after_backfill")

        # 4. Create the composite parent key before the child table.
        conn.execute(
            "CREATE UNIQUE INDEX idx_calibrations_id_device "
            "ON calibrations(id, device_id)"
        )
        _migration_checkpoint("after_unique_index")
        conn.execute(
            """
            CREATE TABLE calibration_active (
                device_id TEXT PRIMARY KEY,
                calibration_id INTEGER NOT NULL,
                activated_at TEXT NOT NULL,
                FOREIGN KEY(device_id) REFERENCES devices(device_id) ON DELETE CASCADE,
                FOREIGN KEY(calibration_id, device_id)
                    REFERENCES calibrations(id, device_id) ON DELETE RESTRICT
            )
            """
        )
        _migration_checkpoint("after_active_table")

        # 5. Resolve legacy defaults to a single highest-id pointer per device.
        #    Multiple legacy is_default=1 rows on the same device are resolved
        #    deterministically (highest id wins). The calibration rows themselves
        #    are NOT rewritten — `is_default` keeps its legacy byte values.
        legacy_devices = [
            r[0] for r in conn.execute(
                "SELECT DISTINCT device_id FROM calibrations WHERE is_default = 1"
            )
        ]
        activated_at = _now_iso()
        for dev_id in legacy_devices:
            max_id = conn.execute(
                "SELECT MAX(id) FROM calibrations WHERE device_id = ? AND is_default = 1",
                (dev_id,),
            ).fetchone()[0]
            if max_id is None:
                continue
            conn.execute(
                "INSERT OR IGNORE INTO calibration_active(device_id, calibration_id, activated_at) "
                "VALUES (?, ?, ?)",
                (dev_id, max_id, activated_at),
            )
        _migration_checkpoint("after_active_backfill")
        conn.execute(
            "CREATE INDEX idx_calibrations_device_created "
            "ON calibrations(device_id, created_at DESC, id DESC)"
        )
        _migration_checkpoint("after_history_index")

        # 6. Make calibration history mechanically append-only after legacy
        # backfill and active-pointer construction are complete.
        conn.execute(
            "CREATE TRIGGER calibrations_immutable_update BEFORE UPDATE ON calibrations "
            "BEGIN SELECT RAISE(ABORT, 'calibrations are immutable'); END"
        )
        conn.execute(
            "CREATE TRIGGER calibrations_immutable_delete BEFORE DELETE ON calibrations "
            "BEGIN SELECT RAISE(ABORT, 'calibrations are immutable'); END"
        )
        _migration_checkpoint("after_triggers")

        # 7. PRAGMA foreign_key_check must return zero rows.
        violations = conn.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise RuntimeError(
                "PRAGMA foreign_key_check returned violations after Phase 4 "
                "migration: " + repr(violations)
            )
        _migration_checkpoint("after_foreign_key_check")

        # 8. Insert ledger row, set user_version, commit.
        conn.execute(
            "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
            (PHASE4_TARGET_VERSION, _now_iso()),
        )
        _migration_checkpoint("after_ledger")
        conn.execute(f"PRAGMA user_version = {PHASE4_TARGET_VERSION}")
        _migration_checkpoint("after_user_version")

        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def _migration_checkpoint(name: str) -> None:
    """No-op seam for deterministic transaction failure tests."""
    del name


def _initialize_fresh_phase4(conn: sqlite3.Connection) -> None:
    """Create the exact V2 schema inside the caller's one open transaction."""
    conn.execute(
        "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
    )
    conn.execute(
        "CREATE TABLE devices ("
        "device_id TEXT PRIMARY KEY,device_name TEXT,"
        "expected_interval_sec INTEGER DEFAULT 300,created_at TEXT NOT NULL,"
        "last_seen TEXT,config_json TEXT,reported_device_name TEXT,"
        "user_device_name TEXT,reported_interval_sec INTEGER,user_interval_sec INTEGER)"
    )
    conn.execute(
        "CREATE TABLE samples ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT,device_id TEXT NOT NULL,ts TEXT NOT NULL,"
        "angle REAL,gravity REAL,temp_c REAL,battery REAL,rssi INTEGER,ssid TEXT,"
        "raw_json TEXT NOT NULL,received_at TEXT,measured_at TEXT,battery_value REAL,"
        "battery_unit TEXT,battery_source TEXT,sample_event_id TEXT,retry_key TEXT,"
        "payload_hash TEXT,FOREIGN KEY(device_id) REFERENCES devices(device_id))"
    )
    conn.execute(
        "CREATE TABLE calibrations ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT,device_id TEXT NOT NULL,label TEXT NOT NULL,"
        "a REAL NOT NULL,b REAL NOT NULL,created_at TEXT NOT NULL,"
        "is_default INTEGER DEFAULT 1,poly_order INTEGER,coefficients_json TEXT,"
        "fit_r2 REAL,point_count INTEGER,points_json TEXT,"
        "FOREIGN KEY(device_id) REFERENCES devices(device_id))"
    )
    conn.execute(
        "CREATE INDEX idx_samples_device_effective_time "
        "ON samples (device_id, COALESCE(measured_at, received_at))"
    )
    conn.execute(
        "CREATE INDEX idx_samples_device_retry_window "
        "ON samples (device_id, retry_key, received_at) WHERE retry_key IS NOT NULL"
    )
    conn.execute(
        "CREATE UNIQUE INDEX idx_calibrations_id_device ON calibrations(id, device_id)"
    )
    conn.execute(
        "CREATE TABLE calibration_active ("
        "device_id TEXT PRIMARY KEY,calibration_id INTEGER NOT NULL,activated_at TEXT NOT NULL,"
        "FOREIGN KEY(device_id) REFERENCES devices(device_id) ON DELETE CASCADE,"
        "FOREIGN KEY(calibration_id,device_id) REFERENCES calibrations(id,device_id) "
        "ON DELETE RESTRICT)"
    )
    conn.execute(
        "CREATE INDEX idx_calibrations_device_created "
        "ON calibrations(device_id, created_at DESC, id DESC)"
    )
    conn.execute(
        "CREATE TRIGGER calibrations_immutable_update BEFORE UPDATE ON calibrations "
        "BEGIN SELECT RAISE(ABORT, 'calibrations are immutable'); END"
    )
    conn.execute(
        "CREATE TRIGGER calibrations_immutable_delete BEFORE DELETE ON calibrations "
        "BEGIN SELECT RAISE(ABORT, 'calibrations are immutable'); END"
    )
    _migration_checkpoint("fresh_after_objects")
    conn.execute(
        "INSERT INTO schema_migrations(version,applied_at) VALUES (?,?)",
        (PHASE3_TARGET_VERSION, _now_iso()),
    )
    conn.execute(
        "INSERT INTO schema_migrations(version,applied_at) VALUES (?,?)",
        (PHASE4_TARGET_VERSION, _now_iso()),
    )
    conn.execute(f"PRAGMA user_version={PHASE4_TARGET_VERSION}")
    if conn.execute("PRAGMA foreign_key_check").fetchall():
        raise RuntimeError("fresh Phase 4 foreign key check failed")


def init_db() -> None:
    """Initialize the database without implicit-commit executescript calls."""
    conn = _open_db()
    try:
        user_objects = conn.execute(
            "SELECT type,name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
        ).fetchall()
        if not user_objects and conn.execute("PRAGMA user_version").fetchone()[0] == 0:
            conn.execute("BEGIN IMMEDIATE")
            try:
                _initialize_fresh_phase4(conn)
                conn.execute("COMMIT")
            except Exception:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
        else:
            # Historical Phase 3 migration remains callable for exact legacy
            # test fixtures; Phase 4 accepts only the exact V1 precondition.
            if _migration_needed(conn):
                _phase3_migrate(conn)
            if _phase4_migration_needed(conn):
                _phase4_migrate(conn)
            else:
                _require_exact_v2(conn)

        # Journal mode changes only after fresh initialization, migration, or
        # exact-V2 validation has completed successfully.
        _enable_wal_after_successful_init(conn)
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


init_db()

BREWING_STORE = BrewingStore(BREW_DB_PATH)
BREWING_STORE.initialize()


def _brewing_device_exists(device_id: str) -> bool:
    with db() as conn:
        return conn.execute(
            "SELECT 1 FROM devices WHERE device_id=?", (device_id,)
        ).fetchone() is not None


def _brewing_latest_sample(device_id: str) -> dict[str, Any] | None:
    with db() as conn:
        row = conn.execute(
            """
            SELECT id,COALESCE(measured_at,received_at) AS observed_at,angle,temp_c
            FROM samples WHERE device_id=?
            ORDER BY COALESCE(measured_at,received_at) DESC,id DESC LIMIT 1
            """,
            (device_id,),
        ).fetchone()
    if row is None:
        return None
    return {
        "sample_id": row["id"],
        "observed_at": row["observed_at"],
        "angle": row["angle"],
        "temperature_c": row["temp_c"],
    }


app.include_router(
    create_brewing_router(
        BREWING_STORE,
        _brewing_device_exists,
        _brewing_latest_sample,
    )
)

ASSISTANT_STORE = AssistantStore(BREW_DB_PATH)
ASSISTANT_CLIENT = ZeroClawClient()
ASSISTANT_STRUCTURED_CLIENT = CombinedGemmaClient()
RESEARCH_BROKER = ResearchBroker()
ASSISTANT_ROUTER = create_assistant_router(
    BREWING_STORE,
    ASSISTANT_STORE,
    ASSISTANT_CLIENT,
    RESEARCH_BROKER,
    _brewing_latest_sample,
    ASSISTANT_STRUCTURED_CLIENT,
)
app.include_router(ASSISTANT_ROUTER)
app.state.assistant_pipeline = getattr(ASSISTANT_ROUTER, "assistant_pipeline", None)


class CalibrationPoint(BaseModel):
    angle: float = Field(ge=-360, le=360, allow_inf_nan=False)
    value: float = Field(ge=0.8, le=1.3, allow_inf_nan=False)

    @field_validator("angle", "value", mode="before")
    @classmethod
    def reject_boolean_number(cls, value: Any) -> Any:
        if isinstance(value, bool):
            raise ValueError("boolean is not a calibration number")
        return value


class CalibrationPayload(BaseModel):
    label: str
    order: StrictInt = Field(default=1, ge=1, le=3)
    activate: StrictBool = True
    points: List[CalibrationPoint] = Field(min_length=2, max_length=32)

    @field_validator("label", mode="before")
    @classmethod
    def strip_and_validate_label(cls, value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("label must be a string")
        label = value.strip()
        if not 1 <= len(label) <= 80:
            raise ValueError("label length after stripping must be 1..80")
        return label


def _evaluate_polynomial(coefficients: List[float], angle: float) -> float:
    """Evaluate ascending-power coefficients using Horner's method."""
    value = 0.0
    for coefficient in reversed(coefficients):
        value = value * angle + coefficient
    return value


def _is_monotonic_calibration(
    coefficients: List[float], minimum_angle: float, maximum_angle: float
) -> bool:
    """Return whether a degree <= 3 fit is non-decreasing on its domain."""
    if len(coefficients) < 2 or len(coefficients) > 4:
        return False
    derivative_candidates = [minimum_angle, maximum_angle]
    if len(coefficients) == 4 and coefficients[3] != 0.0:
        vertex = -coefficients[2] / (3.0 * coefficients[3])
        if minimum_angle < vertex < maximum_angle:
            derivative_candidates.append(vertex)
    slopes = [
        coefficients[1]
        + (2.0 * coefficients[2] * angle if len(coefficients) >= 3 else 0.0)
        + (
            3.0 * coefficients[3] * angle * angle
            if len(coefficients) == 4 else 0.0
        )
        for angle in derivative_candidates
    ]
    tolerance = 1e-12 * max(1.0, *(abs(slope) for slope in slopes))
    return all(math.isfinite(slope) and slope >= -tolerance for slope in slopes)


def _fit_calibration(
    points: List[Tuple[float, float]], order: int
) -> Tuple[List[float], float, List[Tuple[float, float]]]:
    """Return deterministic original-angle coefficients, R², sorted points."""
    if order not in (1, 2, 3) or len(points) < order + 1:
        raise ValueError("insufficient calibration points for order")
    ordered = sorted((float(x), float(y)) for x, y in points)
    if len({x for x, _ in ordered}) != len(ordered):
        raise ValueError("calibration angles must be distinct")
    if not all(math.isfinite(x) and math.isfinite(y) for x, y in ordered):
        raise ValueError("calibration fit produced a non-finite result")
    for (left, _), (right, __) in zip(ordered, ordered[1:]):
        if abs(right - left) <= 1e-12 * max(1.0, abs(left), abs(right)):
            raise ValueError("calibration fit is degenerate")
    if any(
        right_value < left_value
        for (_, left_value), (_, right_value) in zip(ordered, ordered[1:])
    ):
        raise ValueError("calibration values must be monotonic non-decreasing")

    count = len(ordered)
    mean = math.fsum(x for x, _ in ordered) / count
    scale = max(abs(x - mean) for x, _ in ordered)
    if not math.isfinite(scale) or scale == 0.0:
        raise ValueError("calibration fit is degenerate")
    normalized = [((x - mean) / scale, y) for x, y in ordered]
    width = order + 1
    matrix = [
        [math.fsum(z ** (row + column) for z, _ in normalized) for column in range(width)]
        for row in range(width)
    ]
    rhs = [
        math.fsum(y * (z ** row) for z, y in normalized)
        for row in range(width)
    ]
    row_scales = [max(abs(value) for value in row) for row in matrix]
    if any(not math.isfinite(value) or value == 0.0 for value in row_scales):
        raise ValueError("calibration fit is degenerate")

    for column in range(width):
        pivot = max(
            range(column, width),
            key=lambda row: abs(matrix[row][column]) / row_scales[row],
        )
        pivot_ratio = abs(matrix[pivot][column]) / row_scales[pivot]
        if not math.isfinite(pivot_ratio) or pivot_ratio <= 1e-12:
            raise ValueError("calibration fit is degenerate")
        if pivot != column:
            matrix[column], matrix[pivot] = matrix[pivot], matrix[column]
            rhs[column], rhs[pivot] = rhs[pivot], rhs[column]
            row_scales[column], row_scales[pivot] = row_scales[pivot], row_scales[column]
        for row in range(column + 1, width):
            factor = matrix[row][column] / matrix[column][column]
            matrix[row][column] = 0.0
            for index in range(column + 1, width):
                matrix[row][index] -= factor * matrix[column][index]
            rhs[row] -= factor * rhs[column]

    normalized_coefficients = [0.0] * width
    for row in range(width - 1, -1, -1):
        remainder = math.fsum(
            matrix[row][column] * normalized_coefficients[column]
            for column in range(row + 1, width)
        )
        normalized_coefficients[row] = (rhs[row] - remainder) / matrix[row][row]

    d0, d1 = normalized_coefficients[:2]
    if order == 1:
        coefficients = [d0 - d1 * mean / scale, d1 / scale]
    elif order == 2:
        d2 = normalized_coefficients[2]
        scale2 = scale * scale
        coefficients = [
            math.fsum((d0, -d1 * mean / scale, d2 * mean * mean / scale2)),
            d1 / scale - 2.0 * d2 * mean / scale2,
            d2 / scale2,
        ]
    else:
        d2, d3 = normalized_coefficients[2:4]
        scale2 = scale * scale
        scale3 = scale2 * scale
        coefficients = [
            math.fsum((
                d0,
                -d1 * mean / scale,
                d2 * mean * mean / scale2,
                -d3 * mean * mean * mean / scale3,
            )),
            math.fsum((
                d1 / scale,
                -2.0 * d2 * mean / scale2,
                3.0 * d3 * mean * mean / scale3,
            )),
            d2 / scale2 - 3.0 * d3 * mean / scale3,
            d3 / scale3,
        ]
    coefficients = [0.0 if value == 0.0 else value for value in coefficients]
    fitted = [_evaluate_polynomial(coefficients, x) for x, _ in ordered]
    if not all(math.isfinite(value) for value in coefficients + fitted):
        raise ValueError("calibration fit produced a non-finite result")
    if not _is_monotonic_calibration(
        coefficients, ordered[0][0], ordered[-1][0]
    ):
        raise ValueError("fitted calibration is not monotonic over its angle range")
    mean_y = math.fsum(y for _, y in ordered) / count
    sse = math.fsum((y - predicted) ** 2 for (_, y), predicted in zip(ordered, fitted))
    sst = math.fsum((y - mean_y) ** 2 for _, y in ordered)
    if sst == 0.0:
        r2 = 1.0 if sse <= 1e-12 else 0.0
    else:
        r2 = 1.0 - sse / sst
    r2 = 0.0 if r2 == 0.0 else r2
    if not math.isfinite(r2):
        raise ValueError("calibration fit produced a non-finite result")
    return coefficients, r2, ordered


def _calibration_dict(row: sqlite3.Row, active_id: int | None) -> Dict[str, Any]:
    coefficients = json.loads(row["coefficients_json"])
    points = json.loads(row["points_json"]) if row["points_json"] is not None else None
    return {
        "id": row["id"], "device_id": row["device_id"], "label": row["label"],
        "a": row["a"], "b": row["b"], "created_at": row["created_at"],
        "order": row["poly_order"], "coefficients": coefficients,
        "fit_r2": row["fit_r2"], "point_count": row["point_count"],
        "points": points,
        "monotonic": _is_monotonic_calibration(
            coefficients, points[0]["angle"], points[-1]["angle"]
        ) if points else None,
        "is_active": row["id"] == active_id,
    }


class DeviceUpdate(BaseModel):
    # Both null and an empty string are explicit clearing values. The UI sends
    # an empty string; API clients commonly send null.
    device_name: str | None = Field(default=None, min_length=0, max_length=80)
    expected_interval_sec: int | None = Field(default=None, ge=5, le=86_400)


class IngestPayload(BaseModel):
    """Firmware-compatible aliases with bounded telemetry fields."""
    model_config = ConfigDict(extra="allow", populate_by_name=True)
    # Stock ESP8266 firmware sends its uint32 chip ID as a JSON number.
    # Normalize that at the boundary so the constrained field is always a string.
    device_id: str | None = Field(default=None, validation_alias=AliasChoices("ID", "id", "device_id", "name"), min_length=1, max_length=128)
    name: str | None = Field(default=None, max_length=80)
    ssid: str | None = Field(default=None, validation_alias=AliasChoices("SSID", "ssid", "ap", "apname", "AP"), max_length=64)
    angle: float | None = Field(default=None, ge=-360, le=360, allow_inf_nan=False)
    gravity: float | None = Field(default=None, ge=0.8, le=1.3, allow_inf_nan=False)
    temperature: float | None = Field(default=None, validation_alias=AliasChoices("temperature", "temp"), ge=-80, le=120, allow_inf_nan=False)
    tempf: float | None = Field(default=None, ge=-112, le=248, allow_inf_nan=False)
    battery: float | None = Field(default=None, validation_alias=AliasChoices("battery", "batt", "battery_voltage"), ge=0, le=100, allow_inf_nan=False)
    rssi: int | None = Field(default=None, validation_alias=AliasChoices("rssi", "RSSI"), ge=-150, le=0)
    interval: int | None = Field(default=None, validation_alias=AliasChoices("interval", "sleep"), ge=1, le=86_400)
    error: str | None = Field(default=None, max_length=512)

    @field_validator("device_id", mode="before")
    @classmethod
    def normalize_numeric_device_id(cls, value: Any) -> Any:
        if isinstance(value, bool):
            raise ValueError("boolean device IDs are not accepted")
        if isinstance(value, int):
            if not 1 <= value <= 0xFFFFFFFF:
                raise ValueError("numeric device IDs must be unsigned 32-bit integers")
            return str(value)
        if isinstance(value, float):
            raise ValueError("numeric device IDs must be integers")
        return value


def _coerce_float(v: Any) -> Optional[float]:
    try:
        if v is None:
            return None
        f = float(v)
        if not math.isfinite(f):
            return None
        return f
    except (TypeError, ValueError):
        return None


def _coerce_int(v: Any) -> Optional[int]:
    try:
        if v is None:
            return None
        return int(float(v))
    except (TypeError, ValueError):
        return None


def _as_id(payload: Dict[str, Any]) -> str:
    # The stock firmware ID is authoritative; token is strictly a credential.
    for key in ("ID", "id", "device_id", "name"):
        if payload.get(key):
            return str(payload[key])
    raise HTTPException(status_code=422, detail="stable device identity is required")


# ---------------------------------------------------------------------------
# Phase 3 telemetry parsing helpers
# ---------------------------------------------------------------------------


def _normalize_sample_id(raw: Any) -> Optional[str]:
    """Return a trimmed, bounded-length stable event id, or None if absent.

    Stable-id aliases (`sample_id`, `event_id`, `record_id`) are limited
    to 128 characters after string normalization. Whitespace-only or empty
    strings are treated as absent.
    """
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    return s[:128]


def _extract_sample_id(payload: Dict[str, Any]) -> Optional[str]:
    """Find a stable sample event id across the documented aliases."""
    for key in SAMPLE_ID_ALIASES:
        if key in payload:
            sid = _normalize_sample_id(payload.get(key))
            if sid is not None:
                return sid
    return None


def _parse_timestamp(value: Any, *, now: datetime) -> Optional[datetime]:
    """Parse a Phase-3 timestamp value into UTC, applying the contract
    bounds. Returns None if the value is absent. Raises HTTPException(422)
    for malformed, naive, pre-2000, or far-future inputs.
    """
    if value is None or value == "":
        return None
    parsed: Optional[datetime] = None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        # Numeric Unix epoch. Treat integer > 10**12 as milliseconds.
        epoch = float(value)
        if epoch > 1e12:
            epoch = epoch / 1000.0
        try:
            parsed = datetime.fromtimestamp(epoch, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            raise HTTPException(status_code=422, detail="timestamp out of range")
    elif isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        try:
            # `fromisoformat` accepts explicit offsets but NOT a trailing 'Z'.
            candidate = s.replace("Z", "+00:00") if s.endswith("Z") else s
            parsed = datetime.fromisoformat(candidate)
        except ValueError:
            raise HTTPException(status_code=422, detail="malformed timestamp")
        if parsed.tzinfo is None:
            raise HTTPException(
                status_code=422, detail="naive timestamps are not accepted"
            )
        parsed = parsed.astimezone(timezone.utc)
    else:
        raise HTTPException(status_code=422, detail="timestamp has unsupported type")

    # Phase 3 lower bound: 2000-01-01T00:00:00Z
    if parsed < MIN_VALID_TIMESTAMP:
        raise HTTPException(
            status_code=422,
            detail=f"timestamp must be on or after {MIN_VALID_TIMESTAMP.isoformat()}",
        )
    # Phase 3 upper bound: now + 5 minutes. Comparisons are timezone-aware.
    if parsed > now + timedelta(minutes=5):
        raise HTTPException(
            status_code=422, detail="timestamp is too far in the future"
        )
    return parsed


def _extract_measured_at(payload: Dict[str, Any], *, now: datetime) -> Tuple[Optional[datetime], Optional[str]]:
    """Locate a device-reported `measured_at` value using the documented
    aliases. Returns (parsed_datetime_or_None, normalized_iso_or_None).
    `_injected_at` is intentionally excluded.
    """
    for key in TIMESTAMP_ALIASES:
        if key in payload and payload[key] is not None:
            parsed = _parse_timestamp(payload[key], now=now)
            if parsed is not None:
                return parsed, parsed.isoformat()
    return None, None


def _parse_battery(payload: Dict[str, Any]) -> Tuple[Optional[float], str, str]:
    """Apply the Phase 3 battery semantics. Returns (value, unit, source).

    Rules (in order):
      1. explicit_voltage: payload has `battery_voltage` → validate 0..6, unit V
      2. explicit_percent: payload has `battery_percent` → validate 0..100, unit %
      3. explicit_unit: payload has `battery`/`batt` AND `battery_unit` is V/% →
         validate against the supplied unit's range, source='explicit_unit'
      4. ambiguous_unknown: payload has `battery`/`batt` but NO `battery_unit` →
         keep the numeric value (or None), unit='unknown', source='ambiguous_unknown'
      5. omitted: payload has none of the above → value=None, unit='unknown',
         source='omitted'
      6. conflicting explicit representations (e.g. battery_voltage AND
         battery_percent in the same payload) → 422
    """
    has_voltage = "battery_voltage" in payload and payload["battery_voltage"] is not None
    has_percent = "battery_percent" in payload and payload["battery_percent"] is not None

    if has_voltage and has_percent:
        raise HTTPException(
            status_code=422,
            detail="conflicting battery representations: battery_voltage and battery_percent",
        )

    if has_voltage:
        v = _coerce_float(payload.get("battery_voltage"))
        if v is None or not (0 <= v <= 6):
            raise HTTPException(
                status_code=422,
                detail="battery_voltage must be within 0..6 V",
            )
        return v, "V", "explicit_voltage"

    if has_percent:
        v = _coerce_float(payload.get("battery_percent"))
        if v is None or not (0 <= v <= 100):
            raise HTTPException(
                status_code=422,
                detail="battery_percent must be within 0..100",
            )
        return v, "%", "explicit_percent"

    raw_value = payload.get("battery")
    if raw_value is None:
        raw_value = payload.get("batt")
    raw_unit = payload.get("battery_unit") or payload.get("batt_unit")

    if raw_value is None:
        return None, "unknown", "omitted"

    v = _coerce_float(raw_value)
    if v is None:
        return None, "unknown", "ambiguous_unknown"

    if raw_unit in ("V", "%"):
        bounds = (0, 6) if raw_unit == "V" else (0, 100)
        if not (bounds[0] <= v <= bounds[1]):
            raise HTTPException(
                status_code=422,
                detail=f"battery value {v} is out of range for unit {raw_unit!r}",
            )
        return v, raw_unit, "explicit_unit"

    # Bare `battery`/`batt` with no canonical unit — ambiguous.
    return v, "unknown", "ambiguous_unknown"


# ---------------------------------------------------------------------------
# Canonical payload hash
# ---------------------------------------------------------------------------


def _canonical_payload_hash(*, device_id: str, normalized_measured_at: Optional[str],
                            angle: Optional[float], gravity: Optional[float],
                            temp_c: Optional[float], battery_value: Optional[float],
                            battery_unit: str, ssid: Optional[str], rssi: Optional[int]) -> str:
    """Compute a SHA-256 over a deterministic UTF-8 JSON encoding of the
    normalized telemetry fields. `sort_keys=True`, compact separators, no
    NaN/Inf. Raw JSON formatting differences must not alter the hash.
    """
    canonical = {
        "device_id": device_id,
        "measured_at": normalized_measured_at,
        "angle": angle,
        "gravity": gravity,
        "temp_c": temp_c,
        "battery_value": battery_value,
        "battery_unit": battery_unit,
        "ssid": ssid,
        "rssi": rssi,
    }
    blob = json.dumps(canonical, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _retry_key(*, device_id: str, sample_event_id: Optional[str],
               normalized_measured_at: Optional[str], payload_hash: str) -> Optional[str]:
    """Compute the dedup retry key per the contract.

    Stable event ID present: derive from device_id + event_id.
    No event ID but validated measured_at present: derive from device_id +
    measured_at + payload_hash.
    Neither present: None (every request inserts a row).
    """
    if sample_event_id:
        return f"evt:{device_id}:{sample_event_id}"
    if normalized_measured_at is not None:
        return f"mt:{device_id}:{normalized_measured_at}:{payload_hash}"
    return None


class _Phase3Conflict(Exception):
    """Internal signal that ingest must surface as HTTP 409.

    We roll the transaction back inside the ingest endpoint before raising
    this exception, then convert it to the public 409 response.
    """

    def __init__(self, retry_key: str) -> None:
        super().__init__(retry_key)
        self.retry_key = retry_key


@app.get("/health")
def health() -> Dict[str, Any]:
    return {"status": "ok", "service": "ispindel-dashboard"}


@app.get("/health/live")
def health_live() -> Dict[str, Any]:
    """Process-only liveness probe; deliberately independent of SQLite."""
    return {"status": "ok", "service": "ispindel-dashboard"}


@app.get("/health/ready")
def health_ready() -> Any:
    """Read-only readiness probe that requires the exact supported V2 schema."""
    conn: sqlite3.Connection | None = None
    try:
        uri = DB_PATH.resolve().as_uri() + "?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=1.0)
    except (sqlite3.Error, OSError, ValueError):
        return JSONResponse(
            status_code=503,
            content={
                "status": "error",
                "service": "ispindel-dashboard",
                "detail": "database unavailable",
            },
        )
    try:
        conn.row_factory = sqlite3.Row
        _require_exact_v2(conn)
    except (RuntimeError, sqlite3.Error):
        return JSONResponse(
            status_code=503,
            content={
                "status": "error",
                "service": "ispindel-dashboard",
                "detail": "schema validation failed",
            },
        )
    finally:
        conn.close()
    return {"status": "ok", "service": "ispindel-dashboard"}


@app.get("/api/devices")
def list_devices() -> Dict[str, Any]:
    """Return the list of devices with effective + provenance fields.

    `device_name` and `expected_interval_sec` remain the legacy alias keys;
    they continue to carry the effective (user-overridden OR reported) value
    so existing Phase 2 clients and the bundled UI keep working without
    changes. `effective_*` keys are explicit aliases with the same meaning.
    """
    with db() as conn:
        rows = list(
            conn.execute(
                """
                SELECT d.device_id,
                       d.device_name,
                       d.expected_interval_sec,
                       d.reported_device_name,
                       d.user_device_name,
                       d.reported_interval_sec,
                       d.user_interval_sec,
                       d.last_seen,
                       COALESCE(d.user_device_name, d.reported_device_name, d.device_name) AS effective_device_name,
                       COALESCE(d.user_interval_sec, d.reported_interval_sec, d.expected_interval_sec) AS effective_interval_sec,
                       (SELECT COUNT(*) FROM samples s WHERE s.device_id=d.device_id) AS sample_count
                  FROM devices d
                 ORDER BY d.device_id ASC
                """
            )
        )
    intents = BREWING_STORE.get_operating_intents([str(row["device_id"]) for row in rows])
    devices: list[dict[str, Any]] = []
    for row in rows:
        device = dict(row)
        intent = intents[str(device["device_id"])]
        device.update(
            {
                "operating_mode": intent["mode"],
                "camera_policy": intent["camera_policy"],
                "operating_intent_configured": intent["configured"],
                "operating_intent_updated_at": intent["updated_at"],
            }
        )
        devices.append(device)
    return {"devices": devices, "count": len(devices)}


@app.get("/api/device/{device_id}/samples")
def list_samples(device_id: str, hours: float = 24) -> Dict[str, Any]:
    """Return raw samples plus query-time calibration from one pointer snapshot."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=max(hours, 1 / 60))
    with db() as conn:
        conn.execute("BEGIN")
        active = conn.execute(
            "SELECT ca.calibration_id, c.coefficients_json "
            "FROM calibration_active ca JOIN calibrations c "
            "ON c.id=ca.calibration_id AND c.device_id=ca.device_id "
            "WHERE ca.device_id=?",
            (device_id,),
        ).fetchone()
        active_id = active["calibration_id"] if active is not None else None
        coefficients = json.loads(active["coefficients_json"]) if active is not None else None
        rows = conn.execute(
            """
            SELECT id,
                   ts,
                   received_at,
                   measured_at,
                   COALESCE(measured_at, received_at) AS effective_ts,
                   angle,
                   gravity,
                   temp_c,
                   battery_value,
                   battery_unit,
                   battery_source,
                   battery AS battery,
                   rssi,
                   ssid
              FROM samples
             WHERE device_id=?
               AND COALESCE(measured_at, received_at) >= ?
             ORDER BY COALESCE(measured_at, received_at) ASC
            """,
            (device_id, cutoff.isoformat()),
        ).fetchall()
        dev = conn.execute(
            "SELECT device_id, device_name, COALESCE(user_device_name, reported_device_name, device_name) AS effective_name "
            "FROM devices WHERE device_id=?",
            (device_id,),
        ).fetchone()
        conn.execute("COMMIT")

    if dev is None:
        raise HTTPException(status_code=404, detail="device not found")

    samples = []
    for row in rows:
        sample = dict(row)
        sample["ts"] = sample["effective_ts"]
        sample["raw_gravity"] = sample["gravity"]
        sample["calibrated_gravity"] = (
            _evaluate_polynomial(coefficients, sample["angle"])
            if coefficients is not None and sample["angle"] is not None
            else None
        )
        sample["calibration_id"] = active_id
        samples.append(sample)

    return {
        "device_id": device_id,
        "device_name": dev["effective_name"],
        "window_hours": hours,
        "active_calibration_id": active_id,
        "count": len(samples),
        "samples": samples,
    }


@app.get("/api/device/{device_id}/calibration")
def get_calibration(device_id: str) -> Dict[str, Any]:
    with db() as conn:
        active = conn.execute(
            "SELECT calibration_id FROM calibration_active WHERE device_id=?",
            (device_id,),
        ).fetchone()
        active_id = active["calibration_id"] if active is not None else None
        rows = conn.execute(
            "SELECT id, device_id, label, a, b, created_at, poly_order, "
            "coefficients_json, fit_r2, point_count, points_json "
            "FROM calibrations WHERE device_id=? ORDER BY created_at DESC, id DESC",
            (device_id,),
        ).fetchall()
    history = [_calibration_dict(row, active_id) for row in rows]
    active_rows = [row for row in history if row["is_active"]]
    return {
        "device_id": device_id,
        "has_calibration": bool(active_rows),
        "calibrations": active_rows,
        "active_calibration_id": active_id,
        "history": history,
    }


def _calibration_tx_checkpoint(name: str) -> None:
    """No-op seam for deterministic calibration transaction failure tests."""
    del name


@app.post("/api/device/{device_id}/calibration")
def set_calibration(device_id: str, payload: CalibrationPayload) -> Dict[str, Any]:
    label = payload.label.strip()
    if not label:
        raise HTTPException(status_code=422, detail="calibration label must not be blank")
    points = [(point.angle, point.value) for point in payload.points]
    try:
        coefficients, fit_r2, ordered = _fit_calibration(points, payload.order)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    points_json = json.dumps(
        [{"angle": angle, "value": value} for angle, value in ordered],
        separators=(",", ":"), allow_nan=False,
    )
    coefficients_json = json.dumps(coefficients, separators=(",", ":"), allow_nan=False)
    created_at = _now_iso()
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            if conn.execute("SELECT 1 FROM devices WHERE device_id=?", (device_id,)).fetchone() is None:
                raise HTTPException(status_code=404, detail="device not found")
            cursor = conn.execute(
                "INSERT INTO calibrations "
                "(device_id,label,a,b,created_at,is_default,poly_order,coefficients_json,fit_r2,point_count,points_json) "
                "VALUES (?,?,?,?,?,1,?,?,?,?,?)",
                (device_id, label, coefficients[1], coefficients[0], created_at,
                 payload.order, coefficients_json, fit_r2, len(ordered), points_json),
            )
            calibration_id = int(cursor.lastrowid)
            _calibration_tx_checkpoint("post_after_history_insert")
            if payload.activate:
                conn.execute(
                    "INSERT INTO calibration_active(device_id,calibration_id,activated_at) VALUES (?,?,?) "
                    "ON CONFLICT(device_id) DO UPDATE SET calibration_id=excluded.calibration_id, "
                    "activated_at=excluded.activated_at "
                    "WHERE calibration_active.calibration_id <> excluded.calibration_id",
                    (device_id, calibration_id, created_at),
                )
                _calibration_tx_checkpoint("post_after_pointer_upsert")
            row = conn.execute(
                "SELECT id,device_id,label,a,b,created_at,poly_order,coefficients_json,"
                "fit_r2,point_count,points_json FROM calibrations WHERE id=?",
                (calibration_id,),
            ).fetchone()
            response = _calibration_dict(
                row, calibration_id if payload.activate else None
            )
            _calibration_tx_checkpoint("post_before_commit")
            conn.execute("COMMIT")
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
    return response


@app.put("/api/device/{device_id}/calibration/{calibration_id}/active")
def activate_calibration(device_id: str, calibration_id: int) -> Dict[str, Any]:
    activated_at = _now_iso()
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            if conn.execute("SELECT 1 FROM devices WHERE device_id=?", (device_id,)).fetchone() is None:
                raise HTTPException(status_code=404, detail="device not found")
            row = conn.execute(
                "SELECT id,device_id,label,a,b,created_at,poly_order,coefficients_json,"
                "fit_r2,point_count,points_json FROM calibrations WHERE id=?",
                (calibration_id,),
            ).fetchone()
            if row is None:
                raise HTTPException(status_code=404, detail="calibration not found")
            if row["device_id"] != device_id:
                raise HTTPException(status_code=409, detail="calibration belongs to another device")
            conn.execute(
                "INSERT INTO calibration_active(device_id,calibration_id,activated_at) VALUES (?,?,?) "
                "ON CONFLICT(device_id) DO UPDATE SET calibration_id=excluded.calibration_id, "
                "activated_at=excluded.activated_at "
                "WHERE calibration_active.calibration_id <> excluded.calibration_id",
                (device_id, calibration_id, activated_at),
            )
            _calibration_tx_checkpoint("activate_after_pointer_upsert")
            response = _calibration_dict(row, calibration_id)
            _calibration_tx_checkpoint("activate_before_commit")
            conn.execute("COMMIT")
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
    return response


@app.post("/api/ingest")
async def ingest(request: Request):
    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type != "application/json":
        raise HTTPException(status_code=415, detail="content type must be application/json")
    try:
        payload = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="invalid JSON payload") from exc

    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="invalid payload")

    device_id = _as_id(payload)
    if not authenticate(SECURITY_CONFIG, device_id, payload.get("token")):
        raise HTTPException(status_code=401, detail="invalid ingest credentials")
    allowed, retry_after = INGEST_BUCKET.consume(device_id)
    if not allowed:
        return JSONResponse(
            status_code=429,
            content={"detail": "ingest rate limit exceeded"},
            headers={"Retry-After": str(retry_after)},
        )

    # Credentials never reach validation, hashing, responses, or raw_json.
    payload = dict(payload)
    payload.pop("token", None)

    # Validate the known firmware aliases at the API boundary while retaining
    # vendor-specific extra keys verbatim in raw_json for compatibility.
    try:
        validated = IngestPayload.model_validate(payload)
    except ValidationError as exc:
        raise HTTPException(
            status_code=422,
            detail=exc.errors(include_input=False, include_context=False),
        ) from exc

    # The reported name should prefer the firmware-provided `name`/`ID`
    # without persisting a user override. Empty strings are treated as
    # "no reported name" so the existing user override keeps winning.
    reported_name_candidate = (
        payload.get("name")
        or payload.get("esp_id")
        or payload.get("ID")
        or payload.get("id")
    )
    reported_name = str(reported_name_candidate) if reported_name_candidate else None

    angle = validated.angle
    gravity = validated.gravity

    # Temperature handling:
    #  - canonical firmware key: "temperature"
    #  - some forks report "temp" (usually celsius)
    #  - legacy/iSpindel docs sometimes use "tempf"
    temp = validated.temperature
    if temp is None and validated.tempf is not None:
        tf = validated.tempf
        if tf is not None:
            temp = (tf - 32.0) * 5.0 / 9.0

    temp_units = str(payload.get("temp_units", "C")).upper() if payload.get("temp_units") else "C"
    if temp_units == "F" and temp is not None:
        temp = (temp - 32.0) * 5.0 / 9.0

    # Phase 3 battery semantics
    battery_value, battery_unit, battery_source = _parse_battery(payload)

    rssi = validated.rssi
    ssid = validated.ssid
    expected_interval = validated.interval or 300

    # Phase 3 timestamp semantics
    now = datetime.now(timezone.utc)
    measured_at_dt, measured_at_iso = _extract_measured_at(payload, now=now)

    # Stable event id (from documented aliases).
    sample_event_id = _extract_sample_id(payload)

    received_at_iso = _now_iso()

    # Phase 3 payload hash + retry key — only meaningful if we will need to
    # compare against historical rows.
    payload_hash = _canonical_payload_hash(
        device_id=device_id,
        normalized_measured_at=measured_at_iso,
        angle=angle,
        gravity=gravity,
        temp_c=temp,
        battery_value=battery_value,
        battery_unit=battery_unit,
        ssid=ssid,
        rssi=rssi,
    )
    rk = _retry_key(
        device_id=device_id,
        sample_event_id=sample_event_id,
        normalized_measured_at=measured_at_iso,
        payload_hash=payload_hash,
    )

    # Compatibility alias: `battery` legacy column stores the numeric value
    # (or NULL when none was supplied).
    legacy_battery = battery_value

    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            # Upsert the device row, preserving any existing user overrides.
            conn.execute(
                """
                INSERT INTO devices(device_id, device_name, expected_interval_sec, created_at, last_seen,
                                    reported_device_name, reported_interval_sec)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(device_id) DO UPDATE SET
                  reported_device_name = excluded.reported_device_name,
                  reported_interval_sec = excluded.reported_interval_sec,
                  last_seen = excluded.last_seen,
                  -- keep legacy columns coherent mirrors of the reported fields
                  device_name = CASE
                      WHEN devices.user_device_name IS NULL THEN excluded.reported_device_name
                      ELSE devices.device_name
                  END,
                  expected_interval_sec = CASE
                      WHEN devices.user_interval_sec IS NULL THEN excluded.reported_interval_sec
                      ELSE devices.expected_interval_sec
                  END
                """,
                (
                    device_id,
                    reported_name,
                    expected_interval,
                    received_at_iso,
                    received_at_iso,
                    reported_name,
                    expected_interval,
                ),
            )

            # ------------------------------------------------------------------
            # Retry / dedup decision tree
            # ------------------------------------------------------------------
            dedup_status = None  # 'duplicate' | 'conflict' | None
            existing_sample_id: Optional[int] = None

            if rk is not None:
                horizon_cutoff = (
                    now - timedelta(seconds=RETRY_HORIZON_SECONDS)
                ).isoformat()
                row = conn.execute(
                    """
                    SELECT id, payload_hash FROM samples
                     WHERE retry_key = ?
                       AND received_at >= ?
                     ORDER BY id DESC
                     LIMIT 1
                    """,
                    (rk, horizon_cutoff),
                ).fetchone()
                if row is not None:
                    if row["payload_hash"] == payload_hash:
                        dedup_status = "duplicate"
                        existing_sample_id = row["id"]
                    else:
                        # Stable id / measured-time + changed telemetry →
                        # 409 conflict. Do NOT overwrite, do NOT insert.
                        conn.execute("ROLLBACK")
                        raise _Phase3Conflict(rk)

            if dedup_status is None:
                cur = conn.execute(
                    """
                    INSERT INTO samples(
                        device_id, ts, angle, gravity, temp_c, battery, rssi, ssid, raw_json,
                        received_at, measured_at, battery_value, battery_unit, battery_source,
                        sample_event_id, retry_key, payload_hash
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        device_id,
                        received_at_iso,
                        angle,
                        gravity,
                        temp,
                        legacy_battery,
                        rssi,
                        ssid,
                        json.dumps(payload),
                        received_at_iso,
                        measured_at_iso,
                        battery_value,
                        battery_unit,
                        battery_source,
                        sample_event_id,
                        rk,
                        payload_hash,
                    ),
                )
                existing_sample_id = cur.lastrowid

            conn.execute("COMMIT")
        except _Phase3Conflict:
            # Already rolled back inside the branch; re-raise as HTTP 409.
            raise HTTPException(
                status_code=409,
                detail="conflicting payload for stable event id / measured time within retry horizon",
            )
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.OperationalError:
                pass
            raise

    body = {
        "status": "ok",
        "device_id": device_id,
        "sample_id": existing_sample_id,
        "duplicate": dedup_status == "duplicate",
    }
    return body


@app.patch("/api/device/{device_id}")
def update_device(device_id: str, payload: DeviceUpdate) -> Dict[str, Any]:
    """PATCH semantics: update USER override fields only.

    - Omitted field: leave its user override unchanged
    - `device_name: null` or empty string: clear `user_device_name`
    - `expected_interval_sec: null`: clear `user_interval_sec`
    - Missing device: 404
    The legacy mirror columns (`device_name`/`expected_interval_sec`) stay
    coherent with the effective value so legacy clients keep working.
    """
    fields = payload.model_fields_set

    with db() as conn:
        existing = conn.execute(
            "SELECT device_name, expected_interval_sec, reported_device_name, "
            "reported_interval_sec, user_device_name, user_interval_sec "
            "FROM devices WHERE device_id=?",
            (device_id,),
        ).fetchone()
        if existing is None:
            raise HTTPException(status_code=404, detail="device not found")

        # Compute the new user-overrides and resulting effective value.
        if "device_name" in fields:
            new_user_name = payload.device_name
            new_user_name_cleared = (new_user_name is None) or (new_user_name == "")
            user_name_value = None if new_user_name_cleared else new_user_name
        else:
            user_name_value = existing["user_device_name"]

        if "expected_interval_sec" in fields:
            new_interval = payload.expected_interval_sec
            user_interval_value: Optional[int] = (
                None if new_interval is None else int(new_interval)
            )
        else:
            user_interval_value = existing["user_interval_sec"]

        effective_name = (
            user_name_value
            if user_name_value is not None
            else (existing["reported_device_name"] or existing["device_name"])
        )
        effective_interval = (
            user_interval_value
            if user_interval_value is not None
            else (existing["reported_interval_sec"] or existing["expected_interval_sec"])
        )

        conn.execute(
            """
            UPDATE devices
               SET user_device_name      = ?,
                   user_interval_sec     = ?,
                   device_name           = ?,
                   expected_interval_sec = ?
             WHERE device_id = ?
            """,
            (
                user_name_value,
                user_interval_value,
                effective_name,
                effective_interval,
                device_id,
            ),
        )
        conn.commit()

    return {"status": "ok", "device_id": device_id}


def latest_device_battery_evidence(*, now: datetime) -> Dict[str, Any]:
    """Return the newest device-reported battery value when host evidence is absent.

    The firmware's plain ``battery`` field does not declare a unit or charging
    state, so this fallback reports the value and freshness without inferring
    volts, percent, or whether USB power is charging the cell.
    """
    with db() as conn:
        row = conn.execute(
            """
            SELECT s.device_id,
                   COALESCE(d.user_device_name, d.reported_device_name,
                            d.device_name, s.device_id) AS device_name,
                   s.battery_value,
                   COALESCE(NULLIF(s.battery_unit, ''), 'unknown') AS battery_unit,
                   COALESCE(NULLIF(s.battery_source, ''), 'unknown') AS battery_source,
                   COALESCE(s.measured_at, s.received_at) AS observed_at,
                   COALESCE(d.user_interval_sec, d.reported_interval_sec,
                            d.expected_interval_sec) AS expected_interval_sec
              FROM samples s
              JOIN devices d ON d.device_id = s.device_id
             WHERE s.battery_value IS NOT NULL
             ORDER BY COALESCE(s.measured_at, s.received_at) DESC, s.id DESC
             LIMIT 1
            """
        ).fetchone()

    if row is None:
        return _evidence(
            "missing", BATTERY_TTL_SECONDS, now=now,
            detail="no host battery evidence or device battery telemetry is available",
        )

    observed = _parse_evidence_timestamp(row["observed_at"])
    interval = max(1, int(row["expected_interval_sec"] or BATTERY_TTL_SECONDS))
    ttl_seconds = max(BATTERY_TTL_SECONDS, interval * 3)
    if observed is None:
        return _evidence(
            "parse_error", ttl_seconds, now=now,
            detail="latest device battery timestamp is invalid",
        )

    value = float(row["battery_value"])
    unit = row["battery_unit"] if row["battery_unit"] in {"V", "%"} else "unknown"
    unit_text = unit if unit != "unknown" else "(firmware unit not declared)"
    status = "stale" if (now - observed).total_seconds() > ttl_seconds else "ok"
    freshness = "stale" if status == "stale" else "current"
    detail = (
        f"{row['device_name']} reported {value:.2f} {unit_text}; "
        f"sample is {freshness}; charging state unavailable"
    )
    result = _evidence(
        status, ttl_seconds, observed_at=observed, now=now, detail=detail,
        raw={"source": row["battery_source"]},
    )
    result.update({
        "scope": "device_telemetry",
        "device_id": row["device_id"],
        "device_name": row["device_name"],
        "value": value,
        "unit": unit,
        "source": row["battery_source"],
        "charging": "unknown",
    })
    return result


@app.get("/api/system-health")
def system_health() -> Dict[str, Any]:
    now = datetime.now(timezone.utc)
    battery = parse_battery_evidence(BATTERY_PATH, now=now, ttl_seconds=BATTERY_TTL_SECONDS)
    if battery["status"] == "missing":
        battery = latest_device_battery_evidence(now=now)
    heartbeat = parse_heartbeat_evidence(HEARTBEAT_PATH, now=now, ttl_seconds=HEARTBEAT_TTL_SECONDS)
    severity = {"ok": 0, "warning": 1, "critical": 2, "stale": 3, "missing": 4, "parse_error": 5}
    overall = max((battery["status"], heartbeat["status"]), key=lambda state: severity[state])
    return {"status": overall, "battery": battery, "heartbeat": heartbeat,
            # Phase 1 response aliases retained for clients that consume them.
            "battery_state": battery.get("raw", {}), "batteryInfo": battery.get("raw", {}),
            "heartbeat_latest": heartbeat.get("content", "")}


def _parse_backup_health_timestamp(value: Any) -> datetime | None:
    """Parse a backup ``verified_at`` value as ISO-8601 UTC with a trailing Z.

    Returns ``None`` if the value is missing. Raises ``ValueError`` for
    malformed, naive, or offset-tagged inputs. This is intentionally stricter
    than the battery/heartbeat parsers: only the literal ``...Z`` suffix is
    accepted, so a producer that omits the trailing 'Z' is treated as
    malformed and the receipt as non-green.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    candidate = value.strip()
    if not candidate.endswith("Z"):
        raise ValueError("verified_at must be ISO-8601 UTC with a trailing 'Z'")
    try:
        parsed = datetime.fromisoformat(candidate.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"verified_at is malformed: {exc}") from exc
    if parsed.tzinfo is None:
        raise ValueError("verified_at must include a UTC offset")
    return parsed.astimezone(timezone.utc)


def parse_backup_health_receipt(
    path: Path,
    *,
    now: datetime | None = None,
    ttl_seconds: int = BACKUP_HEALTH_TTL_SECONDS,
    schema: str = BACKUP_HEALTH_SCHEMA,
) -> Dict[str, Any]:
    """Validate a backup-health receipt and return a sanitized envelope.

    The returned dict NEVER contains ``path``, ``offhost_path``, or
    ``remote_retention`` — those are sensitive. Status values:

      - ``ok``                     : schema valid, ``verified_at`` is in UTC-Z,
                                    the file mtime is inside the TTL, and the
                                    receipt is not in the future.
      - ``missing``                : the receipt file does not exist.
      - ``parse_error``            : JSON decode / schema / field shape
                                    validation failed.
      - ``freshness_violation``    : ``verified_at`` is malformed, naive, or in
                                    the future.
      - ``stale``                  : ``verified_at`` is older than the TTL OR
                                    the filesystem mtime is older than the TTL.

    The independent filesystem mtime check defends against a forged or
    hand-edited ``verified_at``: even if the receipt claims a fresh value, a
    stale mtime forces the envelope into ``stale``/``freshness_violation``.
    """
    now = now or datetime.now(timezone.utc)
    if not path.exists():
        return {
            "status": "missing", "schema": schema, "ttl_seconds": ttl_seconds,
            "detail": "backup health receipt is absent",
            "run_id": None, "mode": None, "verified_at": None,
            "file_mtime": None, "file_age_seconds": None,
        }
    try:
        stat = path.stat()
    except OSError:
        return {
            "status": "parse_error", "schema": schema, "ttl_seconds": ttl_seconds,
            "detail": "backup health stat failed (configurable path is not exposed)",
            "run_id": None, "mode": None, "verified_at": None,
            "file_mtime": None, "file_age_seconds": None,
        }
    file_age = max(0.0, (now.timestamp() - stat.st_mtime))
    file_mtime_iso = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat().replace("+00:00", "Z")
    try:
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {
            "status": "parse_error", "schema": schema, "ttl_seconds": ttl_seconds,
            "detail": "invalid backup health receipt (configurable path is not exposed)",
            "run_id": None, "mode": None, "verified_at": None,
            "file_mtime": file_mtime_iso, "file_age_seconds": file_age,
        }
    if not isinstance(raw, dict):
        return {
            "status": "parse_error", "schema": schema, "ttl_seconds": ttl_seconds,
            "detail": "backup health receipt must be an object",
            "run_id": None, "mode": None, "verified_at": None,
            "file_mtime": file_mtime_iso, "file_age_seconds": file_age,
        }
    if raw.get("schema") != schema:
        return {
            "status": "parse_error", "schema": schema, "ttl_seconds": ttl_seconds,
            "detail": f"backup health schema must be {schema!r}",
            "run_id": None, "mode": None, "verified_at": None,
            "file_mtime": file_mtime_iso, "file_age_seconds": file_age,
        }
    run_id = raw.get("run_id")
    mode = raw.get("mode")
    if not isinstance(run_id, str) or not run_id:
        return {
            "status": "parse_error", "schema": schema, "ttl_seconds": ttl_seconds,
            "detail": "backup health run_id is missing or invalid",
            "run_id": None, "mode": mode, "verified_at": None,
            "file_mtime": file_mtime_iso, "file_age_seconds": file_age,
        }
    if mode not in {"single", "dual"}:
        return {
            "status": "parse_error", "schema": schema, "ttl_seconds": ttl_seconds,
            "detail": "backup health mode must be 'single' or 'dual'",
            "run_id": run_id, "mode": None, "verified_at": None,
            "file_mtime": file_mtime_iso, "file_age_seconds": file_age,
        }
    try:
        verified = _parse_backup_health_timestamp(raw.get("verified_at"))
    except ValueError as exc:
        return {
            "status": "parse_error", "schema": schema, "ttl_seconds": ttl_seconds,
            "detail": f"verified_at is malformed: {exc}",
            "run_id": run_id, "mode": mode, "verified_at": None,
            "file_mtime": file_mtime_iso, "file_age_seconds": file_age,
        }
    if verified is None:
        return {
            "status": "parse_error", "schema": schema, "ttl_seconds": ttl_seconds,
            "detail": "verified_at is missing",
            "run_id": run_id, "mode": mode, "verified_at": None,
            "file_mtime": file_mtime_iso, "file_age_seconds": file_age,
        }
    if verified > now:
        return {
            "status": "freshness_violation", "schema": schema, "ttl_seconds": ttl_seconds,
            "detail": "verified_at is in the future",
            "run_id": run_id, "mode": mode, "verified_at": verified.isoformat().replace("+00:00", "Z"),
            "file_mtime": file_mtime_iso, "file_age_seconds": file_age,
        }
    verified_age = (now - verified).total_seconds()
    if verified_age > ttl_seconds or file_age > ttl_seconds:
        return {
            "status": "stale", "schema": schema, "ttl_seconds": ttl_seconds,
            "detail": "backup health exceeded TTL",
            "run_id": run_id, "mode": mode, "verified_at": verified.isoformat().replace("+00:00", "Z"),
            "file_mtime": file_mtime_iso, "file_age_seconds": file_age,
        }
    return {
        "status": "ok", "schema": schema, "ttl_seconds": ttl_seconds,
        "detail": "backup health is current",
        "run_id": run_id, "mode": mode,
        "verified_at": verified.isoformat().replace("+00:00", "Z"),
        "file_mtime": file_mtime_iso, "file_age_seconds": file_age,
    }


@app.get("/api/backup-health")
def backup_health() -> Any:
    """Read-only backup-health evidence endpoint.

    Returns HTTP 200 with status=ok only when both the receipt and its
    filesystem mtime are fresh. Every other outcome returns HTTP 503 with a
    sanitized body that never exposes the configured path, the off-host path,
    the retention payload, or the database generations directory.

    This endpoint does NOT claim that a live backup has been performed. It
    only reports the freshness of the most recent successful receipt the
    backup producer wrote under its ``${ISPINDEL_BACKUP_ROOT}``.
    """
    now = datetime.now(timezone.utc)
    envelope = parse_backup_health_receipt(
        BACKUP_HEALTH_PATH, now=now, ttl_seconds=BACKUP_HEALTH_TTL_SECONDS,
    )
    status = envelope["status"]
    if status == "ok":
        return envelope
    return JSONResponse(
        status_code=503,
        content={
            "status": status,
            "service": "ispindel-dashboard",
            "detail": envelope.get("detail"),
            "run_id": envelope.get("run_id"),
            "mode": envelope.get("mode"),
            "verified_at": envelope.get("verified_at"),
            "file_mtime": envelope.get("file_mtime"),
            "file_age_seconds": envelope.get("file_age_seconds"),
            "ttl_seconds": envelope.get("ttl_seconds"),
        },
    )


def _parse_evidence_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)
    except ValueError:
        return None


def _evidence(status: str, ttl_seconds: int, *, observed_at: datetime | None = None,
              now: datetime, detail: str, raw: Any = None, content: str = "") -> Dict[str, Any]:
    age = None if observed_at is None else max(0, (now - observed_at).total_seconds())
    return {"status": status, "age_seconds": age, "ttl_seconds": ttl_seconds,
            "observed_at": observed_at.isoformat() if observed_at else None,
            "detail": detail, "raw": raw if raw is not None else {}, "content": content}


def parse_battery_evidence(path: Path, *, now: datetime | None = None,
                           ttl_seconds: int = 600) -> Dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    if not path.exists():
        return _evidence("missing", ttl_seconds, now=now, detail="battery evidence file is absent")
    try:
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        return _evidence("parse_error", ttl_seconds, now=now, detail=f"invalid battery evidence: {exc}")
    if not isinstance(raw, dict):
        return _evidence("parse_error", ttl_seconds, now=now, detail="battery evidence must be an object", raw=raw)
    if raw.get("schema_version") != "health-evidence-v1" or raw.get("kind") != "battery":
        return _evidence("parse_error", ttl_seconds, now=now,
                         detail="battery evidence schema_version/kind is invalid", raw=raw)
    if raw.get("state") not in {"ok", "warning", "critical"} or not isinstance(raw.get("detail"), str):
        return _evidence("parse_error", ttl_seconds, now=now,
                         detail="battery evidence state/detail is invalid", raw=raw)
    observed_value = raw.get("observed_at")
    observed = _parse_evidence_timestamp(observed_value)
    if not isinstance(observed_value, str) or not observed_value.endswith("Z"):
        observed = None
    percent = raw.get("percent")
    try:
        percent = float(percent)
    except (TypeError, ValueError):
        return _evidence("parse_error", ttl_seconds, now=now, detail="battery percent is missing or invalid", raw=raw)
    if not 0 <= percent <= 100:
        return _evidence("parse_error", ttl_seconds, now=now, detail="battery percent is outside 0..100", raw=raw)
    if observed is None:
        return _evidence("parse_error", ttl_seconds, now=now, detail="battery timestamp is missing or invalid", raw=raw)
    if (now - observed).total_seconds() > ttl_seconds:
        return _evidence("stale", ttl_seconds, observed_at=observed, now=now, detail="battery evidence exceeded TTL", raw=raw)
    if percent <= 10:
        state, detail = "critical", "battery level is critical"
    elif percent <= 20:
        state, detail = "warning", "battery level is low"
    else:
        state, detail = "ok", "battery evidence is current"
    result = _evidence(state, ttl_seconds, observed_at=observed, now=now, detail=detail, raw=raw)
    result["percent"] = percent
    return result


def parse_heartbeat_evidence(path: Path, *, now: datetime | None = None,
                             ttl_seconds: int = 5400) -> Dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    if not path.exists():
        return _evidence("missing", ttl_seconds, now=now, detail="heartbeat evidence file is absent")
    try:
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        return _evidence("parse_error", ttl_seconds, now=now, detail=f"invalid heartbeat evidence: {exc}")
    if not isinstance(raw, dict):
        return _evidence("parse_error", ttl_seconds, now=now,
                         detail="heartbeat evidence must be an object", raw=raw)
    if raw.get("schema_version") != "health-evidence-v1" or raw.get("kind") != "heartbeat":
        return _evidence("parse_error", ttl_seconds, now=now,
                         detail="heartbeat evidence schema_version/kind is invalid", raw=raw)
    observed_value = raw.get("observed_at")
    observed = _parse_evidence_timestamp(observed_value)
    if not isinstance(observed_value, str) or not observed_value.endswith("Z"):
        observed = None
    state = raw.get("state")
    detail = raw.get("detail")
    flags = (raw.get("poll_failed"), raw.get("alert_attempted"), raw.get("alert_delivered"))
    if observed is None or state not in {"ok", "warning", "critical"} or not isinstance(detail, str):
        return _evidence("parse_error", ttl_seconds, now=now,
                         detail="heartbeat timestamp/state/detail is invalid", raw=raw)
    if not all(isinstance(value, bool) for value in flags):
        return _evidence("parse_error", ttl_seconds, now=now,
                         detail="heartbeat boolean fields are invalid", raw=raw)
    if (now - observed).total_seconds() > ttl_seconds:
        return _evidence("stale", ttl_seconds, observed_at=observed, now=now,
                         detail="heartbeat evidence exceeded TTL", raw=raw)
    if raw["poll_failed"] or (raw["alert_attempted"] and not raw["alert_delivered"]):
        state = "critical"
    return _evidence(state, ttl_seconds, observed_at=observed, now=now, detail=detail, raw=raw)


@app.get("/api/status")
def status() -> Dict[str, Any]:
    with db() as conn:
        devices = list(
            conn.execute(
                """
                SELECT device_id,
                       COALESCE(user_device_name, reported_device_name, device_name) AS device_name,
                       COALESCE(user_interval_sec, reported_interval_sec, expected_interval_sec) AS expected_interval_sec,
                       COALESCE(last_seen, '') AS last_seen
                  FROM devices
                 ORDER BY COALESCE(user_device_name, reported_device_name, device_name)
                """
            )
        )
        totals = conn.execute(
            "SELECT COUNT(*) AS c, MAX(COALESCE(measured_at, received_at)) AS latest FROM samples"
        ).fetchone()
        total_samples = totals["c"] if totals else 0

        intents = BREWING_STORE.get_operating_intents(
            [str(device["device_id"]) for device in devices]
        )
        total = 0
        stored_devices = 0
        preparing_devices = 0
        brewing_devices = 0
        stale = 0
        active_brew_count = BREWING_STORE.count_active_brews()
        now = datetime.now(timezone.utc)
        for d in devices:
            total += 1
            mode = intents[str(d["device_id"])]["mode"]
            if mode == "stored":
                stored_devices += 1
                continue
            if mode == "preparing":
                preparing_devices += 1
            else:
                brewing_devices += 1
            if not d["last_seen"]:
                stale += 1
                continue
            try:
                last = datetime.fromisoformat(d["last_seen"])
                age = (now - last).total_seconds()
                if age > (float(d["expected_interval_sec"]) * 3):
                    stale += 1
            except Exception:
                stale += 1

    return {
        "devices": total,
        "stale_devices": stale,
        "stored_devices": stored_devices,
        "preparing_devices": preparing_devices,
        "brewing_devices": brewing_devices,
        "not_required_no_active_brew": active_brew_count == 0,
        "total_samples": total_samples,
    }


@app.get("/static/chart.umd.js", response_class=FileResponse)
def chart_js_asset() -> FileResponse:
    return FileResponse(
        STATIC_DIR / "chart.umd.js",
        media_type="application/javascript",
        headers={"Cache-Control": IMMUTABLE_ASSET_CACHE},
    )


@app.get(
    "/static/chartjs-adapter-date-fns.bundle.min.js",
    response_class=FileResponse,
)
def chart_date_adapter_asset() -> FileResponse:
    return FileResponse(
        STATIC_DIR / "chartjs-adapter-date-fns.bundle.min.js",
        media_type="application/javascript",
        headers={"Cache-Control": IMMUTABLE_ASSET_CACHE},
    )


@app.get("/static/dashboard.css", response_class=FileResponse)
def dashboard_css_asset() -> FileResponse:
    return FileResponse(
        STATIC_DIR / "dashboard.css",
        media_type="text/css",
        headers={"Cache-Control": FIRST_PARTY_ASSET_CACHE},
    )


@app.get("/static/dashboard.js", response_class=FileResponse)
def dashboard_js_asset() -> FileResponse:
    return FileResponse(
        STATIC_DIR / "dashboard.js",
        media_type="application/javascript",
        headers={"Cache-Control": FIRST_PARTY_ASSET_CACHE},
    )


@app.get("/static/brewing.js", response_class=FileResponse)
def brewing_js_asset() -> FileResponse:
    return FileResponse(
        STATIC_DIR / "brewing.js",
        media_type="application/javascript",
        headers={"Cache-Control": FIRST_PARTY_ASSET_CACHE},
    )


@app.get("/static/service-worker.js", response_class=FileResponse)
def service_worker_asset() -> FileResponse:
    return FileResponse(
        STATIC_DIR / "service-worker.js",
        media_type="application/javascript",
        headers={
            "Cache-Control": FIRST_PARTY_ASSET_CACHE,
            "Service-Worker-Allowed": "/",
        },
    )


@app.get("/static/brew-icon.svg", response_class=FileResponse)
def brew_icon_asset() -> FileResponse:
    return FileResponse(
        STATIC_DIR / "brew-icon.svg",
        media_type="image/svg+xml",
        headers={"Cache-Control": FIRST_PARTY_ASSET_CACHE},
    )


@app.get("/manifest.webmanifest", response_class=FileResponse)
def web_manifest_asset() -> FileResponse:
    return FileResponse(
        STATIC_DIR / "manifest.webmanifest",
        media_type="application/manifest+json",
        headers={"Cache-Control": FIRST_PARTY_ASSET_CACHE},
    )


@app.get("/", response_class=HTMLResponse)
def dashboard() -> str:
    return '''
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Brewing Central</title>
  <meta name="theme-color" content="#1f6a3c" />
  <meta name="application-name" content="Brewing Central" />
  <meta name="mobile-web-app-capable" content="yes" />
  <link rel="manifest" href="/manifest.webmanifest" />
  <link rel="apple-touch-icon" href="/static/brew-icon.svg" />
  <link rel="icon" href="data:," />
  <script src="/static/chart.umd.js"></script>
  <script src="/static/chartjs-adapter-date-fns.bundle.min.js"></script>
  <link rel="stylesheet" href="/static/dashboard.css" />
</head>
<body>
  <a class="visually-hidden" href="#main-content">Skip to brewing dashboard</a>
  <main class="container" id="main-content" tabindex="-1">
      <header class="app-header">
        <div>
          <p class="eyebrow">Tailnet brewing workspace</p>
          <h1>Brewing Central</h1>
          <p>Telemetry, recipes, brew records and guided observations in one local system.</p>
        </div>
        <span class="local-badge">Local · private</span>
      </header>

      <nav class="app-tabs" role="tablist" aria-label="Brewing Central sections">
        <button class="app-tab" id="tab-button-dashboard" role="tab" aria-controls="tab-dashboard" aria-selected="true" data-tab="dashboard">Dashboard</button>
        <button class="app-tab" id="tab-button-recipes" role="tab" aria-controls="tab-recipes" aria-selected="false" data-tab="recipes" tabindex="-1">Recipe Book</button>
        <button class="app-tab" id="tab-button-brew" role="tab" aria-controls="tab-brew" aria-selected="false" data-tab="brew" tabindex="-1">Brew Control &amp; Archive</button>
      </nav>

      <section class="tab-panel" id="tab-dashboard" role="tabpanel" aria-labelledby="tab-button-dashboard">
      <div class="grid">
        <!-- System Health -->
        <div class="card">
            <h3>System Health</h3>
            <div id="health-status" class="health-data">Loading...</div>
        </div>
        <!-- Summary -->
        <div class="card">
            <h3>Overview</h3>
            <div id="summary" class="health-data">Loading...</div>
        </div>
      </div>

      <details class="devices-collapse" id="devices-collapse">
        <summary>
          <span class="chev" aria-hidden="true">▶</span>
          <span>Devices</span>
          <span class="count" id="devices-count">(0)</span>
        </summary>
        <div class="collapse-body">
          <div class="table-scroll" tabindex="0" aria-label="Scrollable devices table"><table id="devices-table">
            <thead>
              <tr>
                <th>Device ID</th>
                <th>Name</th>
                <th>Intent</th>
                <th>Camera</th>
                <th>Interval (s)</th>
                <th>Last Seen</th>
                <th>Samples</th>
                <th>Actions</th>
              </tr>
            </thead>
            <tbody id="devices-body"></tbody>
          </table></div>
        </div>
      </details>

      <div class="card">
        <div class="chart-header">
            <h3>Telemetry</h3>
            <div class="chart-tick" aria-label="Chart refresh state">
                <span class="tick-dot" id="tick-dot" aria-hidden="true"></span>
                <span><span id="tick-last">Last refresh —</span> · <span id="tick-next">next in <strong>5s</strong></span></span>
            </div>
        </div>
        <div class="controls">
            <label for="device-select">Device</label><select id="device-select"><option value="">Select Device...</option></select>
            <label for="time-window">Window</label><select id="time-window">
                <option value="0.1666666667">Last 10 Minutes</option>
                <option value="0.5">Last 30 Minutes</option>
                <option value="1">Last 1 Hour</option>
                <option value="6">Last 6 Hours</option>
                <option value="12">Last 12 Hours</option>
                <option value="24" selected>Last 24 Hours</option>
                <option value="48">Last 48 Hours</option>
                <option value="168">Last 1 Week</option>
            </select>
            <label for="view-select">Metric view</label><select id="view-select">
                <option value="fermentation">🍺 Fermentation</option>
                <option value="gravity-only">📏 Gravity Only</option>
                <option value="gravity-temp">🌡 Gravity + Temp</option>
                <option value="battery-telemetry">🔋 Battery / Wi-Fi</option>
                <option value="all-sensors">🛰 All Sensors</option>
            </select>
            <label for="gravity-mode">Gravity display</label><select id="gravity-mode">
                <option value="raw" selected>Raw Gravity</option>
                <option value="calibrated">Calibrated Gravity</option>
            </select>
            <button id="refresh-button">Refresh</button>
            <button class="secondary" id="calibration-button">Calibration</button>
        </div>

        <div class="metric-pills" aria-label="Latest telemetry values">
            <div class="value-pill accent-gravity" data-metric="gravity"><div class="value-pill-label">Gravity</div><div class="value-pill-value"><span class="num">—</span><span class="unit">SG</span></div><div class="value-pill-meta"><span class="delta">—</span><span class="age">—</span></div></div>
            <div class="value-pill accent-temperature" data-metric="temp_c"><div class="value-pill-label">Temperature</div><div class="value-pill-value"><span class="num">—</span><span class="unit">°C</span></div><div class="value-pill-meta"><span class="delta">—</span><span class="age">—</span></div></div>
            <div class="value-pill accent-battery" data-metric="battery"><div class="value-pill-label">Battery</div><div class="value-pill-value"><span class="num">—</span><span class="unit">?</span></div><div class="value-pill-meta"><span class="delta">—</span><span class="age">—</span></div></div>
            <div class="value-pill accent-angle" data-metric="angle"><div class="value-pill-label">Tilt</div><div class="value-pill-value"><span class="num">—</span><span class="unit">°</span></div><div class="value-pill-meta"><span class="delta">—</span><span class="age">—</span></div></div>
            <div class="value-pill accent-rssi" data-metric="rssi"><div class="value-pill-label">RSSI</div><div class="value-pill-value"><span class="num">—</span><span class="unit">dBm</span></div><div class="value-pill-meta"><span class="delta">—</span><span class="age">—</span></div></div>
        </div>

        <div id="metricToggles" class="metric-toggles" role="group" aria-label="Toggle metrics on chart"></div>

        <div class="chart-toolbar" role="toolbar" aria-label="Chart display options">
            <label><input type="checkbox" id="opt-paused"> Pause polling</label>
            <label><input type="checkbox" id="opt-markers"> Show point markers</label>
            <label><input type="checkbox" id="opt-grid" checked> Gridlines</label>
            <label><input type="checkbox" id="opt-smooth" checked> Smooth lines</label>
            <span class="chart-toolbar-spacer"></span>
            <button class="secondary" id="export-chart-button">Export PNG</button>
        </div>

        <div class="chart-container" id="chartContainer">
            <canvas id="chartCanvas" aria-label="Telemetry time-series chart" aria-describedby="chart-summary"></canvas>
            <p id="chart-summary">Telemetry chart data updates visually; the table below provides the latest values.</p>
            <div class="table-scroll" tabindex="0" aria-label="Scrollable telemetry data table"><table id="chart-data-table"><caption>Latest telemetry data</caption><thead><tr><th>Metric</th><th>Value</th><th>Observed</th></tr></thead><tbody></tbody></table></div>
            <div class="empty-state-overlay" id="chartOverlay">Waiting for data…</div>
            <div class="chart-resize-handle" id="chartResizeHandle"
                 role="separator" aria-orientation="horizontal"
                 aria-label="Resize chart height. Click and drag, or use arrow keys."
                 aria-valuemin="250" aria-valuemax="1200" aria-valuenow="600"
                 tabindex="0">
                <span class="hint">⇕ drag · ↑↓ keys</span>
            </div>
            <div class="chart-error-region" id="chartError" role="status" aria-live="polite"></div>
        </div>
      </div>
      </section>

      <section class="tab-panel" id="tab-recipes" role="tabpanel" aria-labelledby="tab-button-recipes" hidden>
        <div class="workspace-grid recipe-workspace">
          <aside class="workspace-panel library-panel" aria-labelledby="recipe-library-title">
            <div class="panel-heading">
              <div><p class="eyebrow">Saved locally</p><h2 id="recipe-library-title">Recipe library</h2></div>
              <button type="button" id="new-recipe">New</button>
            </div>
            <div id="recipe-list" class="recipe-list"></div>
          </aside>

          <section class="workspace-panel editor-panel" aria-labelledby="recipe-editor-title">
            <div class="panel-heading"><div><p class="eyebrow">Editable master</p><h2 id="recipe-editor-title">New recipe</h2></div></div>
            <form id="recipe-form">
              <input type="hidden" id="recipe-id" />
              <input type="hidden" id="recipe-revision" />
              <div class="form-grid two-columns">
                <label><span>Name</span><input id="recipe-name" type="text" maxlength="160" required /></label>
                <label><span>Style / category</span><input id="recipe-style" type="text" maxlength="120" /></label>
                <label><span>Base volume (L)</span><input id="recipe-base-volume" type="number" min="1" max="200" step="0.1" required /></label>
                <label><span>Beverage type</span><select id="recipe-beverage-type"><option value="">Not specified</option><option value="beer">Beer</option><option value="wine">Wine</option><option value="mead">Mead</option><option value="cider">Cider</option><option value="kombucha">Kombucha</option><option value="other">Other</option></select></label>
                <label><span>Initial fermenter volume (L)</span><input id="recipe-initial-volume" type="number" min="1" max="200" step="0.1" /></label>
                <label><span>Target ABV (%)</span><input id="recipe-target-abv" type="number" min="0" max="100" step="0.1" /></label>
                <label><span>Target pH</span><input id="recipe-target-ph" type="number" min="0" max="14" step="0.01" /></label>
                <label><span>Target sweetness</span><select id="recipe-target-sweetness"><option value="unknown">Unknown</option><option value="dry">Dry</option><option value="off_dry">Off-dry</option><option value="medium">Medium</option><option value="sweet">Sweet</option><option value="very_sweet">Very sweet</option></select></label>
              </div>
              <label><span>Description</span><textarea id="recipe-description" rows="3" maxlength="4000"></textarea></label>
              <div class="section-heading"><h3>Ingredients and scale rules</h3><button type="button" class="secondary" id="add-ingredient">Add ingredient</button></div>
              <p class="field-help">Linear suits direct ratios. Fixed keeps an amount unchanged. Power changes the ratio gradually. Piecewise accepts points such as <code>1:10,30:280,200:1600</code>.</p>
              <div id="recipe-ingredients" class="ingredient-list"></div>
              <section class="structured-editor" aria-labelledby="culture-editor-title">
                <div class="section-heading"><div><h3 id="culture-editor-title">Culture profiles</h3><p class="field-help">Record identity as supplied; mixed or unknown cultures are never rewritten as a single strain.</p></div><button type="button" class="secondary" id="add-culture-profile">Add culture</button></div>
                <div id="recipe-culture-profiles" class="structured-list"></div>
              </section>
              <section class="structured-editor" aria-labelledby="addition-editor-title">
                <div class="section-heading"><div><h3 id="addition-editor-title">Scheduled additions</h3><p class="field-help">Schedules are advisory. Recording or skipping is always an explicit brew event.</p></div><button type="button" class="secondary" id="add-scheduled-addition">Add addition</button></div>
                <div id="recipe-scheduled-additions" class="structured-list"></div>
              </section>
              <section class="structured-editor" aria-labelledby="process-editor-title">
                <div class="section-heading"><div><h3 id="process-editor-title">Process steps</h3><p class="field-help">Link steps to stable culture or addition keys where useful.</p></div><button type="button" class="secondary" id="add-process-step">Add step</button></div>
                <div id="recipe-process-steps" class="structured-list"></div>
              </section>
              <label><span>Process and notes</span><textarea id="recipe-notes" rows="6" maxlength="16000"></textarea></label>
              <div class="form-actions"><button type="submit" id="recipe-save">Save recipe</button><span id="recipe-status" class="status" role="status"></span></div>
            </form>

            <div class="scale-preview" aria-labelledby="scale-preview-title">
              <div class="section-heading"><div><p class="eyebrow">Live calculation</p><h3 id="scale-preview-title">Scale to <span id="recipe-target-label">30 L</span></h3></div><input id="recipe-target-volume-number" type="number" min="1" max="200" step="0.1" value="30" aria-label="Target volume in litres" /></div>
              <input id="recipe-target-volume" class="volume-slider" type="range" min="1" max="200" step="1" value="30" aria-label="Recipe target volume from 1 to 200 litres" />
              <div id="recipe-scale-output" class="scale-output"></div>
            </div>
          </section>

          <aside class="workspace-panel assistant-panel" data-assistant-kind="recipe" aria-labelledby="recipe-assistant-title">
            <div class="panel-heading"><div><p class="eyebrow">Phone ZeroClaw</p><h2 id="recipe-assistant-title">Recipe assistant</h2></div><span class="assistant-state">Local agent</span></div>
            <p class="field-help">Ask for research, scaling advice or a draft. Proposed recipe changes require review and an explicit save.</p>
            <div class="chat-log" data-assistant-log aria-label="Recipe assistant messages"></div>
            <label><span>Message</span><textarea data-assistant-input rows="4" placeholder="Ask about this recipe…"></textarea></label>
            <label class="assistant-research"><input type="checkbox" data-assistant-research /><span>Research with SearXNG and Kiwix</span></label>
            <button type="button" data-assistant-send>Send to ZeroClaw</button>
            <span class="status" data-assistant-status role="status">Waiting for phone agent.</span>
          </aside>
        </div>
      </section>

      <section class="tab-panel" id="tab-brew" role="tabpanel" aria-labelledby="tab-button-brew" hidden>
        <div class="workspace-grid brew-workspace">
          <aside class="workspace-panel device-panel" aria-labelledby="brew-device-title">
            <div class="panel-heading"><div><p class="eyebrow">Instrument</p><h2 id="brew-device-title">iSpindel</h2></div></div>
            <label><span>Device</span><select id="brew-device-select"><option value="">Select iSpindel…</option></select></label>
            <div id="brew-device-state" class="device-state"></div>
            <div class="control-block" id="operating-intent-control">
              <h3>Operating intent</h3>
              <p class="field-help">Stored suppresses expected telemetry-absence alerts. It does not change the iSpindel firmware sleep interval.</p>
              <label><span>Sensor mode</span><select id="operating-intent-mode">
                <option value="stored">Stored / intentionally off</option>
                <option value="preparing">Preparing to brew</option>
                <option value="brewing">Brewing</option>
              </select></label>
              <label><span>Camera policy</span><select id="operating-intent-camera">
                <option value="off">Off</option>
                <option value="active_brew_structural">Active brew · structural only</option>
                <option value="area_structural">Optional area · structural only</option>
              </select></label>
              <button type="button" class="secondary" id="save-operating-intent">Save operating intent</button>
              <span id="operating-intent-status" class="status" role="status"></span>
            </div>
          </aside>

          <section class="workspace-panel brew-control-panel" aria-labelledby="brew-control-title">
            <div class="panel-heading"><div><p class="eyebrow">Run lifecycle</p><h2 id="brew-control-title">Brew control</h2></div></div>
            <div class="control-block">
              <h3>1. Water reference</h3>
              <p class="field-help">Snapshots the latest persisted angle and temperature as a 1.000 SG reference. It does not manufacture or replace a full calibration curve.</p>
              <label><span>Reference label</span><input id="water-reference-label" type="text" value="Fresh water reference" maxlength="120" /></label>
              <button type="button" class="secondary" id="record-water-reference">Record water reference</button>
            </div>
            <div class="control-block">
              <h3>2. Begin brewing</h3>
              <div class="form-grid two-columns">
                <label><span>Recipe</span><select id="brew-recipe-select"><option value="">Select recipe…</option></select></label>
                <label><span>Volume (L)</span><input id="brew-volume" type="number" min="1" max="200" step="0.1" value="30" /></label>
              </div>
              <label><span>Start notes</span><textarea id="brew-notes" rows="3" maxlength="16000"></textarea></label>
              <button type="button" id="begin-brew">Begin brewing</button>
            </div>
            <div class="control-block danger-zone">
              <h3>3. Stop brewing</h3>
              <div class="form-grid two-columns">
                <label><span>Outcome</span><select id="brew-outcome"><option value="completed">Completed</option><option value="aborted">Aborted</option></select></label>
                <label><span>Final notes</span><input id="brew-stop-notes" type="text" maxlength="16000" /></label>
              </div>
              <button type="button" class="danger" id="stop-brew" disabled>Stop brewing</button>
            </div>
            <span id="brew-status" class="status" role="status"></span>
            <div class="active-run-section"><div class="section-heading"><h3>Active run</h3></div><div id="active-brew"></div></div>
            <div class="archive-section"><div class="section-heading"><h3>Archive</h3></div><div id="brew-archive" class="archive-list"></div></div>
          </section>

          <aside class="workspace-panel assistant-panel" data-assistant-kind="brew" aria-labelledby="brew-assistant-title">
            <div class="panel-heading"><div><p class="eyebrow">Brew-aware guidance</p><h2 id="brew-assistant-title">Brew assistant</h2></div><span class="assistant-state">Local agent</span></div>
            <p class="field-help">Context includes the selected iSpindel, active or selected archived run, saved events and recent telemetry.</p>
            <div class="chat-log" data-assistant-log aria-label="Brew assistant messages"></div>
            <label><span>Message</span><textarea data-assistant-input rows="4" placeholder="What has this brew been doing?"></textarea></label>
            <label class="assistant-research"><input type="checkbox" data-assistant-research /><span>Research with SearXNG and Kiwix</span></label>
            <button type="button" data-assistant-send>Send to ZeroClaw</button>
            <span class="status" data-assistant-status role="status">Waiting for phone agent.</span>
          </aside>
        </div>
      </section>
  </main>

  <!-- Edit Device Modal -->
  <div id="edit-modal" class="modal" role="dialog" aria-modal="true" aria-labelledby="edit-modal-title" aria-hidden="true">
      <div class="modal-content">
          <h2 id="edit-modal-title">Edit Device</h2>
          <input type="hidden" id="edit-device-id" />
          <div class="form-group">
              <label for="edit-device-name">Device Name</label>
              <input type="text" id="edit-device-name" />
          </div>
          <div class="form-group">
              <label for="edit-device-interval">Expected Interval (sec)</label>
              <input type="number" id="edit-device-interval" />
          </div>
          <div class="modal-actions">
              <button class="secondary" id="edit-cancel">Cancel</button>
              <button id="edit-save">Save</button>
          </div>
      </div>
  </div>

  <!-- Calibration Modal -->
  <div id="cal-modal" class="modal" role="dialog" aria-modal="true" aria-labelledby="cal-modal-title" aria-hidden="true">
      <div class="modal-content">
          <h2 id="cal-modal-title">Calibration</h2>
          <p class="calibration-help">Enter increasing tilt angles and non-decreasing gravity measurements. New fits are saved inactive until a stable water-reference check is complete.</p>
          <input type="hidden" id="cal-device-id" />
          <div class="form-group">
              <label for="cal-label">Label</label>
              <input type="text" id="cal-label" value="My Calibration" />
          </div>
          <div class="form-group">
              <label for="cal-order">Polynomial order</label>
              <select id="cal-order">
                  <option value="1" selected>Linear (1)</option>
                  <option value="2">Quadratic (2)</option>
                  <option value="3">Monotonic cubic (3)</option>
              </select>
          </div>
          <table class="calibration-points-table">
              <thead><tr><th>Angle</th><th>Gravity (SG)</th></tr></thead>
              <tbody id="cal-points">
                  <tr><td><input aria-label="Calibration angle 1" type="number" step="any" class="cal-a" placeholder="e.g. 25.0"></td><td><input aria-label="Calibration gravity 1" type="number" step="any" class="cal-v" placeholder="e.g. 1.000"></td></tr>
                  <tr><td><input aria-label="Calibration angle 2" type="number" step="any" class="cal-a" placeholder="e.g. 50.0"></td><td><input aria-label="Calibration gravity 2" type="number" step="any" class="cal-v" placeholder="e.g. 1.050"></td></tr>
                  <tr><td><input aria-label="Calibration angle 3" type="number" step="any" class="cal-a"></td><td><input aria-label="Calibration gravity 3" type="number" step="any" class="cal-v"></td></tr>
                  <tr><td><input aria-label="Calibration angle 4" type="number" step="any" class="cal-a"></td><td><input aria-label="Calibration gravity 4" type="number" step="any" class="cal-v"></td></tr>
              </tbody>
          </table>
          <div id="cal-status" class="calibration-status" role="status" aria-live="polite"></div>
          <div id="cal-history" aria-label="Calibration history"></div>
          <div class="modal-actions">
              <button class="secondary" id="cal-cancel">Cancel</button>
              <button id="cal-save">Calculate & Save</button>
          </div>
      </div>
  </div>

  <script src="/static/dashboard.js"></script>
  <script src="/static/brewing.js"></script>
</body>
</html>
'''
