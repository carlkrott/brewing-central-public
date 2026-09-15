#!/usr/bin/env python3
"""Build a closed, offline-verifiable iSpindel release payload and archive."""
from __future__ import annotations

import argparse
import atexit
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import socket
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
from pathlib import Path
from typing import Any, Sequence

EXPECTED_CADDY_VERSION = "v2.11.4"
RELEASE_ID_RE = re.compile(r"^[0-9]{8}T[0-9]{6}Z-[0-9a-f]{12}$")


class BuildError(RuntimeError):
    pass


def run(argv: Sequence[str], *, cwd: Path | None = None, env: dict[str, str] | None = None,
        capture: bool = True) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(
        list(argv), cwd=cwd, env=env, text=True, capture_output=capture, check=False,
    )
    if proc.returncode:
        detail = (proc.stderr or proc.stdout or "command failed").strip().splitlines()[-1]
        raise BuildError(f"command failed ({argv[0]}): {detail}")
    return proc


def sha(path: Path, algorithm: str = "sha256") -> str:
    h = hashlib.new(algorithm)
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def canonical_sha(value: Any) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    return hashlib.sha256(raw).hexdigest()


def atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(temporary, 0o644)
    os.replace(temporary, path)


def tree_sha(root: Path, prefix: str) -> str:
    rows: list[str] = []
    for path in sorted(item for item in (root / prefix).rglob("*") if item.is_file()):
        rel = path.relative_to(root).as_posix()
        rows.append(f"{rel}\0{path.stat().st_size}\0{sha(path)}\n")
    if not rows:
        raise BuildError(f"empty required tree: {prefix}")
    return hashlib.sha256("".join(rows).encode()).hexdigest()


def copy_git_tree(root: Path, destination: Path, commit: str) -> None:
    destination.mkdir(parents=True, exist_ok=False)
    archive = subprocess.Popen(
        ["git", "archive", "--format=tar", commit], cwd=root, stdout=subprocess.PIPE,
    )
    assert archive.stdout is not None
    extract = subprocess.run(["tar", "-x", "-C", str(destination)], stdin=archive.stdout, check=False)
    archive.stdout.close()
    archive_rc = archive.wait()
    if archive_rc or extract.returncode:
        raise BuildError("git archive extraction failed")


def run_logged(argv: Sequence[str], log: Path, *, cwd: Path, env: dict[str, str] | None = None) -> None:
    started = dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")
    proc = subprocess.run(list(argv), cwd=cwd, env=env, text=True, capture_output=True, check=False)
    with log.open("a", encoding="utf-8") as handle:
        handle.write(f"===== command={json.dumps(list(argv))} started={started} exit={proc.returncode} =====\n")
        handle.write(proc.stdout)
        handle.write(proc.stderr)
        if not proc.stdout.endswith("\n") and not proc.stderr.endswith("\n"):
            handle.write("\n")
    sys.stdout.write(proc.stdout)
    sys.stderr.write(proc.stderr)
    if proc.returncode:
        raise BuildError(f"test command failed: {argv[0]}")


def choose_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def wait_live(container: str, expected_port: int, timeout: float = 75.0) -> None:
    deadline = time.monotonic() + timeout
    last = ""
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{expected_port}/health/live", timeout=2) as response:
                body = json.load(response)
                if response.status == 200 and body == {"status": "ok", "service": "ispindel-dashboard"}:
                    inspect = json.loads(run(["docker", "inspect", container]).stdout)[0]
                    bindings = inspect["NetworkSettings"]["Ports"].get("8098/tcp") or []
                    if bindings == [{"HostIp": "127.0.0.1", "HostPort": str(expected_port)}]:
                        return
                    last = "PublishedBindingMismatch"
        except Exception as exc:  # bounded diagnostic only
            last = type(exc).__name__
        time.sleep(1)
    raise BuildError(f"disposable release container did not become live: {last}")


def docker_save_gzip(reference: str, output: Path) -> None:
    temporary = output.with_suffix(output.suffix + ".partial")
    save = subprocess.Popen(["docker", "save", reference], stdout=subprocess.PIPE)
    assert save.stdout is not None
    with temporary.open("wb") as handle:
        compress = subprocess.run(
            ["gzip", "-n", "-9"], stdin=save.stdout, stdout=handle, check=False,
        )
        handle.flush()
        os.fsync(handle.fileno())
    save.stdout.close()
    save_rc = save.wait()
    if save_rc or compress.returncode:
        temporary.unlink(missing_ok=True)
        raise BuildError("Docker image export failed")
    os.chmod(temporary, 0o644)
    os.replace(temporary, output)


def docker_archive_config_sha(path: Path) -> str:
    try:
        with tarfile.open(path, "r:gz") as bundle:
            manifest_handle = bundle.extractfile("manifest.json")
            if manifest_handle is None:
                raise BuildError("Docker export lacks manifest.json")
            manifest = json.load(manifest_handle)
            if not isinstance(manifest, list) or len(manifest) != 1:
                raise BuildError("Docker export does not contain exactly one image")
            config_name = manifest[0]["Config"]
            config_handle = bundle.extractfile(config_name)
            if config_handle is None:
                raise BuildError("Docker export lacks its config blob")
            return hashlib.sha256(config_handle.read()).hexdigest()
    except (tarfile.TarError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise BuildError("Docker export metadata is invalid") from exc


def artifact_inventory(payload: Path) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    for path in sorted(item for item in payload.rglob("*") if item.is_file()):
        if path == payload / "RELEASE.json":
            continue
        if path.is_symlink():
            raise BuildError(f"symlink in payload: {path}")
        mode = stat.S_IMODE(path.stat().st_mode)
        if mode not in {0o644, 0o755}:
            mode = 0o755 if mode & 0o111 else 0o644
            os.chmod(path, mode)
        result.append({
            "path": path.relative_to(payload).as_posix(),
            "size": path.stat().st_size,
            "mode": mode,
            "sha256": sha(path),
        })
    return result


def create_archive(payload: Path, output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="ispindel-release-tar-") as td:
        raw_tar = Path(td) / "release.tar"
        with tarfile.open(raw_tar, "w", format=tarfile.GNU_FORMAT) as bundle:
            for path in sorted(payload.rglob("*")):
                rel = path.relative_to(payload).as_posix()
                info = bundle.gettarinfo(str(path), arcname=rel)
                info.uid = info.gid = 0
                info.uname = info.gname = "root"
                info.mtime = 0
                info.pax_headers = {}
                if info.isdir():
                    info.mode = 0o755
                    bundle.addfile(info)
                elif info.isfile():
                    info.mode = stat.S_IMODE(path.stat().st_mode)
                    with path.open("rb") as handle:
                        bundle.addfile(info, handle)
                else:
                    raise BuildError(f"special archive member forbidden: {rel}")
        partial = output.with_suffix(output.suffix + ".partial")
        run(["zstd", "-T0", "-19", "--no-progress", "-f", str(raw_tar), "-o", str(partial)])
        os.chmod(partial, 0o644)
        os.replace(partial, output)


def safe_extract(archive: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=False)
    with tempfile.TemporaryDirectory(prefix="ispindel-release-extract-") as td:
        raw = Path(td) / "release.tar"
        run(["zstd", "-d", "--no-progress", "-f", str(archive), "-o", str(raw)])
        with tarfile.open(raw, "r:") as bundle:
            for member in bundle.getmembers():
                path = Path(member.name)
                if path.is_absolute() or ".." in path.parts or member.issym() or member.islnk():
                    raise BuildError(f"unsafe archive member: {member.name}")
            bundle.extractall(destination, filter="data")


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument("--caddy-cache", type=Path, default=Path.home() / ".cache/ispindel-dashboard/caddy-v2.11.4")
    return value


def main() -> int:
    args = parser().parse_args()
    root = Path(run(["git", "rev-parse", "--show-toplevel"]).stdout.strip()).resolve()
    if Path.cwd().resolve() != root:
        raise BuildError(f"run builder from repository root: {root}")
    docker_host_override = os.environ.get("DOCKER_HOST", "")
    if docker_host_override not in {"", "unix:///var/run/docker.sock"}:
        raise BuildError("release build requires the local Docker Unix socket")
    docker_context = run(["docker", "context", "show"]).stdout.strip()
    context_host = run([
        "docker", "context", "inspect", docker_context,
        "--format", "{{.Endpoints.docker.Host}}",
    ]).stdout.strip()
    if context_host != "unix:///var/run/docker.sock":
        raise BuildError("active Docker context is not the local Unix socket")
    if run(["git", "status", "--porcelain"], cwd=root).stdout:
        raise BuildError("Git tree must be clean before release build")
    commit = run(["git", "rev-parse", "HEAD"], cwd=root).stdout.strip()
    short = commit[:12]
    release_id = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + short
    if not RELEASE_ID_RE.fullmatch(release_id):
        raise BuildError("generated release ID is invalid")
    output_root = (root / "dist/releases" / release_id).resolve()
    if output_root.exists():
        raise BuildError(f"release output already exists: {output_root}")

    caddy_archive = args.caddy_cache / "caddy_2.11.4_linux_amd64.tar.gz"
    caddy_binary = args.caddy_cache / "caddy"
    pin = (root / "release/caddy-v2.11.4.sha512").read_text(encoding="ascii")
    expected_sha512, expected_name = pin.rstrip("\n").split("  ", 1)
    if expected_name != caddy_archive.name or sha(caddy_archive, "sha512") != expected_sha512:
        raise BuildError("cached Caddy tarball does not match committed SHA-512 pin")
    if run([str(caddy_binary), "version"]).stdout.split()[0] != EXPECTED_CADDY_VERSION:
        raise BuildError("cached Caddy binary version mismatch")
    with tarfile.open(caddy_archive, "r:gz") as bundle:
        member = next((item for item in bundle.getmembers() if item.name == "caddy" and item.isfile()), None)
        if member is None:
            raise BuildError("cached Caddy archive lacks binary")
        handle = bundle.extractfile(member)
        assert handle is not None
        if hashlib.sha256(handle.read()).hexdigest() != sha(caddy_binary):
            raise BuildError("cached Caddy binary does not match tarball")

    descriptor_path = root / "descriptors/phase7b-authority.json"
    phase_descriptor_live = json.loads(descriptor_path.read_text(encoding="utf-8"))
    authority_path = Path(phase_descriptor_live["authority"]["path"]).resolve()
    if not authority_path.is_file() or authority_path.is_symlink():
        raise BuildError("Phase 7B repaired authority path is absent or unsafe")
    if sha(authority_path) != phase_descriptor_live["authority"]["sha256"]:
        raise BuildError("Phase 7B repaired authority digest differs from descriptor")
    authority = run([
        sys.executable, "scripts/verify-phase7b-authority.py",
        "--authority", str(authority_path),
    ], cwd=root)
    if "PHASE7B_AUTHORITY_OK" not in authority.stdout:
        raise BuildError("Phase 7B repaired authority did not verify")

    required_tracked = [
        "docker-compose.yml", "release/manifest.schema.json", "release/caddy-v2.11.4.sha512",
        "scripts/build-release.sh", "scripts/build_release.py", "scripts/verify-release.py",
        "scripts/run-tests.sh", "tests/test_release.py",
    ]
    tracked = subprocess.run(
        ["git", "ls-files", "--error-unmatch", "--", *required_tracked],
        cwd=root, text=True, capture_output=True, check=False,
    )
    if tracked.returncode:
        raise BuildError("release producer inputs must be committed before building")

    staging_parent = root / "release-staging"
    staging_parent.mkdir(mode=0o700, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f"{release_id}-", dir=staging_parent))
    cleanup_state = {"complete": False}

    def cleanup_build() -> None:
        shutil.rmtree(staging, ignore_errors=True)
        if not cleanup_state["complete"]:
            shutil.rmtree(output_root, ignore_errors=True)

    atexit.register(cleanup_build)
    payload = output_root / "payload"
    project = "rel" + release_id.replace("-", "").lower()
    container = f"ispindel-release-{release_id}"
    image_ref = f"ispindel-release:{release_id}"
    volume_name = f"{project}_ispindel-data"
    backend_port = choose_loopback_port()
    compose_env = os.environ.copy()
    compose_env.update({
        "ISPINDEL_IMAGE_REF": image_ref,
        "ISPINDEL_CONTAINER_NAME": container,
        "ISPINDEL_BACKEND_PUBLISH": f"127.0.0.1:{backend_port}:8098",
        "ISPINDEL_DATA_VOLUME": volume_name,
        "ISPINDEL_EVIDENCE_DIR": str(staging / "evidence"),
        "ISPINDEL_SECRETS_DIR": str(staging / "secrets"),
        "ISPINDEL_GID": "10001",
        "ISPINDEL_MODE": "test",
    })
    (staging / "evidence").mkdir(mode=0o755)
    (staging / "secrets").mkdir(mode=0o700)
    test_log = staging / "release-tests.log"
    test_commands = [
        "make test",
        "scripts/verify-caddy-config.sh (pinned cache)",
        "scripts/verify-network-boundary.sh",
        "docker compose up -d --build --pull never (disposable project)",
        "pytest -q tests/test_security.py tests/browser",
        "PHASE05C_RUNTIME_IMAGE=<immutable-id> make test-runtime",
    ]
    compose = ["docker", "compose", "--project-name", project, "--file", str(root / "docker-compose.yml")]
    failure: BaseException | None = None
    image_id = ""
    try:
        run_logged(["make", "test"], test_log, cwd=root)
        caddy_env = os.environ.copy()
        caddy_env.update({"CADDY_BIN": str(caddy_binary), "CADDY_TARBALL": str(caddy_archive)})
        run_logged([str(root / "scripts/verify-caddy-config.sh")], test_log, cwd=root, env=caddy_env)
        run_logged([str(root / "scripts/verify-network-boundary.sh")], test_log, cwd=root)
        run_logged(compose + ["build", "--pull=false"], test_log, cwd=root, env=compose_env)
        run_logged([
            "docker", "volume", "create",
            "--label", f"com.docker.compose.project={project}",
            "--label", "com.docker.compose.volume=ispindel-data",
            volume_name,
        ], test_log, cwd=root)
        run_logged([
            "docker", "run", "--rm", "--network", "none", "--user", "0:0",
            "--volume", f"{volume_name}:/data", "--entrypoint", "chown",
            image_ref, "10001:10001", "/data",
        ], test_log, cwd=root)
        run_logged(compose + ["up", "-d", "--build", "--pull", "never"], test_log, cwd=root, env=compose_env)
        wait_live(container, backend_port)
        with test_log.open("a", encoding="utf-8") as handle:
            handle.write(f"DISPOSABLE_BACKEND_READY container={container}\n")
        image_id = run(["docker", "image", "inspect", image_ref, "--format", "{{.Id}}"]).stdout.strip()
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id):
            raise BuildError("build did not produce an immutable image ID")
        run_logged([str(root / ".venv/bin/python"), "-m", "pytest", "-q", "tests/test_security.py", "tests/browser"], test_log, cwd=root)
        runtime_env = os.environ.copy()
        runtime_env["PHASE05C_RUNTIME_IMAGE"] = image_id
        run_logged(["make", "test-runtime"], test_log, cwd=root, env=runtime_env)
    except BaseException as exc:
        failure = exc
    finally:
        down = subprocess.run(
            compose + ["down", "--volumes", "--remove-orphans"], cwd=root, env=compose_env,
            text=True, capture_output=True, check=False,
        )
    leaked_resources: list[str] = []
    for resource in ("container", "volume", "network"):
        probe = subprocess.run(
            ["docker", resource, "ls", "-q", "--filter", f"label=com.docker.compose.project={project}"],
            text=True, capture_output=True, check=False,
        )
        if probe.returncode or probe.stdout.strip():
            leaked_resources.append(resource)
    if down.returncode or leaked_resources:
        shutil.rmtree(staging, ignore_errors=True)
        detail = down.stderr.strip().splitlines()[-1] if down.returncode and down.stderr.strip() else ",".join(leaked_resources)
        raise BuildError(f"disposable Compose teardown failed or leaked resources: {detail}") from failure
    if failure is not None:
        shutil.rmtree(staging, ignore_errors=True)
        raise failure
    with test_log.open("a", encoding="utf-8") as handle:
        handle.write(f"DISPOSABLE_CLEANUP_OK container={container} project={project} resources=0\n")
    if run(["git", "status", "--porcelain"], cwd=root).stdout:
        raise BuildError("tests changed the Git tree; refusing to archive mutable source")

    payload.mkdir(parents=True)
    source_root = payload / "source"
    copy_git_tree(root, source_root, commit)
    authority_dir = payload / "authority"
    authority_dir.mkdir()
    shutil.copy2(authority_path, authority_dir / "PHASE7B-REPAIR-AUTHORITY.json")
    (payload / "evidence").mkdir()
    shutil.copy2(test_log, payload / "evidence/release-tests.log")
    caddy_dir = payload / "caddy"
    caddy_dir.mkdir()
    shutil.copy2(caddy_archive, caddy_dir / caddy_archive.name)
    (caddy_dir / f"{caddy_archive.name}.sha512").write_text(pin, encoding="ascii")
    sbom = run([str(caddy_binary), "list-modules", "--packages", "--versions"]).stdout
    (caddy_dir / "modules.sbom.txt").write_text(sbom, encoding="utf-8")

    image_dir = payload / "images"
    image_dir.mkdir()
    image_archive = image_dir / f"ispindel-{release_id}.docker.tar.gz"
    docker_save_gzip(image_ref, image_archive)
    inspect = json.loads(run(["docker", "image", "inspect", image_id]).stdout)[0]
    phase_descriptor = json.loads((source_root / "descriptors/phase7b-authority.json").read_text())
    static_assets = {
        path.relative_to(payload).as_posix(): sha(path)
        for path in sorted((source_root / "app/static").rglob("*")) if path.is_file()
    }
    artifacts = artifact_inventory(payload)
    manifest = {
        "schema": "ispindel-release-manifest/v1",
        "release_id": release_id,
        "created_at": dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
        "release_root": f"dist/releases/{release_id}/payload",
        "source": {
            "commit": commit,
            "short_commit": short,
            "dockerfile_sha256": sha(source_root / "Dockerfile"),
            "compose_sha256": sha(source_root / "docker-compose.yml"),
            "caddyfile_sha256": sha(source_root / "ops/caddy/Caddyfile"),
            "systemd_tree_sha256": tree_sha(payload, "source/ops/systemd"),
            "scripts_tree_sha256": tree_sha(payload, "source/scripts"),
            "wheelhouse_authority_sha256": sha(source_root / "wheels/SHA256SUMS"),
            "static_assets": static_assets,
        },
        "phase7b": {
            "descriptor_path": "source/descriptors/phase7b-authority.json",
            "descriptor_sha256": sha(source_root / "descriptors/phase7b-authority.json"),
            "authority_path": "authority/PHASE7B-REPAIR-AUTHORITY.json",
            "authority_sha256": sha(authority_dir / "PHASE7B-REPAIR-AUTHORITY.json"),
            "verdict": phase_descriptor["verdict"],
        },
        "image": {
            "id": image_id,
            "reference": image_ref,
            "archive_path": image_archive.relative_to(payload).as_posix(),
            "archive_sha256": sha(image_archive),
            "archive_size": image_archive.stat().st_size,
            "config_sha256": docker_archive_config_sha(image_archive),
            "rootfs_sha256": canonical_sha(inspect["RootFS"]),
            "architecture": inspect["Architecture"],
            "os": inspect["Os"],
        },
        "caddy": {
            "version": EXPECTED_CADDY_VERSION,
            "archive_path": f"caddy/{caddy_archive.name}",
            "archive_sha256": sha(caddy_dir / caddy_archive.name),
            "archive_sha512": expected_sha512,
            "binary_sha256": sha(caddy_binary),
            "sbom_path": "caddy/modules.sbom.txt",
            "sbom_sha256": sha(caddy_dir / "modules.sbom.txt"),
            "checksum_path": f"caddy/{caddy_archive.name}.sha512",
        },
        "tests": {
            "result": "PASS",
            "commands": test_commands,
            "log_path": "evidence/release-tests.log",
            "log_sha256": sha(payload / "evidence/release-tests.log"),
        },
        "artifact_count": len(artifacts),
        "artifacts": artifacts,
    }
    atomic_json(payload / "RELEASE.json", manifest)

    archive = output_root / f"ispindel-{release_id}.tar.zst"
    create_archive(payload, archive)
    archive_sha = sha(archive)
    sidecar = output_root / f"ispindel-{release_id}.tar.zst.sha256"
    sidecar.write_text(f"{archive_sha}  {archive.name}\n", encoding="ascii")
    os.chmod(sidecar, 0o644)

    verify_parent = Path(tempfile.mkdtemp(prefix="ispindel-release-verify-", dir=staging_parent))
    extracted = verify_parent / "payload"
    try:
        safe_extract(archive, extracted)
        verify = run([
            "docker", "run", "--rm", "--network", "none",
            "--mount", f"type=bind,src={extracted},dst=/release,readonly",
            "--entrypoint", "python", image_id,
            "/release/source/scripts/verify-release.py",
            "--manifest", "/release/RELEASE.json", "--root", "/release",
        ])
        if not verify.stdout.startswith("RELEASE_OK "):
            raise BuildError("network-disabled extracted release verification failed")
    finally:
        shutil.rmtree(verify_parent, ignore_errors=True)
        shutil.rmtree(staging, ignore_errors=True)
    atomic_json(root / "descriptors/RELEASE.json", manifest)
    cleanup_state["complete"] = True
    print(verify.stdout.strip())
    print(f"RELEASE_ARCHIVE path={archive} sha256={archive_sha}")
    print(f"RELEASE_MANIFEST path={root / 'descriptors/RELEASE.json'}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except BuildError as exc:
        print(f"RELEASE_BUILD_FAILED reason={exc}", file=sys.stderr)
        raise SystemExit(2)
