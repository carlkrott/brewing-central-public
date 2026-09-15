#!/usr/bin/env python3
"""Fail-closed production secret and token-schema preflight.

This runs before the permanent Compose stack starts.  It deliberately checks
only metadata and schema, never prints token material, and rejects the volatile
/run/secrets path as a host source.
"""
from __future__ import annotations

import json
import os
import re
import sys
import stat
from pathlib import Path
from typing import Mapping, NoReturn

EXPECTED_SECRETS_DIR = Path("/etc/ispindel/secrets")
TOKEN_FILE = "ingest-tokens.json"
HEX_RE = re.compile(r"^[0-9a-fA-F]+$")
DIGEST_RE = re.compile(r"^[0-9a-fA-F]{64}$")


class SecretPreflightError(RuntimeError):
    pass


def _fail(message: str) -> NoReturn:
    raise SecretPreflightError(message)


def _gid(env: Mapping[str, str], expected_gid: int | None) -> int:
    if expected_gid is not None:
        return expected_gid
    value = env.get("ISPINDEL_GID", "")
    if not value.isdigit():
        _fail("ISPINDEL_GID must be a numeric supplemental group")
    result = int(value)
    if not 0 <= result <= 2**31 - 1:
        _fail("ISPINDEL_GID is outside the valid range")
    return result


def _metadata(path: Path, *, kind: str, mode: int, gid: int, label: str) -> os.stat_result:
    try:
        value = os.lstat(path)
    except OSError:
        _fail(f"{label} is unavailable")
    if stat.S_ISLNK(value.st_mode):
        _fail(f"{label} must not be a symlink")
    if kind == "directory" and not stat.S_ISDIR(value.st_mode):
        _fail(f"{label} has the wrong file type")
    if kind == "regular" and not stat.S_ISREG(value.st_mode):
        _fail(f"{label} has the wrong file type")
    if value.st_uid != 0 or value.st_gid != gid:
        _fail(f"{label} owner/group is not root/{gid}")
    if stat.S_IMODE(value.st_mode) != mode:
        _fail(f"{label} mode must be {mode:04o}")
    return value


def _read_json(path: Path) -> object:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as handle:
            data = handle.read(1_048_577)
    except OSError as exc:
        _fail("ingest token verifier cannot be opened safely")
    if len(data) > 1_048_576:
        _fail("ingest token verifier is larger than the contract limit")
    try:
        return json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        _fail("ingest token verifier is not valid UTF-8 JSON")


def validate_tokens(raw: object) -> int:
    if not isinstance(raw, dict) or set(raw) != {"schema_version", "devices"}:
        _fail("token configuration has unexpected fields")
    if raw.get("schema_version") != "ingest-tokens-v1":
        _fail("token configuration schema_version is invalid")
    devices = raw.get("devices")
    if not isinstance(devices, dict) or not devices:
        _fail("token configuration has no devices")
    for device_id, record in devices.items():
        if not isinstance(device_id, str) or not device_id.strip() or len(device_id) > 128:
            _fail("token configuration contains an invalid device ID")
        if not isinstance(record, dict) or set(record) != {"verifiers"}:
            _fail(f"token configuration for {device_id!r} is invalid")
        verifiers = record["verifiers"]
        if not isinstance(verifiers, list) or not 1 <= len(verifiers) <= 2:
            _fail(f"device {device_id!r} must have one or two verifiers")
        for verifier in verifiers:
            if not isinstance(verifier, dict) or set(verifier) != {"salt_hex", "verifier_hex", "not_after"}:
                _fail(f"device {device_id!r} verifier has unexpected fields")
            salt = verifier["salt_hex"]
            digest = verifier["verifier_hex"]
            if not isinstance(salt, str) or len(salt) < 32 or len(salt) % 2 or not HEX_RE.fullmatch(salt):
                _fail(f"device {device_id!r} verifier salt is invalid")
            if not isinstance(digest, str) or not DIGEST_RE.fullmatch(digest):
                _fail(f"device {device_id!r} verifier digest is invalid")
            if verifier["not_after"] is not None and not isinstance(verifier["not_after"], str):
                _fail(f"device {device_id!r} verifier expiry is invalid")
    return len(devices)


def validate(
    env: Mapping[str, str] | None = None,
    *,
    secrets_dir: Path = EXPECTED_SECRETS_DIR,
    expected_gid: int | None = None,
) -> int:
    source = os.environ if env is None else env
    if source.get("ISPINDEL_MODE", "production") != "production":
        _fail("production secret preflight requires ISPINDEL_MODE=production")
    if source.get("ISPINDEL_SECRETS_DIR") != str(EXPECTED_SECRETS_DIR):
        _fail("ISPINDEL_SECRETS_DIR must be the persistent /etc/ispindel/secrets path")
    gid = _gid(source, expected_gid)
    _metadata(secrets_dir, kind="directory", mode=0o750, gid=gid, label="secret directory")
    token_path = secrets_dir / TOKEN_FILE
    _metadata(token_path, kind="regular", mode=0o640, gid=gid, label="ingest token verifier")
    return validate_tokens(_read_json(token_path))


def main() -> int:
    try:
        devices = validate()
    except SecretPreflightError as exc:
        print(f"ISPINDEL_SECRET_PREFLIGHT_FAILED reason={exc}", file=sys.stderr)
        return 2
    print(f"ISPINDEL_SECRET_PREFLIGHT_OK devices={devices}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
