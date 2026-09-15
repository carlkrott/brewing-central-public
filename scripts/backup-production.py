#!/usr/bin/env python3
"""Take one verified SQLite backup and promote an independently verified off-host copy.

Single mode (v1) is preserved unchanged: ``--source-db`` snapshots the legacy
ispindel database; no brew artifacts are required.

Dual mode (W6) is enabled with ``--include-brew``. The one RUN_ID binds both
database artifacts and manifests in one generation directory. The off-host
copy is staged into ``<run_id>.partial`` and only promoted to the final
``<run_id>`` after BOTH manifests have been independently verified. The
local index.json is updated with the new ``databases`` map; legacy fields
like ``latest_verified_manifest`` are preserved so old single generations
remain valid. ``backup_health.json`` is written only after both off-host
verification and promotion succeed.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import shlex
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Sequence

from backup_common import (
    BACKUP_HEALTH_NAME,
    BackupError,
    DUAL_SCHEMA,
    apply_retention,
    atomic_json,
    bind_basename,
    bind_brew_basename,
    bind_run_id,
    build_dual_manifest,
    build_manifest,
    require_capacity,
    update_index_for_dual_generation,
    validate_dual_generation,
    validate_generation,
    write_backup_health_if_verified,
)

CONTAINER_ISPINDEL_DB = "/data/ispindel.db"
CONTAINER_BREW_DB = "/data/brew.db"
CONTAINER_DATA = "/data"

REMOTE_VERIFY_SINGLE = r'''
import hashlib,json,pathlib,sqlite3,sys
m=pathlib.Path(sys.argv[1]); d=json.loads(m.read_text()); p=m.parent/d["basename"]
assert m.name=="manifest.json" and p.is_file() and not p.is_symlink()
h=hashlib.sha256()
with p.open("rb") as f:
  for b in iter(lambda:f.read(1048576),b""): h.update(b)
assert p.stat().st_size==d["file"]["size"] and h.hexdigest()==d["file"]["sha256"]
c=sqlite3.connect(p.resolve().as_uri()+"?mode=ro",uri=True); c.execute("PRAGMA query_only=ON")
assert [r[0] for r in c.execute("PRAGMA integrity_check")]==["ok"]
assert int(c.execute("PRAGMA user_version").fetchone()[0])==d["database"]["user_version"]
for t,n in d["database"]["counts"].items():
  present=c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(t,)).fetchone()
  actual=int(c.execute('SELECT COUNT(*) FROM "'+t+'"').fetchone()[0]) if present else 0
  assert actual==n
c.close(); print("REMOTE_BACKUP_VERIFIED")
'''
REMOTE_VERIFY_DUAL = r'''
import hashlib,json,pathlib,sqlite3,sys
root=pathlib.Path(sys.argv[1])
def verify(basename,manifest_name,prefix):
    m=root/manifest_name
    d=json.loads(m.read_text())
    p=root/basename
    assert m.is_file() and not m.is_symlink()
    assert p.is_file() and not p.is_symlink()
    assert d["run_id"] == root.name or root.name.endswith(".partial")
    assert d["basename"] == basename and basename.startswith(prefix+"-")
    h=hashlib.sha256()
    with p.open("rb") as f:
        for b in iter(lambda:f.read(1048576),b""): h.update(b)
    assert p.stat().st_size==d["file"]["size"] and h.hexdigest()==d["file"]["sha256"]
    c=sqlite3.connect(p.resolve().as_uri()+"?mode=ro",uri=True); c.execute("PRAGMA query_only=ON")
    assert [r[0] for r in c.execute("PRAGMA integrity_check")]==["ok"]
    assert int(c.execute("PRAGMA user_version").fetchone()[0])==d["database"]["user_version"]
    for t,n in d["database"]["counts"].items():
        present=c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(t,)).fetchone()
        actual=int(c.execute('SELECT COUNT(*) FROM "'+t+'"').fetchone()[0]) if present else 0
        assert actual==n
    c.close()
verify(sys.argv[2], "manifest.json", "ispindel")
verify(sys.argv[3], "brew-manifest.json", "brew")
primary=json.loads((root/"manifest.json").read_text())
brew=json.loads((root/"brew-manifest.json").read_text())
assert primary["run_id"]==brew["run_id"]
assert primary.get("paired",{}).get("shared_run_id")==primary["run_id"]
assert brew.get("paired",{}).get("shared_run_id")==primary["run_id"]
for child in root.iterdir():
    assert not child.is_symlink()
    assert child.name in {"manifest.json","brew-manifest.json",sys.argv[2],sys.argv[3]}
print("REMOTE_DUAL_BACKUP_VERIFIED")
'''
REMOTE_RETENTION_SINGLE = r'''
import datetime as dt,hashlib,json,pathlib,shutil,sqlite3,sys
root=pathlib.Path(sys.argv[1]); rolling=int(sys.argv[2]); weeks=int(sys.argv[3])
def valid(p):
  try:
    m=p/"manifest.json"; d=json.loads(m.read_text()); rid=d["run_id"]; db=p/d["basename"]
    assert p.name==rid and d["basename"]=="ispindel-"+rid+".db" and db.is_file() and not db.is_symlink()
    h=hashlib.sha256()
    with db.open("rb") as f:
      for b in iter(lambda:f.read(1048576),b""): h.update(b)
    assert db.stat().st_size==d["file"]["size"] and h.hexdigest()==d["file"]["sha256"]
    c=sqlite3.connect(db.resolve().as_uri()+"?mode=ro",uri=True); c.execute("PRAGMA query_only=ON")
    assert [r[0] for r in c.execute("PRAGMA integrity_check")]==["ok"]
    assert int(c.execute("PRAGMA user_version").fetchone()[0])==d["database"]["user_version"]
    for t,n in d["database"]["counts"].items():
      present=c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(t,)).fetchone()
      actual=int(c.execute('SELECT COUNT(*) FROM "'+t+'"').fetchone()[0]) if present else 0
      assert actual==n
    c.close(); return rid,d
  except Exception: return None
items=[]
for p in root.iterdir() if root.exists() else []:
  if p.is_dir() and not p.is_symlink() and not p.name.endswith(".partial"):
    v=valid(p)
    if v: items.append((v[0],p,v[1]))
items.sort(key=lambda x:x[0]); keep={x[0] for x in items[-rolling:]}; cutoff=dt.datetime.now(dt.timezone.utc)-dt.timedelta(weeks=weeks); seen=set()
for rid,p,d in reversed(items):
  try: when=dt.datetime.fromisoformat(d["produced_at"].replace("Z","+00:00"))
  except Exception: continue
  key=when.isocalendar()[:2]
  if when>=cutoff and key not in seen: seen.add(key); keep.add(rid)
deleted=[]
for rid,p,d in items:
  if rid not in keep: shutil.rmtree(p); deleted.append(rid)
print(json.dumps({"result":"REMOTE_RETENTION_OK","verified":len(items),"kept":len(items)-len(deleted),"deleted":len(deleted),"weekly":len(seen)},sort_keys=True))
'''
REMOTE_RETENTION_DUAL = r'''
import datetime as dt,hashlib,json,pathlib,shutil,sqlite3,sys
root=pathlib.Path(sys.argv[1]); rolling=int(sys.argv[2]); weeks=int(sys.argv[3])
def verify_pair(p):
    try:
        primary=p/"manifest.json"; brew=p/"brew-manifest.json"
        if not (primary.is_file() and brew.is_file()): return None
        dp=json.loads(primary.read_text()); db=json.loads(brew.read_text())
        rid=dp["run_id"]
        assert rid==db["run_id"]==p.name
        for d,basename in ((dp,"ispindel-"+rid+".db"),(db,"brew-"+rid+".db")):
            dbpath=p/basename
            assert dbpath.is_file() and not dbpath.is_symlink()
            h=hashlib.sha256()
            with dbpath.open("rb") as f:
                for b in iter(lambda:f.read(1048576),b""): h.update(b)
            assert dbpath.stat().st_size==d["file"]["size"] and h.hexdigest()==d["file"]["sha256"]
            c=sqlite3.connect(dbpath.resolve().as_uri()+"?mode=ro",uri=True); c.execute("PRAGMA query_only=ON")
            assert [r[0] for r in c.execute("PRAGMA integrity_check")]==["ok"]
            assert int(c.execute("PRAGMA user_version").fetchone()[0])==d["database"]["user_version"]
            c.close()
        return rid,dp
    except Exception:
        return None
items=[]
for p in root.iterdir() if root.exists() else []:
    if p.is_dir() and not p.is_symlink() and not p.name.endswith(".partial"):
        v=verify_pair(p)
        if v: items.append((v[0],p,v[1]))
items.sort(key=lambda x:x[0]); keep={x[0] for x in items[-rolling:]}; cutoff=dt.datetime.now(dt.timezone.utc)-dt.timedelta(weeks=weeks); seen=set()
for rid,p,d in reversed(items):
    try: when=dt.datetime.fromisoformat(d["produced_at"].replace("Z","+00:00"))
    except Exception: continue
    key=when.isocalendar()[:2]
    if when>=cutoff and key not in seen: seen.add(key); keep.add(rid)
deleted=[]
for rid,p,d in items:
    if rid not in keep: shutil.rmtree(p); deleted.append(rid)
print(json.dumps({"result":"REMOTE_DUAL_RETENTION_OK","verified":len(items),"kept":len(items)-len(deleted),"deleted":len(deleted),"weekly":len(seen)},sort_keys=True))
'''


def event(name: str, **fields: object) -> None:
    print(json.dumps({"event": name, **fields}, sort_keys=True), flush=True)


def run(command: list[str]) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(command, text=True, capture_output=True, check=False)
    if result.returncode:
        stderr = (result.stderr or "")[-2000:]
        raise BackupError(f"command failed ({result.returncode}): {command[0]}: {stderr}")
    return result


def discover_volume(container: str) -> str:
    result = run(["docker", "inspect", container, "--format", "{{json .Mounts}}"])
    try:
        mounts = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise BackupError("docker inspect returned invalid mounts JSON") from exc
    matches = [
        item.get("Name") for item in mounts
        if isinstance(item, dict)
        and item.get("Type") == "volume"
        and item.get("Destination") == CONTAINER_DATA
        and isinstance(item.get("Name"), str)
        and item.get("Name")
    ]
    if len(matches) != 1:
        raise BackupError(f"expected exactly one /data named volume; got {len(matches)}")
    return str(matches[0])


def snapshot_sqlite(source: Path, destination: Path) -> None:
    if source.is_symlink() or not source.is_file():
        raise BackupError("fixture source database must be a regular file")
    wal_sidecar = source.with_name(f"{source.name}-wal")
    shm_sidecar = source.with_name(f"{source.name}-shm")
    has_sidecars = wal_sidecar.exists() or shm_sidecar.exists()
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = destination.with_name(f".{destination.name}.sqlitecopy-{os.getpid()}")
    source_copy_dir: Path | None = None
    source_for_sqlite = source
    if has_sidecars:
        source_copy_dir = destination.parent / f".{destination.name}.sourcecopy-{os.getpid()}"
        if source_copy_dir.exists() or source_copy_dir.is_symlink():
            raise BackupError("source database copy path already exists")
        try:
            source_copy_dir.mkdir(mode=0o700)
            for sidecar in (source, wal_sidecar, shm_sidecar):
                if not sidecar.exists():
                    continue
                if sidecar.is_symlink() or not sidecar.is_file():
                    raise BackupError("source database sidecar must be a regular file")
                copied = source_copy_dir / sidecar.name
                shutil.copyfile(sidecar, copied)
                os.chmod(copied, 0o600)
        except BaseException:
            if source_copy_dir.exists():
                shutil.rmtree(source_copy_dir)
            raise
        source_for_sqlite = source_copy_dir / source.name
    try:
        if has_sidecars:
            source_conn = sqlite3.connect(source_for_sqlite)
        else:
            source_conn = sqlite3.connect(
                source.resolve().as_uri() + "?mode=ro&immutable=1", uri=True,
            )
        target_conn = sqlite3.connect(temporary)
        try:
            source_conn.backup(target_conn)
            target_conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            target_conn.execute("PRAGMA journal_mode=DELETE")
        finally:
            target_conn.close()
            source_conn.close()
        os.chmod(temporary, 0o600)
        os.replace(temporary, destination)
    finally:
        if temporary.exists():
            temporary.unlink()
        if source_copy_dir is not None and source_copy_dir.exists():
            shutil.rmtree(source_copy_dir)


def snapshot_container(container: str, basename: str, destination: Path, container_db: str = CONTAINER_ISPINDEL_DB) -> str:
    volume = discover_volume(container)
    container_temp = f"/tmp/{basename}"
    code = (
        "import os,sqlite3,sys;"
        "s=sqlite3.connect('file:'+sys.argv[1]+'?mode=ro',uri=True);"
        "d=sqlite3.connect(sys.argv[2]);s.backup(d);"
        "d.execute('PRAGMA wal_checkpoint(TRUNCATE)');d.execute('PRAGMA journal_mode=DELETE');"
        "d.close();s.close();"
        "os.chmod(sys.argv[2],0o600)"
    )
    host_temp = destination.with_name(f".{destination.name}.containercopy-{os.getpid()}")
    stream_code = (
        "import shutil,sys;"
        "f=open(sys.argv[1],'rb');shutil.copyfileobj(f,sys.stdout.buffer);f.close()"
    )
    try:
        run(["docker", "exec", container, "python3", "-c", code, container_db, container_temp])
        with host_temp.open("xb") as output:
            os.chmod(host_temp, 0o600)
            result = subprocess.run(
                ["docker", "exec", container, "python3", "-c", stream_code, container_temp],
                stdout=output,
                stderr=subprocess.PIPE,
                check=False,
            )
        if result.returncode:
            error = result.stderr.decode("utf-8", errors="replace").strip()
            raise BackupError(f"container backup stream failed ({result.returncode}): {error}")
        os.replace(host_temp, destination)
    finally:
        if host_temp.exists():
            host_temp.unlink()
        subprocess.run(
            ["docker", "exec", container, "python3", "-c", "import os,sys; os.unlink(sys.argv[1]) if os.path.exists(sys.argv[1]) else None", container_temp],
            text=True,
            capture_output=True,
            check=False,
        )
    return volume


def copy_local(source: Path, offhost_root: Path, run_id: str) -> Path:
    """Stage a generation into ``<run_id>.partial``, verify, then promote.

    Returns the final path (the original ``<run_id>`` directory). If
    verification fails the partial is preserved as evidence and the caller
    raises without promotion. This is the canonical local off-host copy
    flow that the SSH off-host copy emulates.
    """
    partial = offhost_root / f"{run_id}.partial"
    final = offhost_root / run_id
    if partial.exists() or final.exists():
        raise BackupError("off-host partial/final generation already exists")
    offhost_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    shutil.copytree(source, partial)
    # Verify the single- or paired-layout before promoting. A dual primary
    # manifest is still schema v1 for legacy readers, so inspect ``paired``
    # explicitly instead of deciding solely by manifest filename.
    manifest_path = partial / "manifest.json"
    if manifest_path.is_file():
        try:
            raw = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise BackupError(f"off-host manifest is unreadable: {exc}") from exc
        if isinstance(raw, dict) and isinstance(raw.get("paired"), dict) and raw["paired"].get("enabled") is True:
            validate_dual_generation(partial, allow_partial=True)
        else:
            validate_generation(manifest_path)
    else:
        validate_dual_generation(partial)
    os.replace(partial, final)
    return final


def split_remote(target: str) -> tuple[str, str] | None:
    if ":" not in target or target.startswith("/"):
        return None
    host, root = target.split(":", 1)
    safe_host = host and not host.startswith("-") and all(
        char.isalnum() or char in ".@_-" for char in host
    )
    if not safe_host or not root.startswith("/"):
        raise BackupError("remote off-host target must be safe HOST:/absolute/path")
    return host, root.rstrip("/")


def ssh_command(host: str, arguments: list[str]) -> subprocess.CompletedProcess[str]:
    remote_command = " ".join(shlex.quote(value) for value in arguments)
    return run(["ssh", "-o", "BatchMode=yes", "--", host, remote_command])


def copy_remote_single(source: Path, target: str, run_id: str, basename: str) -> str:
    remote = split_remote(target)
    if remote is None:
        raise BackupError("copy_remote requires HOST:/absolute/path")
    host, root = remote
    partial = f"{root}/{run_id}.partial"
    final = f"{root}/{run_id}"
    guard = f"set -eu; test ! -e {shlex.quote(partial)}; test ! -e {shlex.quote(final)}; mkdir -p -m 700 {shlex.quote(partial)}"
    ssh_command(host, ["sh", "-c", guard])
    run(["rsync", "-a", "--partial", "--", f"{source}/", f"{host}:{partial}/"])
    try:
        ssh_command(host, ["python3", "-c", REMOTE_VERIFY_SINGLE, f"{partial}/manifest.json"])
        promote = f"set -eu; test ! -e {shlex.quote(final)}; mv -- {shlex.quote(partial)} {shlex.quote(final)}"
        ssh_command(host, ["sh", "-c", promote])
    except Exception:
        raise
    return f"{host}:{final}/manifest.json"


def copy_remote_dual(source: Path, target: str, run_id: str) -> str:
    remote = split_remote(target)
    if remote is None:
        raise BackupError("copy_remote requires HOST:/absolute/path")
    host, root = remote
    partial = f"{root}/{run_id}.partial"
    final = f"{root}/{run_id}"
    guard = f"set -eu; test ! -e {shlex.quote(partial)}; test ! -e {shlex.quote(final)}; mkdir -p -m 700 {shlex.quote(partial)}"
    ssh_command(host, ["sh", "-c", guard])
    run(["rsync", "-a", "--partial", "--", f"{source}/", f"{host}:{partial}/"])
    try:
        ssh_command(host, [
            "python3", "-c", REMOTE_VERIFY_DUAL, partial,
            f"ispindel-{run_id}.db", f"brew-{run_id}.db",
        ])
        promote = f"set -eu; test ! -e {shlex.quote(final)}; mv -- {shlex.quote(partial)} {shlex.quote(final)}"
        ssh_command(host, ["sh", "-c", promote])
    except Exception:
        raise
    return f"{host}:{final}"


def apply_remote_retention_single(target: str, rolling: int = 56, weekly_weeks: int = 12) -> dict[str, object]:
    remote = split_remote(target)
    if remote is None:
        raise BackupError("remote retention requires HOST:/absolute/path")
    host, root = remote
    result = ssh_command(
        host,
        ["python3", "-c", REMOTE_RETENTION_SINGLE, root, str(rolling), str(weekly_weeks)],
    )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise BackupError("remote retention returned invalid JSON") from exc
    if payload.get("result") != "REMOTE_RETENTION_OK":
        raise BackupError("remote retention did not acknowledge success")
    return payload


# Back-compat alias for callers that imported the legacy single-mode name.
apply_remote_retention = apply_remote_retention_single


def apply_remote_retention_dual(target: str, rolling: int = 56, weekly_weeks: int = 12) -> dict[str, object]:
    remote = split_remote(target)
    if remote is None:
        raise BackupError("remote retention requires HOST:/absolute/path")
    host, root = remote
    result = ssh_command(
        host,
        ["python3", "-c", REMOTE_RETENTION_DUAL, root, str(rolling), str(weekly_weeks)],
    )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise BackupError("remote retention returned invalid JSON") from exc
    if payload.get("result") != "REMOTE_DUAL_RETENTION_OK":
        raise BackupError("remote dual retention did not acknowledge success")
    return payload


def execute_backup(args: argparse.Namespace) -> dict[str, object]:
    run_id = args.run_id or bind_run_id()
    backup_root = args.backup_root.resolve()
    require_capacity(backup_root, args.min_free_bytes, args.min_free_inodes)
    run_dir = backup_root / run_id
    if run_dir.exists():
        raise BackupError("local generation already exists")
    run_dir.mkdir(mode=0o700)
    produced_at = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    include_brew = bool(getattr(args, "include_brew", False))
    if include_brew:
        ispindel_basename = bind_basename(run_id)
        brew_basename = bind_brew_basename(run_id)
        ispindel_db = run_dir / ispindel_basename
        brew_db = run_dir / brew_basename
        source: dict[str, object]
        if args.production:
            ispindel_volume = snapshot_container(args.container, ispindel_basename, ispindel_db, CONTAINER_ISPINDEL_DB)
            brew_volume = snapshot_container(args.container, brew_basename, brew_db, CONTAINER_BREW_DB)
            if ispindel_volume != brew_volume:
                raise BackupError(
                    f"ispindel and brew databases must share one /data named volume "
                    f"(got {ispindel_volume!r} vs {brew_volume!r})"
                )
            volume = ispindel_volume
            source = {
                "mode": "container-online-dual-backup",
                "container": args.container,
                "volume": volume,
                "databases": {
                    "ispindel": CONTAINER_ISPINDEL_DB,
                    "brew": CONTAINER_BREW_DB,
                },
            }
        elif args.source_db and args.source_brew_db:
            snapshot_sqlite(args.source_db, ispindel_db)
            snapshot_sqlite(args.source_brew_db, brew_db)
            source = {
                "mode": "fixture-online-dual-backup",
                "databases": {
                    "ispindel": str(args.source_db.resolve()),
                    "brew": str(args.source_brew_db.resolve()),
                },
            }
        elif args.source_db or args.source_brew_db:
            raise BackupError(
                "dual mode requires BOTH --source-db (ispindel) and --source-brew-db"
            )
        else:
            raise BackupError("execution requires --production or --source-db+--source-brew-db")

        envelope = build_dual_manifest(run_id, ispindel_db, brew_db, produced_at, source)
        primary_manifest_path = run_dir / "manifest.json"
        brew_manifest_path = run_dir / "brew-manifest.json"
        atomic_json(primary_manifest_path, envelope["primary"])
        atomic_json(brew_manifest_path, envelope["brew"])
        validate_dual_generation(run_dir)
    else:
        basename = bind_basename(run_id)
        db_path = run_dir / basename
        source_single: dict[str, object]
        if args.production:
            volume = snapshot_container(args.container, basename, db_path, CONTAINER_ISPINDEL_DB)
            source_single = {"mode": "container-online-backup", "container": args.container, "volume": volume, "database": CONTAINER_ISPINDEL_DB}
        elif args.source_db:
            snapshot_sqlite(args.source_db, db_path)
            source_single = {"mode": "fixture-online-backup", "database": str(args.source_db.resolve())}
        else:
            raise BackupError("execution requires --production or --source-db")
        manifest = build_manifest(run_id, db_path, produced_at, source_single)
        primary_manifest_path = run_dir / "manifest.json"
        atomic_json(primary_manifest_path, manifest)
        validate_generation(primary_manifest_path)

    # Off-host copy + verify + promote. State used by backup_health gating.
    state = {"verified": False, "promoted": False}
    remote = split_remote(args.offhost)
    if include_brew:
        if remote:
            offhost_pair_path = copy_remote_dual(run_dir, args.offhost, run_id)
            state["verified"] = True
            state["promoted"] = True
            remote_retention = apply_remote_retention_dual(args.offhost, 56, 12)
        else:
            offhost_dir_final = copy_local(run_dir, Path(args.offhost), run_id)
            state["verified"] = True
            state["promoted"] = True
            offhost_pair_path = str(offhost_dir_final)
            local_offhost_retention = apply_retention(Path(args.offhost), 56, 12)
            remote_retention = {
                "result": "LOCAL_DUAL_OFFHOST_RETENTION_OK",
                "verified": local_offhost_retention.verified,
                "kept": local_offhost_retention.kept,
                "deleted": local_offhost_retention.deleted,
                "weekly": local_offhost_retention.weekly_kept,
            }
    else:
        if remote:
            offhost_manifest = copy_remote_single(run_dir, args.offhost, run_id, basename)
            state["verified"] = True
            state["promoted"] = True
            remote_retention = apply_remote_retention_single(args.offhost, 56, 12)
            offhost_pair_path = str(Path(offhost_manifest).parent)
        else:
            offhost_dir_final = copy_local(run_dir, Path(args.offhost), run_id)
            state["verified"] = True
            state["promoted"] = True
            offhost_manifest = str((offhost_dir_final / "manifest.json").resolve())
            offhost_pair_path = str(offhost_dir_final)
            local_offhost_retention = apply_retention(Path(args.offhost), 56, 12)
            remote_retention = {
                "result": "LOCAL_OFFHOST_RETENTION_OK",
                "verified": local_offhost_retention.verified,
                "kept": local_offhost_retention.kept,
                "deleted": local_offhost_retention.deleted,
                "weekly": local_offhost_retention.weekly_kept,
            }

    # Retention starts only after exact off-host verification and promotion.
    apply_retention(backup_root, 28)

    if include_brew:
        index_payload = update_index_for_dual_generation(
            backup_root / "index.json",
            run_id,
            primary_manifest_path=primary_manifest_path,
            brew_manifest_path=brew_manifest_path,
            offhost_pair_path=offhost_pair_path,
        )
        index = index_payload
    else:
        index = {
            "schema": "ispindel-backup-index/v1",
            "updated_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "latest_verified_run_id": run_id,
            "latest_verified_manifest": str(primary_manifest_path.resolve()),
            "offhost_verified_manifest": offhost_manifest,
            "offhost_retention": remote_retention,
        }
        atomic_json(backup_root / "index.json", index)

    # backup_health.json is written ONLY after both off-host verification AND
    # promotion have succeeded. It MUST NOT be written on execute refusal,
    # partial-copy, or verification failure. The callable is checked at write
    # time so a subsequent failure cannot accidentally write it.
    health_payload = {
        "schema": "ispindel-backup-health/v1",
        "run_id": run_id,
        "verified_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "mode": "dual" if include_brew else "single",
        "offhost_path": offhost_pair_path,
        "remote_retention": remote_retention,
    }
    # Fail closed: if the health receipt itself cannot be committed, surface
    # that error rather than claiming a verified backup without health evidence.
    write_backup_health_if_verified(
        backup_root / BACKUP_HEALTH_NAME,
        lambda: bool(state["verified"] and state["promoted"]),
        payload=health_payload,
    )

    result: dict[str, object] = {
        "result": "DUAL_BACKUP_VERIFIED" if include_brew else "BACKUP_VERIFIED",
        "run_id": run_id,
        "manifest": str(primary_manifest_path),
        "offhost_pair_path": offhost_pair_path,
        "offhost_retention": remote_retention,
        "schema": DUAL_SCHEMA if include_brew else None,
    }
    if include_brew:
        result["brew_manifest"] = str(brew_manifest_path)
        result["basename"] = bind_basename(run_id)
        result["brew_basename"] = bind_brew_basename(run_id)
    else:
        result["basename"] = basename
        result["offhost_manifest"] = offhost_manifest
    event("backup_complete", **result)
    return result


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description="Verified online iSpindel backup; no live-file copy.")
    value.add_argument("--backup-root", type=Path, default=Path(os.getenv("ISPINDEL_BACKUP_ROOT", "/var/backups/ispindel-dashboard")))
    value.add_argument("--offhost", default=os.getenv("ISPINDEL_OFFHOST", "offhost.example.test:/path/to/ispindel-backups"))
    value.add_argument("--container", default=os.getenv("ISPINDEL_CONTAINER", "ispindel-dashboard"))
    value.add_argument("--source-db", type=Path, help="Source ispindel database for fixture mode.")
    value.add_argument("--source-brew-db", type=Path, help="Source brew database for dual fixture mode.")
    value.add_argument("--include-brew", action="store_true", help="Dual mode: snapshot both ispindel.db and brew.db under one RUN_ID.")
    value.add_argument("--run-id", help="Test/replay override; must bind the canonical basename.")
    value.add_argument("--min-free-bytes", type=int, default=int(os.getenv("ISPINDEL_MIN_FREE_BYTES", str(1024**3))))
    value.add_argument("--min-free-inodes", type=int, default=int(os.getenv("ISPINDEL_MIN_FREE_INODES", "1024")))
    value.add_argument("--production", action="store_true", help="Use the named running production container.")
    value.add_argument("--execute", action="store_true", help="Required mutation gate.")
    return value


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if not args.execute:
        event("backup_refused", reason="missing --execute")
        return 2
    if args.production and (args.source_db or args.source_brew_db):
        print(json.dumps({"event": "backup_failed", "error": "choose exactly one of --production or --source-db[+--source-brew-db]"}), file=sys.stderr)
        return 2
    if not args.production and not args.source_db:
        print(json.dumps({"event": "backup_failed", "error": "execution requires --production or --source-db"}), file=sys.stderr)
        return 2
    if args.include_brew and args.production:
        # Container dual mode requires the container to expose /data/brew.db.
        # We do not introspect here; the snapshot will fail closed if absent.
        pass
    if args.include_brew and not args.production and not args.source_brew_db:
        print(json.dumps({"event": "backup_failed", "error": "--include-brew in fixture mode requires --source-brew-db"}), file=sys.stderr)
        return 2
    try:
        execute_backup(args)
    except (BackupError, OSError) as exc:
        print(json.dumps({"event": "backup_failed", "error": str(exc)[:1000]}), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
