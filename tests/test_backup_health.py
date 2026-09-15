"""Deterministic /api/backup-health endpoint contracts.

This slice is intentionally narrow:

- It does not modify the backup producer. ``scripts/backup-production.py``
  still owns writing ``${ISPINDEL_BACKUP_ROOT:-/var/backups/ispindel-dashboard}/backup_health.json``.
- It does not claim that the live container can actually see a real backup
  receipt on disk. It only proves that the source/config test contract is
  green: parser, endpoint, mount, and isolation guarantees.

Contract:
- schema ``ispindel-backup-health/v1`` with ``run_id``, ``verified_at``,
  ``mode``, ``offhost_path``, ``remote_retention`` (last two are NEVER
  returned by the endpoint, even if they exist on disk).
- Verified_at MUST be ISO-8601 UTC with a trailing ``Z``; anything else,
  including future timestamps, is non-green.
- Status ``ok`` requires both the receipt AND the filesystem mtime to be
  inside the bounded TTL (default 86400 seconds — 24 hours, comfortably
  above the 6-hour backup timer).
- Status codes:
    status=ok                       -> HTTP 200
    status=missing|stale|parse_error|freshness_violation
                                  -> HTTP 503
- Response body NEVER contains ``path``, ``offhost_path``,
  ``remote_retention``, or the configured health directory.
"""
from __future__ import annotations

import json
import os
import socket
import stat
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc)


# ----- helpers ---------------------------------------------------------------

def _set_fresh_mtime(path: Path, *, now: datetime, age_seconds: float = 60.0) -> None:
    """Set ``path``'s mtime to ``now - age_seconds`` so the freshness check
    inside the receipt parser observes a fresh filesystem signal."""
    timestamp = (now - timedelta(seconds=age_seconds)).timestamp()
    os.utime(path, (timestamp, timestamp))


def _valid_receipt(*, run_id: str = "2026-09-14T12-00-00Z",
                   verified_at: str = "2026-09-14T11:55:00Z",
                   mode: str = "single",
                   offhost_path: str = "backup.example.test:/srv/backups/2026-09-14T12-00-00Z",
                   remote_retention: dict | None = None) -> dict:
    return {
        "schema": "ispindel-backup-health/v1",
        "run_id": run_id,
        "verified_at": verified_at,
        "mode": mode,
        "offhost_path": offhost_path,
        "remote_retention": remote_retention or {"verified": 28, "deleted": 0, "kept": 28},
    }


# ----- parser unit tests -----------------------------------------------------

def test_parser_accepts_valid_v1_receipt(app_module, tmp_path):
    receipt = tmp_path / "backup_health.json"
    receipt.write_text(json.dumps(_valid_receipt()))
    result = app_module.parse_backup_health_receipt(receipt, now=NOW, ttl_seconds=86400)
    assert result["status"] == "ok"
    assert result["run_id"] == "2026-09-14T12-00-00Z"
    assert result["mode"] == "single"
    assert result["verified_at"] == "2026-09-14T11:55:00Z"
    # Sensitive fields never appear in the envelope.
    assert "offhost_path" not in result
    assert "remote_retention" not in result
    assert "path" not in result


def test_parser_missing_file_is_not_green(app_module, tmp_path):
    result = app_module.parse_backup_health_receipt(tmp_path / "absent.json", now=NOW, ttl_seconds=86400)
    assert result["status"] == "missing"


def test_parser_malformed_json_is_not_green(app_module, tmp_path):
    receipt = tmp_path / "backup_health.json"
    receipt.write_text("{not json")
    result = app_module.parse_backup_health_receipt(receipt, now=NOW, ttl_seconds=86400)
    assert result["status"] == "parse_error"


def test_parser_wrong_schema_is_not_green(app_module, tmp_path):
    receipt = tmp_path / "backup_health.json"
    body = _valid_receipt(); body["schema"] = "ispindel-backup-health/v2"
    receipt.write_text(json.dumps(body))
    result = app_module.parse_backup_health_receipt(receipt, now=NOW, ttl_seconds=86400)
    assert result["status"] == "parse_error"


def test_parser_rejects_naive_verified_at(app_module, tmp_path):
    receipt = tmp_path / "backup_health.json"
    body = _valid_receipt(); body["verified_at"] = "2026-09-14T11:55:00"  # no Z, no offset
    receipt.write_text(json.dumps(body))
    result = app_module.parse_backup_health_receipt(receipt, now=NOW, ttl_seconds=86400)
    assert result["status"] == "parse_error"


def test_parser_rejects_offset_verified_at(app_module, tmp_path):
    receipt = tmp_path / "backup_health.json"
    body = _valid_receipt(); body["verified_at"] = "2026-09-14T11:55:00+00:00"  # offset, not Z
    receipt.write_text(json.dumps(body))
    result = app_module.parse_backup_health_receipt(receipt, now=NOW, ttl_seconds=86400)
    assert result["status"] == "parse_error"


def test_parser_rejects_future_verified_at(app_module, tmp_path):
    receipt = tmp_path / "backup_health.json"
    body = _valid_receipt(verified_at="2099-01-01T00:00:00Z")
    receipt.write_text(json.dumps(body))
    result = app_module.parse_backup_health_receipt(receipt, now=NOW, ttl_seconds=86400)
    assert result["status"] == "freshness_violation"
    assert "future" in (result.get("detail") or "").lower()


def test_parser_rejects_stale_receipt(app_module, tmp_path):
    receipt = tmp_path / "backup_health.json"
    body = _valid_receipt(verified_at="2026-09-13T00:00:00Z")  # > 24h old
    receipt.write_text(json.dumps(body))
    result = app_module.parse_backup_health_receipt(receipt, now=NOW, ttl_seconds=86400)
    assert result["status"] == "stale"


def test_parser_rejects_stale_mtime_even_when_verified_at_is_fresh(app_module, tmp_path):
    """An independent stat()-based mtime check defends against a forged or
    hand-edited verified_at. We set the verified_at to be 5 minutes old but
    back-date the mtime to >24h ago. The receipt must therefore be reported
    as stale/freshness_violation, never green."""
    receipt = tmp_path / "backup_health.json"
    body = _valid_receipt(verified_at="2026-09-14T11:55:00Z")
    receipt.write_text(json.dumps(body))
    stale_mtime = (NOW - timedelta(seconds=90000)).timestamp()
    os.utime(receipt, (stale_mtime, stale_mtime))
    result = app_module.parse_backup_health_receipt(receipt, now=NOW, ttl_seconds=86400)
    assert result["status"] != "ok"
    assert result["status"] in {"stale", "freshness_violation"}


def test_parser_never_returns_configured_path(app_module, tmp_path):
    receipt = tmp_path / "backup_health.json"
    receipt.write_text(json.dumps(_valid_receipt()))
    sensitive_path = str(receipt.resolve())
    result = app_module.parse_backup_health_receipt(receipt, now=NOW, ttl_seconds=86400)
    serialised = json.dumps(result)
    assert sensitive_path not in serialised
    assert str(tmp_path) not in serialised


# ----- endpoint behaviour ----------------------------------------------------

EXPECTED_OK_KEYS = {"status", "schema", "run_id", "mode", "verified_at",
                    "file_mtime", "file_age_seconds", "ttl_seconds"}


def test_endpoint_returns_200_and_metadata_for_valid_receipt(
    app_module, test_client, tmp_path, monkeypatch
):
    receipt = tmp_path / "backup_health.json"
    # Build a receipt whose verified_at is anchored to actual wall-clock so
    # the endpoint's ``datetime.now()`` observes a fresh receipt. The parser's
    # own unit tests already cover the deterministic-NOW branch.
    real_now = datetime.now(timezone.utc)
    recent = (real_now - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    receipt.write_text(json.dumps(_valid_receipt(verified_at=recent)))
    os.utime(receipt, ((real_now - timedelta(seconds=60)).timestamp(),
                       (real_now - timedelta(seconds=60)).timestamp()))
    monkeypatch.setattr(app_module, "BACKUP_HEALTH_PATH", receipt)
    response = test_client.get("/api/backup-health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["schema"] == "ispindel-backup-health/v1"
    assert body["run_id"] == "2026-09-14T12-00-00Z"
    assert body["mode"] == "single"
    assert body["verified_at"] == recent
    assert EXPECTED_OK_KEYS.issubset(body.keys())
    assert isinstance(body["file_mtime"], str) and body["file_mtime"].endswith("Z")
    assert body["file_age_seconds"] >= 0
    assert body["ttl_seconds"] == 86400
    # Sensitive fields never leak.
    assert "offhost_path" not in body
    assert "remote_retention" not in body
    assert "path" not in body
    assert str(receipt) not in response.text


def test_endpoint_returns_503_for_missing_file(
    app_module, test_client, tmp_path, monkeypatch
):
    monkeypatch.setattr(app_module, "BACKUP_HEALTH_PATH", tmp_path / "absent.json")
    response = test_client.get("/api/backup-health")
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "missing"
    assert "service" in body and body["service"] == "ispindel-dashboard"
    assert str(tmp_path) not in response.text


def test_endpoint_returns_503_for_malformed_receipt(
    app_module, test_client, tmp_path, monkeypatch
):
    receipt = tmp_path / "backup_health.json"
    receipt.write_text("{")
    monkeypatch.setattr(app_module, "BACKUP_HEALTH_PATH", receipt)
    response = test_client.get("/api/backup-health")
    assert response.status_code == 503
    assert response.json()["status"] == "parse_error"


def test_endpoint_returns_503_for_wrong_schema(
    app_module, test_client, tmp_path, monkeypatch
):
    receipt = tmp_path / "backup_health.json"
    body = _valid_receipt(); body["schema"] = "ispindel-backup-health/v9"
    receipt.write_text(json.dumps(body))
    monkeypatch.setattr(app_module, "BACKUP_HEALTH_PATH", receipt)
    response = test_client.get("/api/backup-health")
    assert response.status_code == 503
    assert response.json()["status"] == "parse_error"


def test_endpoint_returns_503_for_future_verified_at(
    app_module, test_client, tmp_path, monkeypatch
):
    receipt = tmp_path / "backup_health.json"
    body = _valid_receipt(verified_at="2099-01-01T00:00:00Z")
    receipt.write_text(json.dumps(body))
    monkeypatch.setattr(app_module, "BACKUP_HEALTH_PATH", receipt)
    response = test_client.get("/api/backup-health")
    assert response.status_code == 503
    payload = response.json()
    assert payload["status"] == "freshness_violation"
    assert "future" in (payload.get("detail") or "").lower()


def test_endpoint_returns_503_for_stale_receipt(
    app_module, test_client, tmp_path, monkeypatch
):
    receipt = tmp_path / "backup_health.json"
    body = _valid_receipt(verified_at="2026-09-12T00:00:00Z")  # > 48h old
    receipt.write_text(json.dumps(body))
    monkeypatch.setattr(app_module, "BACKUP_HEALTH_PATH", receipt)
    response = test_client.get("/api/backup-health")
    assert response.status_code == 503
    assert response.json()["status"] == "stale"


def test_endpoint_returns_503_for_stale_mtime(
    app_module, test_client, tmp_path, monkeypatch
):
    receipt = tmp_path / "backup_health.json"
    body = _valid_receipt(verified_at="2026-09-14T11:55:00Z")
    receipt.write_text(json.dumps(body))
    stale_mtime = (NOW - timedelta(seconds=200000)).timestamp()
    os.utime(receipt, (stale_mtime, stale_mtime))
    monkeypatch.setattr(app_module, "BACKUP_HEALTH_PATH", receipt)
    response = test_client.get("/api/backup-health")
    assert response.status_code == 503
    assert response.json()["status"] != "ok"


def test_endpoint_never_leaks_configured_path(
    app_module, test_client, tmp_path, monkeypatch
):
    receipt = tmp_path / "backup_health.json"
    receipt.write_text(json.dumps(_valid_receipt()))
    _set_fresh_mtime(receipt, now=NOW)
    monkeypatch.setattr(app_module, "BACKUP_HEALTH_PATH", receipt)
    response = test_client.get("/api/backup-health")
    assert str(receipt) not in response.text
    assert str(tmp_path) not in response.text
    body = response.json()
    assert "path" not in body
    assert "offhost_path" not in body
    assert "remote_retention" not in body


# ----- compose contract ------------------------------------------------------

def test_parser_rejects_verified_at_one_second_in_the_future(app_module, tmp_path):
    """A verified_at only 1 second in the future must already be non-green.

    The contract requires every future timestamp to be non-green. The previous
    candidate allowed a 5-minute clock skew window which silently turned
    mildly-future receipts green; this regression pins the strict semantics.
    """
    receipt = tmp_path / "backup_health.json"
    body = _valid_receipt(verified_at=(NOW + timedelta(seconds=1)).strftime("%Y-%m-%dT%H:%M:%SZ"))
    receipt.write_text(json.dumps(body))
    result = app_module.parse_backup_health_receipt(receipt, now=NOW, ttl_seconds=86400)
    assert result["status"] == "freshness_violation"


def test_parser_stat_oserror_sanitizes_configured_path(app_module):
    """A stat() OSError must not leak the configured path into the detail.

    The endpoint contract forbids the configured health path from appearing in
    any detail string. We monkeypatch the path-like object's ``stat`` to
    raise an OSError whose message embeds a sentinel path and assert that the
    parser's detail string is path-free.
    """

    class _PathWithPathLeakingStat:
        name = "backup_health.json"

        def exists(self) -> bool:
            return True

        def stat(self):
            raise OSError(13, "Permission denied", "/var/backups/ispindel-dashboard/secret.json")

    sentinel = "/var/backups/ispindel-dashboard/secret.json"
    fp = _PathWithPathLeakingStat()
    result = app_module.parse_backup_health_receipt(fp, now=NOW, ttl_seconds=86400)
    assert result["status"] == "parse_error"
    detail = result.get("detail") or ""
    assert sentinel not in detail
    assert "/var/backups" not in detail
    assert "secret.json" not in detail


def test_parser_read_oserror_sanitizes_configured_path(app_module):
    """A read_text() OSError must not leak the configured path either."""

    class _PathWithPathLeakingRead:
        name = "backup_health.json"

        def __init__(self) -> None:
            self._stat_returned = True

        def exists(self) -> bool:
            return True

        def stat(self):
            # Provide a valid stat so the parser reaches the read step.
            import os as _os
            return _os.stat_result(
                (0o644, 0, 0, 1, 10001, 10001, 0, NOW.timestamp(), NOW.timestamp(), NOW.timestamp())
            )

        def read_text(self, *args, **kwargs):
            raise OSError(13, "Permission denied", "/var/backups/ispindel-dashboard/secret.json")

    sentinel = "/var/backups/ispindel-dashboard/secret.json"
    fp = _PathWithPathLeakingRead()
    result = app_module.parse_backup_health_receipt(fp, now=NOW, ttl_seconds=86400)
    assert result["status"] == "parse_error"
    detail = result.get("detail") or ""
    assert sentinel not in detail
    assert "/var/backups" not in detail


def test_write_backup_health_if_verified_uses_world_readable_mode(tmp_path):
    """backup_health.json must be readable by the dashboard container UID 10001.

    The compose contract binds a single file from the backup producer's root
    read-only into the container at ``/backup-health/backup_health.json``;
    UID 10001 must be able to read the receipt while the producer keeps full
    ownership. The health writer therefore writes ``mode=0o644`` while the
    generic ``atomic_json`` default MUST stay at ``0o600`` so backup manifests
    and indexes retain their restrictive permissions.
    """
    import sys
    sys.path.insert(0, str(ROOT / "scripts"))
    from backup_common import write_backup_health_if_verified, atomic_json

    health = tmp_path / "backup_health.json"
    write_backup_health_if_verified(health, lambda: True, payload={"schema": "x"})
    st = health.stat()
    assert stat.S_IMODE(st.st_mode) == 0o644

    # The generic atomic_json default must remain 0o600 so backup manifests,
    # indexes, and sidecars keep their existing restrictive permissions.
    other = tmp_path / "manifest.json"
    atomic_json(other, {"schema": "ispindel-backup-manifest/v1"})
    assert stat.S_IMODE(other.stat().st_mode) == 0o600


def test_compose_exposes_backup_health_file_read_only(tmp_path):
    """The compose must bind the single ``backup_health.json`` file, not a directory.

    The producer writes ``backup_health.json`` inside its backup root. The
    container must read ONLY that single file, not the whole backup
    generations tree. The endpoint contract therefore depends on a narrow
    file bind mount with a pinned file target.
    """
    evidence = tmp_path / "evidence"; evidence.mkdir()
    secrets = tmp_path / "secrets"; secrets.mkdir()
    backup_health = tmp_path / "backup-health"; backup_health.mkdir()
    env = os.environ | {
        "ISPINDEL_EVIDENCE_DIR": str(evidence),
        "ISPINDEL_SECRETS_DIR": str(secrets),
        "ISPINDEL_GID": "10001",
        # ISPINDEL_BACKUP_HEALTH_FILE is intentionally OPTIONAL: when absent,
        # the compose must still validate without forcing an extra env var.
    }
    proc = subprocess.run(
        ["docker", "compose", "config", "--format", "json"],
        cwd=ROOT, env=env, text=True, capture_output=True, check=True,
    )
    service = json.loads(proc.stdout)["services"]["ispindel-dashboard"]
    mounts = {item["target"]: item for item in service["volumes"]}
    # The mount MUST exist, MUST be a bind mount, MUST be read_only.
    backup_mount = mounts["/backup-health/backup_health.json"]
    assert backup_mount["type"] == "bind"
    assert backup_mount["read_only"] is True
    # The default source MUST point at the backup producer's default root +
    # backup_health.json basename.
    assert backup_mount["source"].endswith("backup_health.json")
    assert backup_mount["source"].endswith("/var/backups/ispindel-dashboard/backup_health.json")
    # The directory mount under /backup-health MUST NOT exist — we must not
    # expose any directory containing DB generations.
    assert "/backup-health" not in mounts or "/backup-health/backup_health.json" in mounts
    directory_targets = {
        item["target"] for item in service["volumes"] if item.get("type") == "bind"
    }
    assert "/backup-health" not in directory_targets
    # The environment MUST carry the configured health path.
    env_vars = service["environment"]
    assert env_vars["BACKUP_HEALTH_PATH"] == "/backup-health/backup_health.json"
    assert env_vars["BACKUP_HEALTH_TTL_SECONDS"] == "86400"
    # The compose file MUST NOT mount the database generations directory.
    for item in service["volumes"]:
        target = item.get("target", "")
        assert "/var/backups" not in target
        assert "/data/backups" not in target


def test_compose_overrides_backup_health_file_when_env_is_set(tmp_path):
    evidence = tmp_path / "evidence"; evidence.mkdir()
    secrets = tmp_path / "secrets"; secrets.mkdir()
    backup_health = tmp_path / "backup-health"; backup_health.mkdir()
    backup_health_file = backup_health / "backup_health.json"
    backup_health_file.write_text("{}")
    env = os.environ | {
        "ISPINDEL_EVIDENCE_DIR": str(evidence),
        "ISPINDEL_SECRETS_DIR": str(secrets),
        "ISPINDEL_GID": "10001",
        "ISPINDEL_BACKUP_HEALTH_FILE": str(backup_health_file),
    }
    proc = subprocess.run(
        ["docker", "compose", "config", "--format", "json"],
        cwd=ROOT, env=env, text=True, capture_output=True, check=True,
    )
    service = json.loads(proc.stdout)["services"]["ispindel-dashboard"]
    mounts = {item["target"]: item for item in service["volumes"]}
    backup_mount = mounts["/backup-health/backup_health.json"]
    assert backup_mount["source"] == str(backup_health_file)
    assert backup_mount["read_only"] is True