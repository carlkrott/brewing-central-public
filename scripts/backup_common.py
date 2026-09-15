#!/usr/bin/env python3
"""Shared fail-closed primitives for verified iSpindel backup generations.

Single (v1) mode is preserved unchanged: callers that only handle a primary
manifest plus ``ispindel-<run_id>.db`` continue to work.

Dual mode (W6) adds a brew-prefixed database, a brew manifest, and a paired
generation directory that binds both artifacts under one run identifier. The
dual-mode helpers never accept mismatched run ids, missing siblings, sidecars,
symlinks, or partial directories. They fail closed with a BackupError.

The brew evidence helper records integrity, user_version, migration versions,
and per-table counts, then verifies FTS5 external-content parity without
mutating the sealed source bytes: drift is detected against a fresh temporary
copy, never the live source.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import json
import os
import secrets
import shutil
import sqlite3
from pathlib import Path
from typing import Callable, Iterable, Mapping

SCHEMA = "ispindel-backup-manifest/v1"
DUAL_SCHEMA = "ispindel-backup-manifest/v2"
INDEX_SCHEMA = "ispindel-backup-index/v1"
BACKUP_HEALTH_SCHEMA = "ispindel-backup-health/v1"
ISPINDEL_PREFIX = "ispindel"
BREW_PREFIX = "brew"
BREW_MANIFEST_NAME = "brew-manifest.json"
BACKUP_HEALTH_NAME = "backup_health.json"

TABLES = ("devices", "samples", "calibrations", "calibration_active")
BREW_TABLES: tuple[str, ...] = (
    "recipes",
    "recipe_ingredients",
    "recipe_culture_profiles",
    "recipe_scheduled_additions",
    "recipe_process_steps",
    "brew_runs",
    "brew_events",
    "water_references",
    "assistant_messages",
    "archive_evidence_bundles",
    "archive_annotations",
    "recipe_lineage",
    "device_operating_intent",
    "assistant_jobs",
    "assistant_job_stages",
    "research_documents",
    "research_documents_fts",
    "research_document_versions",
    "research_evidence_links",
)


class BackupError(RuntimeError):
    """A backup/restore contract violation."""


class BrewEvidenceError(BackupError):
    """Brew evidence integrity, count, or FTS parity check failed."""


class BackupHealthNotWritten(BackupError):
    """backup_health.json must not be written until verification + promotion succeed."""


def _validate_run_id(run_id: str) -> None:
    if (
        not run_id
        or any(c not in "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz-_" for c in run_id)
    ):
        raise BackupError("unsafe RUN_ID")


def bind_run_id(now: dt.datetime | None = None, nonce: str | None = None) -> str:
    moment = now or dt.datetime.now(dt.timezone.utc)
    if moment.tzinfo is None:
        raise BackupError("RUN_ID time must be timezone-aware")
    token = nonce or secrets.token_hex(4)
    if len(token) != 8 or any(c not in "0123456789abcdef" for c in token):
        raise BackupError("RUN_ID nonce must be exactly eight lowercase hex characters")
    return f"{moment.astimezone(dt.timezone.utc):%Y%m%dT%H%M%SZ}-{token}"


def bind_basename(run_id: str) -> str:
    _validate_run_id(run_id)
    return f"{ISPINDEL_PREFIX}-{run_id}.db"


def bind_brew_basename(run_id: str) -> str:
    _validate_run_id(run_id)
    return f"{BREW_PREFIX}-{run_id}.db"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def atomic_json(path: Path, payload: Mapping[str, object], mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
        dir_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    finally:
        if temp.exists():
            temp.unlink()


def _integrity_check(conn: sqlite3.Connection) -> None:
    integrity = [row[0] for row in conn.execute("PRAGMA integrity_check")]
    if integrity != ["ok"]:
        raise BackupError(f"integrity_check failed: {integrity!r}")


def _migration_versions(conn: sqlite3.Connection) -> list[int]:
    migrations: list[int] = []
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'").fetchone():
        columns = [row[1] for row in conn.execute("PRAGMA table_info(schema_migrations)")]
        if "version" in columns:
            migrations = [int(row[0]) for row in conn.execute("SELECT version FROM schema_migrations ORDER BY version")]
    return migrations


def sqlite_evidence(path: Path) -> dict[str, object]:
    if not path.is_file() or path.is_symlink():
        raise BackupError(f"database is missing, non-regular, or symlinked: {path}")
    uri = path.resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=5.0)
    try:
        conn.execute("PRAGMA query_only=ON")
        _integrity_check(conn)
        user_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        migrations = _migration_versions(conn)
        counts: dict[str, int] = {}
        for table in TABLES:
            present = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,),
            ).fetchone()
            counts[table] = int(conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]) if present else 0
    finally:
        conn.close()
    return {
        "integrity": "ok",
        "user_version": user_version,
        "migration_versions": migrations,
        "counts": counts,
    }


def brew_table_allowlist() -> tuple[str, ...]:
    """Return the immutable brew evidence table allowlist (read-only contract)."""
    return BREW_TABLES


def _brew_evidence_on_temp_copy(path: Path, *, expected_prefix: str | None = None) -> dict[str, object]:
    """Open path in read-only mode, never mutate it.

    The source bytes are never opened in write mode. FTS5 drift is detected
    via an EXISTS query against the FTS index (FTS5 external-content tables
    don't materialize SELECT COUNT(*)) and reported as a failure (fail
    closed); the caller may opt into a bounded repair via
    ``brew_evidence_repair_fresh``.
    """
    if path.is_symlink() or not path.is_file():
        raise BrewEvidenceError(f"brew database is missing, non-regular, or symlinked: {path}")
    if expected_prefix is not None and not path.name.startswith(f"{expected_prefix}-"):
        raise BrewEvidenceError(
            f"brew evidence expects a {expected_prefix}-prefixed basename; got {path.name!r}"
        )

    uri = path.resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=5.0)
    try:
        conn.execute("PRAGMA query_only=ON")
        try:
            _integrity_check(conn)
        except (sqlite3.DatabaseError, BackupError) as exc:
            raise BrewEvidenceError(f"brew database integrity check failed: {exc}") from exc
        user_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        migrations = _migration_versions(conn)
        counts: dict[str, int] = {}
        for table in BREW_TABLES:
            present = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,),
            ).fetchone()
            counts[table] = int(conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]) if present else 0

        # FTS5 external-content parity: the canonical external-content FTS5
        # table (content='research_documents', content_rowid='id') does NOT
        # materialize SELECT rowid or SELECT COUNT(*) usefully (it joins
        # against the content table at query time, so orphan rowids in the
        # FTS segment index silently drop out). The reliable parity check
        # is to read the shadow ``<fts>_docsize`` table directly: its ``id``
        # column holds the FTS-indexed rowids and stays in sync with the
        # segment index even when the content table diverges.
        #
        # Drift signals:
        #   * docsize rows whose rowid is missing from research_documents
        #     (orphan FTS row from a missed cleanup or a direct INSERT)
        #   * research_documents rows whose id is missing from docsize
        #     (e.g. snapshot taken before the AFTER INSERT trigger fired;
        #     see app/brewing.py:_rebuild_research_documents_fts)
        #
        # We DO NOT mutate the source. Fail closed; caller decides whether to
        # invoke brew_evidence_repair_fresh.
        fts_present = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='research_documents_fts'"
        ).fetchone() is not None
        docs_present = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='research_documents'"
        ).fetchone() is not None
        if docs_present and not fts_present:
            raise BrewEvidenceError(
                "brew FTS5 external-content parity fails closed: "
                "research_documents_fts table is missing (source untouched)"
            )
        if fts_present and docs_present:
            docsize_row = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='research_documents_fts_docsize'"
            ).fetchone()
            if docsize_row is None:
                raise BrewEvidenceError(
                    "brew FTS5 external-content parity fails closed: "
                    "research_documents_fts_docsize shadow table is missing (source untouched)"
                )
            orphan_count = int(conn.execute(
                "SELECT COUNT(*) FROM 'research_documents_fts_docsize' AS ds "
                "WHERE NOT EXISTS (SELECT 1 FROM research_documents AS d WHERE d.id = ds.id)"
            ).fetchone()[0])
            missing_count = int(conn.execute(
                "SELECT COUNT(*) FROM research_documents AS d "
                "WHERE NOT EXISTS (SELECT 1 FROM 'research_documents_fts_docsize' AS ds WHERE ds.id = d.id)"
            ).fetchone()[0])
            if orphan_count or missing_count:
                raise BrewEvidenceError(
                    "brew FTS5 external-content parity fails closed: "
                    f"orphan_fts_rows={orphan_count} missing_fts_rows={missing_count} "
                    f"research_documents={counts['research_documents']} "
                    "(source untouched; invoke brew_evidence_repair_fresh on a copy to recover)"
                )
    finally:
        conn.close()

    return {
        "integrity": "ok",
        "user_version": user_version,
        "migration_versions": migrations,
        "counts": counts,
    }


def brew_evidence_repair_fresh(path: Path, *, expected_prefix: str | None = None) -> dict[str, object]:
    """Run an FTS5 rebuild against a FRESH TEMPORARY COPY (never the source).

    The source bytes are never opened in write mode. Returns the rebuilt
    evidence so the caller can compare to the original.
    """
    if expected_prefix is not None and not path.name.startswith(f"{expected_prefix}-"):
        raise BrewEvidenceError(
            f"brew evidence expects a {expected_prefix}-prefixed basename; got {path.name!r}"
        )
    return _repair_fts_on_temp_copy(path)


def _repair_fts_on_temp_copy(path: Path) -> dict[str, object]:
    """Copy the source to a temp path, rebuild FTS there, and re-evidence.

    The source bytes are never opened in write mode. The temp copy is
    cleaned up in a ``finally`` block.
    """
    temp_dir = path.parent / f".brew-evidence-tmp-{os.getpid()}-{secrets.token_hex(4)}"
    temp_dir.mkdir(mode=0o700)
    temp_copy = temp_dir / path.name
    try:
        shutil.copyfile(path, temp_copy)
        os.chmod(temp_copy, 0o600)
        rw = sqlite3.connect(temp_copy)
        try:
            rw.execute("PRAGMA query_only=OFF")
            rw.execute(
                "INSERT INTO research_documents_fts(research_documents_fts) VALUES('rebuild')"
            )
            rw.commit()
        finally:
            rw.close()
        # Re-run evidence on the repaired copy using the normal helper,
        # but bypass the rebuild branch (counts now match).
        uri = temp_copy.resolve().as_uri() + "?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=5.0)
        try:
            conn.execute("PRAGMA query_only=ON")
            _integrity_check(conn)
            user_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
            migrations = _migration_versions(conn)
            counts: dict[str, int] = {}
            for table in BREW_TABLES:
                present = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,),
                ).fetchone()
                counts[table] = int(conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]) if present else 0
        finally:
            conn.close()
        return {
            "integrity": "ok",
            "user_version": user_version,
            "migration_versions": migrations,
            "counts": counts,
        }
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def brew_evidence(path: Path, *, expected_prefix: str | None = BREW_PREFIX) -> dict[str, object]:
    """Run the brew evidence contract against a sealed source path.

    Records integrity, user_version, migration_versions, per-table counts for
    the allowlist, and proves FTS5 external-content parity without mutating
    the sealed source. Fails closed (BrewEvidenceError) on any drift.
    """
    if expected_prefix is not None and not path.name.startswith(f"{expected_prefix}-"):
        raise BrewEvidenceError(
            f"brew evidence expects a {expected_prefix}-prefixed basename; got {path.name!r}"
        )
    return _brew_evidence_on_temp_copy(path)


def build_manifest(run_id: str, db_path: Path, produced_at: str, source: Mapping[str, object]) -> dict[str, object]:
    basename = bind_basename(run_id)
    if db_path.name != basename:
        raise BackupError("database basename is not bound to RUN_ID")
    database = sqlite_evidence(db_path)
    return {
        "schema": SCHEMA,
        "run_id": run_id,
        "basename": basename,
        "produced_at": produced_at,
        "file": {"name": basename, "size": db_path.stat().st_size, "sha256": sha256_file(db_path)},
        "database": database,
        "source": dict(source),
    }


def _file_block(run_id: str, db_path: Path) -> dict[str, object]:
    return {
        "name": db_path.name,
        "size": db_path.stat().st_size,
        "sha256": sha256_file(db_path),
    }


def build_dual_manifest(
    run_id: str,
    ispindel_db: Path,
    brew_db: Path,
    produced_at: str,
    source: Mapping[str, object],
) -> dict[str, object]:
    """Build a paired manifest envelope (primary + brew).

    Both halves are independently validated; the envelope is rejected if the
    run identifier disagrees with either basename or either artifact is
    missing/symlinked/non-regular.
    """
    ispindel_basename = bind_basename(run_id)
    brew_basename = bind_brew_basename(run_id)
    if ispindel_db.name != ispindel_basename:
        raise BackupError("ispindel database basename is not bound to RUN_ID")
    if brew_db.name != brew_basename:
        raise BackupError("brew database basename is not bound to RUN_ID")
    ispindel_evidence = sqlite_evidence(ispindel_db)
    brew_evidence_payload = brew_evidence(brew_db)
    primary = {
        "schema": SCHEMA,
        "run_id": run_id,
        "basename": ispindel_basename,
        "produced_at": produced_at,
        "file": _file_block(run_id, ispindel_db),
        "database": ispindel_evidence,
        "source": dict(source),
        "paired": {
            "enabled": True,
            "schema": DUAL_SCHEMA,
            "shared_run_id": run_id,
            "databases": {
                ISPINDEL_PREFIX: {
                    "basename": ispindel_basename,
                    "manifest": "manifest.json",
                },
                BREW_PREFIX: {
                    "basename": brew_basename,
                    "manifest": BREW_MANIFEST_NAME,
                },
            },
        },
    }
    brew = {
        "schema": SCHEMA,
        "run_id": run_id,
        "basename": brew_basename,
        "produced_at": produced_at,
        "prefix": BREW_PREFIX,
        "file": _file_block(run_id, brew_db),
        "database": brew_evidence_payload,
        "source": dict(source),
        "paired": {
            "enabled": True,
            "schema": DUAL_SCHEMA,
            "shared_run_id": run_id,
            "databases": {
                ISPINDEL_PREFIX: {
                    "basename": ispindel_basename,
                    "manifest": "manifest.json",
                },
                BREW_PREFIX: {
                    "basename": brew_basename,
                    "manifest": BREW_MANIFEST_NAME,
                },
            },
        },
    }
    return {"primary": primary, "brew": brew, "shared_run_id": run_id}


def _forbid_sidecars(directory: Path, basenames: Iterable[str]) -> None:
    """Reject all symlinks, SQLite sidecars, and partial-copy artifacts."""
    expected = {
        "manifest.json",
        BREW_MANIFEST_NAME,
        *basenames,
    }
    for child in directory.iterdir():
        if child.is_symlink():
            raise BackupError(f"backup generation contains symlink: {child.name}")
        if child.name not in expected:
            raise BackupError(
                f"backup generation contains unexpected sidecar or artifact: {child.name}"
            )
        if child.name.endswith(("-wal", "-shm", "-journal")):
            raise BackupError(f"backup generation contains SQLite sidecar: {child.name}")
        if child.name.endswith((".sqlitecopy", ".copying", ".partial")) or ".sqlitecopy-" in child.name:
            raise BackupError(f"backup generation contains partial-copy artifact: {child.name}")
    # Keep the explicit named checks for clarity and for callers passing an
    # empty/nonexistent directory entry set.
    for basename in basenames:
        for suffix in ("-wal", "-shm", "-journal"):
            if (directory / f"{basename}{suffix}").exists():
                raise BackupError(f"backup generation contains SQLite sidecar {basename}{suffix}")


def _validate_manifest_file(manifest_path: Path, expected_basename: str, expected_run_id: str) -> dict[str, object]:  # type: ignore[type-arg]
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise BackupError(f"manifest must be an exact regular file: {manifest_path}")
    try:
        raw_obj = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BackupError(f"manifest is unreadable or invalid: {exc}") from exc
    raw: dict[str, object] = raw_obj if isinstance(raw_obj, dict) else {}  # type: ignore[assignment]
    if raw.get("schema") not in (SCHEMA, DUAL_SCHEMA):
        raise BackupError("unsupported manifest schema")
    if raw.get("run_id") != expected_run_id:
        raise BackupError(
            f"manifest run_id {raw.get('run_id')!r} does not match expected {expected_run_id!r}"
        )
    if raw.get("basename") != expected_basename:
        raise BackupError(
            f"manifest basename {raw.get('basename')!r} does not match expected {expected_basename!r}"
        )
    file_block = raw.get("file")
    database_block = raw.get("database")
    if not isinstance(file_block, dict) or not isinstance(database_block, dict):
        raise BackupError("manifest file/database blocks are invalid")
    if file_block.get("name") != expected_basename:
        raise BackupError("manifest file name mismatch")
    db_path = manifest_path.parent / expected_basename
    if db_path.is_symlink() or not db_path.is_file():
        raise BackupError("backup database is missing, non-regular, or symlinked")
    if db_path.stat().st_size != file_block.get("size"):
        raise BackupError("backup size mismatch")
    if sha256_file(db_path) != file_block.get("sha256"):
        raise BackupError("backup sha256 mismatch")
    # Re-evidence by re-running sqlite_evidence against the artifact.
    if expected_basename.startswith(f"{ISPINDEL_PREFIX}-"):
        actual = sqlite_evidence(db_path)
    elif expected_basename.startswith(f"{BREW_PREFIX}-"):
        actual = brew_evidence(db_path)
    else:
        raise BackupError(f"unsupported database prefix in basename {expected_basename!r}")
    if actual != database_block:
        raise BackupError(f"database evidence mismatch: actual={actual!r}")
    return raw


def validate_generation(manifest_path: Path) -> dict[str, object]:
    """Validate the legacy single-database generation layout (unchanged).

    The basename must be ``ispindel-<run_id>.db``; no brew sidecars are
    required. New dual-mode code paths use ``validate_dual_generation``
    instead.
    """
    manifest_path = Path(manifest_path)
    if manifest_path.name != "manifest.json" or manifest_path.is_symlink() or not manifest_path.is_file():
        raise BackupError("manifest must be an exact regular manifest.json path")
    try:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BackupError(f"manifest is unreadable or invalid: {exc}") from exc
    if not isinstance(raw, dict) or raw.get("schema") != SCHEMA:
        raise BackupError("unsupported manifest schema")
    run_id = raw.get("run_id")
    if not isinstance(run_id, str):
        raise BackupError("manifest run_id is invalid")
    basename = bind_basename(run_id)
    if raw.get("basename") != basename:
        raise BackupError("manifest basename mismatch")
    file_block = raw.get("file")
    database_block = raw.get("database")
    if not isinstance(file_block, dict) or not isinstance(database_block, dict):
        raise BackupError("manifest file/database blocks are invalid")
    if file_block.get("name") != basename:
        raise BackupError("manifest file name mismatch")
    db_path = manifest_path.parent / basename
    if db_path.is_symlink() or not db_path.is_file():
        raise BackupError("backup database is missing, non-regular, or symlinked")
    if db_path.stat().st_size != file_block.get("size"):
        raise BackupError("backup size mismatch")
    if sha256_file(db_path) != file_block.get("sha256"):
        raise BackupError("backup sha256 mismatch")
    actual = sqlite_evidence(db_path)
    if actual != database_block:
        raise BackupError(f"database evidence mismatch: actual={actual!r}")
    forbidden = [
        manifest_path.parent / f"{basename}-wal",
        manifest_path.parent / f"{basename}-shm",
        manifest_path.parent / f"{basename}-journal",
    ]
    forbidden.extend(manifest_path.parent.glob("*.sqlitecopy*"))
    forbidden.extend(manifest_path.parent.glob("*.copying"))
    if any(path.exists() for path in forbidden):
        raise BackupError("backup generation contains SQLite sidecars or partial-copy artifacts")
    return raw


def validate_dual_generation(directory: Path, *, allow_partial: bool = False) -> dict[str, object]:
    """Validate a paired generation directory end-to-end.

    Promoted generations use the exact ``<run_id>`` directory name. The
    staging copy may use ``<run_id>.partial`` only when the caller explicitly
    passes ``allow_partial=True``; public/default validation rejects partial
    directories so retention and restore cannot consume them.
      - mismatched run ids between the two manifests
      - missing sibling manifest or database
      - symlinks or non-regular files anywhere in the directory
      - SQLite sidecars (-wal/-shm/-journal) or partial-copy artifacts
      - any database whose re-evidence diverges from its manifest

    Returns the validated envelope including the shared run id.
    """
    directory = Path(directory)
    if directory.is_symlink() or not directory.is_dir():
        raise BackupError("dual generation directory must be an exact regular directory")
    raw_name = directory.name
    if raw_name.endswith(".partial"):
        if not allow_partial:
            raise BackupError("dual generation directory is a partial promotion")
        run_id = raw_name.removesuffix(".partial")
    else:
        run_id = raw_name
    _validate_run_id(run_id)

    ispindel_basename = bind_basename(run_id)
    brew_basename = bind_brew_basename(run_id)
    manifest_path = directory / "manifest.json"
    brew_manifest_path = directory / BREW_MANIFEST_NAME

    # Fail closed on sidecars/partials BEFORE running evidence so we never
    # even open a half-promoted artifact.
    _forbid_sidecars(directory, (ispindel_basename, brew_basename))

    primary = _validate_manifest_file(manifest_path, ispindel_basename, run_id)
    brew = _validate_manifest_file(brew_manifest_path, brew_basename, run_id)

    # Cross-bind: both manifests must agree on the shared_run_id.
    primary_paired = primary["paired"] if isinstance(primary.get("paired"), dict) else None
    brew_paired = brew["paired"] if isinstance(brew.get("paired"), dict) else None
    if primary_paired is None or primary_paired.get("enabled") is not True:
        raise BackupError("primary manifest is not bound to a paired generation")
    if brew_paired is None or brew_paired.get("enabled") is not True:
        raise BackupError("brew manifest is not bound to a paired generation")
    if primary_paired.get("shared_run_id") != run_id or brew_paired.get("shared_run_id") != run_id:
        raise BackupError("paired generation run_id binding disagrees between manifests")
    primary_databases = primary_paired.get("databases")
    brew_databases = brew_paired.get("databases")
    if not (isinstance(primary_databases, dict) and isinstance(brew_databases, dict)):
        raise BackupError("paired generation databases block is missing or invalid")
    if (
        primary_databases.get(ISPINDEL_PREFIX, {}).get("basename") != ispindel_basename  # type: ignore[union-attr]
        or primary_databases.get(BREW_PREFIX, {}).get("basename") != brew_basename  # type: ignore[union-attr]
    ):
        raise BackupError("primary manifest paired databases block disagrees with basenames")
    if (
        brew_databases.get(ISPINDEL_PREFIX, {}).get("basename") != ispindel_basename  # type: ignore[union-attr]
        or brew_databases.get(BREW_PREFIX, {}).get("basename") != brew_basename  # type: ignore[union-attr]
    ):
        raise BackupError("brew manifest paired databases block disagrees with basenames")

    return {"primary": primary, "brew": brew, "shared_run_id": run_id}


def update_index_for_dual_generation(
    index_path: Path,
    run_id: str,
    *,
    primary_manifest_path: Path,
    brew_manifest_path: Path,
    offhost_pair_path: str,
) -> dict[str, object]:
    """Update an off-host index so the pair is recorded while legacy fields persist.

    The legacy ``latest_verified_manifest`` keeps its existing absolute path
    (the primary manifest), and a new ``databases`` map carries both per-
    database verified manifest paths/identities plus the shared run id.
    """
    primary_abs = str(Path(primary_manifest_path).resolve())
    brew_abs = str(Path(brew_manifest_path).resolve())
    legacy_index: dict[str, object] = {}
    if index_path.is_file() and not index_path.is_symlink():
        try:
            existing = json.loads(index_path.read_text(encoding="utf-8"))
            if isinstance(existing, dict):
                legacy_index = dict(existing)
        except (OSError, json.JSONDecodeError):
            legacy_index = {}
    legacy_index.setdefault("schema", INDEX_SCHEMA)
    legacy_index["updated_at"] = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    legacy_index["latest_verified_run_id"] = run_id
    legacy_index["latest_verified_manifest"] = primary_abs
    legacy_index["offhost_verified_manifest"] = primary_abs
    legacy_index["offhost_pair_path"] = offhost_pair_path
    legacy_index["databases"] = {
        ISPINDEL_PREFIX: {
            "identity": ISPINDEL_PREFIX,
            "manifest_path": primary_abs,
            "run_id": run_id,
            "basename": bind_basename(run_id),
        },
        BREW_PREFIX: {
            "identity": BREW_PREFIX,
            "manifest_path": brew_abs,
            "run_id": run_id,
            "basename": bind_brew_basename(run_id),
        },
        "shared_run_id": run_id,
    }
    atomic_json(index_path, legacy_index)
    return legacy_index


def write_backup_health_if_verified(
    health_path: Path,
    verified_and_promoted: Callable[[], bool],
    *,
    payload: Mapping[str, object],
    mode: int = 0o644,
) -> None:
    """Write backup_health.json only when both off-host verification AND promotion have succeeded.

    The caller MUST pass a callable (not a static bool) so the helper always
    re-checks the gating condition at write time. Raises BackupHealthNotWritten
    (without writing) if verification or promotion has not yet succeeded, if
    it has been refused, or if the off-host copy was partial.

    ``mode`` defaults to ``0o644`` because the dashboard container (UID
    10001) reads this file across a read-only bind mount and must NOT be
    granted write access. The generic ``atomic_json`` default remains
    ``0o600`` so backup manifests, indexes, and sidecars retain the existing
    restrictive permissions; only this dedicated health writer opens the
    perms for cross-UID read.
    """
    if not verified_and_promoted():
        raise BackupHealthNotWritten(
            "backup_health.json must not be written until off-host verification AND promotion succeed"
        )
    atomic_json(health_path, dict(payload), mode=mode)


def resolve_manifest_index(index_path: Path) -> Path:
    try:
        index = json.loads(Path(index_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BackupError(f"backup index is unreadable or invalid: {exc}") from exc
    value = index.get("latest_verified_manifest") if isinstance(index, dict) else None
    if not isinstance(value, str) or not Path(value).is_absolute():
        raise BackupError("index lacks an absolute latest_verified_manifest")
    manifest = Path(value)
    raw_obj: object
    try:
        raw_obj = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BackupError(f"indexed manifest is unreadable or invalid: {exc}") from exc
    if isinstance(raw_obj, dict) and isinstance(raw_obj.get("paired"), dict) and raw_obj["paired"].get("enabled") is True:
        validate_dual_generation(manifest.parent)
    else:
        validate_generation(manifest)
    return manifest


@dataclasses.dataclass
class RetentionResult:
    verified: int
    deleted: int
    kept: int
    weekly_kept: int = 0


def _verify_one_generation(entry: Path) -> tuple[str, Path, dict[str, object]] | None:
    """Try to verify one entry as either single (legacy) or paired (dual) layout."""
    if entry.name.endswith(".partial") or not entry.is_dir() or entry.is_symlink():
        return None
    if (entry / "manifest.json").is_file():
        try:
            primary_obj = json.loads((entry / "manifest.json").read_text(encoding="utf-8"))
            if isinstance(primary_obj, dict):
                paired = primary_obj.get("paired")
                if isinstance(paired, dict) and paired.get("enabled") is True:
                    envelope = validate_dual_generation(entry)
                    manifest = envelope["primary"]
                else:
                    manifest = validate_generation(entry / "manifest.json")
            else:
                return None
        except (BackupError, OSError, json.JSONDecodeError):
            return None
        if manifest["run_id"] != entry.name:
            return None
        return str(manifest["run_id"]), entry, manifest
    if (entry / BREW_MANIFEST_NAME).is_file():
        try:
            envelope = validate_dual_generation(entry)
        except BackupError:
            return None
        manifest = envelope["primary"]
        if manifest["run_id"] != entry.name:
            return None
        return str(manifest["run_id"]), entry, manifest
    return None


def verified_generations(root: Path) -> list[tuple[str, Path, dict[str, object]]]:
    result = []
    if not root.exists():
        return result
    for entry in root.iterdir():
        verified = _verify_one_generation(entry)
        if verified is not None:
            result.append(verified)
    return sorted(result, key=lambda item: item[0])


def apply_retention(root: Path, rolling: int, weekly_weeks: int = 0, now: dt.datetime | None = None, suppress: bool = False) -> RetentionResult:
    if rolling < 1 or weekly_weeks < 0:
        raise BackupError("retention values must be positive")
    generations = verified_generations(root)
    rolling_ids = {item[0] for item in generations[-rolling:]}
    weekly_ids: set[str] = set()
    if weekly_weeks:
        moment = (now or dt.datetime.now(dt.timezone.utc)).astimezone(dt.timezone.utc)
        cutoff = moment - dt.timedelta(weeks=weekly_weeks)
        seen: set[tuple[int, int]] = set()
        for run_id, _path, manifest in reversed(generations):
            try:
                produced = dt.datetime.fromisoformat(str(manifest["produced_at"]).replace("Z", "+00:00"))
            except (KeyError, ValueError):
                continue
            if produced < cutoff:
                continue
            key = produced.isocalendar()[:2]
            if key not in seen:
                seen.add(key)
                weekly_ids.add(run_id)
    keep_ids = rolling_ids | weekly_ids
    deleted = 0
    if not suppress:
        for run_id, path, _manifest in generations:
            if run_id not in keep_ids:
                shutil.rmtree(path)
                deleted += 1
    return RetentionResult(len(generations), deleted, len(generations) - deleted, len(weekly_ids))


def require_capacity(path: Path, minimum_free_bytes: int, minimum_free_inodes: int) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    usage = shutil.disk_usage(path)
    stat = os.statvfs(path)
    if usage.free < minimum_free_bytes:
        raise BackupError(f"insufficient free bytes: {usage.free} < {minimum_free_bytes}")
    # Btrfs allocates inodes dynamically and reports both the total and
    # available inode counts as zero. Treat that pair as "not reported";
    # filesystems with a real inode count must still satisfy the threshold.
    inode_count_available = not (stat.f_files == 0 and stat.f_favail == 0)
    if inode_count_available and stat.f_favail < minimum_free_inodes:
        raise BackupError(f"insufficient free inodes: {stat.f_favail} < {minimum_free_inodes}")
