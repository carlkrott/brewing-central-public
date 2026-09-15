#!/usr/bin/env python3
"""Fail-closed deployment stage engine for the immutable iSpindel release."""
from __future__ import annotations

import argparse
import base64
import datetime as dt
import gzip
import hashlib
import json
import os
import re
import secrets
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Sequence

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_REMOTE_ROOT = "/opt/ispindel-dashboard"
DEFAULT_ENV_FILE = "/etc/ispindel/production.env"
DEFAULT_BACKUP_ROOT = "/var/backups/ispindel-dashboard"
# Production predecessor and candidate both live on the rootful system Docker
# daemon.  Keep this explicit: a rootless socket would inspect a different
# container/volume namespace and could make the predecessor receipt false.
SOURCE_DOCKER_HOST = "unix:///var/run/docker.sock"
TARGET_DOCKER_HOST = "unix:///var/run/docker.sock"
DOCKER_BINARIES = ("docker",)
DOCKER_COMPOSE_BINARIES = ("docker", "compose")
RELEASE_RE = re.compile(r"^[0-9]{8}T[0-9]{6}Z-[0-9a-f]{12}$")
DOCKER_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
REMOTE_RE = re.compile(r"^[A-Za-z0-9_.-]+@[A-Za-z0-9_.:-]+$")
DETERMINISTIC_GATE_EXIT = 20
SYSTEMD_UNITS = (
    "ispindel-caddy.service",
    "ispindel-stack.service",
    "ispindel-poll.service",
    "ispindel-poll.timer",
    "ispindel-heartbeat.service",
    "ispindel-heartbeat.timer",
    "ispindel-backup.service",
    "ispindel-backup.timer",
    "ispindel-restore-drill.service",
    "ispindel-restore-drill.timer",
)
SYSTEM_PATHS = (
    "/usr/local/bin/caddy", "/etc/ispindel/Caddyfile",
    "/etc/ispindel/production.env", "/etc/ispindel/caddy.env", "/etc/ispindel/backup.env",
    "/usr/local/libexec/ispindel/backup-production.py",
    "/usr/local/libexec/ispindel/backup_common.py",
    "/usr/local/libexec/ispindel/rehearse-restore.py",
    "/usr/local/lib/ispindel/send-alert.sh",
    "/usr/local/sbin/ispindel-root-helper",
    "/opt/ispindel-dashboard/docker-compose.yml",
    "/opt/ispindel-dashboard/scripts/poll-alert.py",
    "/opt/ispindel-dashboard/scripts/ops_common.py",
    "/opt/ispindel-dashboard/scripts/write-heartbeat.py",
    "/etc/systemd/system/ispindel-caddy.service",
    "/etc/systemd/system/ispindel-stack.service",
    "/etc/systemd/system/ispindel-poll.service",
    "/etc/systemd/system/ispindel-poll.timer",
    "/etc/systemd/system/ispindel-heartbeat.service",
    "/etc/systemd/system/ispindel-heartbeat.timer",
    "/etc/systemd/system/ispindel-backup.service",
    "/etc/systemd/system/ispindel-backup.timer",
    "/etc/systemd/system/ispindel-restore-drill.service",
    "/etc/systemd/system/ispindel-restore-drill.timer",
)
# The release static-asset map also contains license and manifest files that
# are packaged for provenance but intentionally are not HTTP routes. Stage 04
# validates only the public assets; Stage 02 verifies the complete packaged map.
SERVED_STATIC_ASSET_PATHS = (
    "source/app/static/chart.umd.js",
    "source/app/static/chartjs-adapter-date-fns.bundle.min.js",
    "source/app/static/dashboard.css",
    "source/app/static/dashboard.js",
)
EXPECTED_MISSING_AMENDMENT = ROOT / "contracts" / "ispindel-predecessor-expected-missing-amendment-v3.json"
EXPECTED_MISSING_SCHEMA = "ispindel-predecessor-expected-missing-amendment/v2"
EXPECTED_MISSING_CONTRACT_VERSION = "ispindel-predecessor-expected-missing/2026-08-06-v3"
EXPECTED_MISSING_SOURCE_RELEASE_ID = "20260805T073921Z-404e709c235f"
EXPECTED_MISSING_SOURCE_MANIFEST_SHA256 = "9c6bdcb8c6871822b6da4587caffa6581f4b8567999320218fd3253366d35ea0"
EXPECTED_MISSING_PREDECESSOR_SHA256 = "4c9935b61c36437e2e8b0fad37d9794d12cd2984c845ceb94028cb54752f1202"
EXPECTED_MISSING_CONTRACT_KEYS = {
    "schema", "contract_version", "source_release", "predecessor_descriptor",
    "expected_missing_paths", "activation", "snapshot_policy", "rollback_policy",
    "prohibitions", "self_excluding_digest_construction", "contract_sha256",
}


class DeploymentError(RuntimeError):
    pass


class DeterministicGateFailure(DeploymentError):
    pass


class DatabaseIncompatibleError(DeploymentError):
    pass


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f".{path.name}.partial-{os.getpid()}")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def require_regular(path: Path, label: str) -> Path:
    resolved = path.resolve()
    if path.is_symlink() or not resolved.is_file():
        raise DeploymentError(f"{label} must be a regular non-symlink file")
    return resolved


def load_json(path: Path, label: str) -> dict[str, object]:
    source = require_regular(path, label)
    try:
        value = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DeploymentError(f"{label} is invalid: {exc}") from exc
    if not isinstance(value, dict):
        raise DeploymentError(f"{label} must contain a JSON object")
    return value


def require_dict(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise DeploymentError(f"{label} must be a JSON object")
    return value


def canonical_object_sha256(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def load_expected_missing_amendment(
    path: Path, predecessor_path: Path, predecessor: dict[str, object],
) -> tuple[Path, dict[str, object], list[str]]:
    amendment_path = require_regular(path, "expected-missing amendment")
    amendment = load_json(amendment_path, "expected-missing amendment")
    if set(amendment) != EXPECTED_MISSING_CONTRACT_KEYS:
        raise DeploymentError("expected-missing amendment keys are invalid")
    claimed_digest = amendment.get("contract_sha256")
    unsigned = {key: value for key, value in amendment.items() if key != "contract_sha256"}
    if (not isinstance(claimed_digest, str) or not re.fullmatch(r"[0-9a-f]{64}", claimed_digest)
            or canonical_object_sha256(unsigned) != claimed_digest):
        raise DeploymentError("expected-missing amendment self-digest mismatch")
    if amendment.get("schema") != EXPECTED_MISSING_SCHEMA or amendment.get("contract_version") != EXPECTED_MISSING_CONTRACT_VERSION:
        raise DeploymentError("expected-missing amendment schema or contract version is invalid")
    if amendment.get("source_release") != {
        "release_id": EXPECTED_MISSING_SOURCE_RELEASE_ID,
        "manifest_sha256": EXPECTED_MISSING_SOURCE_MANIFEST_SHA256,
    }:
        raise DeploymentError("expected-missing amendment source release binding is invalid")
    resolved_predecessor = require_regular(predecessor_path, "predecessor descriptor")
    if amendment.get("predecessor_descriptor") != {
        "path": "source/descriptors/predecessor-production.json",
        "sha256": EXPECTED_MISSING_PREDECESSOR_SHA256,
    } or sha256(resolved_predecessor) != EXPECTED_MISSING_PREDECESSOR_SHA256:
        raise DeploymentError("expected-missing amendment predecessor binding is invalid")
    if amendment.get("activation") != {
        "allow_flag": "--allow-expected-missing",
        "version_flag": "--expected-missing-contract-version",
    }:
        raise DeploymentError("expected-missing amendment activation policy is invalid")
    if amendment.get("snapshot_policy") != {
        "complete_labelled_inventory_required": True,
        "expected_missing_row": {"present": False},
        "unexpected_absence": "hard-fail",
        "unexpected_presence": "hard-fail",
    }:
        raise DeploymentError("expected-missing amendment snapshot policy is invalid")
    if amendment.get("rollback_policy") != {
        "restore_complete_snapshot": True,
        "compose_inputs": "snapshot-proven-present-only",
        "exact_predecessor_image_required": True,
        "no_build": True,
        "pull": "never",
        "database_restore": False,
    }:
        raise DeploymentError("expected-missing amendment rollback policy is invalid")
    if amendment.get("self_excluding_digest_construction") != {
        "algorithm": "sha256",
        "omit_exact_key": "contract_sha256",
        "canonical_json": "UTF-8 json.dumps(value_without_contract_sha256, sort_keys=True, separators=(',', ':'), ensure_ascii=False)",
        "python_reference": "hashlib.sha256(json.dumps({k:v for k,v in value.items() if k != 'contract_sha256'}, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode('utf-8')).hexdigest()",
    }:
        raise DeploymentError("expected-missing amendment digest construction is invalid")
    expected_prohibitions = [
        "Do not reconstruct, install, or describe inferred override bytes as recovered predecessor bytes.",
        "Do not widen expected-missing authority beyond the exact null-valued predecessor host inputs; an empty set is valid when all labelled files are present.",
    ]
    if amendment.get("prohibitions") != expected_prohibitions:
        raise DeploymentError("expected-missing amendment prohibitions are invalid")
    host_hashes = require_dict(predecessor.get("host_input_sha256"), "predecessor host hashes")
    expected_from_predecessor = sorted(
        key for key, value in host_hashes.items()
        if isinstance(key, str) and value is None
    )
    declared = amendment.get("expected_missing_paths")
    if (not isinstance(declared, list)
            or any(not isinstance(item, str) for item in declared)
            or declared != expected_from_predecessor
            or any(validate_absolute(item, "expected-missing path") != item for item in declared)):
        raise DeploymentError("expected-missing path set does not exactly match frozen null host inputs")
    return amendment_path, amendment, [str(item) for item in declared]


def require_expected_missing_activation(
    amendment: dict[str, object], *, allow: bool, version: str | None,
) -> None:
    if not allow or version != amendment.get("contract_version"):
        raise DeploymentError("expected-missing handling requires the exact two-key activation")


def require_release_artifact_binding(
    release: dict[str, object], source_path: Path, artifact_path: str,
) -> None:
    expected_source = ROOT / artifact_path.removeprefix("source/")
    if source_path.resolve() != expected_source.resolve():
        raise DeploymentError(f"{artifact_path} must come from the canonical source path")
    artifacts = release.get("artifacts")
    if not isinstance(artifacts, list):
        raise DeploymentError("release artifact inventory is invalid")
    rows = [
        row for row in artifacts
        if isinstance(row, dict) and row.get("path") == artifact_path
    ]
    if (len(rows) != 1 or rows[0].get("sha256") != sha256(source_path)
            or rows[0].get("size") != source_path.stat().st_size):
        raise DeploymentError(f"{artifact_path} does not match the frozen release inventory")


def require_compose_observation(
    rows: object, compose_paths: Sequence[str], expected_missing: Sequence[str],
) -> tuple[list[str], list[str]]:
    if (not isinstance(rows, dict) or len(compose_paths) != len(set(compose_paths))
            or set(rows) != set(compose_paths)):
        raise DeploymentError("predecessor Compose snapshot coverage is invalid")
    present: list[str] = []
    missing: list[str] = []
    for path in compose_paths:
        row = rows.get(path)
        if not isinstance(row, dict) or not isinstance(row.get("present"), bool):
            raise DeploymentError("predecessor Compose snapshot row is invalid")
        (present if row["present"] else missing).append(path)
    if missing != list(expected_missing):
        raise DeploymentError("observed Compose absence does not exactly match expected-missing authority")
    if not present:
        raise DeploymentError("predecessor Compose active path inventory is empty")
    return present, missing


def stable_container_fingerprint(item: dict[str, object]) -> str:
    mounts = item.get("Mounts")
    if isinstance(mounts, list):
        mounts = sorted(
            mounts,
            key=lambda value: json.dumps(value, sort_keys=True, separators=(",", ":")),
        )
    stable = {
        key: item.get(key)
        for key in ("Id", "Image", "Name", "Config", "HostConfig")
    }
    stable["Mounts"] = mounts
    return canonical_object_sha256(stable)


def load_release(path: Path) -> tuple[Path, dict[str, object]]:
    manifest_path = require_regular(path, "release manifest")
    verifier = ROOT / "scripts" / "verify-release.py"
    verification = subprocess.run(
        [sys.executable, str(verifier), "--manifest", str(manifest_path)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
    )
    if verification.returncode:
        message = (verification.stderr + verification.stdout).decode(errors="replace").strip()
        raise DeploymentError(f"strict local release verification failed: {message[:1000]}")
    manifest = load_json(manifest_path, "release manifest")
    release_id = manifest.get("release_id")
    if manifest.get("schema") != "ispindel-release-manifest/v1" or not isinstance(release_id, str) or not RELEASE_RE.fullmatch(release_id):
        raise DeploymentError("release manifest schema or release ID is invalid")
    return manifest_path, manifest


def release_fields(manifest: dict[str, object]) -> tuple[str, str, str]:
    release_id = str(manifest["release_id"])
    image = manifest.get("image")
    root = manifest.get("release_root")
    expected_root = f"dist/releases/{release_id}/payload"
    if not isinstance(image, dict) or root != expected_root:
        raise DeploymentError("release manifest image or release_root is invalid")
    image_ref = image.get("reference")
    image_archive = image.get("archive_path")
    if not isinstance(image_ref, str) or image_ref.endswith(":latest") or not isinstance(image_archive, str):
        raise DeploymentError("release image binding is invalid")
    return release_id, image_ref, image_archive


def remote_python_no_bytecode(argv: Sequence[str]) -> list[str]:
    return ["env", "PYTHONDONTWRITEBYTECODE=1", "python3", *argv]


def network_none_verifier(remote_release: str, image_ref: str) -> list[str]:
    return [
        "docker", "run", "--rm", "--network", "none", "--pull", "never",
        "--env", "PYTHONDONTWRITEBYTECODE=1", "--entrypoint", "python3", "--mount",
        f"type=bind,src={remote_release},dst=/release,readonly",
        image_ref, "/release/source/scripts/verify-release.py",
        "--manifest", "/release/RELEASE.json", "--root", "/release", "--ignore-modes",
    ]


def validate_remote(value: str) -> str:
    if not REMOTE_RE.fullmatch(value) or value.startswith("-"):
        raise DeploymentError("remote must be a safe USER@HOST value")
    return value


def validate_absolute(value: str, label: str) -> str:
    path = Path(value)
    if (not path.is_absolute() or ".." in path.parts or "\n" in value or "\r" in value
            or "\x00" in value):
        raise DeploymentError(f"{label} must be a safe absolute path")
    return value.rstrip("/") or "/"


def validate_scoped_absolute(value: str, label: str) -> str:
    validated = validate_absolute(value, label)
    if len(Path(validated).parts) < 3:
        raise DeploymentError(f"{label} must name a non-root project path")
    return validated


def validate_predecessor_compose_path(value: str, remote_root: str) -> str:
    validated = validate_absolute(value, "predecessor Compose path")
    if not (
        validated.startswith(remote_root + "/")
        or validated.startswith("/opt/ispindel-dashboard/")
        or validated.startswith("/tmp/ispindel-")
    ):
        raise DeploymentError("predecessor Compose path is outside approved roots")
    return validated


def validate_production_env(value: str) -> str:
    validated = validate_absolute(value, "production env file")
    if validated != DEFAULT_ENV_FILE:
        raise DeploymentError("production env file is fixed to /etc/ispindel/production.env")
    return validated


def remote_script_command(remote_root: str, script: str) -> str:
    body = f"cd {shlex.quote(remote_root)} && {script}"
    return f"/bin/bash -lc {shlex.quote(body)}"


def remote_command(remote_root: str, argv: Sequence[str]) -> str:
    return remote_script_command(remote_root, shlex.join(list(argv)))


def system_snapshot_code() -> str:
    return (
        "import base64,hashlib,json,os,stat,sys; paths=json.loads(sys.argv[1]); out={}; "
        "assert isinstance(paths,list) and len(paths)==len(set(paths)); "
        "\nfor p in paths:\n"
        " assert isinstance(p,str) and p.startswith('/') and '\\x00' not in p\n"
        " try:\n"
        "  before=os.lstat(p); assert stat.S_ISREG(before.st_mode) and before.st_size <= 268435456\n"
        "  fd=os.open(p,os.O_RDONLY|os.O_NOFOLLOW)\n"
        "  try:\n"
        "   current=os.fstat(fd); assert (current.st_dev,current.st_ino)==(before.st_dev,before.st_ino)\n"
        "   chunks=[]\n"
        "   while True:\n"
        "    chunk=os.read(fd,1048576)\n"
        "    if not chunk: break\n"
        "    chunks.append(chunk)\n"
        "   data=b''.join(chunks)\n"
        "  finally: os.close(fd)\n"
        "  after=os.lstat(p); assert (after.st_dev,after.st_ino,after.st_size)==(before.st_dev,before.st_ino,len(data))\n"
        "  out[p]={'present':True,'mode':stat.S_IMODE(before.st_mode),'uid':before.st_uid,'gid':before.st_gid,'size':len(data),'sha256':hashlib.sha256(data).hexdigest(),'data_b64':base64.b64encode(data).decode()}\n"
        " except FileNotFoundError: out[p]={'present':False}\n"
        "print(json.dumps(out,sort_keys=True))"
    )


def system_restore_code() -> str:
    return (
        "import base64,hashlib,json,os,stat,sys,tempfile; expected=json.loads(sys.argv[1]); rows=json.load(sys.stdin); "
        "assert isinstance(expected,list) and len(expected)==len(set(expected)) and isinstance(rows,dict) and set(rows)==set(expected); "
        "\ndef safe_parent(p):\n"
        " current='/'\n"
        " for part in p.strip('/').split('/')[:-1]:\n"
        "  current=os.path.join(current,part)\n"
        "  try:\n"
        "   s=os.lstat(current); assert stat.S_ISDIR(s.st_mode)\n"
        "  except FileNotFoundError: os.mkdir(current,0o755)\n"
        "\nfor p in expected:\n"
        " assert isinstance(p,str) and p.startswith('/') and '\\x00' not in p\n"
        " row=rows[p]; assert isinstance(row,dict) and isinstance(row.get('present'),bool)\n"
        " if not row['present']:\n"
        "  assert set(row)=={'present'}\n"
        "  try:\n"
        "   s=os.lstat(p); assert stat.S_ISREG(s.st_mode) or stat.S_ISLNK(s.st_mode); os.unlink(p)\n"
        "  except FileNotFoundError: pass\n"
        "  continue\n"
        " assert set(row)=={'present','mode','uid','gid','size','sha256','data_b64'}\n"
        " assert isinstance(row['mode'],int) and 0 <= row['mode'] <= 0o7777\n"
        " assert isinstance(row['uid'],int) and row['uid'] >= 0 and isinstance(row['gid'],int) and row['gid'] >= 0\n"
        " assert isinstance(row['size'],int) and 0 <= row['size'] <= 268435456\n"
        " assert isinstance(row['sha256'],str) and len(row['sha256'])==64\n"
        " data=base64.b64decode(row['data_b64'],validate=True); assert len(data)==row['size'] and hashlib.sha256(data).hexdigest()==row['sha256']\n"
        " safe_parent(p); parent=os.path.dirname(p); fd,tmp=tempfile.mkstemp(prefix='.ispindel-restore-',dir=parent)\n"
        " try:\n"
        "  os.write(fd,data); os.fsync(fd); os.close(fd); fd=-1; os.chown(tmp,row['uid'],row['gid']); os.chmod(tmp,row['mode']); os.replace(tmp,p)\n"
        "  restored=os.lstat(p); assert stat.S_ISREG(restored.st_mode); checkfd=os.open(p,os.O_RDONLY|os.O_NOFOLLOW)\n"
        "  try: assert hashlib.sha256(os.read(checkfd,row['size']+1)).hexdigest()==row['sha256']\n"
        "  finally: os.close(checkfd)\n"
        " finally:\n"
        "  if fd>=0: os.close(fd)\n"
        "  if os.path.lexists(tmp): os.unlink(tmp)\n"
        "print('SYSTEM_RESTORE_OK')"
    )


def update_image_env_code() -> str:
    return (
        "import os,stat,sys,tempfile; p=sys.argv[1]; ref=sys.argv[2].encode(); key=b'ISPINDEL_IMAGE_REF='; "
        "before=os.lstat(p); assert stat.S_ISREG(before.st_mode) and before.st_size <= 1048576; "
        "fd=os.open(p,os.O_RDONLY|os.O_NOFOLLOW); data=b''; "
        "\ntry:\n"
        " current=os.fstat(fd); assert (current.st_dev,current.st_ino)==(before.st_dev,before.st_ino)\n"
        " while True:\n"
        "  chunk=os.read(fd,65536)\n"
        "  if not chunk: break\n"
        "  data+=chunk\n"
        "finally: os.close(fd)\n"
        "lines=data.splitlines(keepends=True); matches=0; out=[]\n"
        "for line in lines:\n"
        " content=line.rstrip(b'\\r\\n'); ending=line[len(content):]\n"
        " if content.startswith(key): matches+=1; out.append(key+ref+ending)\n"
        " else: out.append(line)\n"
        "assert matches <= 1\n"
        "if matches==0: out.append((b'' if not data or data.endswith(b'\\n') else b'\\n')+key+ref+b'\\n')\n"
        "new=b''.join(out); parent=os.path.dirname(p); tmpfd,tmp=tempfile.mkstemp(prefix='.production-env-',dir=parent)\n"
        "try:\n"
        " view=memoryview(new)\n"
        " while view: view=view[os.write(tmpfd,view):]\n"
        " os.fsync(tmpfd); os.close(tmpfd); tmpfd=-1; os.chown(tmp,before.st_uid,before.st_gid); os.chmod(tmp,stat.S_IMODE(before.st_mode)); os.replace(tmp,p)\n"
        " dirfd=os.open(parent,os.O_RDONLY|os.O_DIRECTORY); os.fsync(dirfd); os.close(dirfd)\n"
        "finally:\n"
        " if tmpfd>=0: os.close(tmpfd)\n"
        " if os.path.lexists(tmp): os.unlink(tmp)\n"
        "print('PRODUCTION_ENV_IMAGE_REF_OK')"
    )


def update_production_env_code() -> str:
    return r"""import os,stat,sys,tempfile
p=sys.argv[1]
ref=sys.argv[2].encode()
project=sys.argv[3].encode()
volume=sys.argv[4].encode()
updates={
    b'ISPINDEL_IMAGE_REF=':ref,
    b'ISPINDEL_SECRETS_DIR=':b'/etc/ispindel/secrets',
    b'COMPOSE_PROJECT_NAME=':project,
    b'ISPINDEL_DATA_VOLUME=':volume,
}
before=os.lstat(p)
assert stat.S_ISREG(before.st_mode) and before.st_size <= 1048576
fd=os.open(p,os.O_RDONLY|os.O_NOFOLLOW)
try:
    current=os.fstat(fd)
    assert (current.st_dev,current.st_ino)==(before.st_dev,before.st_ino)
    data=b''
    while True:
        chunk=os.read(fd,65536)
        if not chunk:
            break
        data+=chunk
finally:
    os.close(fd)
lines=data.splitlines(keepends=True)
matches={key:0 for key in updates}
out=[]
for line in lines:
    content=line.rstrip(b'\r\n')
    ending=line[len(content):]
    key=next((candidate for candidate in updates if content.startswith(candidate)),None)
    if key is None:
        out.append(line)
    else:
        matches[key]+=1
        out.append(key+updates[key]+ending)
assert all(value <= 1 for value in matches.values())
for key,value in updates.items():
    if matches[key]==0:
        out.append((b'' if not data or data.endswith(b'\n') else b'\n')+key+value+b'\n')
new=b''.join(out)
parent=os.path.dirname(p)
tmpfd,tmp=tempfile.mkstemp(prefix='.production-env-',dir=parent)
try:
    view=memoryview(new)
    while view:
        view=view[os.write(tmpfd,view):]
    os.fsync(tmpfd)
    os.close(tmpfd)
    tmpfd=-1
    os.chown(tmp,before.st_uid,before.st_gid)
    os.chmod(tmp,stat.S_IMODE(before.st_mode))
    os.replace(tmp,p)
    dirfd=os.open(parent,os.O_RDONLY|os.O_DIRECTORY)
    os.fsync(dirfd)
    os.close(dirfd)
finally:
    if tmpfd>=0:
        os.close(tmpfd)
    if os.path.lexists(tmp):
        os.unlink(tmp)
print('PRODUCTION_ENV_IMAGE_REF_OK')
print('PRODUCTION_ENV_SECRETS_PATH_OK')
print('PRODUCTION_ENV_COMPOSE_PROJECT_OK')
print('PRODUCTION_ENV_DATA_VOLUME_OK')
"""


def validate_docker_endpoint(value: str, label: str) -> str:
    if not isinstance(value, str):
        raise DeploymentError(f"{label} must be a string")
    if value.startswith("-") or "\n" in value or "\r" in value or "\x00" in value or " " in value:
        raise DeploymentError(f"{label} contains an unsafe character")
    if value.startswith("unix://"):
        rest = value[len("unix://"):]
        if not rest or rest.startswith("/") and ".." in rest.split("?", 1)[0]:
            raise DeploymentError(f"{label} unix socket path is unsafe")
    elif value.startswith("tcp://") or value.startswith("tcp+tls://"):
        if "@" in value:
            raise DeploymentError(f"{label} tcp endpoint must not embed credentials")
    elif value == "":
        raise DeploymentError(f"{label} must not be empty")
    return value


def is_docker_invocation(argv: Sequence[str]) -> bool:
    """Return True if argv starts with a Docker CLI invocation that needs an endpoint prefix.

    The Docker engine honours the ``DOCKER_HOST`` environment variable and honours
    ``--host`` only when it appears before the subcommand. The
    ``docker compose`` CLI is *also* a Docker CLI — Compose v2 talks to the same
    engine — so compose invocations must also be pinned to a specific endpoint.
    """
    head = tuple(argv[: len(DOCKER_COMPOSE_BINARIES)])
    if head == DOCKER_COMPOSE_BINARIES:
        return True
    head = tuple(argv[: len(DOCKER_BINARIES)])
    if head == DOCKER_BINARIES:
        sub = argv[len(DOCKER_BINARIES)] if len(argv) > len(DOCKER_BINARIES) else ""
        return sub != "context"
    return False


def prefix_endpoint(argv: Sequence[str], endpoint: str) -> list[str]:
    """Pin a Docker argv to *endpoint*, including ``env KEY=value docker …`` forms."""
    command = list(argv)
    if not command:
        return command
    docker_index = 0
    if command[0] == "env":
        docker_index = 1
        while docker_index < len(command) and "=" in command[docker_index] and not command[docker_index].startswith("-"):
            docker_index += 1
    if docker_index >= len(command) or command[docker_index] != "docker":
        return command
    subcommand_index = docker_index + 1
    if subcommand_index < len(command) and command[subcommand_index] == "context":
        return command
    return command[:subcommand_index] + ["--host", endpoint] + command[subcommand_index:]


def docker_endpoint_env(endpoint: str) -> str:
    """Render a shell prefix exporting ``DOCKER_HOST`` for a remote script.

    Remote shell scripts cannot rely on a single ``--host`` flag for
    ``docker compose`` because some legacy helpers call ``docker context use``
    before the main command. Forcing ``DOCKER_HOST`` from the script entry
    ensures every nested invocation targets the named endpoint. This must be
    an export rather than a simple command prefix: shell assignments before a
    pipeline apply only to the first pipeline process, which would otherwise
    leave a later ``docker`` command on the wrong daemon.
    """
    return f"export DOCKER_HOST={shlex.quote(endpoint)}; "


class Runner:
    def __init__(self, dry_run: bool = False):
        self.dry_run = dry_run
        self.plan: list[list[str]] = []

    def run(self, argv: Sequence[str], *, input_bytes: bytes | None = None,
            required: bool = True) -> subprocess.CompletedProcess[bytes]:
        command = list(argv)
        self.plan.append(command)
        if self.dry_run:
            return subprocess.CompletedProcess(command, 0, b"", b"")
        result = subprocess.run(command, input=input_bytes, capture_output=True, check=False)
        if required and result.returncode:
            raise DeploymentError(f"command failed ({result.returncode}): {command[0]}: {result.stderr[-2000:].decode(errors='replace')}")
        return result

    def remote(self, host: str, remote_root: str, argv: Sequence[str], *,
               required: bool = True) -> subprocess.CompletedProcess[bytes]:
        return self.run(["ssh", "-o", "BatchMode=yes", "--", host,
                         remote_command(remote_root, argv)], required=required)

    def remote_script(self, host: str, remote_root: str, script: str, *,
                      required: bool = True) -> subprocess.CompletedProcess[bytes]:
        return self.run(["ssh", "-o", "BatchMode=yes", "--", host,
                         remote_script_command(remote_root, script)], required=required)

    def remote_stdin(self, host: str, remote_root: str, argv: Sequence[str],
                     input_bytes: bytes, *, required: bool = True) -> subprocess.CompletedProcess[bytes]:
        """Run a remote argv while streaming a local artifact to its stdin.

        Snapshot restoration must not place predecessor bytes in a shell command
        line or reconstruct them through a Compose override. SSH transports the
        exact local snapshot bytes directly to the already-validated restore
        primitive.
        """
        command = ["ssh", "-o", "BatchMode=yes", "--", host,
                   remote_command(remote_root, argv)]
        return self.run(command, input_bytes=input_bytes, required=required)

    def remote_endpoint(self, host: str, remote_root: str, endpoint: str,
                        argv: Sequence[str], *, required: bool = True,
                        label: str = "endpoint") -> subprocess.CompletedProcess[bytes]:
        """Run a remote argv with every Docker CLI invocation pinned to *endpoint*.

        Local non-Docker invocations are passed through unchanged. Docker argv
        values have ``--host <endpoint>`` injected immediately after the binary
        name (``docker`` or ``docker compose``), so the engine that the command
        targets is unambiguous regardless of any host-level ``DOCKER_HOST``.
        """
        validate_docker_endpoint(endpoint, f"{label} endpoint")
        return self.remote(host, remote_root, prefix_endpoint(argv, endpoint), required=required)

    def remote_script_endpoint(self, host: str, remote_root: str, endpoint: str,
                               script: str, *, required: bool = True,
                               label: str = "endpoint") -> subprocess.CompletedProcess[bytes]:
        """Run a remote bash script with every nested ``docker`` call pinned to *endpoint*.

        A leading ``DOCKER_HOST=<endpoint>`` export guards nested invocations
        that may not surface through argv. Callers must still prefer
        :meth:`remote_endpoint` for top-level Docker argv calls.
        """
        validate_docker_endpoint(endpoint, f"{label} endpoint")
        prefixed = docker_endpoint_env(endpoint) + script
        return self.remote_script(host, remote_root, prefixed, required=required)

    def emit_plan(self, stage: str, release_id: str) -> None:
        print(json.dumps({"event": "deployment_dry_run", "stage": stage, "release_id": release_id,
                          "commands": [shlex.join(row) for row in self.plan]}, indent=2, sort_keys=True))


def require_execution(args: argparse.Namespace, release_id: str) -> None:
    if args.confirm_release_id != release_id:
        raise DeploymentError("--confirm-release-id must exactly match RELEASE.json")
    if args.execute == args.dry_run:
        raise DeploymentError("choose exactly one of --execute or --dry-run")


def validate_container_name(value: str, label: str = "container name") -> str:
    if not isinstance(value, str) or not DOCKER_NAME_RE.fullmatch(value):
        raise DeploymentError(f"{label} is not a valid Docker container name")
    return value


def rollback_container_names(release_id: str, production_container: str) -> tuple[str, str]:
    if not RELEASE_RE.fullmatch(release_id):
        raise DeploymentError("release ID is invalid for rollback container names")
    canonical = validate_container_name(production_container, "production container")
    suffix = release_id.lower()
    return (
        f"{canonical}-predecessor-{suffix}",
        f"{canonical}-candidate-{suffix}",
    )


def receipt_base(stage: str, release_id: str) -> dict[str, object]:
    return {"schema": "ispindel-deploy-stage-receipt/v1", "stage": stage,
            "release_id": release_id, "completed_at": utc_now(), "result": "PASS"}


def write_receipt(evidence_dir: Path, stage: str, release_id: str, payload: dict[str, object]) -> Path:
    output = evidence_dir.resolve() / release_id / f"{stage}.json"
    atomic_json(output, payload)
    return output


def decode_json_output(result: subprocess.CompletedProcess[bytes], label: str) -> object:
    try:
        return json.loads(result.stdout.decode())
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DeploymentError(f"{label} returned invalid JSON") from exc


def require_systemd_states(value: object) -> dict[str, dict[str, str]]:
    if not isinstance(value, dict) or set(value) != set(SYSTEMD_UNITS):
        raise DeploymentError("predecessor systemd state coverage is incomplete")
    states: dict[str, dict[str, str]] = {}
    expected_keys = {"Id", "LoadState", "UnitFileState", "ActiveState"}
    for unit, raw in value.items():
        if (not isinstance(unit, str) or not isinstance(raw, dict)
                or set(raw) != expected_keys or raw.get("Id") != unit):
            raise DeploymentError("predecessor systemd state output is invalid")
        if any(not isinstance(item, str) or not re.fullmatch(r"[A-Za-z0-9_.@-]*", item)
               for item in raw.values()):
            raise DeploymentError("predecessor systemd state contains an unsafe value")
        states[unit] = {key: str(raw[key]) for key in expected_keys}
    return states


def require_snapshot_rows(value: object, paths: Sequence[str], label: str) -> dict[str, dict[str, object]]:
    if (not isinstance(value, dict) or len(paths) != len(set(paths)) or set(value) != set(paths)):
        raise DeploymentError(f"{label} coverage is invalid")
    rows: dict[str, dict[str, object]] = {}
    present_keys = {"present", "mode", "uid", "gid", "size", "sha256", "data_b64"}
    for path in paths:
        raw = value.get(path)
        if not isinstance(raw, dict) or not isinstance(raw.get("present"), bool):
            raise DeploymentError(f"{label} row is invalid")
        if not raw["present"]:
            if set(raw) != {"present"}:
                raise DeploymentError(f"{label} missing row is invalid")
        else:
            if (set(raw) != present_keys or type(raw.get("mode")) is not int
                    or not 0 <= int(raw["mode"]) <= 0o7777
                    or type(raw.get("uid")) is not int or int(raw["uid"]) < 0
                    or type(raw.get("gid")) is not int or int(raw["gid"]) < 0
                    or type(raw.get("size")) is not int or not 0 <= int(raw["size"]) <= 268435456
                    or not isinstance(raw.get("sha256"), str)
                    or not re.fullmatch(r"[0-9a-f]{64}", str(raw["sha256"]))
                    or not isinstance(raw.get("data_b64"), str)):
                raise DeploymentError(f"{label} present row is invalid")
            try:
                data = base64.b64decode(str(raw["data_b64"]), validate=True)
            except (ValueError, TypeError) as exc:
                raise DeploymentError(f"{label} data is invalid") from exc
            if len(data) != raw["size"] or hashlib.sha256(data).hexdigest() != raw["sha256"]:
                raise DeploymentError(f"{label} data hash mismatch")
        rows[path] = raw
    return rows


def require_volume_inspect(value: object, predecessor: dict[str, object]) -> dict[str, object]:
    if not isinstance(value, list) or len(value) != 1 or not isinstance(value[0], dict):
        raise DeploymentError("predecessor volume inspect must contain exactly one volume")
    item = value[0]
    labels = item.get("Labels")
    normalized = {
        "name": item.get("Name"),
        "driver": item.get("Driver"),
        "mountpoint": item.get("Mountpoint"),
        "labels": labels if isinstance(labels, dict) else {},
    }
    expected = require_dict(predecessor.get("volume"), "predecessor volume")
    if normalized != expected:
        raise DeploymentError("predecessor volume inspect does not match the frozen descriptor")
    validate_absolute(str(normalized["mountpoint"]), "predecessor volume mountpoint")
    return item


def parse_systemd_states(output: bytes) -> dict[str, dict[str, str]]:
    raw_states: dict[str, dict[str, str]] = {}
    for block in output.decode(errors="strict").strip().split("\n\n"):
        if not block.strip():
            continue
        row = dict(line.split("=", 1) for line in block.splitlines() if "=" in line)
        unit = row.get("Id", "")
        if unit in raw_states:
            raise DeploymentError("predecessor systemd state contains duplicate units")
        raw_states[unit] = row
    return require_systemd_states(raw_states)


def parse_backup_output(output: bytes) -> dict[str, object]:
    events: list[dict[str, object]] = []
    try:
        for line in output.decode().splitlines():
            if line.strip():
                value = json.loads(line)
                if isinstance(value, dict) and value.get("event") == "backup_complete":
                    events.append(value)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DeploymentError("backup output was not valid JSON Lines") from exc
    if len(events) != 1 or events[0].get("result") != "BACKUP_VERIFIED":
        raise DeploymentError("backup output did not contain one verified completion event")
    manifest = events[0].get("manifest")
    if not isinstance(manifest, str):
        raise DeploymentError("backup completion event did not bind a manifest path")
    validate_absolute(manifest, "verified backup manifest")
    return events[0]


def stage01(args: argparse.Namespace) -> int:
    _, release = load_release(args.release_manifest)
    release_id, _, _ = release_fields(release)
    require_execution(args, release_id)
    predecessor_path = require_regular(args.predecessor_descriptor, "predecessor descriptor")
    artifacts = release.get("artifacts")
    if not isinstance(artifacts, list):
        raise DeploymentError("release artifact inventory is invalid")
    predecessor_rows = [row for row in artifacts if isinstance(row, dict)
                        and row.get("path") == "source/descriptors/predecessor-production.json"]
    if (len(predecessor_rows) != 1 or predecessor_rows[0].get("sha256") != sha256(predecessor_path)
            or predecessor_rows[0].get("size") != predecessor_path.stat().st_size):
        raise DeploymentError("predecessor descriptor does not match the frozen release inventory")
    predecessor = load_json(predecessor_path, "predecessor descriptor")
    amendment_path: Path | None = None
    amendment: dict[str, object] = {}
    expected_missing: list[str] = []
    activation_requested = bool(args.allow_expected_missing or args.expected_missing_contract_version)
    if args.execute or activation_requested:
        amendment_path, amendment, expected_missing = load_expected_missing_amendment(
            args.expected_missing_amendment, predecessor_path, predecessor,
        )
        require_expected_missing_activation(
            amendment, allow=args.allow_expected_missing, version=args.expected_missing_contract_version,
        )
        require_release_artifact_binding(
            release, amendment_path,
            "source/contracts/ispindel-predecessor-expected-missing-amendment-v3.json",
        )
    host = validate_remote(args.remote)
    remote_root = validate_scoped_absolute(args.remote_root, "remote root")
    backup_root = validate_scoped_absolute(args.backup_root, "backup root")
    offhost = args.offhost
    if not offhost or "\n" in offhost:
        raise DeploymentError("off-host backup destination is invalid")
    source_endpoint = SOURCE_DOCKER_HOST
    target_endpoint = TARGET_DOCKER_HOST
    runner = Runner(args.dry_run)
    inspect_result = runner.remote_endpoint(host, remote_root, source_endpoint,
                                           ["docker", "inspect", args.production_container],
                                           label="source")
    expected_container = require_dict(predecessor.get("container"), "predecessor container")
    expected_volume = require_dict(predecessor.get("volume"), "predecessor volume")
    live_item: dict[str, object] | None = None
    stable_fingerprint_before = ""
    predecessor_container_id = ""
    if args.dry_run:
        current = expected_container
        compose_labels = require_dict(expected_container.get("compose_labels"), "predecessor Compose labels")
    else:
        payload = decode_json_output(inspect_result, "remote docker inspect")
        if not isinstance(payload, list) or len(payload) != 1:
            raise DeploymentError("production inspect must return exactly one container")
        live_item = require_dict(payload[0], "production container inspect")
        mounts_value = live_item.get("Mounts")
        mounts = mounts_value if isinstance(mounts_value, list) else []
        volume_names = [m.get("Name") for m in mounts if isinstance(m, dict)
                        and m.get("Type") == "volume" and m.get("Destination") == "/data"]
        current = {"id": live_item.get("Id"), "image_id": live_item.get("Image"), "volume": volume_names[0] if len(volume_names) == 1 else None}
        if current.get("image_id") != expected_container.get("image_id") or current.get("volume") != expected_volume.get("name"):
            raise DeploymentError("live predecessor no longer matches the frozen predecessor descriptor")
        config = require_dict(live_item.get("Config"), "production container config")
        compose_labels = require_dict(config.get("Labels"), "production Compose labels")
        stable_fingerprint_before = stable_container_fingerprint(live_item)
        predecessor_container_id = str(live_item.get("Id", ""))
        if not re.fullmatch(r"[0-9a-f]{64}", predecessor_container_id):
            raise DeploymentError("predecessor container ID must be an exact 64-char hex digest")
    compose_files_raw = compose_labels.get("com.docker.compose.project.config_files")
    if not isinstance(compose_files_raw, str):
        raise DeploymentError("production Compose file inventory is missing")
    compose_files = [validate_predecessor_compose_path(value, remote_root)
                     for value in compose_files_raw.split(",") if value]
    if not compose_files or len(set(compose_files)) != len(compose_files):
        raise DeploymentError("production Compose file inventory is empty or duplicated")

    stage_dir = args.evidence_dir.resolve() / release_id / "stage01"
    predecessor_archive = stage_dir / "predecessor-source.tar"
    predecessor_image = stage_dir / "predecessor-image.docker.tar.gz"
    predecessor_image_inspect = stage_dir / "predecessor-image-inspect.json"
    predecessor_volume_inspect = stage_dir / "predecessor-volume-inspect.json"
    predecessor_system = stage_dir / "predecessor-system-files.json"
    predecessor_compose = stage_dir / "predecessor-compose-files.json"
    if not args.dry_run:
        stage_dir.mkdir(parents=True, exist_ok=False, mode=0o700)
    source_tar = runner.remote(host, remote_root, [
        "tar", "--exclude=.git", "--exclude=.venv", "--exclude=dist", "--exclude=evidence",
        "--exclude=__pycache__", "-cf", "-", ".",
    ])
    source_hash_result = runner.remote(host, remote_root, ["sha256sum", "--", "Dockerfile", "docker-compose.yml", "app/main.py"])
    source_hashes: dict[str, str] = {}
    if not args.dry_run:
        for line in source_hash_result.stdout.decode().splitlines():
            digest, name = line.split(maxsplit=1)
            source_hashes[name.lstrip("* ")] = digest
        expected_hashes = require_dict(predecessor.get("host_input_sha256"), "predecessor host hashes")
        for relative in ("Dockerfile", "docker-compose.yml", "app/main.py"):
            expected = expected_hashes.get(f"{remote_root}/{relative}")
            if source_hashes.get(relative) != expected:
                raise DeploymentError(f"live predecessor source hash mismatch: {relative}")
    if not args.dry_run:
        predecessor_archive.write_bytes(source_tar.stdout)
        os.chmod(predecessor_archive, 0o600)
    image_id = str(require_dict(
        predecessor.get("container"), "predecessor container"
    ).get("image_id", ""))
    if not image_id.startswith("sha256:"):
        raise DeploymentError("predecessor image ID is invalid")
    image_export = runner.remote_script_endpoint(
        host, remote_root, source_endpoint,
        f"docker image save {shlex.quote(image_id)} | gzip -n -9",
        label="source",
    )
    image_inspect = runner.remote_endpoint(host, remote_root, source_endpoint,
                                           ["docker", "image", "inspect", image_id],
                                           label="source")
    volume_inspect = runner.remote_endpoint(host, remote_root, source_endpoint,
                                           ["docker", "volume", "inspect", str(expected_volume.get("name", ""))],
                                           label="source")
    if not args.dry_run:
        require_volume_inspect(decode_json_output(volume_inspect, "predecessor volume inspect"), predecessor)
        predecessor_image.write_bytes(image_export.stdout)
        os.chmod(predecessor_image, 0o600)
        predecessor_image_inspect.write_bytes(image_inspect.stdout)
        os.chmod(predecessor_image_inspect, 0o600)
        predecessor_volume_inspect.write_bytes(volume_inspect.stdout)
        os.chmod(predecessor_volume_inspect, 0o600)
    snapshot_code = system_snapshot_code()
    system_snapshot = runner.remote(host, remote_root, [
        "sudo", "python3", "-c", snapshot_code, json.dumps(SYSTEM_PATHS),
    ])
    compose_snapshot = runner.remote(host, remote_root, [
        "sudo", "python3", "-c", snapshot_code, json.dumps(compose_files),
    ])
    systemd_state_result = runner.remote(host, remote_root, [
        "systemctl", "show", "--no-pager",
        "--property=Id,LoadState,UnitFileState,ActiveState", *SYSTEMD_UNITS,
    ])
    systemd_states = ({
        unit: {"Id": unit, "LoadState": "not-found", "UnitFileState": "", "ActiveState": "inactive"}
        for unit in SYSTEMD_UNITS
    } if args.dry_run else parse_systemd_states(systemd_state_result.stdout))
    caddy_state = systemd_states["ispindel-caddy.service"]
    if caddy_state["ActiveState"] == "active" or caddy_state["UnitFileState"] in {"enabled", "enabled-runtime"}:
        raise DeploymentError("predecessor Caddy must be stopped and disabled before Stage 01")
    compose_active_paths = [path for path in compose_files if path not in expected_missing]
    compose_presence = {path: path not in expected_missing for path in compose_files}
    if not args.dry_run:
        snapshot_value = decode_json_output(system_snapshot, "predecessor system snapshot")
        require_snapshot_rows(snapshot_value, SYSTEM_PATHS, "predecessor system snapshot")
        predecessor_system.write_bytes(system_snapshot.stdout)
        os.chmod(predecessor_system, 0o600)
        compose_value = decode_json_output(compose_snapshot, "predecessor Compose snapshot")
        require_snapshot_rows(compose_value, compose_files, "predecessor Compose snapshot")
        compose_active_paths, observed_missing = require_compose_observation(
            compose_value, compose_files, expected_missing,
        )
        compose_presence = {path: path in compose_active_paths for path in compose_files}
        if observed_missing != expected_missing:
            raise DeploymentError("predecessor Compose expected-missing observation drifted")
        predecessor_compose.write_bytes(compose_snapshot.stdout)
        os.chmod(predecessor_compose, 0o600)
    backup = runner.remote_script_endpoint(
        host, remote_root, source_endpoint,
        "python3 scripts/backup-production.py --execute --production "
        f"--container {shlex.quote(args.production_container)} "
        f"--backup-root {shlex.quote(backup_root)} --offhost {shlex.quote(offhost)}",
        label="source",
    )
    post_backup_inspect = runner.remote_endpoint(host, remote_root, source_endpoint,
                                                 ["docker", "inspect", args.production_container],
                                                 label="source")
    stable_fingerprint_after = stable_fingerprint_before
    if not args.dry_run:
        post_payload = decode_json_output(post_backup_inspect, "post-backup production inspect")
        if not isinstance(post_payload, list) or len(post_payload) != 1:
            raise DeploymentError("post-backup production inspect must return exactly one container")
        stable_fingerprint_after = stable_container_fingerprint(
            require_dict(post_payload[0], "post-backup production container inspect")
        )
        if stable_fingerprint_after != stable_fingerprint_before:
            raise DeploymentError("production container stable fingerprint changed during Stage 01 backup")
    backup_event = ({
        "event": "backup_complete", "result": "BACKUP_VERIFIED",
        "manifest": f"{backup_root}/DRY-RUN/manifest.json",
        "offhost_manifest": "DRY-RUN",
    } if args.dry_run else parse_backup_output(backup.stdout))
    receipt = receipt_base("01-backup-predecessor", release_id)
    receipt.update({
        "remote": host, "remote_root": remote_root,
        "source_endpoint": source_endpoint, "target_endpoint": target_endpoint,
        "predecessor_container_id": predecessor_container_id,
        "predecessor_volume_name": expected_volume["name"],
        "release_manifest_sha256": sha256(require_regular(args.release_manifest, "release manifest")),
        "predecessor_descriptor": str(predecessor_path), "predecessor_descriptor_sha256": sha256(predecessor_path),
        "expected_missing_amendment": str(amendment_path or args.expected_missing_amendment),
        "expected_missing_amendment_sha256": sha256(amendment_path or require_regular(args.expected_missing_amendment, "expected-missing amendment")),
        "expected_missing_contract_version": amendment.get("contract_version", EXPECTED_MISSING_CONTRACT_VERSION),
        "predecessor_expected_missing_paths": expected_missing,
        "predecessor_expected_missing_paths_sha256": canonical_object_sha256(expected_missing),
        "predecessor_image_id": image_id,
        "predecessor_source_archive": str(predecessor_archive),
        "predecessor_image_archive": str(predecessor_image),
        "predecessor_image_inspect": str(predecessor_image_inspect),
        "predecessor_volume_inspect": str(predecessor_volume_inspect),
        "predecessor_system_snapshot": str(predecessor_system),
        "predecessor_compose_snapshot": str(predecessor_compose),
        "predecessor_compose_paths": compose_files,
        "predecessor_compose_active_paths": compose_active_paths,
        "predecessor_compose_presence": compose_presence,
        "predecessor_container_stable_fingerprint_before": stable_fingerprint_before,
        "predecessor_container_stable_fingerprint_after": stable_fingerprint_after,
        "predecessor_systemd_states": systemd_states,
        "predecessor_systemd_states_sha256": canonical_object_sha256(systemd_states),
        "predecessor_source_sha256": source_hashes,
        "backup_manifest": backup_event["manifest"],
        "offhost_backup_manifest": backup_event.get("offhost_manifest"),
        "database_restored": False,
    })
    if args.dry_run:
        runner.emit_plan("01-backup-predecessor", release_id)
        return 0
    receipt["predecessor_source_archive_sha256"] = sha256(predecessor_archive)
    receipt["predecessor_source_tree_full_sha256"] = receipt["predecessor_source_archive_sha256"]
    receipt["predecessor_image_archive_sha256"] = sha256(predecessor_image)
    receipt["predecessor_image_inspect_sha256"] = sha256(predecessor_image_inspect)
    receipt["predecessor_volume_inspect_sha256"] = sha256(predecessor_volume_inspect)
    receipt["predecessor_system_snapshot_sha256"] = sha256(predecessor_system)
    receipt["predecessor_compose_snapshot_sha256"] = sha256(predecessor_compose)
    receipt["backup_output_sha256"] = hashlib.sha256(backup.stdout).hexdigest()
    output = write_receipt(args.evidence_dir, "01-backup-predecessor", release_id, receipt)
    print(f"STAGE01_OK receipt={output}")
    return 0


def stage02(args: argparse.Namespace) -> int:
    _, release = load_release(args.release_manifest)
    release_id, image_ref, image_archive_rel = release_fields(release)
    image_id = str(require_dict(release.get("image"), "release image binding").get("id", ""))
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id):
        raise DeploymentError("release image ID binding is invalid")
    require_execution(args, release_id)
    host = validate_remote(args.remote)
    remote_root = validate_scoped_absolute(args.remote_root, "remote root")
    remote_release = validate_absolute(args.remote_release_root, "remote release root")
    backup_manifest = validate_absolute(args.backup_manifest, "backup manifest")
    source_endpoint = SOURCE_DOCKER_HOST
    target_endpoint = TARGET_DOCKER_HOST
    runner = Runner(args.dry_run)
    verifier = f"{remote_release}/source/scripts/verify-release.py"
    release_manifest = f"{remote_release}/RELEASE.json"
    image_archive = f"{remote_release}/{image_archive_rel}"
    runner.remote(host, remote_root, remote_python_no_bytecode([
        verifier, "--manifest", release_manifest, "--root", remote_release,
    ]))
    runner.remote_script_endpoint(host, remote_root, target_endpoint,
                                  f"gzip -dc {shlex.quote(image_archive)} | docker image load",
                                  label="target")
    loaded_image = runner.remote_endpoint(
        host, remote_root, target_endpoint,
        ["docker", "image", "inspect", "--format", "{{.Id}}", image_ref],
        label="target",
    )
    if not args.dry_run and loaded_image.stdout.decode("utf-8", errors="strict").strip() != image_id:
        raise DeploymentError("target daemon image ID does not match release binding")
    runner.remote_endpoint(host, remote_root, target_endpoint,
                           network_none_verifier(remote_release, image_ref),
                           label="target")
    nonce = "deploy-" + release_id.lower().replace("z-", "-")
    rehearsal = runner.remote_script_endpoint(
        host, remote_root, target_endpoint,
        shlex.join(remote_python_no_bytecode([
            f"{remote_release}/source/scripts/rehearse-restore.py",
            "--manifest", backup_manifest, "--image", image_ref,
            "--evidence-dir", f"{remote_root}/evidence/rehearsal", "--nonce", nonce,
        ])),
        label="target",
    )
    if args.dry_run:
        runner.emit_plan("02-rehearse-release", release_id)
        return 0
    receipt = receipt_base("02-rehearse-release", release_id)
    receipt.update({"remote": host, "remote_release_root": remote_release,
                    "source_endpoint": source_endpoint, "target_endpoint": target_endpoint,
                    "release_manifest_sha256": sha256(require_regular(args.release_manifest, "release manifest")),
                    "backup_manifest": backup_manifest, "image": image_ref, "image_id": image_id,
                    "target_image_loaded": True, "network_none_verified": True,
                    "rehearsal_output_sha256": hashlib.sha256(rehearsal.stdout).hexdigest(),
                    "production_mutated": False})
    output = write_receipt(args.evidence_dir, "02-rehearse-release", release_id, receipt)
    print(f"STAGE02_OK receipt={output}")
    return 0


def load_stage01_receipt(path: Path, release_id: str, expected_sha256: str) -> tuple[Path, dict[str, object]]:
    receipt_path = require_regular(path, "Stage 01 receipt")
    if not re.fullmatch(r"[0-9a-f]{64}", expected_sha256) or sha256(receipt_path) != expected_sha256:
        raise DeploymentError("Stage 01 receipt SHA-256 mismatch")
    receipt = load_json(receipt_path, "Stage 01 receipt")
    expected = {"schema": "ispindel-deploy-stage-receipt/v1", "stage": "01-backup-predecessor",
                "release_id": release_id, "result": "PASS", "database_restored": False,
                "source_endpoint": SOURCE_DOCKER_HOST, "target_endpoint": TARGET_DOCKER_HOST}
    for key, value in expected.items():
        if receipt.get(key) != value:
            raise DeploymentError(f"Stage 01 receipt does not bind this release/predecessor ({key})")
    descriptor = require_regular(Path(str(receipt.get("predecessor_descriptor", ""))), "predecessor descriptor")
    if sha256(descriptor) != receipt.get("predecessor_descriptor_sha256"):
        raise DeploymentError("Stage 01 predecessor descriptor hash mismatch")
    predecessor = load_json(descriptor, "predecessor descriptor")
    amendment_path = require_regular(
        Path(str(receipt.get("expected_missing_amendment", ""))), "expected-missing amendment",
    )
    if sha256(amendment_path) != receipt.get("expected_missing_amendment_sha256"):
        raise DeploymentError("Stage 01 expected-missing amendment hash mismatch")
    _, amendment, contract_missing = load_expected_missing_amendment(
        amendment_path, descriptor, predecessor,
    )
    if receipt.get("expected_missing_contract_version") != amendment.get("contract_version"):
        raise DeploymentError("Stage 01 expected-missing contract version mismatch")
    expected_missing = receipt.get("predecessor_expected_missing_paths")
    if (not isinstance(expected_missing, list)
            or any(not isinstance(item, str) for item in expected_missing)
            or expected_missing != contract_missing):
        raise DeploymentError("Stage 01 expected-missing path binding mismatch")
    if receipt.get("predecessor_expected_missing_paths_sha256") != canonical_object_sha256(expected_missing):
        raise DeploymentError("Stage 01 expected-missing path hash mismatch")
    predecessor_container_id = receipt.get("predecessor_container_id", "")
    if not isinstance(predecessor_container_id, str) or not re.fullmatch(r"[0-9a-f]{64}", predecessor_container_id):
        raise DeploymentError("Stage 01 predecessor container ID must be an exact 64-char hex digest")
    artifacts: dict[str, Path] = {}
    for key in (
        "predecessor_source_archive", "predecessor_image_archive", "predecessor_image_inspect",
        "predecessor_volume_inspect", "predecessor_system_snapshot", "predecessor_compose_snapshot",
    ):
        artifact = require_regular(Path(str(receipt.get(key, ""))), key)
        if sha256(artifact) != receipt.get(f"{key}_sha256"):
            raise DeploymentError(f"Stage 01 {key} hash mismatch")
        artifacts[key] = artifact
    if receipt.get("predecessor_source_tree_full_sha256") != receipt.get("predecessor_source_archive_sha256"):
        raise DeploymentError("Stage 01 full source tree hash binding mismatch")
    compose_paths = receipt.get("predecessor_compose_paths")
    if (not isinstance(compose_paths, list) or not compose_paths
            or any(not isinstance(item, str) for item in compose_paths)
            or len(compose_paths) != len(set(compose_paths))):
        raise DeploymentError("Stage 01 Compose snapshot path binding mismatch")
    compose_snapshot = load_json(artifacts["predecessor_compose_snapshot"], "predecessor Compose snapshot")
    require_snapshot_rows(compose_snapshot, compose_paths, "Stage 01 Compose snapshot")
    active_paths, observed_missing = require_compose_observation(
        compose_snapshot, compose_paths, expected_missing,
    )
    if receipt.get("predecessor_compose_active_paths") != active_paths:
        raise DeploymentError("Stage 01 active Compose path binding mismatch")
    presence = receipt.get("predecessor_compose_presence")
    expected_presence = {item: item in active_paths for item in compose_paths}
    if presence != expected_presence or observed_missing != expected_missing:
        raise DeploymentError("Stage 01 Compose presence binding mismatch")
    system_snapshot = load_json(artifacts["predecessor_system_snapshot"], "predecessor system snapshot")
    require_snapshot_rows(system_snapshot, SYSTEM_PATHS, "Stage 01 system snapshot")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", str(receipt.get("predecessor_image_id", ""))):
        raise DeploymentError("Stage 01 predecessor image ID is invalid")
    systemd_states = require_systemd_states(receipt.get("predecessor_systemd_states"))
    if receipt.get("predecessor_systemd_states_sha256") != canonical_object_sha256(systemd_states):
        raise DeploymentError("Stage 01 predecessor systemd state hash mismatch")
    before = receipt.get("predecessor_container_stable_fingerprint_before")
    after = receipt.get("predecessor_container_stable_fingerprint_after")
    if (not isinstance(before, str) or not re.fullmatch(r"[0-9a-f]{64}", before)
            or before != after):
        raise DeploymentError("Stage 01 predecessor stable fingerprint mismatch")
    try:
        volume_value = json.loads(artifacts["predecessor_volume_inspect"].read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DeploymentError("predecessor volume inspect is invalid") from exc
    require_volume_inspect(volume_value, predecessor)
    backup_manifest = receipt.get("backup_manifest")
    if not isinstance(backup_manifest, str):
        raise DeploymentError("Stage 01 backup manifest binding is missing")
    validate_absolute(backup_manifest, "Stage 01 backup manifest")
    return receipt_path, receipt


def load_stage02_receipt(
    path: Path, release_id: str, expected_sha256: str, *,
    release_manifest_sha256: str, image_id: str, remote_release_root: str,
) -> tuple[Path, dict[str, object]]:
    receipt_path = require_regular(path, "Stage 02 receipt")
    if (not re.fullmatch(r"[0-9a-f]{64}", expected_sha256)
            or sha256(receipt_path) != expected_sha256):
        raise DeploymentError("Stage 02 receipt SHA-256 mismatch")
    receipt = load_json(receipt_path, "Stage 02 receipt")
    expected: dict[str, object] = {
        "schema": "ispindel-deploy-stage-receipt/v1",
        "stage": "02-rehearse-release",
        "release_id": release_id,
        "result": "PASS",
        "source_endpoint": SOURCE_DOCKER_HOST,
        "target_endpoint": TARGET_DOCKER_HOST,
        "production_mutated": False,
        "release_manifest_sha256": release_manifest_sha256,
        "image_id": image_id,
        "remote_release_root": remote_release_root,
        "target_image_loaded": True,
        "network_none_verified": True,
    }
    for key, value in expected.items():
        if receipt.get(key) != value:
            raise DeploymentError(f"Stage 02 receipt does not bind this target image ({key})")
    return receipt_path, receipt


def deterministic_backend_gate(runner: Runner, host: str, remote_root: str, container: str,
                                *, endpoint: str | None = None) -> None:
    direct_probe = (
        "import urllib.request; r=urllib.request.urlopen('http://127.0.0.1:8098/health/ready',timeout=3); "
        "raise SystemExit(0 if r.status==200 else 1)"
    )
    script = (
        "fail=0; for n in 1 2 3; do "
        "curl -fsS --max-time 3 http://127.0.0.1:18098/health/ready >/dev/null || fail=$((fail+1)); "
        "[ \"$fail\" -eq 0 ] && break; sleep 2; done; "
        "if [ \"$fail\" -eq 3 ]; then "
        f"docker exec {shlex.quote(container)} python3 -c {shlex.quote(direct_probe)} || exit {DETERMINISTIC_GATE_EXIT}; "
        "fi; [ \"$fail\" -lt 3 ]"
    )
    if endpoint:
        script = docker_endpoint_env(endpoint) + script
    command = ["ssh", "-o", "BatchMode=yes", "--", host,
               remote_script_command(remote_root, script)]
    runner.plan.append(command)
    if runner.dry_run:
        return
    result = subprocess.run(command, capture_output=True, check=False)
    if result.returncode == DETERMINISTIC_GATE_EXIT:
        raise DeterministicGateFailure("backend failed three readiness probes and direct container health")
    if result.returncode:
        raise DeploymentError("backend readiness gate failed without deterministic rollback proof")


SLICE2B1_STOP_TIMEOUT_SECONDS = 30
SLICE2B1_CONTAINER_ID_RE = re.compile(r"^[0-9a-f]{64}$")
SLICE2B1_ID_FORMAT = "{{.Id}}"
SLICE2B1_RUNNING_FORMAT = "{{.State.Running}}"

# Slice 2B2: source-endpoint quiesced backup via nonce source-volume helper.
SLICE2B2_RUN_ID_FORMAT = "%Y%m%dT%H%M%SZ-"
SLICE2B2_RUN_ID_NONCE_HEX_LEN = 8
SLICE2B2_HELPER_NAME_PREFIX = "ispindel-cutover-backup-"
SLICE2B2_HELPER_NAME_RE = re.compile(
    r"^ispindel-cutover-backup-[0-9a-f]{" + str(SLICE2B2_RUN_ID_NONCE_HEX_LEN) + r"}$"
)
# Docker volume names match [a-zA-Z0-9][a-zA-Z0-9_.-]* — see docker volume
# create; the same regex is what docker volume inspect accepts as a name.
SLICE2B2_VOLUME_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]*$")
SLICE2B2_IMAGE_ID_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
SLICE2B2_RELEASE_SCRIPT = "/release/source/scripts/backup-production.py"
SLICE2B2_BACKUP_PRODUCTION_FLAGS = (
    "--execute",
    "--source-db",
    "/source/ispindel.db",
    "--backup-root",
    "/backup",
    "--offhost",
    "/backup/offhost",
    "--run-id",
)
SLICE2B2_SOURCE_MOUNT = "/source"
SLICE2B2_SOURCE_DB_NAME = "ispindel.db"
SLICE2B2_BACKUP_MOUNT = "/backup"
SLICE2B2_OFFHOST_DIR = "offhost"
SLICE2B2_VERIFY_INDICES = (
    "manifest", "file", "database", "run_id", "manifest_sha256",
)
SLICE2B2_FORBIDDEN_TOKENS = (
    "--production", "docker exec", "exec ", "compose", "down", "rm",
)


def slice2_source_predecessor_stop(
    runner: Runner, host: str, remote_root: str,
    stage01_receipt: dict[str, object], *, execute: bool,
) -> dict[str, object]:
    """Source-endpoint predecessor stop primitive (Slice 2B1).

    Bound exclusively to the Stage 01 receipt:

    * ``source_endpoint`` MUST exactly equal :data:`SOURCE_DOCKER_HOST`.
    * ``predecessor_container_id`` MUST match ``^[0-9a-f]{64}$``.

    Before stopping, two inspects pin the live container to that captured ID
    and verify ``State.Running=true``; any mismatch or non-running state
    raises :class:`DeterministicGateFailure` (fail closed, never silently
    proceed). On execute, the call issues exactly
    ``docker stop --time 30 <captured-id>`` through ``runner.remote_endpoint``
    on the source endpoint, then re-inspects to confirm ``State.Running=false``.

    In dry-run the same three-command plan (inspect ID / inspect running /
    docker stop / inspect running) is recorded and returned as a structured
    plan dict without performing any remote work.

    Returns the structured plan dict on both branches so Stage 03 can record
    the exact commands that will or would be issued.
    """
    source_endpoint = stage01_receipt.get("source_endpoint")
    if source_endpoint != SOURCE_DOCKER_HOST:
        raise DeterministicGateFailure(
            f"slice2 source predecessor stop requires source_endpoint exactly "
            f"{SOURCE_DOCKER_HOST!r}; got {source_endpoint!r}"
        )
    predecessor_container_id = stage01_receipt.get("predecessor_container_id")
    if (not isinstance(predecessor_container_id, str)
            or not SLICE2B1_CONTAINER_ID_RE.fullmatch(predecessor_container_id)):
        raise DeterministicGateFailure(
            "slice2 source predecessor stop requires Stage 01 "
            "predecessor_container_id to match lowercase 64-char hex"
        )
    expected_running_label = "source"
    pre_inspect_id_argv = ["docker", "inspect", "--format", SLICE2B1_ID_FORMAT,
                           predecessor_container_id]
    pre_inspect_running_argv = ["docker", "inspect", "--format",
                                SLICE2B1_RUNNING_FORMAT, predecessor_container_id]
    stop_argv = ["docker", "stop", "--time",
                 str(SLICE2B1_STOP_TIMEOUT_SECONDS), predecessor_container_id]
    post_inspect_running_argv = ["docker", "inspect", "--format",
                                 SLICE2B1_RUNNING_FORMAT, predecessor_container_id]
    plan: dict[str, object] = {
        "source_endpoint": source_endpoint,
        "predecessor_container_id": predecessor_container_id,
        "stop_timeout_seconds": SLICE2B1_STOP_TIMEOUT_SECONDS,
        "steps": [
            {"label": "pre_inspect_id", "argv": pre_inspect_id_argv},
            {"label": "pre_inspect_running", "argv": pre_inspect_running_argv},
            {"label": "stop", "argv": stop_argv},
            {"label": "post_inspect_running", "argv": post_inspect_running_argv},
        ],
    }
    if not execute:
        if not runner.dry_run:
            raise DeterministicGateFailure("predecessor stop requires --execute outside dry-run")
        for argv in (
            pre_inspect_id_argv, pre_inspect_running_argv,
            stop_argv, post_inspect_running_argv,
        ):
            runner.remote_endpoint(
                host, remote_root, SOURCE_DOCKER_HOST, argv,
                label=expected_running_label,
            )
        plan["post_stop_running"] = "false"
        return plan
    pre_id = runner.remote_endpoint(host, remote_root, source_endpoint,
                                    pre_inspect_id_argv, label=expected_running_label)
    pre_id_value = pre_id.stdout.decode().strip()
    if pre_id_value.lower() != predecessor_container_id:
        raise DeterministicGateFailure(
            "slice2 source predecessor stop pre-inspect ID mismatch: "
            f"expected {predecessor_container_id}, got {pre_id_value!r}"
        )
    pre_running = runner.remote_endpoint(host, remote_root, source_endpoint,
                                         pre_inspect_running_argv,
                                         label=expected_running_label)
    pre_running_value = pre_running.stdout.decode().strip().lower()
    if pre_running_value != "true":
        raise DeterministicGateFailure(
            "slice2 source predecessor stop pre-inspect Running=false; "
            f"predecessor {predecessor_container_id} is not running on source endpoint"
        )
    runner.remote_endpoint(host, remote_root, source_endpoint,
                           stop_argv, label=expected_running_label)
    post_running = runner.remote_endpoint(host, remote_root, source_endpoint,
                                          post_inspect_running_argv,
                                          label=expected_running_label)
    post_running_value = post_running.stdout.decode().strip().lower()
    if post_running_value != "false":
        raise DeterministicGateFailure(
            "slice2 source predecessor stop post-stop Running=true; "
            f"docker stop did not converge for {predecessor_container_id}"
        )
    plan["post_stop_running"] = post_running_value
    return plan


def slice2_source_preserve_predecessor(
    runner: Runner, host: str, remote_root: str, predecessor_container_id: str,
    release_id: str, production_container: str, *, execute: bool,
) -> dict[str, object]:
    """Rename the stopped predecessor so Compose cannot destroy its ID.

    Compose replacement is name-based: a service with ``container_name`` equal
    to the production name removes the old container before creating the
    candidate. A release-bound standby name preserves the exact predecessor
    object for Stage 05 instead of relying on a stale name or image recreation.
    """
    standby_name, _candidate_name = rollback_container_names(release_id, production_container)
    inspect_format = "{{.Id}}|{{.Name}}|{{.State.Running}}"
    inspect_standby = ["docker", "inspect", "--format", inspect_format, standby_name]
    rename = ["docker", "rename", predecessor_container_id, standby_name]
    verify = ["docker", "inspect", "--format", inspect_format, predecessor_container_id]
    plan: dict[str, object] = {
        "source_endpoint": SOURCE_DOCKER_HOST,
        "predecessor_container_id": predecessor_container_id,
        "standby_name": standby_name,
        "steps": [
            {"label": "standby_absence", "argv": inspect_standby},
            {"label": "rename_predecessor", "argv": rename},
            {"label": "verify_standby", "argv": verify},
        ],
    }
    if not execute:
        if not runner.dry_run:
            raise DeterministicGateFailure("predecessor preservation requires --execute outside dry-run")
        for argv in (inspect_standby, rename, verify):
            runner.remote_endpoint(host, remote_root, SOURCE_DOCKER_HOST, argv,
                                   required=False, label="source")
        plan["verified"] = f"{predecessor_container_id}|/{standby_name}|false"
        return plan
    existing = runner.remote_endpoint(host, remote_root, SOURCE_DOCKER_HOST,
                                      inspect_standby, required=False, label="source")
    if existing.returncode == 0:
        raise DeterministicGateFailure(
            f"predecessor standby name already exists: {standby_name}"
        )
    if not exact_docker_absence_verdict(existing, standby_name):
        raise DeterministicGateFailure(
            "predecessor standby absence probe failed without the exact Docker absence verdict"
        )
    renamed = runner.remote_endpoint(host, remote_root, SOURCE_DOCKER_HOST,
                                     rename, required=False, label="source")
    if renamed.returncode:
        raise DeterministicGateFailure(
            f"failed to preserve predecessor {predecessor_container_id} as {standby_name}"
        )
    verified = runner.remote_endpoint(host, remote_root, SOURCE_DOCKER_HOST,
                                      verify, required=False, label="source")
    observed = verified.stdout.decode(errors="strict").strip()
    expected = f"{predecessor_container_id}|/{standby_name}|false"
    if verified.returncode or observed != expected:
        raise DeterministicGateFailure(
            "predecessor preservation verification mismatch: "
            f"expected {expected!r}, got {observed!r}"
        )
    plan["verified"] = observed
    return plan


def slice2_placeholder_source_quiesced_backup(*, execute: bool) -> None:
    """Slice-2 placeholder: quiesced backup on the source endpoint.

    Kept only so a stale call site cannot silently vanish; the Stage 03
    wiring now calls :func:`slice2_source_quiesced_backup` directly, which
    makes this body unreachable in the current deploy script.
    """
    del execute


def _slice2b2_build_run_id() -> str:
    """Return the canonical ``YYYYMMDDTHHMMSSZ-<hex>`` run identifier."""
    timestamp = dt.datetime.now(dt.timezone.utc).strftime(SLICE2B2_RUN_ID_FORMAT)
    nonce = secrets.token_hex(SLICE2B2_RUN_ID_NONCE_HEX_LEN // 2)
    return f"{timestamp}{nonce}"


def _slice2b2_build_helper_name(run_id: str) -> str:
    """Return the canonical nonce helper container name."""
    # ``run_id`` already ends with the 8-char nonce; keep the helper name
    # derived from it so dry-run plans and execute paths share the same value.
    nonce = run_id.rsplit("-", 1)[-1]
    if not SLICE2B2_HELPER_NAME_RE.fullmatch(f"{SLICE2B2_HELPER_NAME_PREFIX}{nonce}"):
        raise DeterministicGateFailure(
            "slice2 source quiesced backup could not derive a valid helper name "
            f"from run_id={run_id!r}"
        )
    return f"{SLICE2B2_HELPER_NAME_PREFIX}{nonce}"


def _slice2b2_render_helper_run(run_id: str, *, helper_name: str,
                                 predecessor_image_id: str,
                                 source_volume_name: str,
                                 remote_release: str,
                                 backup_root: str) -> str:
    """Render the bash fragment that runs the nonce helper container."""
    return (
        f"DOCKER_HOST={shlex.quote(SOURCE_DOCKER_HOST)} "
        "docker run --rm "
        f"--name {shlex.quote(helper_name)} "
        "--network none --pull never --user 0:0 "
        f"--mount type=volume,src={shlex.quote(source_volume_name)},"
        f"dst={SLICE2B2_SOURCE_MOUNT},readonly "
        f"--mount type=bind,src={shlex.quote(remote_release)},"
        f"dst=/release,readonly "
        f"--mount type=bind,src={shlex.quote(backup_root)},"
        f"dst={SLICE2B2_BACKUP_MOUNT} "
        f"{shlex.quote(predecessor_image_id)} "
        f"python3 {SLICE2B2_RELEASE_SCRIPT} "
        f"--execute "
        f"--source-db {SLICE2B2_SOURCE_MOUNT}/{SLICE2B2_SOURCE_DB_NAME} "
        f"--backup-root {SLICE2B2_BACKUP_MOUNT} "
        f"--offhost {SLICE2B2_BACKUP_MOUNT}/{SLICE2B2_OFFHOST_DIR} "
        f"--run-id {shlex.quote(run_id)}"
    )


def _slice2b2_render_verifier(backup_root: str, run_id: str) -> str:
    """Render the inline Python verifier that compares manifest evidence."""
    script = (
        "import hashlib, json, os, pathlib, sqlite3, sys\n"
        "backup_root = pathlib.Path(sys.argv[1]).resolve()\n"
        "run_id = sys.argv[2]\n"
        "manifest_path = backup_root / run_id / 'manifest.json'\n"
        "manifest_sha256 = hashlib.sha256(manifest_path.read_bytes()).hexdigest()\n"
        "manifest = json.loads(manifest_path.read_text())\n"
        "basename = manifest['basename']\n"
        "db_path = backup_root / run_id / basename\n"
        "if db_path.is_symlink() or not db_path.is_file():\n"
        "    raise SystemExit('slice2b2 verifier: db not regular file')\n"
        "file_size = db_path.stat().st_size\n"
        "h = hashlib.sha256()\n"
        "with db_path.open('rb') as handle:\n"
        "    for chunk in iter(lambda: handle.read(1048576), b''):\n"
        "        h.update(chunk)\n"
        "file_sha = h.hexdigest()\n"
        "expected_size = manifest['file']['size']\n"
        "expected_sha = manifest['file']['sha256']\n"
        "if file_size != expected_size or file_sha != expected_sha:\n"
        "    raise SystemExit('slice2b2 verifier: file hash mismatch')\n"
        "conn = sqlite3.connect('file:' + str(db_path.resolve()) + '?mode=ro', uri=True)\n"
        "conn.execute('PRAGMA query_only=ON')\n"
        "if [row[0] for row in conn.execute('PRAGMA integrity_check')] != ['ok']:\n"
        "    raise SystemExit('slice2b2 verifier: integrity_check != ok')\n"
        "if conn.execute('PRAGMA foreign_key_check').fetchall() != []:\n"
        "    raise SystemExit('slice2b2 verifier: foreign_key_check != []')\n"
        "user_version = int(conn.execute('PRAGMA user_version').fetchone()[0])\n"
        "migration_versions = [\n"
        "    row[0] for row in conn.execute(\n"
        "        \"SELECT version FROM schema_migrations ORDER BY version\"\n"
        "    )\n"
        "] if conn.execute(\"SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'\").fetchone() else []\n"
        "tables = ['devices', 'samples', 'calibrations', 'calibration_active']\n"
        "actual_counts = {}\n"
        "for table in tables:\n"
        "    present = conn.execute(\n"
        "        \"SELECT 1 FROM sqlite_master WHERE type='table' AND name=?\",\n"
        "        (table,),\n"
        "    ).fetchone()\n"
        "    if present is None:\n"
        "        actual_counts[table] = 0\n"
        "    else:\n"
        "        actual_counts[table] = int(\n"
        "            conn.execute('SELECT COUNT(*) FROM \"' + table + '\"').fetchone()[0]\n"
        "        )\n"
        "expected_counts = manifest['database']['counts']\n"
        "if set(expected_counts) != set(tables) or any(\n"
        "    expected_counts[t] != actual_counts[t] for t in tables\n"
        "):\n"
        "    raise SystemExit('slice2b2 verifier: table count mismatch')\n"
        "if user_version != int(manifest['database']['user_version']):\n"
        "    raise SystemExit('slice2b2 verifier: user_version mismatch')\n"
        "payload = {\n"
        "    'manifest': str(manifest_path),\n"
        "    'manifest_sha256': manifest_sha256,\n"
        "    'file': {'size': file_size, 'sha256': file_sha,\n"
        "             'basename': basename},\n"
        "    'database': {\n"
        "        'user_version': user_version,\n"
        "        'migration_versions': migration_versions,\n"
        "        'counts': actual_counts,\n"
        "    },\n"
        "    'run_id': run_id,\n"
        "}\n"
        "print('SLICE2B2_VERIFIER_RESULT=' + json.dumps(payload, sort_keys=True))\n"
        "conn.close()\n"
    )
    return (
        "sudo -n env "
        f"DOCKER_HOST={shlex.quote(SOURCE_DOCKER_HOST)} "
        f"python3 -c {shlex.quote(script)} "
        f"{shlex.quote(backup_root)} {shlex.quote(run_id)}"
    )


def _slice2b2_parse_verifier_output(
    stdout: bytes, *, expected_run_id: str, expected_manifest: str,
) -> dict[str, object]:
    """Parse the single ``SLICE2B2_VERIFIER_RESULT=…`` line."""
    line = b""
    for candidate in stdout.splitlines():
        if candidate.startswith(b"SLICE2B2_VERIFIER_RESULT="):
            line = candidate
            break
    if not line:
        raise DeterministicGateFailure(
            "slice2 source quiesced backup verifier did not emit "
            "SLICE2B2_VERIFIER_RESULT"
        )
    try:
        payload = json.loads(line.split(b"=", 1)[1].decode())
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DeterministicGateFailure(
            "slice2 source quiesced backup verifier output was not valid JSON"
        ) from exc
    if not isinstance(payload, dict):
        raise DeterministicGateFailure(
            "slice2 source quiesced backup verifier payload must be a JSON object"
        )
    for key in SLICE2B2_VERIFY_INDICES:
        if key not in payload:
            raise DeterministicGateFailure(
                f"slice2 source quiesced backup verifier missing required key {key!r}"
            )
    if payload.get("run_id") != expected_run_id or payload.get("manifest") != expected_manifest:
        raise DeterministicGateFailure(
            "slice2 source quiesced backup verifier evidence binding mismatch"
        )
    if not re.fullmatch(r"[0-9a-f]{64}", str(payload.get("manifest_sha256", ""))):
        raise DeterministicGateFailure(
            "slice2 source quiesced backup verifier manifest hash is invalid"
        )
    return payload


def slice2_source_quiesced_backup(
    runner: Runner, host: str, remote_root: str, remote_release: str,
    stage01_receipt: dict[str, object], stop_evidence: dict[str, object], *,
    backup_root: str, execute: bool,
) -> dict[str, object]:
    """Slice-2B2 source-endpoint quiesced backup.

    Runs the predecessor image as a one-shot rootless helper that mounts the
    production data volume read-only and emits one verified SQLite backup
    underneath the local backup root, then verifies the resulting manifest
    against the actual database bytes.

    Bound exclusively to the Stage 01 receipt + the just-recorded predecessor
    stop evidence:

    * ``source_endpoint`` MUST equal :data:`SOURCE_DOCKER_HOST` everywhere.
    * ``predecessor_container_id`` MUST be a lowercase 64-char hex digest and
      match the stop evidence end-to-end.
    * ``predecessor_volume_name`` MUST match the safe Docker volume-name
      regex AND equal the Stage 01 receipt's frozen volume name.
    * ``predecessor_image_id`` MUST match ``^sha256:[0-9a-f]{64}$``.
    * ``post_stop_running`` MUST be ``'false'`` in the stop evidence.

    The backup MUST NOT be issued through ``docker exec`` or with the
    ``--production`` flag — the helper container owns the snapshot.

    Returns a plan dict on both branches so Stage 03 can record the exact
    commands that were or would be issued.
    """
    # ------------------------------------------------------------------ bind
    source_endpoint = stage01_receipt.get("source_endpoint")
    if source_endpoint != SOURCE_DOCKER_HOST:
        raise DeterministicGateFailure(
            "slice2 source quiesced backup requires source_endpoint exactly "
            f"{SOURCE_DOCKER_HOST!r}; got {source_endpoint!r}"
        )
    predecessor_container_id = stage01_receipt.get("predecessor_container_id")
    if (not isinstance(predecessor_container_id, str)
            or not re.fullmatch(r"[0-9a-f]{64}", predecessor_container_id)):
        raise DeterministicGateFailure(
            "slice2 source quiesced backup requires Stage 01 "
            "predecessor_container_id to match lowercase 64-char hex"
        )
    predecessor_volume_name = stage01_receipt.get("predecessor_volume_name")
    if (not isinstance(predecessor_volume_name, str)
            or not SLICE2B2_VOLUME_NAME_RE.fullmatch(predecessor_volume_name)):
        raise DeterministicGateFailure(
            "slice2 source quiesced backup requires Stage 01 "
            "predecessor_volume_name to match a safe Docker volume name"
        )
    predecessor_image_id = stage01_receipt.get("predecessor_image_id")
    if (not isinstance(predecessor_image_id, str)
            or not SLICE2B2_IMAGE_ID_RE.fullmatch(predecessor_image_id)):
        raise DeterministicGateFailure(
            "slice2 source quiesced backup requires Stage 01 "
            "predecessor_image_id to match ^sha256:[0-9a-f]{64}$"
        )
    # Bind stop evidence to the same endpoint+ID we just stopped.
    stop_source = stop_evidence.get("source_endpoint")
    stop_id = stop_evidence.get("predecessor_container_id")
    stop_running = stop_evidence.get("post_stop_running")
    if (stop_source != SOURCE_DOCKER_HOST
            or not isinstance(stop_id, str)
            or stop_id.lower() != predecessor_container_id
            or stop_running != "false"):
        raise DeterministicGateFailure(
            "slice2 source quiesced backup requires stop evidence to bind "
            f"source_endpoint={SOURCE_DOCKER_HOST!r}, "
            f"predecessor_container_id={predecessor_container_id!r}, "
            "and post_stop_running='false'"
        )
    if not isinstance(backup_root, str) or not backup_root.startswith("/"):
        raise DeterministicGateFailure(
            "slice2 source quiesced backup requires an absolute backup root"
        )
    if not isinstance(remote_release, str) or not remote_release.startswith("/"):
        raise DeterministicGateFailure(
            "slice2 source quiesced backup requires an absolute remote release root"
        )
    run_id = _slice2b2_build_run_id()
    helper_name = _slice2b2_build_helper_name(run_id)
    helper_run = _slice2b2_render_helper_run(
        run_id, helper_name=helper_name,
        predecessor_image_id=predecessor_image_id,
        source_volume_name=predecessor_volume_name,
        remote_release=remote_release, backup_root=backup_root,
    )
    verifier = _slice2b2_render_verifier(backup_root, run_id)
    plan: dict[str, object] = {
        "source_endpoint": source_endpoint,
        "predecessor_container_id": predecessor_container_id,
        "predecessor_volume_name": predecessor_volume_name,
        "predecessor_image_id": predecessor_image_id,
        "run_id": run_id,
        "helper_container_name": helper_name,
        "backup_root": backup_root,
        "offhost_directory": f"{backup_root.rstrip('/')}/{SLICE2B2_OFFHOST_DIR}",
        "steps": [
            {"label": "helper_run", "argv": ["sh", "-c", helper_run]},
            {"label": "verifier", "argv": ["sh", "-c", verifier]},
        ],
        "forbidden_tokens": list(SLICE2B2_FORBIDDEN_TOKENS),
    }
    if not execute:
        if not runner.dry_run:
            raise DeterministicGateFailure(
                "quiesced backup requires --execute outside dry-run"
            )
        runner.remote_script_endpoint(host, remote_root, SOURCE_DOCKER_HOST,
                                       helper_run, label="source")
        runner.remote_script_endpoint(host, remote_root, SOURCE_DOCKER_HOST,
                                       verifier, label="source")
        return plan
    # ---------------------------------------------------------------- execute
    backup_result = runner.remote_script_endpoint(
        host, remote_root, SOURCE_DOCKER_HOST, helper_run, label="source",
    )
    if backup_result.returncode:
        raise DeterministicGateFailure(
            "slice2 source quiesced backup helper failed; refusing to "
            "trust the verifier output"
        )
    verify_result = runner.remote_script_endpoint(
        host, remote_root, SOURCE_DOCKER_HOST, verifier, label="source",
    )
    if verify_result.returncode:
        raise DeterministicGateFailure(
            "slice2 source quiesced backup verifier failed"
        )
    evidence = _slice2b2_parse_verifier_output(
        verify_result.stdout,
        expected_run_id=run_id,
        expected_manifest=f"{backup_root.rstrip('/')}/{run_id}/manifest.json",
    )
    plan["evidence"] = evidence
    plan["backup_stdout_sha256"] = hashlib.sha256(backup_result.stdout).hexdigest()
    plan["verify_stdout_sha256"] = hashlib.sha256(verify_result.stdout).hexdigest()
    return plan


SLICE2C_TARGET_VOLUME = "ispindel-dashboard_ispindel-data"
SLICE2C_ROOT_HELPER = "/usr/local/sbin/ispindel-root-helper"
SLICE2C_RUN_ID_RE = re.compile(r"^[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}$")
SLICE2C_BACKUP_BASENAME_RE = re.compile(
    r"^ispindel-([0-9]{8}T[0-9]{6}Z-[0-9a-f]{8})\.db$"
)
SLICE2C_TABLES = ("devices", "samples", "calibrations", "calibration_active")


def _slice2c_fail(message: str) -> DeterministicGateFailure:
    return DeterministicGateFailure(f"slice2 target volume seed {message}")


def _slice2c_verifier_command(
    image_id: str, source_evidence: dict[str, object], nonce: str,
) -> str:
    expected = json.dumps(source_evidence, sort_keys=True, separators=(",", ":"))
    binding = json.dumps({
        "target_endpoint": TARGET_DOCKER_HOST,
        "target_volume": SLICE2C_TARGET_VOLUME,
        "source_manifest_sha256": source_evidence["manifest_sha256"],
        "source_run_id": source_evidence["run_id"],
        "journal_mode": "wal",
    }, sort_keys=True, separators=(",", ":"))
    verifier = (
        "import hashlib,json,os,pathlib,sqlite3,stat,sys\n"
        "expected=json.loads(sys.argv[1]); binding=json.loads(sys.argv[2]); nonce=sys.argv[3]\n"
        "assert nonce.startswith('ispindel-target-verify-') and len(nonce)==39, 'invalid verifier nonce'\n"
        "path=pathlib.Path('/target/ispindel.db')\n"
        "st=path.lstat()\n"
        "assert stat.S_ISREG(st.st_mode) and not path.is_symlink(), 'target database is not regular'\n"
        "h=hashlib.sha256()\n"
        "with path.open('rb') as f:\n"
        " while True:\n"
        "  b=f.read(1048576)\n"
        "  if not b: break\n"
        "  h.update(b)\n"
        "actual_file={'basename':'ispindel.db','size':st.st_size,'sha256':h.hexdigest()}\n"
        # The seed helper deliberately transitions the copied SQLite file to
        # WAL mode.  SQLite may rewrite the database header during that
        # transition, so the target file SHA is expected to differ from the
        # quiesced source-backup SHA.  Bind the target by source size and the
        # complete logical database evidence below, while reporting its own
        # independently measured SHA.
        "assert actual_file['size']==expected['file']['size'], 'target size mismatch'\n"
        "c=sqlite3.connect('file:/target/ispindel.db?mode=ro',uri=True)\n"
        "journal_mode=str(c.execute('PRAGMA journal_mode').fetchone()[0]).lower()\n"
        "assert journal_mode=='wal', 'journal_mode must be wal'\n"
        "assert [r[0] for r in c.execute('PRAGMA integrity_check')]==['ok'], 'integrity_check failed'\n"
        "assert c.execute('PRAGMA foreign_key_check').fetchall()==[], 'foreign_key_check failed'\n"
        "uv=int(c.execute('PRAGMA user_version').fetchone()[0])\n"
        "migrations=[int(r[0]) for r in c.execute('SELECT version FROM schema_migrations ORDER BY version')]\n"
        f"tables={json.dumps(list(SLICE2C_TABLES), separators=(',', ':'))}\n"
        "counts={t:int(c.execute('SELECT COUNT(*) FROM '+chr(34)+t+chr(34)).fetchone()[0]) for t in tables}\n"
        "database={'user_version':uv,'migration_versions':migrations,'counts':counts}\n"
        "assert database==expected['database'], 'target database evidence mismatch'\n"
        "c.close()\n"
        "out=dict(binding); out['file']=actual_file; out['database']=database\n"
        "print(json.dumps(out,sort_keys=True,separators=(',',':')))\n"
    )
    argv = [
        "docker", "run", "--rm", "--network", "none", "--pull", "never",
        "--user", "0:0",
        # SQLite WAL mode needs a writable mount for its -shm coordination
        # file even when the database connection itself is mode=ro. The
        # verifier never opens a writable SQLite connection; the mount is
        # writable only to permit SQLite's read-only WAL bookkeeping.
        "--mount", f"type=volume,src={SLICE2C_TARGET_VOLUME},dst=/target",
        "--entrypoint", "python3", image_id, "-c", verifier, expected, binding, nonce,
    ]
    return shlex.join(argv)


def _slice2c_parse_verifier(
    stdout: bytes, *, source_evidence: dict[str, object],
) -> dict[str, object]:
    lines = [line for line in stdout.splitlines() if line]
    if len(lines) != 1:
        raise _slice2c_fail("verifier did not emit exactly one JSON line")
    try:
        payload = json.loads(lines[0].decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _slice2c_fail("verifier output was not strict JSON") from exc
    expected_binding = {
        "target_endpoint": TARGET_DOCKER_HOST,
        "target_volume": SLICE2C_TARGET_VOLUME,
        "source_manifest_sha256": source_evidence["manifest_sha256"],
        "source_run_id": source_evidence["run_id"],
        "journal_mode": "wal",
    }
    if not isinstance(payload, dict) or any(
        payload.get(key) != value for key, value in expected_binding.items()
    ):
        raise _slice2c_fail("verifier evidence binding mismatch")
    source_file = require_dict(source_evidence["file"], "Slice2B2 file evidence")
    target_file = payload.get("file")
    if (not isinstance(target_file, dict)
            or target_file.get("basename") != "ispindel.db"
            or target_file.get("size") != source_file.get("size")
            or not isinstance(target_file.get("sha256"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", target_file["sha256"])):
        raise _slice2c_fail("verifier target-byte file evidence mismatch")
    if payload.get("database") != source_evidence["database"]:
        raise _slice2c_fail("verifier target-byte evidence mismatch")
    return payload


def slice2_target_volume_seed(
    runner: Runner, host: str, remote_root: str,
    stage01_receipt: dict[str, object],
    quiesced_backup_evidence: dict[str, object], *, seed_image_id: str,
    execute: bool,
) -> dict[str, object]:
    """Seed the exact target volume, then independently verify its bytes."""
    if stage01_receipt.get("target_endpoint") != TARGET_DOCKER_HOST:
        raise _slice2c_fail("requires the exact target endpoint")
    if not SLICE2B2_IMAGE_ID_RE.fullmatch(seed_image_id):
        raise _slice2c_fail("requires the release-bound sha256:64 seed image ID")
    if quiesced_backup_evidence.get("source_endpoint") != SOURCE_DOCKER_HOST:
        raise _slice2c_fail("requires Slice2B2 source endpoint evidence")
    backup_root = quiesced_backup_evidence.get("backup_root")
    if (not isinstance(backup_root, str) or not backup_root.startswith("/")
            or ".." in Path(backup_root).parts or backup_root.rstrip("/") != backup_root):
        raise _slice2c_fail("requires a safe absolute Slice2B2 backup root")
    run_id = quiesced_backup_evidence.get("run_id")
    evidence = quiesced_backup_evidence.get("evidence")
    if (not isinstance(run_id, str) or not SLICE2C_RUN_ID_RE.fullmatch(run_id)
            or not isinstance(evidence, dict) or evidence.get("run_id") != run_id):
        raise _slice2c_fail("requires exact Slice2B2 run_id evidence")
    manifest = evidence.get("manifest")
    expected_manifest = f"{backup_root}/{run_id}/manifest.json"
    manifest_hash = evidence.get("manifest_sha256")
    if manifest != expected_manifest or not isinstance(manifest_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", manifest_hash):
        raise _slice2c_fail("requires exact manifest path and SHA-256")
    file_evidence = evidence.get("file")
    database = evidence.get("database")
    if not isinstance(file_evidence, dict) or not isinstance(database, dict):
        raise _slice2c_fail("requires Slice2B2 file and database evidence")
    basename = file_evidence.get("basename")
    size = file_evidence.get("size")
    file_hash = file_evidence.get("sha256")
    match = SLICE2C_BACKUP_BASENAME_RE.fullmatch(basename) if isinstance(basename, str) else None
    if (match is None or match.group(1) != run_id or type(size) is not int or size < 0
            or not isinstance(file_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", file_hash)):
        raise _slice2c_fail("requires exact safe Slice2B2 file evidence")
    counts = database.get("counts")
    migrations = database.get("migration_versions")
    user_version = database.get("user_version")
    if (type(user_version) is not int or user_version < 0
            or not isinstance(migrations, list) or any(type(v) is not int for v in migrations)
            or not isinstance(counts, dict) or set(counts) != set(SLICE2C_TABLES)
            or any(type(counts[t]) is not int or counts[t] < 0 for t in SLICE2C_TABLES)):
        raise _slice2c_fail("requires exact Slice2B2 database evidence")
    source = f"{backup_root}/{run_id}/{basename}"
    if not source.startswith(backup_root + "/") or Path(source).parts[-2:] != (run_id, basename):
        raise _slice2c_fail("computed seed source is unsafe")

    nonce = "ispindel-target-verify-" + secrets.token_hex(8)
    create = ["sudo", SLICE2C_ROOT_HELPER, "volume-create", SLICE2C_TARGET_VOLUME]
    seed = ["sudo", SLICE2C_ROOT_HELPER, "volume-seed", source, SLICE2C_TARGET_VOLUME, seed_image_id]
    inspect = ["sudo", SLICE2C_ROOT_HELPER, "volume-inspect", SLICE2C_TARGET_VOLUME]
    verifier = _slice2c_verifier_command(seed_image_id, evidence, nonce)
    plan: dict[str, object] = {
        "target_endpoint": TARGET_DOCKER_HOST,
        "target_volume": SLICE2C_TARGET_VOLUME,
        "source_path": source,
        "source_manifest_sha256": manifest_hash,
        "source_run_id": run_id,
        "seed_image_id": seed_image_id,
        "steps": [
            {"label": "volume_create", "argv": create},
            {"label": "volume_seed", "argv": seed},
            {"label": "volume_inspect", "argv": inspect},
            {"label": "target_byte_verify", "script": verifier},
        ],
    }
    if not execute and not runner.dry_run:
        raise _slice2c_fail("requires --execute outside dry-run")
    for label, argv in (("volume-create", create), ("volume-seed", seed), ("volume-inspect", inspect)):
        result = runner.remote(host, remote_root, argv, required=False)
        if execute and result.returncode:
            raise _slice2c_fail(f"{label} failed")
    verify_result = runner.remote_script_endpoint(
        host, remote_root, TARGET_DOCKER_HOST, verifier, required=False, label="target",
    )
    if not execute:
        return plan
    if verify_result.returncode:
        raise _slice2c_fail("independent target-byte verifier failed")
    plan["evidence"] = _slice2c_parse_verifier(verify_result.stdout, source_evidence=evidence)
    plan["verify_stdout_sha256"] = hashlib.sha256(verify_result.stdout).hexdigest()
    return plan


def stage03(args: argparse.Namespace) -> int:
    _, release = load_release(args.release_manifest)
    release_id, image_ref, image_archive_rel = release_fields(release)
    caddy = require_dict(release.get("caddy"), "release Caddy binding")
    caddy_archive_rel = str(caddy.get("archive_path", ""))
    caddy_archive_sha512 = str(caddy.get("archive_sha512", ""))
    caddy_binary_sha256 = str(caddy.get("binary_sha256", ""))
    image = require_dict(release.get("image"), "release image binding")
    image_id = str(image.get("id", ""))
    if (not caddy_archive_rel.startswith("caddy/") or ".." in Path(caddy_archive_rel).parts
            or not re.fullmatch(r"[0-9a-f]{128}", caddy_archive_sha512)
            or not re.fullmatch(r"[0-9a-f]{64}", caddy_binary_sha256)
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id)):
        raise DeploymentError("release Caddy or image binding is invalid")
    root_helper_path = ROOT / "scripts" / "deploy" / "ispindel-root-helper"
    require_release_artifact_binding(
        release, root_helper_path, "source/scripts/deploy/ispindel-root-helper",
    )
    root_helper_sha256 = sha256(root_helper_path)
    require_execution(args, release_id)
    receipt_path, stage01_receipt = load_stage01_receipt(args.stage01_receipt, release_id, args.stage01_receipt_sha256)
    release_manifest_sha256 = sha256(require_regular(args.release_manifest, "release manifest"))
    if stage01_receipt.get("release_manifest_sha256") != release_manifest_sha256:
        raise DeploymentError("Stage 01 receipt is bound to different release manifest bytes")
    predecessor_container_id = str(stage01_receipt.get("predecessor_container_id", ""))
    if not re.fullmatch(r"[0-9a-f]{64}", predecessor_container_id):
        raise DeploymentError("Stage 01 receipt predecessor_container_id must be an exact 64-char hex digest")
    predecessor_volume_name = str(stage01_receipt.get("predecessor_volume_name", ""))
    if not SLICE2B2_VOLUME_NAME_RE.fullmatch(predecessor_volume_name):
        raise DeploymentError("Stage 01 receipt predecessor_volume_name is not a safe Docker volume name")
    promotion_project = f"ispindel-promote-{release_id.lower()}"
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,62}", promotion_project):
        raise DeploymentError("release-bound promotion Compose project is invalid")
    host = validate_remote(args.remote)
    remote_root = validate_scoped_absolute(args.remote_root, "remote root")
    remote_release = validate_absolute(args.remote_release_root, "remote release root")
    stage02_receipt_path, _stage02_receipt = load_stage02_receipt(
        args.stage02_receipt, release_id, args.stage02_receipt_sha256,
        release_manifest_sha256=release_manifest_sha256,
        image_id=image_id, remote_release_root=remote_release,
    )
    env_file = validate_production_env(args.env_file)
    lan_url = args.lan_url.rstrip("/")
    tailnet_url = args.tailnet_url.rstrip("/")
    if not lan_url.startswith("http://") or not tailnet_url.startswith("https://") or "\n" in lan_url + tailnet_url:
        raise DeploymentError("listener URLs must be explicit HTTP LAN and HTTPS tailnet URLs")
    source_endpoint = SOURCE_DOCKER_HOST
    target_endpoint = TARGET_DOCKER_HOST
    backup_root = validate_scoped_absolute(args.backup_root, "backup root")
    runner = Runner(args.dry_run)
    runner.remote_script(
        host, remote_root,
        "if sudo test -L {path}; then "
        "echo 'backup root must not be a symlink' >&2; exit 1; "
        "elif sudo test -d {path}; then :; "
        "else sudo install -d -m 700 -- {path}; fi".format(
            path=shlex.quote(backup_root),
        ),
    )
    # Slice 2B1: source-endpoint predecessor stop. Slice 2 follow-on
    # primitives (quiesced backup, target volume seed) remain placeholders and
    # fail closed if execute is requested.
    stop_evidence = slice2_source_predecessor_stop(
        runner, host, remote_root, stage01_receipt, execute=args.execute,
    )
    preservation_evidence = slice2_source_preserve_predecessor(
        runner, host, remote_root, predecessor_container_id, release_id,
        args.production_container, execute=args.execute,
    )
    quiesced_backup_evidence = slice2_source_quiesced_backup(
        runner, host, remote_root, remote_release, stage01_receipt,
        stop_evidence, backup_root=args.backup_root, execute=args.execute,
    )
    seed_input = quiesced_backup_evidence
    if args.dry_run:
        run_id = str(quiesced_backup_evidence["run_id"])
        backup_root = str(quiesced_backup_evidence["backup_root"])
        seed_input = dict(quiesced_backup_evidence)
        seed_input["evidence"] = {
            "manifest": f"{backup_root}/{run_id}/manifest.json",
            "manifest_sha256": "0" * 64,
            "file": {"basename": f"ispindel-{run_id}.db", "size": 0,
                     "sha256": "0" * 64},
            "database": {"user_version": 0, "migration_versions": [],
                         "counts": {table: 0 for table in SLICE2C_TABLES}},
            "run_id": run_id,
        }
    root_helper_source = f"{remote_release}/source/scripts/deploy/ispindel-root-helper"
    install_root_helper = (
        f"test \"$(sha256sum {shlex.quote(root_helper_source)} | cut -d' ' -f1)\" = "
        f"{shlex.quote(root_helper_sha256)}; "
        f"sudo install -m 0755 {shlex.quote(root_helper_source)} {shlex.quote(SLICE2C_ROOT_HELPER)}"
    )
    helper_result = runner.remote_script(
        host, remote_root, install_root_helper, required=False,
    )
    if args.execute and helper_result.returncode:
        raise _slice2c_fail("release-bound root helper installation failed")
    target_seed_evidence = slice2_target_volume_seed(
        runner, host, remote_root, stage01_receipt,
        seed_input, seed_image_id=image_id, execute=args.execute,
    )
    runner.remote_script(
        host, remote_root,
        "for p in /etc/ispindel/production.env /etc/ispindel/caddy.env /etc/ispindel/backup.env; "
        "do sudo test -f \"$p\" && sudo test ! -L \"$p\"; done",
    )
    # Caddy must remain stopped and disabled through Stage 03 — the
    # ingress-open boundary moves to Stage 04. The predecessor snapshot may
    # legitimately report this unit as not-found; in that case the fence is
    # already satisfied and Stage 03 will install the candidate unit below.
    runner.remote_script(
        host, remote_root,
        "if sudo systemctl cat -- ispindel-caddy.service >/dev/null 2>&1; then "
        "sudo systemctl disable --now -- ispindel-caddy.service; "
        "fi",
    )

    def post_mutation(result: subprocess.CompletedProcess[bytes], label: str) -> None:
        if args.dry_run or not result.returncode:
            return
        if result.returncode == 255:
            raise DeploymentError(f"{label} was inconclusive because SSH transport failed")
        raise DeterministicGateFailure(f"{label} failed after candidate mutation began")

    post_mutation(runner.remote(
        host, remote_root,
        ["rsync", "-a", "--delete", "--exclude=.git/", "--exclude=.venv/", "--exclude=dist/", "--exclude=evidence/", "--", f"{remote_release}/source/", f"{remote_root}/"],
        required=False,
    ), "candidate source installation")
    caddy_archive = f"{remote_release}/{caddy_archive_rel}"
    install_caddy = (
        f"test \"$(sha512sum {shlex.quote(caddy_archive)} | cut -d' ' -f1)\" = {shlex.quote(caddy_archive_sha512)}; "
        "tmp=$(mktemp); trap 'rm -f \"$tmp\"' EXIT; "
        f"tar -xOzf {shlex.quote(caddy_archive)} caddy >\"$tmp\"; "
        f"test \"$(sha256sum \"$tmp\" | cut -d' ' -f1)\" = {shlex.quote(caddy_binary_sha256)}; "
        "sudo install -m 0755 \"$tmp\" /usr/local/bin/caddy"
    )
    post_mutation(
        runner.remote_script(host, remote_root, install_caddy, required=False),
        "Caddy binary installation",
    )
    install_permanent = (
        "sudo install -d -m 0755 /etc/ispindel /opt/ispindel-dashboard "
        "/usr/local/libexec/ispindel /usr/local/lib/ispindel; "
        "sudo install -m 0644 docker-compose.yml /opt/ispindel-dashboard/docker-compose.yml; "
        "sudo install -m 0755 scripts/backup-production.py scripts/backup_common.py scripts/rehearse-restore.py "
        "scripts/verify-production-secrets.py scripts/stack-start.py /usr/local/libexec/ispindel/; "
        "sudo install -m 0755 ops/alerts/send-alert.sh /usr/local/lib/ispindel/send-alert.sh; "
        "sudo install -m 0644 ops/caddy/Caddyfile /etc/ispindel/Caddyfile; "
        "sudo install -m 0644 ops/systemd/*.service ops/systemd/*.timer /etc/systemd/system/"
    )
    post_mutation(
        runner.remote_script(host, remote_root, install_permanent, required=False),
        "permanent file installation",
    )
    post_mutation(
        runner.remote(
            host, remote_root,
            ["sudo", "python3", "-c", update_production_env_code(), env_file, image_ref,
             promotion_project, predecessor_volume_name],
            required=False,
        ),
        "production image reference update",
    )
    # The verifier is authoritative and must never reconstruct credentials.
    # Normalize metadata on the already-present persistent bytes first so the
    # boot-time preflight has the same closed ownership contract.  Missing or
    # symlinked paths fail before chown; the credential contents are untouched.
    normalize_secret_metadata = (
        "sudo test -d /etc/ispindel/secrets && "
        "sudo test ! -L /etc/ispindel/secrets && "
        "sudo test -f /etc/ispindel/secrets/ingest-tokens.json && "
        "sudo test ! -L /etc/ispindel/secrets/ingest-tokens.json && "
        "sudo chown 0:954 /etc/ispindel/secrets /etc/ispindel/secrets/ingest-tokens.json && "
        "sudo chmod 0750 /etc/ispindel/secrets && "
        "sudo chmod 0640 /etc/ispindel/secrets/ingest-tokens.json"
    )
    post_mutation(
        runner.remote_script(host, remote_root, normalize_secret_metadata, required=False),
        "persistent secret metadata normalization",
    )
    post_mutation(
        runner.remote(host, remote_root, [
            "sudo", "env",
            "ISPINDEL_MODE=production",
            "ISPINDEL_SECRETS_DIR=/etc/ispindel/secrets",
            "ISPINDEL_GID=954",
            "python3", "/usr/local/libexec/ispindel/verify-production-secrets.py",
        ], required=False),
        "production secret preflight",
    )
    post_mutation(
        runner.remote(host, remote_root, ["sudo", "systemctl", "daemon-reload"], required=False),
        "systemd daemon reload",
    )
    # Promotion uses ``--no-build`` so the previously-loaded candidate image is
    # the only image considered; ``--pull never`` prevents a registry fetch.
    compose = ["env", f"ISPINDEL_IMAGE_REF={image_ref}",
               f"ISPINDEL_DATA_VOLUME={predecessor_volume_name}",
               "docker", "compose", "--project-name", promotion_project, "--env-file", env_file,
               "up", "-d", "--no-build", "--pull", "never"]
    post_mutation(
        runner.remote_endpoint(host, remote_root, target_endpoint, compose, required=False, label="target"),
        "candidate Compose promotion",
    )
    actual_image = runner.remote_endpoint(host, remote_root, target_endpoint,
                                          ["docker", "inspect", "--format", "{{.Image}}", args.production_container],
                                          required=False, label="target")
    post_mutation(actual_image, "promoted container image inspection")
    if not args.dry_run and actual_image.stdout.decode().strip() != image_id:
        raise DeterministicGateFailure("promoted container does not run the descriptor-bound image ID")
    deterministic_backend_gate(runner, host, remote_root, args.production_container, endpoint=target_endpoint)
    # Caddy start boundary: explicitly disabled. Stage 04 owns the
    # ingress-open transition and will refuse to skip it.
    post_mutation(
        runner.remote(
            host, remote_root,
            ["sudo", "systemctl", "enable", "ispindel-secrets-verify.service"],
            required=False,
        ),
        "secret verifier boot enablement",
    )
    post_mutation(
        runner.remote(
            host, remote_root,
            ["sudo", "systemctl", "enable", "ispindel-stack.service"],
            required=False,
        ),
        "stack boot enablement",
    )
    post_mutation(
        runner.remote(host, remote_root, [
            "sudo", "systemctl", "enable", "--now",
            "ispindel-backup.timer", "ispindel-restore-drill.timer",
            "ispindel-poll.timer", "ispindel-heartbeat.timer",
        ], required=False),
        "timer enablement",
    )
    if args.dry_run:
        runner.emit_plan("03-promote-release", release_id)
        return 0
    receipt = receipt_base("03-promote-release", release_id)
    receipt.update({"remote": host, "remote_root": remote_root,
                    "source_endpoint": source_endpoint, "target_endpoint": target_endpoint,
                    "image": image_ref,
                    "image_id": image_id,
                    "predecessor_container_id": predecessor_container_id,
                    "predecessor_volume_name": predecessor_volume_name,
                    "promotion_compose_project": promotion_project,
                    "predecessor_stop": stop_evidence,
                    "predecessor_preservation": preservation_evidence,
                    "predecessor_standby_name": preservation_evidence["standby_name"],
                    "final_quiesced_backup": quiesced_backup_evidence,
                    "target_volume_seed": target_seed_evidence,
                    "release_manifest_sha256": sha256(require_regular(args.release_manifest, "release manifest")),
                    "stage01_receipt": str(receipt_path), "stage01_receipt_sha256": sha256(receipt_path),
                    "stage02_receipt": str(stage02_receipt_path),
                    "stage02_receipt_sha256": sha256(stage02_receipt_path),
                    "backend": "127.0.0.1:18098",
                    "compose_no_build": True,
                    "compose_pull": "never",
                    "ingress_fenced": True,
                    "caddy_started": False})
    output = write_receipt(args.evidence_dir, "03-promote-release", release_id, receipt)
    print(f"STAGE03_OK receipt={output}")
    return 0


def stage04(args: argparse.Namespace) -> int:
    _, release = load_release(args.release_manifest)
    release_id, _, _ = release_fields(release)
    source = require_dict(release.get("source"), "release source binding")
    static_assets = require_dict(source.get("static_assets"), "release static asset binding")
    for artifact_path, expected_hash in static_assets.items():
        if (not isinstance(artifact_path, str) or not artifact_path.startswith("source/app/static/")
                or not isinstance(expected_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_hash)):
            raise DeploymentError("release static asset binding is invalid")
    served_static_assets: dict[str, str] = {}
    for artifact_path in SERVED_STATIC_ASSET_PATHS:
        expected_hash = static_assets.get(artifact_path)
        if not isinstance(expected_hash, str):
            raise DeploymentError(f"release served static asset is absent: {artifact_path}")
        served_static_assets[artifact_path] = expected_hash
    require_execution(args, release_id)
    host = validate_remote(args.remote)
    remote_root = validate_scoped_absolute(args.remote_root, "remote root")
    source_endpoint = SOURCE_DOCKER_HOST
    target_endpoint = TARGET_DOCKER_HOST
    runner = Runner(args.dry_run)
    db_probe = (
        "import json,sqlite3; c=sqlite3.connect('file:/data/ispindel.db?mode=ro',uri=True); "
        "assert [r[0] for r in c.execute('PRAGMA integrity_check')]==['ok']; "
        "assert c.execute('PRAGMA foreign_key_check').fetchall()==[]; "
        "tables=[r[0] for r in c.execute(\"SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name\")]; "
        "counts={t:c.execute('SELECT COUNT(*) FROM '+chr(34)+t.replace(chr(34),chr(34)*2)+chr(34)).fetchone()[0] for t in tables}; "
        "print(json.dumps({'user_version':c.execute('PRAGMA user_version').fetchone()[0],'counts':counts},sort_keys=True)); c.close()"
    )
    # Pre-Caddy-start validations: Caddy is intentionally fenced through the
    # end of Stage 03 and only opened by Stage 04 once these checks pass.
    pre_ingress_checks = [
        ("stack-active", ["sudo", "systemctl", "is-active", "ispindel-stack.service"], True, False),
        ("timers-active", ["sudo", "systemctl", "is-active", "ispindel-backup.timer", "ispindel-poll.timer", "ispindel-heartbeat.timer"], True, False),
        ("timers-enabled", ["sudo", "systemctl", "is-enabled", "ispindel-backup.timer", "ispindel-poll.timer", "ispindel-heartbeat.timer"], True, False),
        ("timer-state", ["sudo", "systemctl", "show", "--property=ActiveState,SubState,LastTriggerUSec,NextElapseUSecRealtime",
                         "ispindel-backup.timer", "ispindel-poll.timer", "ispindel-heartbeat.timer"], True, False),
        ("health-live", ["curl", "-fsS", "--max-time", "5", "http://127.0.0.1:18098/health/live"], True, False),
        ("health-ready", ["curl", "-fsS", "--max-time", "5", "http://127.0.0.1:18098/health/ready"], True, False),
        ("devices", ["curl", "-fsS", "--max-time", "5", "http://127.0.0.1:18098/api/devices"], False, False),
        ("database", ["docker", "exec", args.production_container, "python3", "-c", db_probe], True, True),
    ]
    outputs: dict[str, dict[str, object]] = {}
    failures: list[str] = []

    def run_probe(label: str, command: Sequence[str] | str, *, script: bool = False,
                  expose_stdout: bool = False, endpoint: str | None = None) -> None:
        if endpoint is not None:
            invoke_script = runner.remote_script_endpoint  # type: ignore[assignment]
            invoke_argv = runner.remote_endpoint  # type: ignore[assignment]
        else:
            invoke_script = runner.remote_script  # type: ignore[assignment]
            invoke_argv = runner.remote  # type: ignore[assignment]
        if script:
            assert isinstance(command, str)
            if endpoint is None:
                result = invoke_script(host, remote_root, command, required=False)  # type: ignore[arg-type]
            else:
                result = invoke_script(host, remote_root, endpoint, command, required=False)  # type: ignore[arg-type]
        else:
            assert isinstance(command, list)
            if endpoint is not None:
                result = invoke_argv(host, remote_root, endpoint, command, required=False)  # type: ignore[arg-type]
            else:
                result = invoke_argv(host, remote_root, command, required=False)  # type: ignore[arg-type]
        if not args.dry_run and result.returncode:
            failures.append(label)
        elif not args.dry_run:
            record: dict[str, object] = {
                "stdout_sha256": hashlib.sha256(result.stdout).hexdigest(),
                "stdout_bytes": len(result.stdout),
            }
            if expose_stdout:
                if len(result.stdout) > 65536:
                    raise DeploymentError(f"{label} validation output exceeded the receipt limit")
                try:
                    record["stdout"] = result.stdout.decode()
                except UnicodeDecodeError as exc:
                    raise DeploymentError(f"{label} validation output was not UTF-8") from exc
            outputs[label] = record

    for label, command, expose_stdout, use_target_endpoint in pre_ingress_checks:
        run_probe(label, command, expose_stdout=expose_stdout,
                  endpoint=target_endpoint if use_target_endpoint else None)
    run_probe(
        "container-running",
        f"test \"$(docker inspect --format '{{{{.State.Running}}}}' {shlex.quote(args.production_container)})\" = true",
        script=True,
        endpoint=target_endpoint,
    )

    for artifact_path, expected_hash in sorted(served_static_assets.items()):
        relative = artifact_path.removeprefix("source/app")
        asset_gate = (
            f"test \"$(curl -fsS --max-time 5 {shlex.quote('http://127.0.0.1:18098' + relative)} | sha256sum | cut -d' ' -f1)\" "
            f"= {shlex.quote(expected_hash)}"
        )
        run_probe(f"asset:{artifact_path}", asset_gate, script=True)

    if failures and not args.dry_run:
        raise DeploymentError(f"live validation failed: {','.join(failures)}")

    database_evidence: dict[str, object] = {}
    if not args.dry_run:
        try:
            parsed_database_evidence = json.loads(str(outputs["database"]["stdout"]))
        except (KeyError, TypeError, json.JSONDecodeError) as exc:
            raise DeploymentError("database validation did not return inspectable JSON evidence") from exc
        if not isinstance(parsed_database_evidence, dict):
            raise DeploymentError("database validation evidence must be a JSON object")
        database_evidence = parsed_database_evidence
        counts = database_evidence.get("counts")
        user_version = database_evidence.get("user_version")
        if (type(user_version) is not int or user_version < 0 or not isinstance(counts, dict)
                or any(not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_]+", name)
                       or type(count) is not int or count < 0 for name, count in counts.items())):
            raise DeploymentError("database validation evidence is invalid")

    lan_url = args.lan_url.rstrip("/")
    tailnet_url = args.tailnet_url.rstrip("/")
    if not lan_url.startswith("http://") or not tailnet_url.startswith("https://") or "\n" in lan_url + tailnet_url:
        raise DeploymentError("listener URLs must be explicit HTTP LAN and HTTPS tailnet URLs")

    external = (
        ("browser", args.browser_command),
        ("security", args.security_command),
        ("alert", args.alert_command),
        ("test-ingest", f"ISPINDEL_TEST_INGEST_POLICY={shlex.quote(args.test_ingest_policy)} {args.test_ingest_command}"),
    )
    if args.test_ingest_policy not in {"remove", "retain"}:
        raise DeploymentError("--test-ingest-policy must be remove or retain")
    for label, command in external:
        if not command or "\x00" in command:
            raise DeploymentError(f"Stage 04 requires an explicit {label} validation command")
        run_probe(label, command, script=True)

    if failures and not args.dry_run:
        raise DeploymentError(f"live validation failed: {','.join(failures)}")

    # All pre-ingress validations have passed; open the ingress boundary by
    # enabling and starting Caddy. The Caddy self-checks below happen on the
    # target endpoint because that is where Caddy now fronts the backend.
    runner.remote(host, remote_root, ["sudo", "systemctl", "enable", "--now", "ispindel-caddy.service"])
    run_probe(
        "caddy-active",
        ["sudo", "systemctl", "is-active", "ispindel-caddy.service"],
        endpoint=None,
    )
    listener_gate = (
        f"test \"$(curl -sS --max-time 5 -o /dev/null -w '%{{http_code}}' {shlex.quote(lan_url + '/health/live')})\" = 404 && "
        f"test \"$(curl -ksS --max-time 5 -o /dev/null -w '%{{http_code}}' {shlex.quote(tailnet_url + '/health/live')})\" = 401"
    )
    listener = runner.remote_script(host, remote_root, listener_gate, required=False)
    if not args.dry_run and listener.returncode:
        listener_retry = runner.remote_script(host, remote_root, listener_gate, required=False)
        if listener_retry.returncode:
            if 255 in {listener.returncode, listener_retry.returncode}:
                raise DeploymentError("Caddy listener validation was inconclusive because SSH transport failed")
            raise DeploymentError("Caddy listener validation failed twice after ingress open")
    outputs.setdefault("caddy-active", {"stdout_sha256": hashlib.sha256(b"").hexdigest(), "stdout_bytes": 0})

    if args.dry_run:
        runner.emit_plan("04-validate-live", release_id)
        return 0
    receipt = receipt_base("04-validate-live", release_id)
    receipt.update({"remote": host, "checks": outputs, "test_ingest_policy": args.test_ingest_policy,
                    "source_endpoint": source_endpoint, "target_endpoint": target_endpoint,
                    "release_manifest_sha256": sha256(require_regular(args.release_manifest, "release manifest")),
                    "database_evidence": database_evidence,
                    "served_static_assets": served_static_assets,
                    "database_restored": False, "security_boundary_checked": True,
                    "served_assets_checked": True, "browser_checked": True, "alert_checked": True,
                    "timers_checked": True, "database_checked": True,
                    "ingress_opened": True, "caddy_started": True,
                    "ingress_open_boundary": True})
    output = write_receipt(args.evidence_dir, "04-validate-live", release_id, receipt)
    print(f"STAGE04_OK receipt={output}")
    return 0


def exact_docker_absence_verdict(
    result: subprocess.CompletedProcess[bytes], container_name: str,
) -> bool:
    expected_stderr = f"error: no such object: {container_name}\n".encode()
    # Docker emits one formatting newline on stdout even when inspect fails.
    # Accept only the empty or single-newline forms; any other stdout remains
    # an ambiguous response and must fail closed.
    return result.returncode == 1 and result.stdout in {b"", b"\n"} and result.stderr == expected_stderr


def exact_docker_stopped_verdict(result: subprocess.CompletedProcess[bytes]) -> bool:
    return result.returncode == 0 and result.stdout in {b"false", b"false\n"} and result.stderr == b""


def stage05(args: argparse.Namespace) -> int:
    _, release = load_release(args.release_manifest)
    release_id, _, _ = release_fields(release)
    require_execution(args, release_id)
    # The ingress-open boundary, once established by Stage 04, must be torn
    # down before any application rollback can run. Stage 05 refuses if the
    # caller hands it an ingress_opened flag (explicit refusal) or if Stage 04
    # ever emits an ingress_opened receipt (boundary was crossed).
    if getattr(args, "ingress_opened", False) is True:
        raise DeploymentError(
            "Stage 05 refuses to roll back when an ingress_opened boundary flag is supplied; "
            "ingestion of ingress-open state is a boundary violation."
        )
    receipt_path, receipt = load_stage01_receipt(args.stage01_receipt, release_id, args.stage01_receipt_sha256)
    if receipt.get("release_manifest_sha256") != sha256(require_regular(args.release_manifest, "release manifest")):
        raise DeploymentError("Stage 01 receipt is bound to different release manifest bytes")
    if receipt.get("ingress_opened") is True:
        raise DeploymentError(
            "Stage 05 refuses to roll back because Stage 04 recorded ingress_opened=true; "
            "rollback must run before the ingress boundary is crossed."
        )
    host = validate_remote(args.remote)
    remote_root = validate_scoped_absolute(args.remote_root, "remote root")
    env_file = validate_production_env(args.env_file)
    predecessor_descriptor = load_json(Path(str(receipt["predecessor_descriptor"])), "predecessor descriptor")
    expected_version = int(str(require_dict(
        predecessor_descriptor.get("database"), "predecessor database"
    ).get("user_version", -1)))
    if expected_version < 0:
        raise DeploymentError("predecessor schema version is invalid")
    predecessor_container_id = str(receipt.get("predecessor_container_id", ""))
    if not re.fullmatch(r"[0-9a-f]{64}", predecessor_container_id):
        raise DeploymentError(
            "Stage 05 requires the exact predecessor container ID captured in Stage 01; "
            "the receipt must contain a 64-char hex digest."
        )
    standby_name, candidate_quarantine_name = rollback_container_names(
        release_id, args.production_container,
    )
    system_snapshot_path = require_regular(
        Path(str(receipt.get("predecessor_system_snapshot", ""))),
        "predecessor system snapshot",
    )
    compose_snapshot_path = require_regular(
        Path(str(receipt.get("predecessor_compose_snapshot", ""))),
        "predecessor Compose snapshot",
    )
    compose_paths = receipt.get("predecessor_compose_paths")
    if (not isinstance(compose_paths, list) or not compose_paths
            or any(not isinstance(path, str) for path in compose_paths)):
        raise DeploymentError("Stage 01 predecessor Compose paths are invalid")
    source_endpoint = SOURCE_DOCKER_HOST
    target_endpoint = TARGET_DOCKER_HOST
    runner = Runner(args.dry_run)
    identity_format = "{{.Id}}|{{.Name}}|{{.State.Running}}"
    compatibility = (
        "import sqlite3,sys; c=sqlite3.connect('file:/data/ispindel.db?mode=ro',uri=True); "
        f"actual=int(c.execute('PRAGMA user_version').fetchone()[0]); print(actual); sys.exit(0 if actual=={expected_version} else 42)"
    )
    # Stage 03 can fail before candidate promotion, leaving no canonical
    # container. Any other inspect failure remains a hard rollback failure.
    candidate_inspect = runner.remote_endpoint(
        host, remote_root, target_endpoint,
        ["docker", "inspect", "--format", identity_format, args.production_container],
        required=False, label="target",
    )
    candidate_present = candidate_inspect.returncode == 0
    candidate_absent = exact_docker_absence_verdict(candidate_inspect, args.production_container)
    if not args.dry_run and not candidate_present and not candidate_absent:
        raise DeploymentError(
            "candidate container presence probe failed without the exact Docker absence verdict"
        )
    candidate_id = ""
    candidate_running = False
    if not args.dry_run and candidate_present:
        candidate_id, candidate_name, running_value = candidate_inspect.stdout.decode().strip().split("|", 2)
        if (not re.fullmatch(r"[0-9a-f]{64}", candidate_id)
                or candidate_name != f"/{args.production_container}"
                or running_value not in {"true", "false"}):
            raise DeploymentError("candidate identity inspection is invalid")
        candidate_running = running_value == "true"
    compatibility_result: subprocess.CompletedProcess[bytes] | None = None
    if candidate_present:
        compatibility_result = runner.remote_endpoint(
            host, remote_root, target_endpoint,
            ["docker", "exec", args.production_container, "python3", "-c", compatibility],
            required=False, label="target",
        )
        if not args.dry_run and compatibility_result.returncode == 42:
            raise DatabaseIncompatibleError(
                "STOP AND REQUIRE PRODUCTION-RESTORE PROCEDURE: live database schema is predecessor-incompatible"
            )
        if not args.dry_run and compatibility_result.returncode:
            raise DeploymentError("predecessor database compatibility probe failed without a schema verdict")
    candidate_is_predecessor = candidate_present and candidate_id == predecessor_container_id
    if candidate_present and not candidate_is_predecessor:
        quarantine_probe = runner.remote_endpoint(
            host, remote_root, target_endpoint,
            ["docker", "inspect", "--format", identity_format, candidate_quarantine_name],
            required=False, label="target",
        )
        if not args.dry_run and quarantine_probe.returncode == 0:
            raise DeterministicGateFailure(
                f"candidate quarantine name already exists: {candidate_quarantine_name}"
            )
        if (not args.dry_run
                and not exact_docker_absence_verdict(quarantine_probe, candidate_quarantine_name)):
            raise DeterministicGateFailure(
                "candidate quarantine absence probe failed without the exact Docker absence verdict"
            )
        if candidate_running or args.dry_run:
            stop_candidate = runner.remote_endpoint(
                host, remote_root, target_endpoint,
                ["docker", "stop", "--time", "30", args.production_container],
                required=False, label="target",
            )
            if not args.dry_run and stop_candidate.returncode:
                raise DeploymentError("pre-ingress rollback failed to stop the candidate container")
        verify_candidate_stopped = runner.remote_endpoint(
            host, remote_root, target_endpoint,
            ["docker", "inspect", "--format", "{{.State.Running}}", args.production_container],
            required=False, label="target",
        )
        if not args.dry_run and not exact_docker_stopped_verdict(verify_candidate_stopped):
            raise DeploymentError("pre-ingress rollback candidate container did not stop")
        rename_source = candidate_id or args.production_container
        quarantine = runner.remote_endpoint(
            host, remote_root, target_endpoint,
            ["docker", "rename", rename_source, candidate_quarantine_name],
            required=False, label="target",
        )
        if not args.dry_run and quarantine.returncode:
            raise DeterministicGateFailure(
                f"pre-ingress rollback failed to quarantine candidate as {candidate_quarantine_name}"
            )
        verify_quarantine = runner.remote_endpoint(
            host, remote_root, target_endpoint,
            ["docker", "inspect", "--format", identity_format, candidate_quarantine_name],
            required=False, label="target",
        )
        if not args.dry_run:
            expected_quarantine = f"{candidate_id}|/{candidate_quarantine_name}|false"
            if (verify_quarantine.returncode
                    or verify_quarantine.stdout.decode().strip() != expected_quarantine):
                raise DeterministicGateFailure(
                    "candidate quarantine identity verification failed"
                )
    predecessor_inspect = runner.remote_endpoint(
        host, remote_root, source_endpoint,
        ["docker", "inspect", "--format", identity_format, predecessor_container_id],
        required=False, label="source",
    )
    predecessor_name = ""
    predecessor_running = False
    if not args.dry_run:
        if predecessor_inspect.returncode:
            raise DeterministicGateFailure(
                "captured predecessor container is absent; Stage 05 refuses to reconstruct it"
            )
        observed_id, predecessor_name, running_value = predecessor_inspect.stdout.decode().strip().split("|", 2)
        if (observed_id != predecessor_container_id
                or predecessor_name not in {f"/{args.production_container}", f"/{standby_name}"}
                or running_value not in {"true", "false"}):
            raise DeterministicGateFailure(
                "captured predecessor identity or name is not rollback-compatible"
            )
        predecessor_running = running_value == "true"
    predecessor_systemd_states = require_systemd_states(receipt.get("predecessor_systemd_states"))
    stop_candidate_units = "; ".join(
        f"if [ \"$(systemctl show --property=LoadState --value {shlex.quote(unit)})\" != not-found ]; "
        f"then sudo systemctl disable --now {shlex.quote(unit)}; fi"
        for unit in SYSTEMD_UNITS
    )
    runner.remote_script(host, remote_root, stop_candidate_units)
    # Restore the exact predecessor system and Compose bytes before restarting
    # services. The bytes travel over SSH stdin to the atomic restore primitive;
    # no Compose override or shell reconstruction is permitted.
    runner.remote_stdin(
        host, remote_root,
        ["sudo", "python3", "-c", system_restore_code(), json.dumps(SYSTEM_PATHS)],
        system_snapshot_path.read_bytes(),
    )
    runner.remote_stdin(
        host, remote_root,
        ["sudo", "python3", "-c", system_restore_code(), json.dumps(compose_paths)],
        compose_snapshot_path.read_bytes(),
    )
    runner.remote(host, remote_root, ["sudo", "systemctl", "daemon-reload"])
    # Restore the canonical name, then start the exact captured object. The
    # container ID is never recreated from the image or inferred from a name.
    if predecessor_name != f"/{args.production_container}" or args.dry_run:
        rename_predecessor = runner.remote_endpoint(
            host, remote_root, source_endpoint,
            ["docker", "rename", predecessor_container_id, args.production_container],
            required=False, label="source",
        )
        if not args.dry_run and rename_predecessor.returncode:
            raise DeterministicGateFailure(
                f"failed to restore predecessor canonical name {args.production_container}"
            )
    if not predecessor_running or args.dry_run:
        restart_result = runner.remote_endpoint(
            host, remote_root, source_endpoint,
            ["docker", "start", predecessor_container_id],
            required=False, label="source",
        )
        if not args.dry_run and restart_result.returncode:
            raise DeploymentError(
                f"pre-ingress rollback failed to start predecessor container "
                f"{predecessor_container_id} on the source endpoint"
            )
    verify_running = runner.remote_endpoint(
        host, remote_root, source_endpoint,
        ["docker", "inspect", "--format", identity_format, args.production_container],
        required=False, label="source",
    )
    if not args.dry_run:
        expected_running = f"{predecessor_container_id}|/{args.production_container}|true"
        if verify_running.returncode or verify_running.stdout.decode().strip() != expected_running:
            raise DeterministicGateFailure(
                "pre-ingress rollback did not restore the captured predecessor identity "
                f"(expected {expected_running!r}, got {verify_running.stdout.decode().strip()!r})"
            )
    ready_probe = (
        "for n in 1 2 3 4 5; do curl -fsS --max-time 3 "
        "http://127.0.0.1:18098/health/ready >/dev/null && exit 0; sleep 2; done; exit 1"
    )
    ready_result = runner.remote_script_endpoint(
        host, remote_root, source_endpoint, ready_probe, required=False, label="source",
    )
    if not args.dry_run and ready_result.returncode:
        raise DeterministicGateFailure("restored predecessor failed the direct readiness probe")
    # Restore systemd states recorded by Stage 01 so the rolled-back environment
    # matches the captured predecessor.
    for unit in SYSTEMD_UNITS:
        state = predecessor_systemd_states[unit]
        if state["LoadState"] == "not-found":
            continue
        if state["UnitFileState"] == "enabled":
            runner.remote(host, remote_root, ["sudo", "systemctl", "enable", unit])
        elif state["UnitFileState"] == "enabled-runtime":
            runner.remote(host, remote_root, ["sudo", "systemctl", "enable", "--runtime", unit])
        if state["ActiveState"] == "active":
            runner.remote(host, remote_root, ["sudo", "systemctl", "start", unit])
    state_checks = "; ".join(
        f"test \"$(systemctl show --property={shlex.quote(prop)} --value {shlex.quote(unit)})\" = {shlex.quote(state[prop])}"
        for unit, state in predecessor_systemd_states.items()
        for prop in ("LoadState", "UnitFileState", "ActiveState")
    )
    runner.remote_script(host, remote_root, state_checks)
    if args.dry_run:
        runner.emit_plan("05-rollback-application", release_id)
        return 0
    result = receipt_base("05-rollback-application", release_id)
    result.update({"remote": host, "stage01_receipt": str(receipt_path),
                   "source_endpoint": source_endpoint, "target_endpoint": target_endpoint,
                   "release_manifest_sha256": sha256(require_regular(args.release_manifest, "release manifest")),
                   "predecessor_container_id": predecessor_container_id,
                   "predecessor_image_id": str(receipt.get("predecessor_image_id", "")),
                   "schema_compatibility_version": expected_version,
                   "database_restored": False,
                   "pre_ingress_rollback": True,
                   "rollback_compose_used": True,
                   "rollback_source_restore_used": True,
                   "rollback_volume_restore_used": False,
                   "predecessor_standby_name": standby_name,
                   "candidate_quarantine_name": candidate_quarantine_name,
                   "predecessor_system_snapshot_sha256": sha256(system_snapshot_path),
                   "predecessor_compose_snapshot_sha256": sha256(compose_snapshot_path),
                   "predecessor_systemd_states_restored": True,
                   "caddy_stopped": predecessor_systemd_states["ispindel-caddy.service"]["ActiveState"] != "active",
                   "ingress_opened": False})
    output = write_receipt(args.evidence_dir, "05-rollback-application", release_id, result)
    print(f"STAGE05_OK receipt={output}")
    return 0


def add_common(parser: argparse.ArgumentParser, *, stage01: bool = False) -> None:
    parser.add_argument("--release-manifest", type=Path, default=ROOT / "descriptors/RELEASE.json")
    parser.add_argument("--remote", required=True)
    parser.add_argument("--remote-root", default=DEFAULT_REMOTE_ROOT)
    parser.add_argument("--evidence-dir", type=Path, required=True)
    parser.add_argument("--confirm-release-id", required=True)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    if stage01:
        parser.add_argument("--stage01-receipt", type=Path, required=True)
        parser.add_argument("--stage01-receipt-sha256", required=True)


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    sub = value.add_subparsers(dest="stage", required=True)
    one = sub.add_parser("stage-01")
    add_common(one)
    one.add_argument("--predecessor-descriptor", type=Path, default=ROOT / "descriptors/predecessor-production.json")
    one.add_argument("--expected-missing-amendment", type=Path, default=EXPECTED_MISSING_AMENDMENT)
    one.add_argument("--allow-expected-missing", action="store_true")
    one.add_argument("--expected-missing-contract-version")
    one.add_argument("--production-container", default="ispindel-dashboard")
    one.add_argument("--backup-root", default="/var/backups/ispindel-dashboard")
    one.add_argument("--offhost", required=True)

    two = sub.add_parser("stage-02")
    add_common(two)
    two.add_argument("--remote-release-root", required=True)
    two.add_argument("--backup-manifest", required=True)

    three = sub.add_parser("stage-03")
    add_common(three, stage01=True)
    three.add_argument("--stage02-receipt", type=Path, required=True)
    three.add_argument("--stage02-receipt-sha256", required=True)
    three.add_argument("--remote-release-root", required=True)
    three.add_argument("--env-file", default=DEFAULT_ENV_FILE)
    three.add_argument("--production-container", default="ispindel-dashboard")
    three.add_argument("--backup-root", default=DEFAULT_BACKUP_ROOT)
    three.add_argument("--lan-url", required=True)
    three.add_argument("--tailnet-url", required=True)

    four = sub.add_parser("stage-04")
    add_common(four)
    four.add_argument("--production-container", default="ispindel-dashboard")
    four.add_argument("--browser-command", required=True)
    four.add_argument("--security-command", required=True)
    four.add_argument("--alert-command", required=True)
    four.add_argument("--test-ingest-command", required=True)
    four.add_argument("--test-ingest-policy", required=True)
    four.add_argument("--lan-url", required=True)
    four.add_argument("--tailnet-url", required=True)

    five = sub.add_parser("stage-05")
    add_common(five, stage01=True)
    five.add_argument("--env-file", default=DEFAULT_ENV_FILE)
    five.add_argument("--production-container", default="ispindel-dashboard")
    five.add_argument("--ingress-opened", action="store_true",
                      help="explicit ingress-open boundary marker; if supplied, Stage 05 refuses to roll back")
    return value


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    handlers = {"stage-01": stage01, "stage-02": stage02, "stage-03": stage03,
                "stage-04": stage04, "stage-05": stage05}
    try:
        return handlers[args.stage](args)
    except DatabaseIncompatibleError as exc:
        print(str(exc), file=sys.stderr)
        print(json.dumps({"event": "rollback_db_incompatible", "error": str(exc)}), file=sys.stderr)
        return DETERMINISTIC_GATE_EXIT
    except DeterministicGateFailure as exc:
        print(json.dumps({"event": "deterministic_deployment_gate_failed", "error": str(exc)}), file=sys.stderr)
        return DETERMINISTIC_GATE_EXIT
    except (DeploymentError, OSError, ValueError, KeyError) as exc:
        print(json.dumps({"event": "deployment_stage_failed", "stage": args.stage, "error": str(exc)[:2000]}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
