#!/usr/bin/env python3
"""Strict offline verifier for an iSpindel release payload."""
from __future__ import annotations

import argparse
import datetime as dt
import gzip
import hashlib
import json
import os
import re
import stat
import tarfile
from pathlib import Path, PurePosixPath
from typing import Any

SCHEMA = "ispindel-release-manifest/v1"
HEX64 = re.compile(r"^[0-9a-f]{64}$")
HEX128 = re.compile(r"^[0-9a-f]{128}$")
RELEASE_ID = re.compile(r"^[0-9]{8}T[0-9]{6}Z-[0-9a-f]{12}$")
TOP_KEYS = {
    "schema", "release_id", "created_at", "release_root", "source",
    "phase7b", "image", "caddy", "tests", "artifact_count", "artifacts",
}
ARTIFACT_KEYS = {"path", "size", "mode", "sha256"}
CACHE_SEGMENTS = {
    ".git", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    ".tox", "node_modules",
}


class ReleaseError(RuntimeError):
    pass


def digest(path: Path, algorithm: str = "sha256") -> str:
    h = hashlib.new(algorithm)
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def canonical_json_sha(value: Any) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    return hashlib.sha256(raw).hexdigest()


def safe_relative(raw: str) -> PurePosixPath:
    if not raw or "\\" in raw:
        raise ReleaseError(f"non-portable artifact path: {raw!r}")
    path = PurePosixPath(raw)
    if path.is_absolute() or str(path) in {"", "."} or any(part in {"", ".", ".."} for part in path.parts):
        raise ReleaseError(f"unsafe artifact path: {raw!r}")
    if any(part in CACHE_SEGMENTS for part in path.parts):
        raise ReleaseError(f"cache path is forbidden: {raw}")
    if path.as_posix() != raw:
        raise ReleaseError(f"non-canonical artifact path: {raw}")
    return path


def require_regular(root: Path, relative: PurePosixPath) -> Path:
    current = root
    for part in relative.parts:
        current = current / part
        try:
            info = current.lstat()
        except FileNotFoundError as exc:
            raise ReleaseError(f"missing artifact: {relative}") from exc
        if stat.S_ISLNK(info.st_mode):
            raise ReleaseError(f"symlink is forbidden: {relative}")
    if not current.is_file():
        raise ReleaseError(f"artifact is not a regular file: {relative}")
    try:
        current.resolve().relative_to(root.resolve())
    except ValueError as exc:
        raise ReleaseError(f"artifact escapes release root: {relative}") from exc
    return current


def file_tree_sha(root: Path, prefix: str) -> str:
    rows: list[str] = []
    base = root / prefix
    if not base.is_dir():
        raise ReleaseError(f"required tree is absent: {prefix}")
    for path in sorted(item for item in base.rglob("*") if item.is_file()):
        rel = path.relative_to(root).as_posix()
        rows.append(f"{rel}\0{path.stat().st_size}\0{digest(path)}\n")
    if not rows:
        raise ReleaseError(f"required tree is empty: {prefix}")
    return hashlib.sha256("".join(rows).encode()).hexdigest()


def resolve_root(manifest_path: Path, manifest: dict[str, Any], explicit: Path | None) -> Path:
    if explicit is not None:
        return explicit.resolve()
    adjacent = manifest_path.parent.resolve()
    if (adjacent / "source").is_dir():
        return adjacent
    candidate = manifest_path.parent.parent / str(manifest.get("release_root", ""))
    return candidate.resolve()


def exact_keys(value: object, expected: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != expected:
        actual = sorted(value) if isinstance(value, dict) else type(value).__name__
        raise ReleaseError(f"{label} keys are not closed: {actual}")
    return value


def verify_docker_archive(path: Path, image: dict[str, Any]) -> None:
    try:
        bundle = tarfile.open(path, "r:gz")
    except tarfile.TarError as exc:
        raise ReleaseError("Docker archive is invalid") from exc
    with bundle:
        member_list = bundle.getmembers()
        names = [member.name for member in member_list]
        if len(names) != len(set(names)):
            raise ReleaseError("Docker archive contains duplicate member names")
        members = {member.name: member for member in member_list}
        allowed_roots = {"blobs", "index.json", "manifest.json", "oci-layout"}
        allowed_top_files = {"index.json", "manifest.json", "oci-layout"}
        allowed_directories = {"blobs", "blobs/sha256"}
        for member in member_list:
            raw = member.name.rstrip("/")
            safe_relative(raw)
            if raw.split("/", 1)[0] not in allowed_roots:
                raise ReleaseError(f"Docker archive member root is forbidden: {raw}")
            if not (member.isfile() or member.isdir()):
                raise ReleaseError(f"Docker archive special member is forbidden: {raw}")
            if member.isdir() and raw not in allowed_directories:
                raise ReleaseError(f"Docker archive directory is unexpected: {raw}")
            if member.isfile() and raw not in allowed_top_files and re.fullmatch(r"blobs/sha256/[0-9a-f]{64}", raw) is None:
                raise ReleaseError(f"Docker archive file is not content-addressed: {raw}")

        blob_members: dict[str, tarfile.TarInfo] = {}
        for name, member in members.items():
            match = re.fullmatch(r"blobs/sha256/([0-9a-f]{64})", name)
            if not match or not member.isfile():
                continue
            handle = bundle.extractfile(member)
            if handle is None:
                raise ReleaseError(f"Docker blob could not be read: {name}")
            h = hashlib.sha256()
            while True:
                block = handle.read(1024 * 1024)
                if not block:
                    break
                h.update(block)
            digest_value = "sha256:" + h.hexdigest()
            if h.hexdigest() != match.group(1):
                raise ReleaseError(f"Docker blob path digest mismatch: {name}")
            blob_members[digest_value] = member
        if not blob_members:
            raise ReleaseError("Docker archive contains no content-addressed blobs")

        def json_file(name: str, label: str) -> Any:
            member = members.get(name)
            if member is None or not member.isfile():
                raise ReleaseError(f"{label} is absent")
            handle = bundle.extractfile(member)
            if handle is None:
                raise ReleaseError(f"{label} could not be read")
            try:
                return json.load(handle)
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise ReleaseError(f"{label} is invalid JSON") from exc

        def descriptor_member(descriptor: object, label: str) -> tarfile.TarInfo:
            if not isinstance(descriptor, dict):
                raise ReleaseError(f"{label} descriptor is invalid")
            digest_value = descriptor.get("digest")
            size = descriptor.get("size")
            if not isinstance(digest_value, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", digest_value):
                raise ReleaseError(f"{label} descriptor digest is invalid")
            member = blob_members.get(digest_value)
            if member is None or not isinstance(size, int) or member.size != size:
                raise ReleaseError(f"{label} descriptor blob or size mismatch")
            return member

        def descriptor_json(descriptor: object, label: str) -> tuple[dict[str, Any], tarfile.TarInfo]:
            member = descriptor_member(descriptor, label)
            obj = json_file(member.name, label)
            if not isinstance(obj, dict):
                raise ReleaseError(f"{label} blob must be a JSON object")
            return obj, member

        layout = json_file("oci-layout", "Docker OCI layout")
        if layout != {"imageLayoutVersion": "1.0.0"}:
            raise ReleaseError("Docker OCI layout version is invalid")
        legacy = json_file("manifest.json", "Docker archive manifest.json")
        if not isinstance(legacy, list) or len(legacy) != 1 or not isinstance(legacy[0], dict):
            raise ReleaseError("Docker archive must contain exactly one legacy image manifest")
        entry = legacy[0]
        if set(entry) != {"Config", "RepoTags", "Layers"}:
            raise ReleaseError("Docker archive legacy manifest keys are not closed")
        if entry["RepoTags"] != [image["reference"]]:
            raise ReleaseError("Docker archive does not bind the exact release tag")

        top_index = json_file("index.json", "Docker OCI index.json")
        if not isinstance(top_index, dict) or top_index.get("schemaVersion") != 2 or not isinstance(top_index.get("manifests"), list):
            raise ReleaseError("Docker OCI top index structure is invalid")
        targets = [item for item in top_index["manifests"] if isinstance(item, dict) and item.get("digest") == image["id"]]
        if len(targets) != 1 or len(top_index["manifests"]) != 1:
            raise ReleaseError("Docker OCI top index does not uniquely bind image ID")
        target_obj, _ = descriptor_json(targets[0], "Docker OCI image ID")
        media_type = targets[0].get("mediaType")
        if media_type in {"application/vnd.oci.image.index.v1+json", "application/vnd.docker.distribution.manifest.list.v2+json"}:
            candidates = [
                item for item in target_obj.get("manifests", [])
                if isinstance(item, dict)
                and item.get("platform") == {"architecture": image["architecture"], "os": image["os"]}
                and item.get("annotations", {}).get("vnd.docker.reference.type") != "attestation-manifest"
            ]
            if len(candidates) != 1:
                raise ReleaseError("Docker OCI index does not contain one exact platform image")
            manifest_descriptor = candidates[0]
            platform_manifest, _ = descriptor_json(manifest_descriptor, "Docker OCI platform manifest")
        elif media_type in {"application/vnd.oci.image.manifest.v1+json", "application/vnd.docker.distribution.manifest.v2+json"}:
            platform_manifest = target_obj
        else:
            raise ReleaseError("Docker OCI image ID media type is unsupported")
        if platform_manifest.get("schemaVersion") != 2 or not isinstance(platform_manifest.get("layers"), list):
            raise ReleaseError("Docker OCI platform manifest structure is invalid")

        config_descriptor = platform_manifest.get("config")
        config, config_member = descriptor_json(config_descriptor, "Docker OCI config")
        config_digest = config_member.name.rsplit("/", 1)[-1]
        if config_digest != image["config_sha256"]:
            raise ReleaseError("Docker OCI config digest mismatch")
        if entry["Config"] != config_member.name:
            raise ReleaseError("legacy and OCI config bindings differ")
        try:
            diff_ids = config["rootfs"]["diff_ids"]
            normalized_rootfs = {"Type": config["rootfs"]["type"], "Layers": diff_ids}
        except (KeyError, TypeError) as exc:
            raise ReleaseError("Docker archive config structure is invalid") from exc
        if canonical_json_sha(normalized_rootfs) != image["rootfs_sha256"]:
            raise ReleaseError("Docker archive RootFS binding mismatch")
        if config.get("architecture") != image["architecture"] or config.get("os") != image["os"]:
            raise ReleaseError("Docker archive platform binding mismatch")

        layers = platform_manifest["layers"]
        legacy_layers = entry["Layers"]
        if not layers or not isinstance(legacy_layers, list) or len(layers) != len(legacy_layers) or len(layers) != len(diff_ids):
            raise ReleaseError("Docker archive layer inventories differ or are empty")
        for descriptor, legacy_name, diff_id in zip(layers, legacy_layers, diff_ids, strict=True):
            layer_member = descriptor_member(descriptor, "Docker OCI layer")
            if legacy_name != layer_member.name:
                raise ReleaseError("legacy and OCI layer bindings differ")
            if not isinstance(diff_id, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", diff_id):
                raise ReleaseError("Docker layer DiffID is invalid")
            compressed = bundle.extractfile(layer_member)
            if compressed is None:
                raise ReleaseError("Docker layer blob could not be read")
            h = hashlib.sha256()
            try:
                with gzip.GzipFile(fileobj=compressed, mode="rb") as stream:
                    for block in iter(lambda: stream.read(1024 * 1024), b""):
                        h.update(block)
            except (gzip.BadGzipFile, EOFError, OSError) as exc:
                raise ReleaseError("Docker layer gzip stream is invalid") from exc
            if "sha256:" + h.hexdigest() != diff_id:
                raise ReleaseError("Docker layer content does not match config DiffID")


def verify_caddy(root: Path, caddy: dict[str, Any]) -> None:
    archive = require_regular(root, safe_relative(str(caddy["archive_path"])))
    checksum = require_regular(root, safe_relative(str(caddy["checksum_path"])))
    sbom = require_regular(root, safe_relative(str(caddy["sbom_path"])))
    if digest(archive) != caddy["archive_sha256"] or digest(archive, "sha512") != caddy["archive_sha512"]:
        raise ReleaseError("Caddy archive digest mismatch")
    expected_line = f"{caddy['archive_sha512']}  {archive.name}\n"
    if checksum.read_text(encoding="ascii") != expected_line:
        raise ReleaseError("Caddy SHA-512 sidecar is not exact")
    if digest(sbom) != caddy["sbom_sha256"] or not sbom.read_text(encoding="utf-8").strip():
        raise ReleaseError("Caddy SBOM mismatch or empty")
    with tarfile.open(archive, "r:gz") as bundle:
        members = [member for member in bundle.getmembers() if member.name == "caddy" and member.isfile()]
        if len(members) != 1:
            raise ReleaseError("Caddy archive must contain exactly one caddy binary")
        handle = bundle.extractfile(members[0])
        if handle is None:
            raise ReleaseError("Caddy binary could not be read")
        if hashlib.sha256(handle.read()).hexdigest() != caddy["binary_sha256"]:
            raise ReleaseError("Caddy binary digest mismatch")


def verify_wheelhouse(root: Path, rows: dict[str, dict[str, Any]]) -> None:
    authority_rel = "source/wheels/SHA256SUMS"
    authority = require_regular(root, safe_relative(authority_rel))
    expected: dict[str, str] = {}
    try:
        lines = authority.read_text(encoding="ascii").splitlines()
    except UnicodeDecodeError as exc:
        raise ReleaseError("wheelhouse authority is not ASCII") from exc
    if not lines:
        raise ReleaseError("wheelhouse authority is empty")
    for line in lines:
        match = re.fullmatch(r"([0-9a-f]{64})  ([^/\\]+\.whl)", line)
        if match is None or match.group(2) in expected:
            raise ReleaseError("wheelhouse authority line is invalid or duplicated")
        expected[match.group(2)] = match.group(1)
    wheel_paths = [
        path for path in rows
        if path.startswith("source/wheels/") and path.endswith(".whl")
    ]
    if any("/" in path.removeprefix("source/wheels/") for path in wheel_paths):
        raise ReleaseError("wheelhouse may not contain nested wheel paths")
    actual = {path.removeprefix("source/wheels/") for path in wheel_paths}
    if set(expected) != actual:
        raise ReleaseError("wheelhouse authority does not exactly cover wheel files")
    for filename, expected_sha in expected.items():
        rel = f"source/wheels/{filename}"
        if rows[rel]["sha256"] != expected_sha:
            raise ReleaseError(f"wheelhouse digest mismatch: {filename}")


def verify_release(manifest_path: Path, root: Path | None = None, *, check_modes: bool = True) -> dict[str, Any]:
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise ReleaseError("manifest must be a regular non-symlink file")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReleaseError(f"manifest is invalid JSON: {exc}") from exc
    manifest = exact_keys(manifest, TOP_KEYS, "manifest")
    if manifest["schema"] != SCHEMA or not RELEASE_ID.fullmatch(str(manifest["release_id"])):
        raise ReleaseError("manifest schema or release ID is invalid")
    release_id = str(manifest["release_id"])
    if manifest["release_root"] != f"dist/releases/{release_id}/payload":
        raise ReleaseError("release root binding is invalid")
    try:
        created = str(manifest["created_at"]).replace("Z", "+00:00")
        parsed_created = dt.datetime.fromisoformat(created)
        if parsed_created.tzinfo is None:
            raise ValueError("timezone required")
    except ValueError as exc:
        raise ReleaseError("release timestamp is invalid") from exc
    release_root = resolve_root(manifest_path, manifest, root)
    if not release_root.is_dir() or release_root.is_symlink():
        raise ReleaseError("release root is absent or unsafe")

    source = exact_keys(manifest["source"], {
        "commit", "short_commit", "dockerfile_sha256", "compose_sha256",
        "caddyfile_sha256", "systemd_tree_sha256", "scripts_tree_sha256",
        "wheelhouse_authority_sha256", "static_assets",
    }, "source")
    phase7b = exact_keys(manifest["phase7b"], {
        "descriptor_path", "descriptor_sha256", "authority_path", "authority_sha256", "verdict",
    }, "phase7b")
    image = exact_keys(manifest["image"], {
        "id", "reference", "archive_path", "archive_sha256", "archive_size",
        "config_sha256", "rootfs_sha256", "architecture", "os",
    }, "image")
    caddy = exact_keys(manifest["caddy"], {
        "version", "archive_path", "archive_sha256", "archive_sha512",
        "binary_sha256", "sbom_path", "sbom_sha256", "checksum_path",
    }, "caddy")
    tests = exact_keys(manifest["tests"], {"result", "commands", "log_path", "log_sha256"}, "tests")
    if not re.fullmatch(r"[0-9a-f]{40}", str(source["commit"])) or source["short_commit"] != source["commit"][:12]:
        raise ReleaseError("source commit binding is invalid")
    expected_image_path = f"images/ispindel-{release_id}.docker.tar.gz"
    if image["reference"] != f"ispindel-release:{release_id}" or image["archive_path"] != expected_image_path:
        raise ReleaseError("image release-ID binding is invalid")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", str(image["id"])):
        raise ReleaseError("image ID is invalid")
    if any(not HEX64.fullmatch(str(image[field])) for field in ("archive_sha256", "config_sha256", "rootfs_sha256")):
        raise ReleaseError("image digest field is invalid")
    if not isinstance(image["archive_size"], int) or image["archive_size"] < 1:
        raise ReleaseError("image archive size is invalid")
    if phase7b["descriptor_path"] != "source/descriptors/phase7b-authority.json" or phase7b["authority_path"] != "authority/PHASE7B-REPAIR-AUTHORITY.json":
        raise ReleaseError("Phase 7B artifact paths are invalid")
    if caddy["version"] != "v2.11.4" or caddy["archive_path"] != "caddy/caddy_2.11.4_linux_amd64.tar.gz":
        raise ReleaseError("Caddy version or archive path is invalid")
    if caddy["sbom_path"] != "caddy/modules.sbom.txt" or caddy["checksum_path"] != "caddy/caddy_2.11.4_linux_amd64.tar.gz.sha512":
        raise ReleaseError("Caddy evidence paths are invalid")
    if any(not HEX64.fullmatch(str(caddy[field])) for field in ("archive_sha256", "binary_sha256", "sbom_sha256")) or not HEX128.fullmatch(str(caddy["archive_sha512"])):
        raise ReleaseError("Caddy digest field is invalid")
    if tests["log_path"] != "evidence/release-tests.log" or not isinstance(tests["commands"], list) or not tests["commands"] or not all(isinstance(command, str) and command for command in tests["commands"]):
        raise ReleaseError("test evidence binding is invalid")
    if phase7b["verdict"] != "GO" or tests["result"] != "PASS":
        raise ReleaseError("authority or test verdict is not PASS/GO")

    artifacts = manifest["artifacts"]
    if not isinstance(artifacts, list) or manifest["artifact_count"] != len(artifacts):
        raise ReleaseError("artifact count mismatch")
    rows: dict[str, dict[str, Any]] = {}
    for item in artifacts:
        item = exact_keys(item, ARTIFACT_KEYS, "artifact")
        rel = safe_relative(str(item["path"]))
        if rel.as_posix() in rows:
            raise ReleaseError(f"duplicate artifact: {rel}")
        if not isinstance(item["size"], int) or not isinstance(item["mode"], int) or item["mode"] not in {0o644, 0o755}:
            raise ReleaseError(f"invalid artifact metadata: {rel}")
        if not HEX64.fullmatch(str(item["sha256"])):
            raise ReleaseError(f"invalid artifact digest: {rel}")
        path = require_regular(release_root, rel)
        actual_mode = stat.S_IMODE(path.stat().st_mode)
        if (path.stat().st_size != item["size"]
                or (check_modes and actual_mode != item["mode"])
                or digest(path) != item["sha256"]):
            raise ReleaseError(f"artifact mismatch: {rel}")
        rows[rel.as_posix()] = item
    if list(rows) != sorted(rows):
        raise ReleaseError("artifact inventory is not sorted")

    all_nodes = list(release_root.rglob("*"))
    for path in all_nodes:
        if path.is_symlink():
            raise ReleaseError(f"symlink is forbidden anywhere in payload: {path.relative_to(release_root)}")
        if not (path.is_file() or path.is_dir()):
            raise ReleaseError(f"special node is forbidden in payload: {path.relative_to(release_root)}")
    actual_files = {
        path.relative_to(release_root).as_posix()
        for path in all_nodes
        if path.is_file() and path != release_root / "RELEASE.json"
    }
    if actual_files != set(rows):
        raise ReleaseError(f"two-way closure mismatch missing={sorted(set(rows)-actual_files)} extra={sorted(actual_files-set(rows))}")

    required = {
        "source/Dockerfile", "source/docker-compose.yml", "source/ops/caddy/Caddyfile",
        "source/wheels/SHA256SUMS", "source/release/manifest.schema.json",
        str(image["archive_path"]), str(caddy["archive_path"]), str(caddy["sbom_path"]),
        str(caddy["checksum_path"]), str(tests["log_path"]), str(phase7b["descriptor_path"]),
        str(phase7b["authority_path"]),
    }
    if not required.issubset(rows):
        raise ReleaseError(f"required artifacts missing: {sorted(required-set(rows))}")
    if not any(path.startswith("source/ops/systemd/") for path in rows):
        raise ReleaseError("systemd units missing from release")
    if not any(path.startswith("source/scripts/") for path in rows):
        raise ReleaseError("scripts missing from release")
    if not any(path.startswith("source/wheels/") and path.endswith(".whl") for path in rows):
        raise ReleaseError("wheelhouse payload missing from release")
    verify_wheelhouse(release_root, rows)

    direct = {
        "dockerfile_sha256": "source/Dockerfile",
        "compose_sha256": "source/docker-compose.yml",
        "caddyfile_sha256": "source/ops/caddy/Caddyfile",
        "wheelhouse_authority_sha256": "source/wheels/SHA256SUMS",
    }
    for field, rel in direct.items():
        if source[field] != rows[rel]["sha256"]:
            raise ReleaseError(f"source binding mismatch: {field}")
    if source["systemd_tree_sha256"] != file_tree_sha(release_root, "source/ops/systemd"):
        raise ReleaseError("systemd tree binding mismatch")
    if source["scripts_tree_sha256"] != file_tree_sha(release_root, "source/scripts"):
        raise ReleaseError("scripts tree binding mismatch")
    assets = source["static_assets"]
    if not isinstance(assets, dict):
        raise ReleaseError("static asset map is invalid")
    actual_assets = {path for path in rows if path.startswith("source/app/static/")}
    if set(assets) != actual_assets:
        raise ReleaseError("static asset map does not exactly cover the static tree")
    for rel, expected in assets.items():
        safe_relative(rel)
        if not HEX64.fullmatch(str(expected)) or rows[rel]["sha256"] != expected:
            raise ReleaseError(f"static asset binding mismatch: {rel}")

    if phase7b["descriptor_sha256"] != rows[phase7b["descriptor_path"]]["sha256"]:
        raise ReleaseError("Phase 7B descriptor binding mismatch")
    if not HEX64.fullmatch(str(phase7b["authority_sha256"])):
        raise ReleaseError("Phase 7B authority SHA is invalid")
    if phase7b["authority_sha256"] != rows[phase7b["authority_path"]]["sha256"]:
        raise ReleaseError("Phase 7B authority artifact binding mismatch")
    image_path = require_regular(release_root, safe_relative(str(image["archive_path"])))
    if digest(image_path) != image["archive_sha256"] or image_path.stat().st_size != image["archive_size"]:
        raise ReleaseError("image archive binding mismatch")
    verify_docker_archive(image_path, image)
    log_path = require_regular(release_root, safe_relative(str(tests["log_path"])))
    if digest(log_path) != tests["log_sha256"]:
        raise ReleaseError("test log binding mismatch")
    verify_caddy(release_root, caddy)
    return {"release_id": manifest["release_id"], "image_id": image["id"], "artifacts": len(rows), "root": str(release_root)}


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument("--manifest", type=Path, required=True)
    value.add_argument("--root", type=Path)
    value.add_argument("--ignore-modes", action="store_true",
                       help="Skip filesystem mode comparison for read-only bind mounts.")
    return value


def main() -> int:
    args = parser().parse_args()
    try:
        result = verify_release(args.manifest.resolve(), args.root, check_modes=not args.ignore_modes)
    except ReleaseError as exc:
        print(f"RELEASE_INVALID reason={exc}")
        return 2
    print(
        f"RELEASE_OK release_id={result['release_id']} image_id={result['image_id']} "
        f"artifacts={result['artifacts']} missing=0 mismatched=0"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
