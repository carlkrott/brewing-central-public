#!/usr/bin/env python3
"""Fleet-side phone-aware backup coordinator.

The coordinator is invoked from the dedicated phone-backup systemd unit
``ops/systemd/ispindel-phone-backup.service`` (new unit; the legacy
deployment-host ``ops/systemd/ispindel-backup.service`` is NEVER touched by
this slice).

It owns the cross-machine protocol that:

  1. discovers / pins the exact ADB serial,
  2. invokes the phone-local helper (``$ROOT/current/ops/android/
     phone-backup-snapshot.py``) via ``adb -s SERIAL shell run-as
     com.termux`` so the helper runs inside the Termux app context
     and can read the private databases,
  4. exports the staged pair binary-safely via ``adb -s SERIAL
     exec-out run-as com.termux cat EXACT_PATH`` (NEVER text-mode
     ``adb shell cat`` or a text-mode file transfer),
  5. verifies the staged pair off-phone via ``backup_common``,
  6. atomically promotes ``<run_id>``,
  7. atomically publishes the ``ispindel-backup-health/v1`` receipt
     back to the phone's caller-configured health path via the
     helper's ``publish-health`` subcommand, and only THEN writes the
     local host copy.

The coordinator NEVER shells out to ``adb`` directly: every external
command goes through the injected ``adb_call`` boundary. The lockfile
serialises concurrent invocations on the same host using
``LOCK_NB`` with a finite deadline; the second invocation refuses
fail-closed instead of blocking indefinitely.
"""

from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path, PurePosixPath
from typing import Callable, Sequence

# Allow tests + systemd invocations to find the canonical helpers.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from backup_common import (  # noqa: E402 - intentional post-sys.path mutation
    BACKUP_HEALTH_NAME,
    BACKUP_HEALTH_SCHEMA,
    BackupError,
    atomic_json,
    bind_run_id,
    require_capacity,
    validate_dual_generation,
)

DEFAULT_LOCK = Path("/var/run/ispindel/phone-backup.lock")
DEFAULT_TIMEOUT = 600.0
DEFAULT_LOCK_WAIT_SECONDS = 5.0
DEFAULT_PHONE_HEALTH_RELATIVE = "data/backup_health.json"
PHONE_ROOT_DEFAULT = str(Path.home() / "brewing-central")
PHONE_STAGING_RELATIVE = "data/backup-staging"
PHONE_DBS_RELATIVE = ("data/ispindel.db", "data/brew.db")


class PhoneBackupError(RuntimeError):
    """A fleet-side phone backup coordinator contract violation."""


def _default_adb_call(argv: list[str], *, timeout: float) -> subprocess.CompletedProcess:
    """Real ADB boundary used by the systemd unit; tests inject their own.

    Convention: ``argv`` is the complete literal command and begins with
    exactly one ``"adb"`` element. The wrapper executes it unchanged.
    """
    if not argv or argv[0] != "adb":
        raise PhoneBackupError(
            f"adb_call argv must begin with 'adb' (got {argv!r})"
        )
    if len(argv) >= 2 and argv[1] == "adb":
        raise PhoneBackupError(
            f"adb_call argv would double-invoke adb (got {argv!r})"
        )
    return subprocess.run(
        argv,
        capture_output=True,
        text=False,
        timeout=timeout,
        check=False,
    )


def _validate_run_id(run_id: str) -> None:
    if not run_id or any(c not in "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz-_" for c in run_id):
        raise PhoneBackupError(f"unsafe run_id: {run_id!r}")


def _phone_child(phone_root: str, relative: str, *, label: str) -> str:
    """Resolve a configured relative path beneath the fixed phone root."""
    root = PurePosixPath(phone_root)
    child = PurePosixPath(relative)
    if not root.is_absolute():
        raise PhoneBackupError("phone_root must be absolute")
    if child.is_absolute() or not child.parts or any(
        part in {"", ".", ".."} for part in child.parts
    ):
        raise PhoneBackupError(f"{label} must be a confined relative path")
    return str(root / child)


def _assert_serial(argv: list[str], serial: str) -> None:
    if "-s" not in argv:
        raise PhoneBackupError(f"adb call missing -s serial injection: {argv!r}")
    serial_index = argv.index("-s") + 1
    if serial_index >= len(argv) or argv[serial_index] != serial:
        raise PhoneBackupError(
            f"adb call serial mismatch: expected {serial!r}, got {argv[serial_index]!r}"
        )


def _discover_serial(
    *,
    adb_call: Callable[..., subprocess.CompletedProcess],
    timeout: float,
) -> str:
    """Confirm the requested serial is the sole device-visible entry.

    This is intentionally strict: P3 binds the backup to the exact
    serial configured by the operator. Any drift aborts the backup.
    """
    result = adb_call(["adb", "devices", "-l"], timeout=timeout)
    if result.returncode != 0:
        raise PhoneBackupError(
            f"adb devices failed ({result.returncode}): "
            f"{(result.stderr or b'').decode('utf-8', 'replace').strip()}"
        )
    stdout = (result.stdout or b"").decode("utf-8", "replace")
    found: list[str] = []
    for line in stdout.splitlines():
        parts = line.split()
        if not parts or parts[0] == "List" or parts[0].startswith("*"):
            continue
        if len(parts) >= 2 and parts[1] == "device":
            found.append(parts[0])
    if len(found) != 1:
        raise PhoneBackupError(
            f"adb devices must show exactly one device for phone backup; "
            f"got {found!r}"
        )
    return found[0]


def _shq(value: str) -> str:
    """Shell-quote ``value`` for inclusion in an ``adb shell`` argument.

    Uses POSIX ``sh`` quoting (``'`` for the bulk, ``'\"'\"'`` to
    escape embedded single quotes) so paths with spaces, quotes, or
    unicode cannot break out of the intended argument.
    """
    import shlex

    if value == "":
        return "''"
    if not any(c in value for c in " \t\n\"'`$\\;|&<>(){}*?#[]!~"):
        return value
    return shlex.quote(value)


def _build_helper_subcommand_args(
    *,
    subcommand: str,
    python_path: str,
    helper_path: str,
    args: Sequence[str],
) -> list[str]:
    """Build the argv that goes inside the ``adb shell run-as`` body.

    The literal command run inside ``run-as`` is::

        $ROOT/venv/bin/python $ROOT/current/ops/android/phone-backup-snapshot.py
            <subcommand> <args...>

    and the whole body is passed as the last ``argv`` element to
    ``adb -s SERIAL shell``.
    """
    quoted_args = " ".join(_shq(part) for part in args)
    return [
        "shell",
        f"run-as com.termux {_shq(python_path)} "
        f"{_shq(helper_path)} {_shq(subcommand)} {quoted_args}".rstrip(),
    ]


def _exec_out_cat_argv(
    *,
    serial: str,
    remote_path: str,
    host_path: Path,
) -> list[str]:
    """Build the argv for a binary-safe ``adb exec-out run-as cat``.

    The remote path is the exact staged path inside ``$ROOT/data/``;
    the host path is the bounded temp file the helper writes into.
    Returns the argv WITHOUT a leading ``"adb"`` element — the
    injected boundary adds it exactly once.
    """
    return [
        "adb",
        "-s",
        serial,
        "exec-out",
        f"run-as com.termux cat {_shq(remote_path)}",
    ]


def _helper_subcommand_argv(
    *,
    serial: str,
    python_path: str,
    helper_path: str,
    subcommand: str,
    args: Sequence[str],
) -> list[str]:
    return [
        "adb",
        "-s",
        serial,
        *_build_helper_subcommand_args(
            subcommand=subcommand,
            python_path=python_path,
            helper_path=helper_path,
            args=args,
        ),
    ]


def _run_as_cleanup_argv(
    *,
    serial: str,
    python_path: str,
    helper_path: str,
    staging_root: str,
    run_id: str,
) -> list[str]:
    """Build the exact helper cleanup invocation for one run child."""
    return _helper_subcommand_argv(
        serial=serial,
        python_path=python_path,
        helper_path=helper_path,
        subcommand="cleanup",
        args=["--staging-root", staging_root, "--run-id", run_id],
    )


def _publish_health_argv(
    *,
    serial: str,
    python_path: str,
    helper_path: str,
    health_path: str,
    payload: dict[str, object],
) -> list[str]:
    """Build the argv that publishes the health receipt back to the
    phone via the helper's ``publish-health`` subcommand.
    """
    payload_json = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return [
        "adb",
        "-s",
        serial,
        "shell",
        (
            "run-as com.termux "
            f"{_shq(python_path)} {_shq(helper_path)} "
            "publish-health "
            f"{_shq('--health-path')} {_shq(health_path)} "
            f"{_shq('--payload-json')} {_shq(payload_json)}"
        ),
    ]


def _invoke_remote_snapshot(
    *,
    serial: str,
    python_path: str,
    helper_path: str,
    run_id: str,
    staging_root: str,
    ispindel_src: str,
    brew_src: str,
    adb_call: Callable[..., subprocess.CompletedProcess],
    timeout: float,
) -> None:
    argv = [
        "adb",
        "-s",
        serial,
        *_build_helper_subcommand_args(
            subcommand="snapshot",
            python_path=python_path,
            helper_path=helper_path,
            args=[
                "--run-id",
                run_id,
                "--staging-root",
                staging_root,
                "--ispindel-src",
                ispindel_src,
                "--brew-src",
                brew_src,
            ],
        ),
    ]
    _assert_serial(argv, serial)
    result = adb_call(argv, timeout=timeout)
    if result.returncode != 0:
        raise PhoneBackupError(
            f"phone snapshot invocation failed ({result.returncode}): "
            f"{(result.stderr or b'').decode('utf-8', 'replace').strip()}"
        )


def _pull_pair_exec_out(
    *,
    serial: str,
    run_id: str,
    staging_root: str,
    host_temp_dir: Path,
    adb_call: Callable[..., subprocess.CompletedProcess],
    timeout: float,
) -> None:
    """Export the staged pair binary-safely via ``adb exec-out``.

    Every file is fetched into a dedicated bounded temp file (mode
    ``0o600``) inside ``host_temp_dir``. The host temp dir's parent
    creates it with mode ``0o700`` so the staged bytes are visible
    only to the coordinator.
    """
    host_temp_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    for local_name, remote_name in (
        (PRIMARY_MANIFEST, PRIMARY_MANIFEST),
        (BREW_MANIFEST, BREW_MANIFEST),
        (f"{ISPINDEL_PREFIX}-{run_id}.db", f"{ISPINDEL_PREFIX}-{run_id}.db"),
        (f"{BREW_PREFIX}-{run_id}.db", f"{BREW_PREFIX}-{run_id}.db"),
    ):
        remote_path = f"{staging_root}/{run_id}/{remote_name}"
        host_path = host_temp_dir / local_name
        argv = _exec_out_cat_argv(
            serial=serial, remote_path=remote_path, host_path=host_path,
        )
        _assert_serial(argv, serial)
        result = adb_call(argv, timeout=timeout)
        if result.returncode != 0:
            raise PhoneBackupError(
                f"adb exec-out {remote_name} failed ({result.returncode}): "
                f"{(result.stderr or b'').decode('utf-8', 'replace').strip()}"
            )
        # ``exec-out run-as cat`` returns the file bytes (no newline
        # added) and we never trust the surrounding wrapper to do
        # text-mode conversion: dump the raw bytes into the bounded
        # host temp file.
        payload = result.stdout or b""
        with open(host_path, "wb") as handle:
            handle.write(payload)
        os.chmod(host_path, 0o600)


PRIMARY_MANIFEST = "manifest.json"
BREW_MANIFEST = "brew-manifest.json"
ISPINDEL_PREFIX = "ispindel"
BREW_PREFIX = "brew"


def _stage_under_offhost(
    *,
    offhost_root: Path,
    run_id: str,
) -> tuple[Path, Path]:
    """Return (partial, final) generation paths under ``offhost_root``.

    Both must be absent for a fresh run; a previous partial is a
    coordination failure and is reported fail-closed.
    """
    partial = offhost_root / f"{run_id}.partial"
    final = offhost_root / run_id
    if final.exists() or final.is_symlink():
        raise PhoneBackupError(f"off-host generation already exists: {final}")
    if partial.exists() or partial.is_symlink():
        raise PhoneBackupError(f"off-host partial generation already exists: {partial}")
    return partial, final


def _promote(
    *,
    host_temp_dir: Path,
    partial: Path,
    final: Path,
) -> None:
    """Copy the staged pair into ``partial`` and atomically rename it."""
    partial.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    shutil.copytree(host_temp_dir, partial)
    validate_dual_generation(partial, allow_partial=True)
    os.replace(partial, final)


def _acquire_lock(
    *,
    lock_path: Path,
    deadline_seconds: float,
) -> tuple[io.TextIOBase, Callable[[], None]] | None:
    """Try to acquire ``lock_path`` using ``LOCK_NB`` with a bounded deadline.

    Returns ``(handle, release)`` on success, ``None`` on timeout. The
    caller is responsible for calling ``release()`` from a ``finally``.
    """
    lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    deadline = time.monotonic() + deadline_seconds
    handle = open(lock_path, "w")
    try:
        while True:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return handle, lambda: _release_lock(handle)
            except OSError:
                if time.monotonic() >= deadline:
                    return None
                time.sleep(0.05)
    except BaseException:
        handle.close()
        raise


def _release_lock(handle: io.TextIOBase) -> None:
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


def run_phone_backup(
    serial: str,
    *,
    adb_call: Callable[..., subprocess.CompletedProcess],
    backup_root: Path,
    offhost_root: Path,
    run_id: str | None = None,
    min_free_bytes: int = 1024 * 1024 * 1024,
    min_free_inodes: int = 1024,
    timeout: float = DEFAULT_TIMEOUT,
    lock_path: Path | None = DEFAULT_LOCK,
    helper_path: str = (
        f"{PHONE_ROOT_DEFAULT}/current/ops/android/phone-backup-snapshot.py"
    ),
    phone_root: str = PHONE_ROOT_DEFAULT,
    staging_relative: str = PHONE_STAGING_RELATIVE,
    health_relative: str = DEFAULT_PHONE_HEALTH_RELATIVE,
    lock_wait_seconds: float = DEFAULT_LOCK_WAIT_SECONDS,
    produced_at: str | None = None,
) -> dict[str, object]:
    """Top-level coordinator entrypoint.

    Order is fixed and observable: lock → discover serial → snapshot
    on phone → exec-out export → off-host verify → atomic promote →
    publish health receipt back to phone → write local host copy.
    Any failure aborts the sequence before the health receipt is
    published or the host copy is written. The phone-side staging
    directory is removed after a successful export (scoped cleanup)
    and on a best-effort basis after a failure.
    """
    backup_root = Path(backup_root).resolve()
    offhost_root = Path(offhost_root).resolve()

    require_capacity(offhost_root, min_free_bytes, min_free_inodes)

    if run_id is None or run_id == "auto":
        run_id = bind_run_id()
    if not isinstance(run_id, str) or not run_id:
        raise PhoneBackupError("run_id must be a non-empty string")
    _validate_run_id(run_id)

    python_path = f"{phone_root}/venv/bin/python"
    staging_root = _phone_child(
        phone_root, staging_relative, label="staging_relative"
    )
    health_path = _phone_child(
        phone_root, health_relative, label="health_relative"
    )
    ispindel_src = f"{phone_root}/data/ispindel.db"
    brew_src = f"{phone_root}/data/brew.db"

    lock_acquired: tuple[object, Callable[[], None]] | None = None
    if lock_path is not None:
        lock_acquired = _acquire_lock(
            lock_path=Path(lock_path),
            deadline_seconds=lock_wait_seconds,
        )
        if lock_acquired is None:
            raise PhoneBackupError(
                f"failed to acquire phone backup lock within {lock_wait_seconds:.1f}s"
            )
    cleanup_succeeded = False
    snapshot_started = False
    host_temp_dir: Path | None = None
    promoted: Path | None = None
    try:
        observed = _discover_serial(adb_call=adb_call, timeout=timeout)
        if observed != serial:
            raise PhoneBackupError(
                f"serial mismatch: requested {serial!r}, adb shows {observed!r}"
            )
        _invoke_remote_snapshot(
            serial=serial,
            python_path=python_path,
            helper_path=helper_path,
            run_id=run_id,
            staging_root=staging_root,
            ispindel_src=ispindel_src,
            brew_src=brew_src,
            adb_call=adb_call,
            timeout=timeout,
        )
        snapshot_started = True

        with tempfile.TemporaryDirectory(
            prefix="phone-pull-", dir=offhost_root,
        ) as tmp:
            host_temp_dir = Path(tmp)
            _pull_pair_exec_out(
                serial=serial,
                run_id=run_id,
                staging_root=staging_root,
                host_temp_dir=host_temp_dir,
                adb_call=adb_call,
                timeout=timeout,
            )
            partial, final = _stage_under_offhost(
                offhost_root=offhost_root, run_id=run_id,
            )
            _promote(host_temp_dir=host_temp_dir, partial=partial, final=final)
            promoted = final

        # Scoped phone-staging cleanup AFTER successful export. The
        # helper performs no broad ``rm -rf``; the coordinator invokes
        # a python-scoped rmtree via ``run-as`` so the only directory
        # removed is the validated ``<staging_root>/<run_id>`` child.
        cleanup_argv = _run_as_cleanup_argv(
            serial=serial,
            python_path=python_path,
            helper_path=helper_path,
            staging_root=staging_root,
            run_id=run_id,
        )
        _assert_serial(cleanup_argv, serial)
        cleanup_result = adb_call(cleanup_argv, timeout=timeout)
        cleanup_succeeded = cleanup_result.returncode == 0

        # Health receipt publication: ONLY after promotion AND scoped
        # cleanup. The receipt is published atomically back to the
        # phone via the helper's ``publish-health`` subcommand.
        timestamp = produced_at or dt.datetime.now(dt.timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        health_payload = {
            "schema": BACKUP_HEALTH_SCHEMA,
            "run_id": run_id,
            "verified_at": timestamp,
            "mode": "dual",
            "offhost_path": str(promoted),
        }
        publish_argv = _publish_health_argv(
            serial=serial,
            python_path=python_path,
            helper_path=helper_path,
            health_path=health_path,
            payload=dict(health_payload),
        )
        _assert_serial(publish_argv, serial)
        publish_result = adb_call(publish_argv, timeout=timeout)
        if publish_result.returncode != 0:
            raise PhoneBackupError(
                f"phone health publish failed ({publish_result.returncode}): "
                f"{(publish_result.stderr or b'').decode('utf-8', 'replace').strip()}"
            )

        # Local host copy of the health receipt — written AFTER the
        # phone publication succeeded.
        local_health = backup_root / BACKUP_HEALTH_NAME
        atomic_json(local_health, health_payload, mode=0o644)

        return {
            "result": "PHONE_DUAL_BACKUP_VERIFIED",
            "run_id": run_id,
            "manifest": str(promoted / PRIMARY_MANIFEST),
            "offhost_path": str(promoted),
        }
    finally:
        if not cleanup_succeeded and snapshot_started:
            best_effort = _run_as_cleanup_argv(
                serial=serial,
                python_path=python_path,
                helper_path=helper_path,
                staging_root=staging_root,
                run_id=run_id,
            )
            _assert_serial(best_effort, serial)
            try:
                adb_call(best_effort, timeout=timeout)
            except (PhoneBackupError, Exception):  # noqa: BLE001
                pass
        if lock_acquired is not None:
            _, release = lock_acquired
            release()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--serial", required=True)
    parser.add_argument("--backup-root", type=Path, required=True)
    parser.add_argument("--offhost-root", type=Path, required=True)
    parser.add_argument(
        "--run-id",
        default="auto",
        help='Validated UTC run id or the literal "auto" (default).',
    )
    parser.add_argument(
        "--min-free-bytes",
        type=int,
        default=int(os.getenv("ISPINDEL_MIN_FREE_BYTES", str(1024 ** 3))),
    )
    parser.add_argument(
        "--min-free-inodes",
        type=int,
        default=int(os.getenv("ISPINDEL_MIN_FREE_INODES", "1024")),
    )
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    parser.add_argument("--lock-path", type=Path, default=DEFAULT_LOCK)
    parser.add_argument(
        "--helper-path",
        default=f"{PHONE_ROOT_DEFAULT}/current/ops/android/phone-backup-snapshot.py",
    )
    parser.add_argument("--phone-root", default=PHONE_ROOT_DEFAULT)
    parser.add_argument(
        "--staging-relative",
        default=PHONE_STAGING_RELATIVE,
    )
    parser.add_argument(
        "--health-relative",
        default=DEFAULT_PHONE_HEALTH_RELATIVE,
    )
    parser.add_argument(
        "--lock-wait-seconds",
        type=float,
        default=DEFAULT_LOCK_WAIT_SECONDS,
    )
    parser.add_argument("--execute", action="store_true", help="Required mutation gate.")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if not args.execute:
        print(
            json.dumps({"event": "phone_backup_refused", "reason": "missing --execute"}),
            file=sys.stderr,
        )
        return 2
    try:
        result = run_phone_backup(
            args.serial,
            adb_call=_default_adb_call,
            backup_root=args.backup_root,
            offhost_root=args.offhost_root,
            run_id=args.run_id,
            min_free_bytes=args.min_free_bytes,
            min_free_inodes=args.min_free_inodes,
            timeout=args.timeout,
            lock_path=args.lock_path,
            helper_path=args.helper_path,
            phone_root=args.phone_root,
            staging_relative=args.staging_relative,
            health_relative=args.health_relative,
            lock_wait_seconds=args.lock_wait_seconds,
        )
    except (PhoneBackupError, BackupError) as exc:
        print(
            json.dumps({"event": "phone_backup_failed", "error": str(exc)[:1000]}),
            file=sys.stderr,
        )
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())