"""Authentication and rate limiting for stock iSpindel ingest."""
from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping

PBKDF2_ITERATIONS = 100_000
MAX_ACTIVE_VERIFIERS = 2
_HEX_32 = re.compile(r"^[0-9a-f]{64}$")
_HOST = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$")
_DUMMY_SALTS = (
    bytes.fromhex("1f" * 16),
    bytes.fromhex("a7" * 16),
)
_DUMMY_DIGESTS = (
    bytes.fromhex("63" * 32),
    bytes.fromhex("d2" * 32),
)


@dataclass(frozen=True)
class TokenVerifier:
    salt: bytes
    digest: bytes
    not_after: datetime | None


@dataclass(frozen=True)
class SecurityConfig:
    mode: str
    devices: dict[str, tuple[TokenVerifier, ...]]
    tailnet_fqdn: str | None
    rate_capacity: int
    rate_refill_per_second: float

    @property
    def authentication_enabled(self) -> bool:
        return self.mode == "production"

    @property
    def allowed_hosts(self) -> list[str]:
        hosts = [
            os.environ.get("ISPINDEL_LAN_HOST", "lan.example.test"),
            "127.0.0.1",
            "localhost",
            "testserver",
        ]
        if self.tailnet_fqdn:
            hosts.insert(0, self.tailnet_fqdn)
        return hosts


def _parse_utc(value: object) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.endswith("Z"):
        raise RuntimeError("token not_after must be null or UTC with a Z suffix")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise RuntimeError("token not_after is invalid") from exc
    if parsed.tzinfo is None:
        raise RuntimeError("token not_after must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def _positive_int(env: Mapping[str, str], key: str, default: int) -> int:
    raw = env.get(key, str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{key} must be an integer") from exc
    if value <= 0:
        raise RuntimeError(f"{key} must be positive")
    return value


def _positive_float(env: Mapping[str, str], key: str, default: float) -> float:
    raw = env.get(key, str(default))
    try:
        value = float(raw)
    except ValueError as exc:
        raise RuntimeError(f"{key} must be numeric") from exc
    if not math.isfinite(value) or value <= 0:
        raise RuntimeError(f"{key} must be positive and finite")
    return value


def load_security_config(env: Mapping[str, str] | None = None) -> SecurityConfig:
    source = os.environ if env is None else env
    mode = source.get("ISPINDEL_MODE", "production")
    if mode not in {"production", "development", "test"}:
        raise RuntimeError("ISPINDEL_MODE must be production, development, or test")
    capacity = _positive_int(source, "ISPINDEL_RATE_CAPACITY", 12)
    refill = _positive_float(source, "ISPINDEL_RATE_REFILL_PER_SECOND", 1 / 30)
    if mode != "production":
        return SecurityConfig(mode, {}, source.get("TAILNET_FQDN"), capacity, refill)

    if source.get("ISPINDEL_ADMIN_CONFIGURED") != "true":
        raise RuntimeError("production requires ISPINDEL_ADMIN_CONFIGURED=true")
    fqdn = source.get("TAILNET_FQDN", "")
    if not fqdn or not _HOST.fullmatch(fqdn):
        raise RuntimeError("production requires a valid TAILNET_FQDN")
    path = Path(source.get("ISPINDEL_TOKEN_FILE", "/run/secrets/ingest-tokens.json"))
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("production ingest token configuration is missing or invalid") from exc
    if not isinstance(raw, dict) or set(raw) != {"schema_version", "devices"}:
        raise RuntimeError("token configuration has unexpected fields")
    if raw["schema_version"] != "ingest-tokens-v1" or not isinstance(raw["devices"], dict) or not raw["devices"]:
        raise RuntimeError("token configuration schema/devices are invalid")

    devices: dict[str, tuple[TokenVerifier, ...]] = {}
    for device_id, record in raw["devices"].items():
        if not isinstance(device_id, str) or not device_id.strip() or len(device_id) > 128:
            raise RuntimeError("token configuration contains an invalid device ID")
        if not isinstance(record, dict) or set(record) != {"verifiers"}:
            raise RuntimeError(f"token configuration for {device_id!r} is invalid")
        entries = record["verifiers"]
        if not isinstance(entries, list) or not 1 <= len(entries) <= MAX_ACTIVE_VERIFIERS:
            raise RuntimeError(f"device {device_id!r} must have one or two verifiers")
        parsed_entries: list[TokenVerifier] = []
        for entry in entries:
            if not isinstance(entry, dict) or set(entry) != {"salt_hex", "verifier_hex", "not_after"}:
                raise RuntimeError(f"device {device_id!r} verifier has unexpected fields")
            salt_hex, digest_hex = entry["salt_hex"], entry["verifier_hex"]
            if not isinstance(salt_hex, str) or len(salt_hex) < 32 or len(salt_hex) % 2:
                raise RuntimeError(f"device {device_id!r} verifier salt is invalid")
            if not isinstance(digest_hex, str) or not _HEX_32.fullmatch(digest_hex):
                raise RuntimeError(f"device {device_id!r} verifier digest is invalid")
            try:
                salt = bytes.fromhex(salt_hex)
            except ValueError as exc:
                raise RuntimeError(f"device {device_id!r} verifier salt is invalid") from exc
            parsed_entries.append(TokenVerifier(salt, bytes.fromhex(digest_hex), _parse_utc(entry["not_after"])))
        devices[device_id] = tuple(parsed_entries)
    return SecurityConfig(mode, devices, fqdn, capacity, refill)


def derive_verifier(token: str, salt: bytes) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", token.encode("utf-8"), salt, PBKDF2_ITERATIONS)


def authenticate(config: SecurityConfig, device_id: str, token: object, *, now: datetime | None = None) -> bool:
    if not config.authentication_enabled:
        return True
    supplied = token if isinstance(token, str) else ""
    candidates = list(config.devices.get(device_id, ()))[:MAX_ACTIVE_VERIFIERS]
    while len(candidates) < MAX_ACTIVE_VERIFIERS:
        slot = len(candidates)
        candidates.append(TokenVerifier(_DUMMY_SALTS[slot], _DUMMY_DIGESTS[slot], None))
    observed = now or datetime.now(timezone.utc)
    accepted = False
    for candidate in candidates:
        computed = derive_verifier(supplied, candidate.salt)
        active = candidate.not_after is None or observed <= candidate.not_after
        accepted = (hmac.compare_digest(computed, candidate.digest) and active) or accepted
    return accepted and device_id in config.devices


class DeviceTokenBucket:
    def __init__(self, capacity: int, refill_per_second: float) -> None:
        self.capacity = float(capacity)
        self.refill = refill_per_second
        self._buckets: dict[str, tuple[float, float]] = {}
        self._lock = threading.Lock()

    def consume(self, device_id: str, *, observed: float | None = None) -> tuple[bool, int]:
        now = time.monotonic() if observed is None else observed
        with self._lock:
            tokens, previous = self._buckets.get(device_id, (self.capacity, now))
            tokens = min(self.capacity, tokens + max(0.0, now - previous) * self.refill)
            if tokens >= 1.0:
                self._buckets[device_id] = (tokens - 1.0, now)
                return True, 0
            self._buckets[device_id] = (tokens, now)
            return False, max(1, math.ceil((1.0 - tokens) / self.refill))
