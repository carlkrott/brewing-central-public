#!/usr/bin/env python3
"""Restore a verified backup into nonce-only Docker resources and prove it serves.

Single mode (v1) is preserved unchanged: ``--manifest`` points at one
single-database manifest, the rehearsal restores it into nonce-only isolated
Docker resources and probes the live app.

Dual mode (W6) is enabled with ``--paired``. The rehearsal restores both
databases (ispindel + brew) into the same nonce-only isolated volume,
probes the real app at /health/live, /health/ready, /api/devices,
/api/recipes, and a search query that exercises research_documents_fts
when the route supports it, then cross-checks every application-owned
brew data-table count recorded in the paired manifest. Production fingerprint
and cleanup gates remain.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import secrets
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Sequence

from backup_common import (
    BREW_TABLES,
    BackupError,
    atomic_json,
    bind_basename,
    bind_brew_basename,
    resolve_manifest_index,
    validate_dual_generation,
    validate_generation,
)

PRODUCTION_CONTAINER = "ispindel-dashboard"
PRODUCTION_VOLUME = "ispindel-dashboard_ispindel-data"
SUCCESS = {"status": "ok", "service": "ispindel-dashboard"}


def docker(*args: str, required: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(["docker", *args], text=True, capture_output=True, check=False)
    if required and result.returncode:
        raise BackupError(f"docker {args[0]} failed ({result.returncode}): {(result.stderr or '')[-2000:]}")
    return result


def _stable_inspect(value: object, kind: str) -> object:
    if not isinstance(value, dict):
        return value
    if kind == "container":
        # Mounts is Docker's daemon-resolved runtime view and can flap while
        # unrelated disposable resources are created. HostConfig retains the
        # declared bind/volume configuration and is the stable identity check.
        keys = ("Id", "Image", "Name", "Config", "HostConfig")
    elif kind == "volume":
        keys = ("Name", "Driver", "Mountpoint", "Labels", "Options", "Scope")
    else:
        keys = tuple(sorted(value))
    return {key: value.get(key) for key in keys}


def inspect_fingerprint(kind: str, name: str) -> str | None:
    result = docker(kind, "inspect", name, required=False)
    if result.returncode:
        return None
    try:
        inspected = json.loads(result.stdout)
        if not isinstance(inspected, list) or len(inspected) != 1:
            raise ValueError("Docker inspect did not return one object")
        stable = _stable_inspect(inspected[0], kind)
        encoded = json.dumps(stable, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError, json.JSONDecodeError):
        encoded = result.stdout
    return hashlib.sha256(encoded.encode()).hexdigest()


def get_json(url: str) -> tuple[int, object]:
    with urllib.request.urlopen(url, timeout=3) as response:
        return response.status, json.load(response)


def _read_optional_json(url: str) -> tuple[int, object] | None:
    try:
        return get_json(url)
    except (urllib.error.HTTPError, urllib.error.URLError, json.JSONDecodeError, OSError):
        return None


def exact_manifest(args: argparse.Namespace) -> Path:
    if bool(args.manifest) == bool(args.manifest_index):
        raise BackupError("choose exactly one of --manifest or --manifest-index")
    if args.paired and args.manifest:
        # For paired mode we accept the directory containing both manifests.
        return args.manifest.resolve()
    manifest = args.manifest if args.manifest else resolve_manifest_index(args.manifest_index)
    if args.paired:
        return manifest.resolve()
    return manifest.resolve()


def execute(args: argparse.Namespace) -> dict[str, object]:
    if args.paired:
        return _execute_paired(args)
    return _execute_single(args)


def _execute_single(args: argparse.Namespace) -> dict[str, object]:
    manifest_path = exact_manifest(args)
    manifest = validate_generation(manifest_path)
    if docker("image", "inspect", args.image, required=False).returncode:
        raise BackupError("candidate image is not present locally; pulling is forbidden")
    nonce = args.nonce or f"{os.getpid()}-{secrets.token_hex(5)}"
    if any(ch not in "0123456789abcdefghijklmnopqrstuvwxyz-" for ch in nonce.lower()):
        raise BackupError("unsafe rehearsal nonce")
    volume = f"ispindel-rehearsal-volume-{nonce}"
    container = f"ispindel-rehearsal-container-{nonce}"
    network = f"ispindel-rehearsal-network-{nonce}"
    helper = f"ispindel-rehearsal-helper-{nonce}"
    forbidden = {args.production_container, args.production_volume, PRODUCTION_CONTAINER, PRODUCTION_VOLUME}
    if {volume, container, network, helper} & forbidden:
        raise BackupError("rehearsal resource collides with a production identifier")
    before = {
        "container": inspect_fingerprint("container", args.production_container),
        "volume": inspect_fingerprint("volume", args.production_volume),
    }
    created = {"volume": False, "network": False, "container": False}
    cleanup: dict[str, bool] = {}
    started = time.monotonic()
    failure: Exception | None = None
    result: dict[str, object] = {}
    try:
        docker("volume", "create", volume)
        created["volume"] = True
        docker("network", "create", network)
        created["network"] = True
        basename = str(manifest["basename"])
        restore_code = (
            "import os,sqlite3,sys;"
            "s=sqlite3.connect('file:/backup/'+sys.argv[1]+'?mode=ro',uri=True);"
            "d=sqlite3.connect('/data/.restore.sqlitecopy');s.backup(d);d.close();s.close();"
            "os.chown('/data',10001,10001);os.chmod('/data/.restore.sqlitecopy',0o600);"
            "os.chown('/data/.restore.sqlitecopy',10001,10001);"
            "os.replace('/data/.restore.sqlitecopy','/data/ispindel.db')"
        )
        docker(
            "run", "--pull", "never", "--rm", "--name", helper,
            "--network", "none", "--user", "0:0", "--entrypoint", "python3",
            "-v", f"{manifest_path.parent}:/backup:ro", "-v", f"{volume}:/data",
            args.image, "-c", restore_code, basename,
        )
        docker(
            "run", "--pull", "never", "-d", "--name", container,
            "--network", network, "--user", "10001:10001", "--read-only",
            "--tmpfs", "/tmp:size=64m,mode=1777", "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges:true", "-e", "ISPINDEL_MODE=test",
            "-e", "SQLITE_PATH=/data/ispindel.db", "-v", f"{volume}:/data",
            "-p", "127.0.0.1::8098", args.image,
        )
        created["container"] = True
        deadline = time.monotonic() + args.timeout
        port = ""
        while time.monotonic() < deadline:
            state_result = docker("inspect", container)
            state = json.loads(state_result.stdout)[0]
            if state.get("State", {}).get("Status") == "exited":
                logs = docker("logs", container, required=False)
                raise BackupError(f"candidate exited during rehearsal: {logs.stdout[-2000:]}{logs.stderr[-2000:]}")
            health = state.get("State", {}).get("Health", {}).get("Status")
            if health == "healthy":
                port = docker("port", container, "8098/tcp").stdout.strip()
                break
            time.sleep(0.25)
        if not port.startswith("127.0.0.1:"):
            raise BackupError("candidate did not become healthy on a loopback random port")
        base = f"http://{port}"
        probes: dict[str, object] = {}
        for path in ("/health/live", "/health/ready", "/api/devices"):
            status, body = get_json(base + path)
            if status != 200:
                raise BackupError(f"rehearsal probe failed: {path} status={status}")
            if path.startswith("/health") and body != SUCCESS:
                raise BackupError(f"unexpected health body for {path}: {body!r}")
            probes[path] = {"status": status}
        with urllib.request.urlopen(base + "/", timeout=3) as response:
            html = response.read().decode("utf-8", errors="replace")
        if response.status != 200 or "iSpindel" not in html:
            raise BackupError("served dashboard marker is absent")
        probes["/"] = {"status": 200, "marker": "iSpindel"}
        result = {
            "schema": "ispindel-restore-rehearsal/v1",
            "result": "PASS",
            "completed_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "manifest": str(manifest_path),
            "run_id": manifest["run_id"],
            "image": args.image,
            "resources": {"volume": volume, "container": container, "network": network, "helper": helper},
            "probes": probes,
            "duration_seconds": round(time.monotonic() - started, 3),
        }
    except Exception as exc:
        failure = exc
    finally:
        if created["container"]:
            docker("rm", "-f", container, required=False)
        if created["network"]:
            docker("network", "rm", network, required=False)
        if created["volume"]:
            docker("volume", "rm", "-f", volume, required=False)
        cleanup = {
            "container_absent": docker("container", "inspect", container, required=False).returncode != 0,
            "helper_absent": docker("container", "inspect", helper, required=False).returncode != 0,
            "network_absent": docker("network", "inspect", network, required=False).returncode != 0,
            "volume_absent": docker("volume", "inspect", volume, required=False).returncode != 0,
        }
    after = {
        "container": inspect_fingerprint("container", args.production_container),
        "volume": inspect_fingerprint("volume", args.production_volume),
    }
    unchanged = before == after
    if failure is not None:
        raise BackupError(f"rehearsal failed: {failure}; cleanup={cleanup}") from failure
    if not all(cleanup.values()):
        raise BackupError(f"rehearsal cleanup leaked resources: {cleanup}")
    if not unchanged:
        raise BackupError("production container/volume metadata changed during rehearsal")
    result["cleanup"] = cleanup
    result["production_unchanged"] = True
    args.evidence_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    output = args.evidence_dir / f"REHEARSAL-{manifest['run_id']}.json"
    atomic_json(output, result)
    result["evidence"] = str(output)
    return result


def _execute_paired(args: argparse.Namespace) -> dict[str, object]:
    manifest_path = exact_manifest(args)
    # Paired target directory must contain both manifest.json and brew-manifest.json.
    envelope = validate_dual_generation(manifest_path.parent)
    run_id = str(envelope["shared_run_id"])
    if docker("image", "inspect", args.image, required=False).returncode:
        raise BackupError("candidate image is not present locally; pulling is forbidden")
    nonce = args.nonce or f"{os.getpid()}-{secrets.token_hex(5)}"
    if any(ch not in "0123456789abcdefghijklmnopqrstuvwxyz-" for ch in nonce.lower()):
        raise BackupError("unsafe rehearsal nonce")
    volume = f"ispindel-rehearsal-volume-{nonce}"
    container = f"ispindel-rehearsal-container-{nonce}"
    network = f"ispindel-rehearsal-network-{nonce}"
    helper = f"ispindel-rehearsal-helper-{nonce}"
    forbidden = {args.production_container, args.production_volume, PRODUCTION_CONTAINER, PRODUCTION_VOLUME}
    if {volume, container, network, helper} & forbidden:
        raise BackupError("rehearsal resource collides with a production identifier")
    before = {
        "container": inspect_fingerprint("container", args.production_container),
        "volume": inspect_fingerprint("volume", args.production_volume),
    }
    created = {"volume": False, "network": False, "container": False}
    cleanup: dict[str, bool] = {}
    started = time.monotonic()
    failure: Exception | None = None
    result: dict[str, object] = {}
    try:
        docker("volume", "create", volume)
        created["volume"] = True
        docker("network", "create", network)
        created["network"] = True
        ispindel_basename = bind_basename(run_id)
        brew_basename = bind_brew_basename(run_id)
        # Paired restore code: snapshot both databases via sqlite3.Connection.backup()
        # inside the container, integrity-check each, then atomic per-database
        # replacement with sidecar cleanup. Container has network=none.
        restore_code = '''
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
    os.chown("/data", 10001, 10001)
    os.chmod(temporary, 0o600)
    os.chown(temporary, 10001, 10001)
    os.replace(temporary, target)

restore(sys.argv[1], "/data/ispindel.db")
restore(sys.argv[2], "/data/brew.db")
'''
        docker(
            "run", "--pull", "never", "--rm", "--name", helper,
            "--network", "none", "--user", "0:0", "--entrypoint", "python3",
            "-v", f"{manifest_path.parent}:/backup:ro", "-v", f"{volume}:/data",
            args.image, "-c", restore_code, ispindel_basename, brew_basename,
        )
        docker(
            "run", "--pull", "never", "-d", "--name", container,
            "--network", network, "--user", "10001:10001", "--read-only",
            "--tmpfs", "/tmp:size=64m,mode=1777", "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges:true", "-e", "ISPINDEL_MODE=test",
            "-e", "SQLITE_PATH=/data/ispindel.db", "-e", "BREW_SQLITE_PATH=/data/brew.db",
            "-v", f"{volume}:/data", "-p", "127.0.0.1::8098", args.image,
        )
        created["container"] = True
        deadline = time.monotonic() + args.timeout
        port = ""
        while time.monotonic() < deadline:
            state_result = docker("inspect", container)
            state = json.loads(state_result.stdout)[0]
            if state.get("State", {}).get("Status") == "exited":
                logs = docker("logs", container, required=False)
                raise BackupError(f"candidate exited during rehearsal: {logs.stdout[-2000:]}{logs.stderr[-2000:]}")
            health = state.get("State", {}).get("Health", {}).get("Status")
            if health == "healthy":
                port = docker("port", container, "8098/tcp").stdout.strip()
                break
            time.sleep(0.25)
        if not port.startswith("127.0.0.1:"):
            raise BackupError("candidate did not become healthy on a loopback random port")
        base = f"http://{port}"
        probes: dict[str, object] = {}
        for path in ("/health/live", "/health/ready", "/api/devices", "/api/recipes"):
            status, body = get_json(base + path)
            if status != 200:
                raise BackupError(f"rehearsal probe failed: {path} status={status}")
            if path.startswith("/health") and body != SUCCESS:
                raise BackupError(f"unexpected health body for {path}: {body!r}")
            probes[path] = {"status": status}
        # Probe a search route that exercises research_documents_fts (when supported).
        search_probes: dict[str, object] = {}
        for path in ("/api/research/search?q=alpha", "/api/research/search?q=test"):
            probed = _read_optional_json(base + path)
            if probed is not None:
                status, body = probed
                search_probes[path] = {"status": status, "rows": len(body) if isinstance(body, (list, tuple)) else None}
                if status == 200:
                    probes["research_documents_fts"] = search_probes[path]
        # Cross-check every application-owned brew table recorded in the paired
        # manifest against the restored rehearsal database.
        counts_check = _cross_check_brew_counts(args.image, volume, envelope["brew"]["database"]["counts"], nonce)
        with urllib.request.urlopen(base + "/", timeout=3) as response:
            html = response.read().decode("utf-8", errors="replace")
        if response.status != 200 or "iSpindel" not in html:
            raise BackupError("served dashboard marker is absent")
        probes["/"] = {"status": 200, "marker": "iSpindel"}
        result = {
            "schema": "ispindel-restore-rehearsal/v1",
            "result": "PASS",
            "completed_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "manifest": str(manifest_path),
            "run_id": run_id,
            "shared_run_id": run_id,
            "mode": "paired",
            "image": args.image,
            "resources": {"volume": volume, "container": container, "network": network, "helper": helper},
            "probes": probes,
            "search_probes": search_probes,
            "brew_counts_check": counts_check,
            "duration_seconds": round(time.monotonic() - started, 3),
        }
    except Exception as exc:
        failure = exc
    finally:
        if created["container"]:
            docker("rm", "-f", container, required=False)
        if created["network"]:
            docker("network", "rm", network, required=False)
        if created["volume"]:
            docker("volume", "rm", "-f", volume, required=False)
        cleanup = {
            "container_absent": docker("container", "inspect", container, required=False).returncode != 0,
            "helper_absent": docker("container", "inspect", helper, required=False).returncode != 0,
            "network_absent": docker("network", "inspect", network, required=False).returncode != 0,
            "volume_absent": docker("volume", "inspect", volume, required=False).returncode != 0,
        }
    after = {
        "container": inspect_fingerprint("container", args.production_container),
        "volume": inspect_fingerprint("volume", args.production_volume),
    }
    unchanged = before == after
    if failure is not None:
        raise BackupError(f"rehearsal failed: {failure}; cleanup={cleanup}") from failure
    if not all(cleanup.values()):
        raise BackupError(f"rehearsal cleanup leaked resources: {cleanup}")
    if not unchanged:
        raise BackupError("production container/volume metadata changed during rehearsal")
    result["cleanup"] = cleanup
    result["production_unchanged"] = True
    args.evidence_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    output = args.evidence_dir / f"REHEARSAL-PAIRED-{run_id}.json"
    atomic_json(output, result)
    result["evidence"] = str(output)
    return result


def _cross_check_brew_counts(image: str, volume: str, expected: dict[str, object], nonce: str) -> dict[str, object]:
    """Open the rehearse volume's brew.db via a one-shot helper and compare counts.

    The table names are restricted to the canonical application allowlist; the
    expected counts come from the paired manifest's brew database block.
    """
    helper = f"ispindel-rehearsal-counts-{nonce}"
    tables = tuple(table for table in BREW_TABLES if isinstance(expected.get(table), int))
    if not tables:
        raise BackupError("paired manifest contains no canonical brew table counts")
    code = '''
import json
import sqlite3
import sys

con = sqlite3.connect("/data/brew.db")
out = {}
for table in json.loads(sys.argv[1]):
    present = con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    out[table] = int(con.execute("SELECT COUNT(*) FROM \\\"" + table + "\\\"").fetchone()[0]) if present else 0
con.close()
print("BREW_COUNTS_JSON=" + json.dumps(out, sort_keys=True))
'''
    proc = docker(
        "run", "--pull", "never", "--rm", "--name", helper,
        "--network", "none", "--user", "0:0", "--entrypoint", "python3",
        "-v", f"{volume}:/data", image, "-c", code, json.dumps(tables),
    )
    observed: dict[str, int] = {}
    for line in proc.stdout.splitlines():
        if line.startswith("BREW_COUNTS_JSON="):
            observed = json.loads(line.split("=", 1)[1])
            break
    diffs: dict[str, dict[str, object]] = {}
    for table in tables:
        expected_count = expected[table]
        if observed.get(table) != expected_count:
            diffs[table] = {"expected": expected_count, "observed": observed.get(table)}
    if diffs:
        raise BackupError(f"brew counts mismatch against paired manifest: {diffs}")
    return {"expected": expected, "observed": observed}


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description="Disposable restore rehearsal from one exact manifest or index binding.")
    source = value.add_mutually_exclusive_group(required=True)
    source.add_argument("--manifest", type=Path, help="Exact manifest.json path; no glob/latest.")
    source.add_argument("--manifest-index", type=Path, help="Exact index.json whose absolute manifest binding is verified.")
    value.add_argument("--paired", action="store_true", help="Restore both ispindel.db and brew.db into the rehearsal volume under one RUN_ID.")
    value.add_argument("--image", required=True, help="Existing image reference; Docker always uses --pull never.")
    value.add_argument("--evidence-dir", type=Path, required=True)
    value.add_argument("--timeout", type=float, default=75.0)
    value.add_argument("--nonce", help="Test-only deterministic nonce.")
    value.add_argument("--production-container", default=PRODUCTION_CONTAINER)
    value.add_argument("--production-volume", default=PRODUCTION_VOLUME)
    return value


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        result = execute(args)
    except (BackupError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"event": "restore_rehearsal_failed", "error": str(exc)[:2000]}), file=sys.stderr)
        return 1
    print(json.dumps({"event": "restore_rehearsal_complete", **result}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
