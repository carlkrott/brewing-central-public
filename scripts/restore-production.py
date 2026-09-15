#!/usr/bin/env python3
"""Explicitly authorized production database restore. Never called automatically.

Single mode (v1): ``--manifest`` points at the primary manifest.json.
Dual mode (W6): ``--paired`` switches to a paired contract that restores
both databases into the stopped production volume using pull-never, fresh
pre-restore backup, quiescence proof, exact run identifier confirmation,
and atomic per-database replacement with sidecar cleanup.

The old single manifest path is preserved; old single manifests remain
valid and restore the old one-database path.

This script must not catch broad Exception to hide errors; only the
narrow set of expected error categories is caught and surfaced.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import subprocess
import sys
from pathlib import Path
from typing import Sequence

from backup_common import (
    BackupError,
    atomic_json,
    bind_basename,
    bind_brew_basename,
    validate_dual_generation,
    validate_generation,
)


def docker(*args: str, required: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(["docker", *args], text=True, capture_output=True, check=False)
    if required and result.returncode:
        raise BackupError(f"docker {args[0]} failed ({result.returncode}): {(result.stderr or '')[-2000:]}")
    return result


def discover_stopped_volume(container: str) -> str:
    result = docker("inspect", container)
    payload = json.loads(result.stdout)
    if len(payload) != 1 or payload[0].get("State", {}).get("Running") is not False:
        raise BackupError("production container must exist and be stopped")
    matches = [
        item.get("Name") for item in payload[0].get("Mounts", [])
        if item.get("Type") == "volume" and item.get("Destination") == "/data" and item.get("Name")
    ]
    if len(matches) != 1:
        raise BackupError("production container must have exactly one /data named volume")
    return str(matches[0])


def load_quiescence(path: Path, container: str, run_id: str) -> dict[str, object]:
    try:
        proof = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BackupError(f"quiescence proof is invalid: {exc}") from exc
    expected = {
        "schema": "ispindel-quiescence-proof/v1",
        "container": container,
        "state": "stopped",
        "restore_run_id": run_id,
    }
    if not isinstance(proof, dict) or any(proof.get(key) != value for key, value in expected.items()):
        raise BackupError("quiescence proof does not bind the stopped container and target RUN_ID")
    try:
        observed = dt.datetime.fromisoformat(str(proof["observed_at"]).replace("Z", "+00:00"))
    except (KeyError, ValueError) as exc:
        raise BackupError("quiescence proof timestamp is invalid") from exc
    age = dt.datetime.now(dt.timezone.utc) - observed.astimezone(dt.timezone.utc)
    if age < dt.timedelta(0) or age > dt.timedelta(minutes=10):
        raise BackupError("quiescence proof is stale or future-dated")
    return proof


def require_production_manifest(manifest: dict[str, object], container: str, label: str) -> None:
    source = manifest.get("source")
    if not isinstance(source, dict):
        raise BackupError(f"{label} manifest source block is invalid")
    if source.get("mode") != "container-online-backup" or source.get("container") != container:
        raise BackupError(f"{label} manifest is not a production-container online backup")


def require_production_dual_source(source: dict[str, object], container: str) -> None:
    if source.get("mode") != "container-online-dual-backup" or source.get("container") != container:
        raise BackupError("paired target manifest is not a production-container dual online backup")


def _restore_single(args: argparse.Namespace, run_id: str, target: dict[str, object]) -> dict[str, object]:
    basename = str(target["basename"])
    helper = f"ispindel-production-restore-{run_id}"
    code = (
        "import os,sqlite3,sys;"
        "s=sqlite3.connect('file:/backup/'+sys.argv[1]+'?mode=ro',uri=True);"
        "d=sqlite3.connect('/data/.restore.sqlitecopy');s.backup(d);d.close();s.close();"
        "c=sqlite3.connect('/data/.restore.sqlitecopy');"
        "assert [r[0] for r in c.execute('PRAGMA integrity_check')]==['ok'];c.close();"
        "[os.unlink(p) for p in ['/data/ispindel.db-wal','/data/ispindel.db-shm','/data/ispindel.db-journal'] if os.path.exists(p)];"
        "os.chmod('/data/.restore.sqlitecopy',0o600);os.chown('/data/.restore.sqlitecopy',10001,10001);"
        "os.replace('/data/.restore.sqlitecopy','/data/ispindel.db')"
    )
    docker(
        "run", "--pull", "never", "--rm", "--name", helper,
        "--network", "none", "--user", "0:0", "--entrypoint", "python3",
        "-v", f"{args.manifest.resolve().parent}:/backup:ro", "-v", f"{_resolved_volume(args)}:/data",
        args.image, "-c", code, basename,
    )
    return {
        "schema": "ispindel-production-restore-receipt/v1",
        "result": "RESTORED",
        "restored_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "run_id": run_id,
        "manifest": str(args.manifest.resolve()),
        "pre_restore_manifest": str(args.pre_restore_manifest.resolve()),
        "container": args.container,
        "volume": _resolved_volume(args),
        "image": args.image,
        "mode": "single",
        "databases": {"ispindel": {"basename": basename}},
    }


def _restore_paired(args: argparse.Namespace, run_id: str, envelope: dict[str, object]) -> dict[str, object]:
    primary = envelope["primary"]
    brew = envelope["brew"]
    ispindel_basename = bind_basename(run_id)
    brew_basename = bind_brew_basename(run_id)
    helper = f"ispindel-production-restore-{run_id}"
    # Paired restore code: snapshot both databases via sqlite3.Connection.backup()
    # inside the container, run integrity_check, then atomically replace each
    # database file with sidecar cleanup (any -wal/-shm/-journal left from the
    # stopped production volume must be removed before the atomic replace).
    code = '''
import os
import sqlite3
import sys

def restore(basename, target):
    source = sqlite3.connect("file:/backup/" + basename + "?mode=ro", uri=True)
    temporary = "/data/.restore.sqlitecopy." + basename
    destination = sqlite3.connect(temporary)
    source.backup(destination)
    destination.close()
    source.close()
    checked = sqlite3.connect(temporary)
    assert [row[0] for row in checked.execute("PRAGMA integrity_check")] == ["ok"]
    checked.close()
    for sidecar in (target + "-wal", target + "-shm", target + "-journal"):
        if os.path.exists(sidecar):
            os.unlink(sidecar)
    os.chmod(temporary, 0o600)
    os.chown(temporary, 10001, 10001)
    os.replace(temporary, target)

restore(sys.argv[1], "/data/ispindel.db")
restore(sys.argv[2], "/data/brew.db")
'''
    docker(
        "run", "--pull", "never", "--rm", "--name", helper,
        "--network", "none", "--user", "0:0", "--entrypoint", "python3",
        "-v", f"{args.target_manifest.resolve().parent}:/backup:ro", "-v", f"{_resolved_volume(args)}:/data",
        args.image, "-c", code, ispindel_basename, brew_basename,
    )
    return {
        "schema": "ispindel-production-restore-receipt/v1",
        "result": "RESTORED_PAIRED",
        "restored_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "run_id": run_id,
        "manifest": str(args.target_manifest.resolve()),
        "pre_restore_manifest": str(args.pre_restore_manifest.resolve()),
        "container": args.container,
        "volume": _resolved_volume(args),
        "image": args.image,
        "mode": "paired",
        "databases": {
            "ispindel": {"basename": ispindel_basename, "manifest": str(args.target_manifest.resolve())},
            "brew": {"basename": brew_basename, "manifest": str((args.target_manifest.resolve().parent / "brew-manifest.json"))},
            "shared_run_id": run_id,
        },
    }


def _resolved_volume(args: argparse.Namespace) -> str:
    if getattr(args, "_resolved_volume", None) is None:
        setattr(args, "_resolved_volume", discover_stopped_volume(args.container))
    return getattr(args, "_resolved_volume")


def execute(args: argparse.Namespace) -> dict[str, object]:
    if getattr(args, "paired", False):
        return _execute_paired(args)
    return _execute_single(args)


def _execute_single(args: argparse.Namespace) -> dict[str, object]:
    target = validate_generation(args.manifest.resolve())
    run_id = str(target["run_id"])
    if args.confirm_production_restore != run_id:
        raise BackupError("--confirm-production-restore must exactly match target manifest RUN_ID")
    require_production_manifest(target, args.container, "target")
    pre = validate_generation(args.pre_restore_manifest.resolve())
    require_production_manifest(pre, args.container, "pre-restore")
    if pre["run_id"] == run_id:
        raise BackupError("pre-restore backup must be distinct from the restore target")
    try:
        produced = dt.datetime.fromisoformat(str(pre["produced_at"]).replace("Z", "+00:00"))
    except (KeyError, ValueError) as exc:
        raise BackupError("pre-restore backup timestamp is invalid") from exc
    age = dt.datetime.now(dt.timezone.utc) - produced.astimezone(dt.timezone.utc)
    if age < dt.timedelta(0) or age > dt.timedelta(hours=args.max_prebackup_age_hours):
        raise BackupError("pre-restore backup is not fresh enough")
    load_quiescence(args.quiescence_proof, args.container, run_id)
    discover_stopped_volume(args.container)
    if docker("image", "inspect", args.image, required=False).returncode:
        raise BackupError("restore image is not present locally; pulling is forbidden")
    receipt = _restore_single(args, run_id, target)
    atomic_json(args.receipt, receipt)
    return receipt


def _execute_paired(args: argparse.Namespace) -> dict[str, object]:
    # The paired target is the directory containing both manifest.json and brew-manifest.json.
    target_dir = args.target_manifest.resolve().parent
    envelope = validate_dual_generation(target_dir)
    run_id = str(envelope["shared_run_id"])
    if args.confirm_production_restore != run_id:
        raise BackupError("--confirm-production-restore must exactly match target RUN_ID")
    primary = envelope["primary"]
    require_production_dual_source(primary.get("source", {}), args.container)  # type: ignore[arg-type]
    pre_manifest_path = args.pre_restore_manifest.resolve()
    pre_dir = pre_manifest_path.parent
    if not (pre_dir / "brew-manifest.json").is_file():
        raise BackupError("paired pre-restore backup must contain brew-manifest.json")
    pre_envelope = validate_dual_generation(pre_dir)
    pre = pre_envelope["primary"]
    require_production_dual_source(pre.get("source", {}), args.container)  # type: ignore[arg-type]
    if pre["run_id"] == run_id:
        raise BackupError("pre-restore backup must be distinct from the restore target")
    try:
        produced = dt.datetime.fromisoformat(str(pre["produced_at"]).replace("Z", "+00:00"))
    except (KeyError, ValueError) as exc:
        raise BackupError("pre-restore backup timestamp is invalid") from exc
    age = dt.datetime.now(dt.timezone.utc) - produced.astimezone(dt.timezone.utc)
    if age < dt.timedelta(0) or age > dt.timedelta(hours=args.max_prebackup_age_hours):
        raise BackupError("pre-restore backup is not fresh enough")
    load_quiescence(args.quiescence_proof, args.container, run_id)
    discover_stopped_volume(args.container)
    if docker("image", "inspect", args.image, required=False).returncode:
        raise BackupError("restore image is not present locally; pulling is forbidden")
    receipt = _restore_paired(args, run_id, envelope)
    atomic_json(args.receipt, receipt)
    return receipt


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description="Human-authorized production restore; never automatic.")
    value.add_argument("--manifest", type=Path, help="Single-mode target manifest.json path.")
    value.add_argument("--target-manifest", type=Path, help="Paired-mode target primary manifest.json path (sibling brew-manifest.json must exist).")
    value.add_argument("--paired", action="store_true", help="Restore both ispindel.db and brew.db under one RUN_ID with atomic per-database replacement.")
    value.add_argument("--pre-restore-manifest", type=Path, required=True)
    value.add_argument("--quiescence-proof", type=Path, required=True)
    value.add_argument("--confirm-production-restore", required=True)
    value.add_argument("--container", default="ispindel-dashboard")
    value.add_argument("--image", required=True, help="Existing local image; Docker uses --pull never.")
    value.add_argument("--receipt", type=Path, required=True)
    value.add_argument("--max-prebackup-age-hours", type=float, default=1.0)
    value.add_argument("--execute", action="store_true", help="Required in addition to exact confirmation.")
    return value


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if not args.execute:
        print(json.dumps({"event": "production_restore_refused", "error": "missing --execute"}), file=sys.stderr)
        return 2
    if args.paired:
        if not args.target_manifest or args.manifest:
            print(json.dumps({"event": "production_restore_refused", "error": "paired mode requires --target-manifest and cannot use --manifest"}), file=sys.stderr)
            return 2
    elif not args.manifest or args.target_manifest:
        print(json.dumps({"event": "production_restore_refused", "error": "single mode requires --manifest and cannot use --target-manifest"}), file=sys.stderr)
        return 2
    try:
        receipt = execute(args)
    except (BackupError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"event": "production_restore_failed", "error": str(exc)[:2000]}), file=sys.stderr)
        return 1
    print(json.dumps({"event": "production_restore_complete", **receipt}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
