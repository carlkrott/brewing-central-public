#!/usr/bin/env python3
"""Phone-local snapshot helper for the iSpindel dashboard.

This helper runs on the Android phone (Termux) under the host
coordinator's control. It is stdlib-only and Termux-compatible so the
deployment never drags third-party packages onto the device.

Behaviour:

* ``snapshot`` — uses ``sqlite3.Connection.backup`` to produce exact
  snapshots of ``ispindel.db`` and ``brew.db`` and writes the paired
  ``manifest.json`` + ``brew-manifest.json`` under a phone-local
  staging directory whose basename is the validated ``run_id``.
* ``publish-health`` — atomically writes a minimal
  ``ispindel-backup-health/v1`` receipt to a caller-configured phone
  path. The host coordinator invokes this subcommand ONLY after the
  off-host pair has been verified AND promoted.

The helper imports the canonical ``scripts/backup_common.py`` from the
deployed current tree so its manifest envelope, evidence shape, and
brew allowlist stay byte-identical to the off-host validator. Failures
in the canonical import are fatal — the helper never invents its own
schema or evidence shape.

Staging directory semantics:

* ``--staging-root`` is the validated phone-local parent. The helper
  refuses symlinks, refuses pre-existing children of the same run id,
  and creates the child directory itself with mode ``0o700``.
* Every artifact written inside the child is a regular file with mode
  ``0o600``.

The coordinator invokes this helper via
``adb -s SERIAL shell run-as com.termux $ROOT/venv/bin/python
$ROOT/current/ops/android/phone-backup-snapshot.py ...`` and pulls the
staged pair into a host temp directory using
``adb -s SERIAL exec-out run-as com.termux cat EXACT_PATH`` so binary
contents never pass through a text-mode shell.
"""

from __future__ import annotations

import argparse
import datetime as dt
import importlib
import json
import os
import secrets
import shutil
import sqlite3
import sys
from pathlib import Path
from typing import Sequence

ISPINDEL_PREFIX = "ispindel"
BREW_PREFIX = "brew"
PRIMARY_MANIFEST_NAME = "manifest.json"
BREW_MANIFEST_NAME = "brew-manifest.json"
SCHEMA = "ispindel-backup-manifest/v1"
DUAL_SCHEMA = "ispindel-backup-manifest/v2"
BACKUP_HEALTH_SCHEMA = "ispindel-backup-health/v1"
VALID_RUN_ID_CHARS = set(
    "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz-_"
)


class HelperError(RuntimeError):
    """A phone backup helper contract violation."""


def _load_backup_common() -> object:
    """Import the canonical ``scripts/backup_common.py`` from the
    deployed current tree.

    The helper runs on the phone inside ``$ROOT/current/ops/android``.
    The ``scripts/`` tree lives at ``$ROOT/current/scripts`` and ships
    with every release. We refuse to fall back to a duplicated copy:
    the manifest schema, brew allowlist, and evidence shapes MUST be
    byte-identical to what ``backup_common`` validates off-phone.
    """
    here = Path(__file__).resolve()
    scripts_dir = here.parent.parent.parent / "scripts"
    if not scripts_dir.is_dir() or scripts_dir.is_symlink():
        raise HelperError(
            f"canonical scripts directory is missing or symlinked: {scripts_dir}"
        )
    sys.path.insert(0, str(scripts_dir))
    try:
        return importlib.import_module("backup_common")
    except (ImportError, OSError) as exc:  # pragma: no cover - import error
        raise HelperError(
            f"failed to import canonical backup_common from {scripts_dir}: {exc}"
        ) from exc


_BACKUP_COMMON: object | None = None


def backup_common() -> object:
    global _BACKUP_COMMON
    if _BACKUP_COMMON is None:
        _BACKUP_COMMON = _load_backup_common()
    return _BACKUP_COMMON


def _validate_run_id(run_id: str) -> None:
    if (
        not run_id
        or any(c not in VALID_RUN_ID_CHARS for c in run_id)
        or ".." in run_id
    ):
        raise HelperError(f"unsafe RUN_ID {run_id!r}")


def bind_auto_run_id(now: dt.datetime | None = None, nonce: str | None = None) -> str:
    """Return a validated UTC run id, exactly as ``backup_common.bind_run_id``.

    The helper delegates to the canonical ``backup_common.bind_run_id``
    helper so the run id format matches every other producer.
    """
    bc = backup_common()
    return bc.bind_run_id(now=now, nonce=nonce)  # type: ignore[attr-defined]


def _sha256_file(path: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _validate_source(path: Path, *, label: str) -> None:
    if path.is_symlink() or not path.is_file():
        raise HelperError(
            f"{label} source must be a regular file (not a symlink or missing): {path}"
        )


def _validate_staging_child(staging_root: Path, run_id: str) -> Path:
    """Validate that the staging child is a fresh, non-symlinked
    regular directory we are about to create ourselves.
    """
    if staging_root.is_symlink():
        raise HelperError(f"staging root is a symlink: {staging_root}")
    if not staging_root.is_dir():
        raise HelperError(f"staging root is not a directory: {staging_root}")
    child = staging_root / run_id
    if child.exists() or child.is_symlink():
        raise HelperError(
            f"staging child already exists or is symlinked: {child}"
        )
    return child


def _copy_via_sqlite_backup(source: Path, destination: Path) -> None:
    """Snapshot ``source`` into ``destination`` via ``sqlite3.backup``.

    The destination is opened in write mode so ``backup()`` can land a
    complete copy. ``PRAGMA wal_checkpoint(TRUNCATE)`` + journal mode
    DELETE + commit + chmod ``0o600`` complete the snapshot.
    """
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    src_uri = source.resolve().as_uri() + "?mode=ro"
    src_conn = sqlite3.connect(src_uri, uri=True)
    try:
        dst_conn = sqlite3.connect(destination)
        try:
            src_conn.backup(dst_conn)
            dst_conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            dst_conn.execute("PRAGMA journal_mode=DELETE")
            dst_conn.commit()
        finally:
            dst_conn.close()
    finally:
        src_conn.close()
    os.chmod(destination, 0o600)


def _atomic_write(path: Path, payload: dict[str, object]) -> None:
    """Atomically write ``payload`` as JSON to ``path``.

    The implementation mirrors ``backup_common.atomic_json`` but is
    intentionally duplicated so the helper stays stdlib-only without
    requiring an early import of ``backup_common`` at module load time
    (the import is loaded lazily by :func:`backup_common`).
    """
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temp = path.with_name(f".{path.name}.tmp-{os.getpid()}-{secrets.token_hex(4)}")
    try:
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
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


def _build_paired_manifest(
    run_id: str,
    ispindel_db: Path,
    brew_db: Path,
    produced_at: str,
    source: dict[str, object],
) -> dict[str, object]:
    """Build the dual envelope by delegating to ``backup_common``.

    The host coordinator re-validates the envelope via
    ``backup_common.validate_dual_generation``. The helper never
    duplicates the manifest schema: ``backup_common.build_dual_manifest``
    IS the source of truth, and this function calls it directly.
    """
    bc = backup_common()
    return bc.build_dual_manifest(  # type: ignore[attr-defined]
        run_id=run_id,
        ispindel_db=ispindel_db,
        brew_db=brew_db,
        produced_at=produced_at,
        source=source,
    )


def snapshot_pair(
    *,
    run_id: str,
    staging_root: Path,
    ispindel_src: Path,
    brew_src: Path,
    produced_at: str | None = None,
) -> dict[str, object]:
    """Snapshot the dual database pair into ``staging_root/<run_id>``.

    Returns the envelope that ``validate_dual_generation`` will
    re-verify off-phone.
    """
    _validate_run_id(run_id)
    _validate_source(ispindel_src, label="ispindel")
    _validate_source(brew_src, label="brew")
    staging_root = Path(staging_root)
    child = _validate_staging_child(staging_root, run_id)
    child.mkdir(mode=0o700)
    ispindel_dst = child / f"{ISPINDEL_PREFIX}-{run_id}.db"
    brew_dst = child / f"{BREW_PREFIX}-{run_id}.db"
    _copy_via_sqlite_backup(ispindel_src, ispindel_dst)
    _copy_via_sqlite_backup(brew_src, brew_dst)
    timestamp = produced_at or dt.datetime.now(dt.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    source = {
        "mode": "phone-offline-dual-backup",
        "ispindel": str(ispindel_src.resolve()),
        "brew": str(brew_src.resolve()),
    }
    envelope = _build_paired_manifest(
        run_id=run_id,
        ispindel_db=ispindel_dst,
        brew_db=brew_dst,
        produced_at=timestamp,
        source=source,
    )
    primary = envelope["primary"]
    brew = envelope["brew"]
    assert isinstance(primary, dict) and isinstance(brew, dict)
    _atomic_write(child / PRIMARY_MANIFEST_NAME, primary)
    _atomic_write(child / BREW_MANIFEST_NAME, brew)
    return envelope


def cleanup_staging(*, staging_root: Path, run_id: str) -> None:
    """Remove exactly ``staging_root/<run_id>`` if it is a regular child.

    This is intentionally not a recursive parent cleanup and never
    follows a symlink. It is the only phone-side staging cleanup
    operation exposed to the coordinator.
    """
    _validate_run_id(run_id)
    staging_root = Path(staging_root)
    if staging_root.is_symlink() or not staging_root.is_dir():
        raise HelperError(f"staging root is missing or symlinked: {staging_root}")
    child = staging_root / run_id
    if child.is_symlink():
        raise HelperError(f"staging child is a symlink: {child}")
    if child.exists() and not child.is_dir():
        raise HelperError(f"staging child is not a directory: {child}")
    if child.exists():
        shutil.rmtree(child)


def publish_health(    *,
    health_path: Path,
    payload: dict[str, object],
) -> None:
    """Atomically write the ``ispindel-backup-health/v1`` receipt.

    The payload schema MUST already match ``BACKUP_HEALTH_SCHEMA``;
    this helper does not synthesize fields. The host coordinator
    invokes this subcommand ONLY after off-host verification AND
    promotion have both succeeded. Any earlier call is a contract
    violation and the helper refuses.
    """
    if payload.get("schema") != BACKUP_HEALTH_SCHEMA:
        raise HelperError(
            f"health receipt schema {payload.get('schema')!r} does not match "
            f"{BACKUP_HEALTH_SCHEMA!r}"
        )
    required = ("run_id", "verified_at", "mode", "offhost_path")
    missing = [key for key in required if key not in payload]
    if missing:
        raise HelperError(f"health receipt missing required keys: {missing!r}")
    health_path = Path(health_path)
    if health_path.is_symlink():
        raise HelperError(f"health path is a symlink: {health_path}")
    _atomic_write(health_path, payload)
    os.chmod(health_path, 0o644)


def _snapshot_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="snapshot the phone database pair")
    parser.add_argument("--run-id", required=True)
    parser.add_argument(
        "--staging-root",
        required=True,
        help="validated phone-local parent (e.g. $ROOT/data/backup-staging)",
    )
    parser.add_argument(
        "--ispindel-src",
        required=True,
        type=Path,
        help="absolute source path for ispindel.db on the phone",
    )
    parser.add_argument(
        "--brew-src",
        required=True,
        type=Path,
        help="absolute source path for brew.db on the phone",
    )
    parser.add_argument(
        "--produced-at",
        help="Override the produced_at timestamp (RFC3339 UTC). Tests-only.",
    )
    return parser


def _cleanup_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="remove one validated phone staging child")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--staging-root", required=True)
    return parser


def _publish_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="publish the off-host verified health receipt to the phone")
    parser.add_argument(
        "--health-path",
        required=True,
        help="absolute phone-local destination for backup_health.json",
    )
    parser.add_argument(
        "--payload-json",
        required=True,
        help="literal JSON payload as a single argument (host writes it after promotion)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    if not argv or argv[0] not in {"snapshot", "cleanup", "publish-health"}:
        print(
            json.dumps(
                {
                    "event": "phone_backup_failed",
                    "error": "first argument must be 'snapshot', 'cleanup', or 'publish-health'",
                }
            ),
            file=sys.stderr,
        )
        return 2
    subcommand = argv[0]
    if subcommand == "snapshot":
        args = _snapshot_parser().parse_args(argv[1:])
        try:
            if args.run_id == "auto":
                run_id = bind_auto_run_id()
            else:
                _validate_run_id(args.run_id)
                run_id = args.run_id
            envelope = snapshot_pair(
                run_id=run_id,
                staging_root=Path(args.staging_root),
                ispindel_src=args.ispindel_src,
                brew_src=args.brew_src,
                produced_at=args.produced_at,
            )
        except HelperError as exc:
            print(json.dumps({"event": "phone_backup_failed", "error": str(exc)}), file=sys.stderr)
            return 2
        print(json.dumps({"event": "phone_snapshot_ok", "run_id": envelope["shared_run_id"]}))
        return 0
    if subcommand == "cleanup":
        args = _cleanup_parser().parse_args(argv[1:])
        try:
            cleanup_staging(staging_root=Path(args.staging_root), run_id=args.run_id)
        except HelperError as exc:
            print(json.dumps({"event": "phone_backup_cleanup_failed", "error": str(exc)}), file=sys.stderr)
            return 2
        return 0
    if subcommand == "publish-health":
        args = _publish_parser().parse_args(argv[1:])
        try:
            payload_obj = json.loads(args.payload_json)
            if not isinstance(payload_obj, dict):
                raise HelperError("payload must be a JSON object")
            publish_health(health_path=Path(args.health_path), payload=payload_obj)
        except (HelperError, json.JSONDecodeError) as exc:
            print(json.dumps({"event": "phone_health_publish_failed", "error": str(exc)}), file=sys.stderr)
            return 2
        return 0
    print(json.dumps({"event": "phone_backup_failed", "error": f"unknown subcommand: {subcommand}"}), file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())