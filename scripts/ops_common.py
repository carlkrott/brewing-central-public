#!/usr/bin/env python3
"""Shared fail-closed primitives for iSpindel operational scripts."""
from __future__ import annotations

import json
import os
import re
import secrets
import stat
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

SCHEMA_VERSION = "health-evidence-v1"
RETENTION_IDENTITY_RE = re.compile(r"^[0-9]{8}T[0-9]{6}Z-[0-9a-f]{12}$")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + secrets.token_hex(4)


def atomic_write_json(path: Path, value: dict[str, Any], *, mode: int = 0o640) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp"
    payload = (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except Exception:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise


def terminal_event(
    event: str,
    run: str,
    started: float,
    outcome: str,
    *,
    error_class: str | None = None,
    **fields: Any,
) -> None:
    record: dict[str, Any] = {
        "event": event,
        "run_id": run,
        "duration_ms": max(0, round((time.monotonic() - started) * 1000)),
        "outcome": outcome,
    }
    if error_class:
        record["error_class"] = error_class
    record.update(fields)
    print(json.dumps(record, sort_keys=True, separators=(",", ":")), file=sys.stdout, flush=True)


class RetentionError(ValueError):
    """Fail-closed signal from the retention dry-run helper."""


def _validate_identity(identity: object) -> str:
    if not isinstance(identity, str) or not RETENTION_IDENTITY_RE.fullmatch(identity):
        raise RetentionError(f"malformed retention identity: {identity!r}")
    return identity


def retention_dry_run(
    root: Path,
    retained: Iterable[str],
    candidates: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """List candidate-retention outcomes without mutating the filesystem.

    Each candidate entry must declare ``identity`` and ``path``. The path
    is constrained to an exact direct child of ``root`` (no symlinks, no
    traversal, no nested children). Identities must match the release-id
    pattern. Entries whose identity appears in ``retained`` are reported
    with ``status="retained"``; everything else is reported with
    ``status="stale"``. The helper returns records only — it never
    deletes, renames, chmods, signals, runs subprocesses, or scans
    outside ``root``.
    """
    if root.is_symlink() or not root.is_dir():
        raise RetentionError(f"candidate root is not a regular directory: {root}")
    resolved_root = root.resolve()
    authorised: set[str] = set()
    for value in retained:
        authorised.add(_validate_identity(value))

    records: list[dict[str, Any]] = []
    for entry in candidates:
        if not isinstance(entry, Mapping):
            raise RetentionError("candidate entry must be a mapping")
        identity = _validate_identity(entry.get("identity"))
        raw_path = entry.get("path")
        if not isinstance(raw_path, str) or not raw_path:
            raise RetentionError(f"candidate path must be a non-empty string: {entry!r}")
        if "\\" in raw_path or "\x00" in raw_path:
            raise RetentionError(f"non-portable candidate path: {raw_path!r}")
        relative = Path(raw_path)
        if relative.is_absolute():
            raise RetentionError(f"candidate path must be relative: {raw_path!r}")
        if any(part in {"", ".", ".."} for part in relative.parts):
            raise RetentionError(f"candidate path escapes root: {raw_path!r}")
        if len(relative.parts) != 1:
            raise RetentionError(f"candidate path must be a direct child: {raw_path!r}")
        if relative.name != identity:
            raise RetentionError(
                f"candidate identity does not match directory name: {identity!r} != {raw_path!r}"
            )
        child = resolved_root / relative
        try:
            info = child.lstat()
        except FileNotFoundError as exc:
            raise RetentionError(f"candidate is absent: {raw_path}") from exc
        if stat.S_ISLNK(info.st_mode):
            raise RetentionError(f"candidate symlink is forbidden: {raw_path}")
        if not stat.S_ISDIR(info.st_mode):
            raise RetentionError(f"candidate is not a directory: {raw_path}")
        try:
            child.resolve().relative_to(resolved_root)
        except ValueError as exc:
            raise RetentionError(f"candidate escapes root: {raw_path}") from exc
        records.append({
            "identity": identity,
            "path": relative.as_posix(),
            "status": "retained" if identity in authorised else "stale",
        })
    records.sort(key=lambda record: (record["status"] != "stale", record["path"]))
    return records
