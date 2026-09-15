from __future__ import annotations

import hashlib
import gzip
import importlib.util
import io
import json
import os
import stat
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"


def load_script(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / filename)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


verify_release = load_script("verify_release_script", "verify-release.py")
build_release = load_script("build_release_script", "build_release.py")
ops_common = load_script("ops_common", "ops_common.py")


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write(path: Path, data: bytes, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    path.chmod(mode)


def release_fixture(tmp_path: Path) -> tuple[Path, Path, dict]:
    root = tmp_path / "payload"
    files = {
        "source/Dockerfile": b"FROM scratch\n",
        "source/docker-compose.yml": b"services: {}\n",
        "source/ops/caddy/Caddyfile": b":443 {}\n",
        "source/ops/systemd/ispindel.service": b"[Service]\nExecStart=true\n",
        "source/scripts/deploy.sh": b"#!/bin/sh\nexit 0\n",
        "source/wheels/SHA256SUMS": hashlib.sha256(b"wheel").hexdigest().encode() + b"  fixture.whl\n",
        "source/wheels/fixture.whl": b"wheel",
        "source/app/static/app.css": b"body{}\n",
        "source/release/manifest.schema.json": b"{}\n",
        "source/descriptors/phase7b-authority.json": b"{}\n",
        "authority/PHASE7B-REPAIR-AUTHORITY.json": b"{\"verdict\":\"GO\"}\n",
        "evidence/release-tests.log": b"PASS\n",
        "caddy/modules.sbom.txt": b"http.handlers.reverse_proxy v2.11.4 github.com/caddyserver/caddy/v2\n",
    }
    for rel, data in files.items():
        write(root / rel, data, 0o755 if rel.endswith(".sh") else 0o644)
    caddy_binary = b"caddy-fixture-binary"
    caddy_archive = root / "caddy/caddy_2.11.4_linux_amd64.tar.gz"
    caddy_archive.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(caddy_archive, "w:gz") as bundle:
        info = tarfile.TarInfo("caddy")
        info.size = len(caddy_binary)
        info.mode = 0o755
        bundle.addfile(info, io.BytesIO(caddy_binary))
    caddy_archive.chmod(0o644)
    caddy_sha512 = hashlib.sha512(caddy_archive.read_bytes()).hexdigest()
    checksum = root / "caddy/caddy_2.11.4_linux_amd64.tar.gz.sha512"
    write(checksum, f"{caddy_sha512}  {caddy_archive.name}\n".encode())

    release_id = "20260803T030000Z-123456789abc"
    layer_bytes = b"fixture-layer-tar"
    diff_id = "sha256:" + hashlib.sha256(layer_bytes).hexdigest()
    compressed_layer = gzip.compress(layer_bytes, mtime=0)
    layer_digest = hashlib.sha256(compressed_layer).hexdigest()
    layer_name = f"blobs/sha256/{layer_digest}"
    config = {
        "architecture": "amd64", "os": "linux",
        "rootfs": {"type": "layers", "diff_ids": [diff_id]},
    }
    config_bytes = json.dumps(config, sort_keys=True, separators=(",", ":")).encode()
    config_digest = hashlib.sha256(config_bytes).hexdigest()
    config_name = f"blobs/sha256/{config_digest}"
    platform_manifest = {
        "schemaVersion": 2,
        "mediaType": "application/vnd.oci.image.manifest.v1+json",
        "config": {"mediaType": "application/vnd.oci.image.config.v1+json", "digest": f"sha256:{config_digest}", "size": len(config_bytes)},
        "layers": [{"mediaType": "application/vnd.oci.image.layer.v1.tar+gzip", "digest": f"sha256:{layer_digest}", "size": len(compressed_layer)}],
    }
    platform_bytes = json.dumps(platform_manifest, sort_keys=True, separators=(",", ":")).encode()
    platform_digest = hashlib.sha256(platform_bytes).hexdigest()
    platform_name = f"blobs/sha256/{platform_digest}"
    nested_index = {
        "schemaVersion": 2,
        "mediaType": "application/vnd.oci.image.index.v1+json",
        "manifests": [{
            "mediaType": "application/vnd.oci.image.manifest.v1+json",
            "digest": f"sha256:{platform_digest}", "size": len(platform_bytes),
            "platform": {"architecture": "amd64", "os": "linux"},
        }],
    }
    nested_bytes = json.dumps(nested_index, sort_keys=True, separators=(",", ":")).encode()
    image_digest = hashlib.sha256(nested_bytes).hexdigest()
    nested_name = f"blobs/sha256/{image_digest}"
    top_index = {
        "schemaVersion": 2,
        "mediaType": "application/vnd.oci.image.index.v1+json",
        "manifests": [{
            "mediaType": "application/vnd.oci.image.index.v1+json",
            "digest": f"sha256:{image_digest}", "size": len(nested_bytes),
        }],
    }
    image_path = f"images/ispindel-{release_id}.docker.tar.gz"
    image_archive = root / image_path
    image_archive.parent.mkdir(parents=True, exist_ok=True)
    docker_manifest = [{
        "Config": config_name,
        "RepoTags": [f"ispindel-release:{release_id}"],
        "Layers": [layer_name],
    }]
    archive_files = (
        ("manifest.json", json.dumps(docker_manifest).encode(), 0o644),
        ("index.json", json.dumps(top_index, sort_keys=True, separators=(",", ":")).encode(), 0o644),
        ("oci-layout", b'{"imageLayoutVersion":"1.0.0"}', 0o644),
        (config_name, config_bytes, 0o644),
        (layer_name, compressed_layer, 0o644),
        (platform_name, platform_bytes, 0o644),
        (nested_name, nested_bytes, 0o644),
    )
    with tarfile.open(image_archive, "w:gz") as bundle:
        for name, data, mode in archive_files:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = mode
            bundle.addfile(info, io.BytesIO(data))
    image_archive.chmod(0o644)

    artifacts = []
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        artifacts.append({
            "path": path.relative_to(root).as_posix(),
            "size": path.stat().st_size,
            "mode": stat.S_IMODE(path.stat().st_mode),
            "sha256": sha(path),
        })
    manifest = {
        "schema": "ispindel-release-manifest/v1",
        "release_id": release_id,
        "created_at": "2026-08-03T03:00:00Z",
        "release_root": f"dist/releases/{release_id}/payload",
        "source": {
            "commit": "1" * 40,
            "short_commit": "1" * 12,
            "dockerfile_sha256": sha(root / "source/Dockerfile"),
            "compose_sha256": sha(root / "source/docker-compose.yml"),
            "caddyfile_sha256": sha(root / "source/ops/caddy/Caddyfile"),
            "systemd_tree_sha256": verify_release.file_tree_sha(root, "source/ops/systemd"),
            "scripts_tree_sha256": verify_release.file_tree_sha(root, "source/scripts"),
            "wheelhouse_authority_sha256": sha(root / "source/wheels/SHA256SUMS"),
            "static_assets": {"source/app/static/app.css": sha(root / "source/app/static/app.css")},
        },
        "phase7b": {
            "descriptor_path": "source/descriptors/phase7b-authority.json",
            "descriptor_sha256": sha(root / "source/descriptors/phase7b-authority.json"),
            "authority_path": "authority/PHASE7B-REPAIR-AUTHORITY.json",
            "authority_sha256": sha(root / "authority/PHASE7B-REPAIR-AUTHORITY.json"),
            "verdict": "GO",
        },
        "image": {
            "id": "sha256:" + image_digest,
            "reference": f"ispindel-release:{release_id}",
            "archive_path": image_path,
            "archive_sha256": sha(root / image_path),
            "archive_size": (root / image_path).stat().st_size,
            "config_sha256": config_digest,
            "rootfs_sha256": verify_release.canonical_json_sha({"Type": "layers", "Layers": [diff_id]}),
            "architecture": "amd64",
            "os": "linux",
        },
        "caddy": {
            "version": "v2.11.4",
            "archive_path": "caddy/caddy_2.11.4_linux_amd64.tar.gz",
            "archive_sha256": sha(caddy_archive),
            "archive_sha512": caddy_sha512,
            "binary_sha256": hashlib.sha256(caddy_binary).hexdigest(),
            "sbom_path": "caddy/modules.sbom.txt",
            "sbom_sha256": sha(root / "caddy/modules.sbom.txt"),
            "checksum_path": "caddy/caddy_2.11.4_linux_amd64.tar.gz.sha512",
        },
        "tests": {
            "result": "PASS",
            "commands": ["make test"],
            "log_path": "evidence/release-tests.log",
            "log_sha256": sha(root / "evidence/release-tests.log"),
        },
        "artifact_count": len(artifacts),
        "artifacts": artifacts,
    }
    manifest_path = root / "RELEASE.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return root, manifest_path, manifest


def test_valid_closed_release_passes(tmp_path: Path):
    root, manifest_path, manifest = release_fixture(tmp_path)
    result = verify_release.verify_release(manifest_path, root)
    assert result["release_id"] == manifest["release_id"]
    assert result["artifacts"] == manifest["artifact_count"]


def test_ignore_modes_only_relaxes_readonly_mount_mode_drift(tmp_path: Path):
    root, manifest_path, _ = release_fixture(tmp_path)
    target = root / "source/Dockerfile"
    target.chmod(0o444)
    with pytest.raises(verify_release.ReleaseError, match="artifact mismatch"):
        verify_release.verify_release(manifest_path, root)
    result = verify_release.verify_release(manifest_path, root, check_modes=False)
    assert result["artifacts"] > 0


def test_extra_unlisted_artifact_is_rejected(tmp_path: Path):
    root, manifest_path, _ = release_fixture(tmp_path)
    write(root / "unexpected.txt", b"not-authorized")
    with pytest.raises(verify_release.ReleaseError, match="two-way closure"):
        verify_release.verify_release(manifest_path, root)


def test_digest_drift_is_rejected(tmp_path: Path):
    root, manifest_path, _ = release_fixture(tmp_path)
    (root / "source/Dockerfile").write_text("drift\n")
    with pytest.raises(verify_release.ReleaseError, match="artifact mismatch"):
        verify_release.verify_release(manifest_path, root)


def test_traversal_path_is_rejected(tmp_path: Path):
    root, manifest_path, manifest = release_fixture(tmp_path)
    manifest["artifacts"][0]["path"] = "../escape"
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(verify_release.ReleaseError, match="unsafe artifact path"):
        verify_release.verify_release(manifest_path, root)


def test_symlink_artifact_is_rejected(tmp_path: Path):
    root, manifest_path, _ = release_fixture(tmp_path)
    dockerfile = root / "source/Dockerfile"
    saved = root / "Dockerfile.saved"
    dockerfile.rename(saved)
    dockerfile.symlink_to(saved)
    with pytest.raises(verify_release.ReleaseError, match="symlink is forbidden"):
        verify_release.verify_release(manifest_path, root)


def test_symlink_directory_anywhere_is_rejected(tmp_path: Path):
    root, manifest_path, _ = release_fixture(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "unlisted-directory-link").symlink_to(outside, target_is_directory=True)
    with pytest.raises(verify_release.ReleaseError, match="symlink is forbidden anywhere"):
        verify_release.verify_release(manifest_path, root)


def test_static_asset_map_must_be_exact(tmp_path: Path):
    root, manifest_path, manifest = release_fixture(tmp_path)
    manifest["source"]["static_assets"] = {}
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(verify_release.ReleaseError, match="exactly cover"):
        verify_release.verify_release(manifest_path, root)


def test_wheel_authority_must_cover_exact_wheel_set(tmp_path: Path):
    root, manifest_path, manifest = release_fixture(tmp_path)
    extra = root / "source/wheels/extra.whl"
    write(extra, b"extra-wheel")
    manifest["artifacts"].append({
        "path": "source/wheels/extra.whl", "size": extra.stat().st_size,
        "mode": 0o644, "sha256": sha(extra),
    })
    manifest["artifacts"].sort(key=lambda row: row["path"])
    manifest["artifact_count"] = len(manifest["artifacts"])
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(verify_release.ReleaseError, match="exactly cover wheel"):
        verify_release.verify_release(manifest_path, root)


def test_nested_wheel_is_rejected_even_when_artifact_inventory_lists_it(tmp_path: Path):
    root, manifest_path, manifest = release_fixture(tmp_path)
    nested = root / "source/wheels/nested/extra.whl"
    nested.parent.mkdir()
    nested.write_bytes(b"nested-wheel")
    manifest["artifacts"].append({
        "path": "source/wheels/nested/extra.whl", "size": nested.stat().st_size,
        "mode": 0o644, "sha256": sha(nested),
    })
    manifest["artifacts"].sort(key=lambda row: row["path"])
    manifest["artifact_count"] = len(manifest["artifacts"])
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(verify_release.ReleaseError, match="nested wheel paths"):
        verify_release.verify_release(manifest_path, root)


def test_unaddressed_docker_blob_file_is_rejected(tmp_path: Path):
    root, manifest_path, manifest = release_fixture(tmp_path)
    archive_rel = manifest["image"]["archive_path"]
    archive = root / archive_rel
    extracted = tmp_path / "docker-tree"
    extracted.mkdir()
    with tarfile.open(archive, "r:gz") as source:
        source.extractall(extracted, filter="data")
    (extracted / "blobs/unaddressed").write_bytes(b"ambiguous")
    replacement = tmp_path / "replacement.tar.gz"
    with tarfile.open(replacement, "w:gz") as target:
        for name in ("blobs", "index.json", "manifest.json", "oci-layout"):
            target.add(extracted / name, arcname=name)
    replacement.replace(archive)
    row = next(item for item in manifest["artifacts"] if item["path"] == archive_rel)
    archive_sha = sha(archive)
    archive_size = archive.stat().st_size
    row.update({"sha256": archive_sha, "size": archive_size})
    manifest["image"].update({"archive_sha256": archive_sha, "archive_size": archive_size})
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(verify_release.ReleaseError, match="not content-addressed"):
        verify_release.verify_release(manifest_path, root)


def test_docker_release_tag_substitution_is_rejected(tmp_path: Path):
    root, manifest_path, manifest = release_fixture(tmp_path)
    manifest["image"]["reference"] = "ispindel-release:20260803T030001Z-123456789abc"
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(verify_release.ReleaseError, match="release-ID binding"):
        verify_release.verify_release(manifest_path, root)


def test_docker_config_identity_substitution_is_rejected(tmp_path: Path):
    root, manifest_path, manifest = release_fixture(tmp_path)
    manifest["image"]["config_sha256"] = "6" * 64
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(verify_release.ReleaseError, match="OCI config digest mismatch"):
        verify_release.verify_release(manifest_path, root)


def test_wait_live_retries_transient_probe_and_verifies_binding(monkeypatch):
    url_attempts = iter((OSError("not ready"), None))

    class Response:
        status = 200
        def __enter__(self):
            return self
        def __exit__(self, *_args):
            return False

    def fake_urlopen(*_args, **_kwargs):
        result = next(url_attempts)
        if isinstance(result, Exception):
            raise result
        return Response()

    inspect = {
        "NetworkSettings": {
            "Ports": {"8098/tcp": [{"HostIp": "127.0.0.1", "HostPort": "32831"}]}
        }
    }
    monkeypatch.setattr(build_release, "run", lambda _argv: subprocess.CompletedProcess([], 0, json.dumps([inspect]), ""))
    monkeypatch.setattr(build_release.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(build_release.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(build_release.json, "load", lambda _response: {"status": "ok", "service": "ispindel-dashboard"})
    build_release.wait_live("fixture", 32831, timeout=5)


def test_release_archive_extracts_into_absent_child(tmp_path):
    payload = tmp_path / "source"
    payload.mkdir()
    (payload / "proof.txt").write_text("verified\n", encoding="utf-8")
    archive = tmp_path / "release.tar.zst"
    build_release.create_archive(payload, archive)
    verify_parent = tmp_path / "verify-parent"
    verify_parent.mkdir()
    destination = verify_parent / "payload"
    build_release.safe_extract(archive, destination)
    assert (destination / "proof.txt").read_text(encoding="utf-8") == "verified\n"


def test_compose_publish_spec_supports_fixed_and_disposable_loopback_ports():
    compose = (ROOT / "docker-compose.yml").read_text()
    builder = (SCRIPTS / "build_release.py").read_text()
    assert '${ISPINDEL_BACKEND_PUBLISH:-127.0.0.1:18098:8098}' in compose
    assert '"ISPINDEL_BACKEND_PUBLISH": f"127.0.0.1:{backend_port}:8098"' in builder
    assert "ISPINDEL_BACKEND_BIND" not in compose + builder


def test_release_builder_has_offline_and_clean_tree_gates():
    source = (SCRIPTS / "build_release.py").read_text()
    assert '"git", "status", "--porcelain"' in source
    assert 'docker_host_override not in {"", "unix:///var/run/docker.sock"}' in source
    assert 'context_host != "unix:///var/run/docker.sock"' in source
    assert '"build", "--pull=false"' in source
    assert '"up", "-d", "--build", "--pull", "never"' in source
    assert '"docker", "run", "--rm", "--network", "none"' in source
    assert '"docker", "volume", "create"' in source
    assert 'image_ref, "10001:10001", "/data"' in source
    assert source.count('"git", "status", "--porcelain"') == 2
    assert '"git", "ls-files", "--error-unmatch"' in source
    assert "PHASE7B_AUTHORITY_OK" in source and "PHASE7B_REPAIR_AUTHORITY_OK" not in source
    assert "verify-caddy-config.sh" in source and "verify-network-boundary.sh" in source
    assert "--output-root" not in source
    assert source.index('"docker", "run", "--rm", "--network", "none"') < source.index('atomic_json(root / "descriptors/RELEASE.json"')
    assert "DISPOSABLE_CLEANUP_OK" in source
    assert "curl" not in source and "wget" not in source
    assert "latest" not in source


def test_compose_disposable_overrides_preserve_production_defaults(tmp_path: Path):
    evidence = tmp_path / "evidence"
    secrets = tmp_path / "secrets"
    evidence.mkdir()
    secrets.mkdir()
    env = os.environ.copy()
    env.update({
        "ISPINDEL_EVIDENCE_DIR": str(evidence),
        "ISPINDEL_SECRETS_DIR": str(secrets),
        "ISPINDEL_GID": "10001",
    })
    proc = subprocess.run(
        ["docker", "compose", "config", "--format", "json"],
        cwd=ROOT, env=env, text=True, capture_output=True, check=True,
    )
    config = json.loads(proc.stdout)
    service = config["services"]["ispindel-dashboard"]
    assert service["container_name"] == "ispindel-dashboard"
    assert service["environment"]["BREW_SQLITE_PATH"] == "/data/brew.db"
    assert service["ports"][0]["host_ip"] == "127.0.0.1"
    assert service["ports"][0]["published"] == "18098"


def test_manifest_schema_is_strict_json_schema():
    schema = json.loads((ROOT / "release/manifest.schema.json").read_text())
    assert schema["additionalProperties"] is False
    assert schema["properties"]["artifacts"]["items"]["additionalProperties"] is False
    assert schema["properties"]["schema"]["const"] == "ispindel-release-manifest/v1"
    phase = schema["properties"]["phase7b"]
    assert set(phase["required"]) == set(phase["properties"])
    assert phase["properties"]["authority_path"]["const"] == "authority/PHASE7B-REPAIR-AUTHORITY.json"


def test_release_retention_dry_run_lists_only_authorised_paths(tmp_path: Path):
    root = tmp_path / "candidates"
    root.mkdir()
    retained_id = "20260914T120000Z-aaaaaaaaaaaa"
    stale_id = "20260914T120100Z-bbbbbbbbbbbb"
    current_id = "20260914T120200Z-cccccccccccc"
    symlink_id = "20260914T120300Z-dddddddddddd"
    regular_file_id = "20260914T120400Z-eeeeeeeeeeee"
    missing_id = "20260914T120500Z-ffffffffffff"
    stale_dir = root / stale_id
    current_dir = root / current_id
    retained_dir = root / retained_id
    for child in (stale_dir, current_dir, retained_dir):
        child.mkdir()
        (child / "manifest.json").write_bytes(b"manifest")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "marker.txt").write_bytes(b"outside")
    nested = root / "nested"
    nested.mkdir()
    (nested / "deep").mkdir()

    snapshot = sorted(item.relative_to(root).as_posix() for item in root.rglob("*"))

    records = ops_common.retention_dry_run(
        root,
        [retained_id, current_id],
        [
            {"identity": stale_id, "path": stale_id},
            {"identity": current_id, "path": current_id},
            {"identity": retained_id, "path": retained_id},
        ],
    )
    assert records == [
        {"identity": stale_id, "path": stale_id, "status": "stale"},
        {"identity": retained_id, "path": retained_id, "status": "retained"},
        {"identity": current_id, "path": current_id, "status": "retained"},
    ]
    assert sorted(item.relative_to(root).as_posix() for item in root.rglob("*")) == snapshot

    with pytest.raises(ops_common.RetentionError, match="malformed retention identity"):
        ops_common.retention_dry_run(root, ["bogus"], [{"identity": stale_id, "path": stale_id}])
    with pytest.raises(ops_common.RetentionError, match="malformed retention identity"):
        ops_common.retention_dry_run(root, [], [{"identity": "not-a-valid-identity", "path": stale_id}])
    with pytest.raises(ops_common.RetentionError, match="candidate path escapes root"):
        ops_common.retention_dry_run(root, [], [{"identity": current_id, "path": "../outside"}])
    with pytest.raises(ops_common.RetentionError, match="candidate path must be a direct child"):
        ops_common.retention_dry_run(root, [], [{"identity": current_id, "path": "nested/deep"}])
    with pytest.raises(ops_common.RetentionError, match="candidate path must be a non-empty string"):
        ops_common.retention_dry_run(root, [], [{"identity": current_id, "path": ""}])
    with pytest.raises(ops_common.RetentionError, match="identity does not match directory name"):
        ops_common.retention_dry_run(
            root,
            [current_id],
            [{"identity": current_id, "path": stale_id}],
        )

    symlink_child = root / symlink_id
    symlink_child.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ops_common.RetentionError, match="candidate symlink is forbidden"):
        ops_common.retention_dry_run(root, [], [{"identity": symlink_id, "path": symlink_id}])

    regular_file = root / regular_file_id
    regular_file.write_bytes(b"file")
    with pytest.raises(ops_common.RetentionError, match="candidate is not a directory"):
        ops_common.retention_dry_run(
            root,
            [],
            [{"identity": regular_file_id, "path": regular_file_id}],
        )

    with pytest.raises(ops_common.RetentionError, match="candidate is absent"):
        ops_common.retention_dry_run(root, [], [{"identity": missing_id, "path": missing_id}])

    with pytest.raises(ops_common.RetentionError, match="candidate root is not a regular directory"):
        ops_common.retention_dry_run(tmp_path / "missing-root", [], [])

    post_snapshot = sorted(item.relative_to(root).as_posix() for item in root.rglob("*"))
    ops_common.retention_dry_run(
        root,
        [retained_id, current_id],
        [{"identity": stale_id, "path": stale_id}],
    )
    assert sorted(item.relative_to(root).as_posix() for item in root.rglob("*")) == post_snapshot
    assert (outside / "marker.txt").read_bytes() == b"outside"
