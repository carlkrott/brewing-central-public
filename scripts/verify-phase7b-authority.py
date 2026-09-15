#!/usr/bin/env python3
"""Independently verify a repaired Phase 7B freeze authority."""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
import stat
import sys
from pathlib import Path, PurePosixPath
from typing import Any

HEX64 = re.compile(r"^[0-9a-f]{64}$")
EXCLUDED_BASENAMES = {
    "PHASE7B-FREEZE.json",
    "PHASE7B-FREEZE-RECONSTRUCTED.json",
    "PHASE7B-REPAIR-RECEIPT.json",
    "PHASE7B-REPAIR-AUTHORITY.json",
    "FINAL-REVIEW-AUTHORITY.json",
    "final-parent.txt",
    "final-adversarial.txt",
    "reconstructed-parent.txt",
    "reconstructed-adversarial.txt",
}


class AuthorityError(ValueError):
    """Closed verification failure."""


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def load_json_bytes(path: Path) -> tuple[bytes, dict[str, Any]]:
    data = path.read_bytes()
    value = json.loads(data)
    if not isinstance(value, dict):
        raise AuthorityError(f"not-object:{path.name}")
    return data, value


def validate_hex(value: Any, field: str) -> str:
    if not isinstance(value, str) or not HEX64.fullmatch(value):
        raise AuthorityError(f"invalid-sha256:{field}")
    return value


def safe_relative(root: Path, rel: Any) -> Path:
    if not isinstance(rel, str) or not rel or "\\" in rel:
        raise AuthorityError("invalid-path")
    pure = PurePosixPath(rel)
    if pure.is_absolute() or rel in {".", ".."} or any(part in {"", ".", ".."} for part in pure.parts):
        raise AuthorityError(f"unsafe-path:{rel}")
    current = root
    for part in pure.parts:
        current = current / part
        st = os.lstat(current)
        if stat.S_ISLNK(st.st_mode) or (current != root / rel and not stat.S_ISDIR(st.st_mode)):
            raise AuthorityError(f"unsafe-file-type:{rel}")
    if not stat.S_ISREG(os.lstat(current).st_mode):
        raise AuthorityError(f"not-regular:{rel}")
    if current.resolve().parent != (root / rel).resolve().parent or root.resolve() not in current.resolve().parents:
        raise AuthorityError(f"path-escape:{rel}")
    return current


def verify_file(path: Path, expected: Any, field: str) -> bytes:
    expected_sha = validate_hex(expected, field)
    data = path.read_bytes()
    if not hmac.compare_digest(digest(data), expected_sha):
        raise AuthorityError(f"hash-mismatch:{field}")
    return data


def verify(authority_path: Path) -> tuple[int, int]:
    authority_bytes, authority = load_json_bytes(authority_path)
    del authority_bytes
    if authority.get("schema_version") != "phase7b-repair-authority-v1":
        raise AuthorityError("authority-schema")
    if authority.get("receipt_kind") != "phase7b_repair_authority" or authority.get("verdict") != "GO":
        raise AuthorityError("authority-verdict")

    root_value = authority.get("evidence_root")
    if not isinstance(root_value, str):
        raise AuthorityError("evidence-root")
    root = Path(root_value)
    if not root.is_absolute() or not root.is_dir() or root.is_symlink():
        raise AuthorityError("evidence-root")

    contract = authority.get("contract")
    if not isinstance(contract, dict) or not isinstance(contract.get("path"), str):
        raise AuthorityError("contract-binding")
    contract_path = Path(contract["path"])
    if not contract_path.is_absolute() or not contract_path.is_file() or contract_path.is_symlink():
        raise AuthorityError("contract-path")
    contract_sha = validate_hex(contract.get("sha256"), "contract")
    verify_file(contract_path, contract_sha, "contract")

    repair = authority.get("repair_receipt")
    freeze_ref = authority.get("reconstructed_freeze")
    if not isinstance(repair, dict) or not isinstance(freeze_ref, dict):
        raise AuthorityError("authority-bindings")
    repair_path = safe_relative(root, repair.get("path"))
    repair_sha = validate_hex(repair.get("sha256"), "repair-receipt")
    verify_file(repair_path, repair_sha, "repair-receipt")
    freeze_path = safe_relative(root, freeze_ref.get("path"))
    freeze_sha = validate_hex(freeze_ref.get("sha256"), "freeze")
    freeze_bytes = verify_file(freeze_path, freeze_sha, "freeze")
    freeze = json.loads(freeze_bytes)

    if freeze.get("schema_version") != "phase06a2-r4c-phase7b-reconstructed-freeze-v1":
        raise AuthorityError("freeze-schema")
    entries = freeze.get("artifacts")
    if freeze.get("self_excluding") is not True or freeze.get("artifact_count") != 38:
        raise AuthorityError("freeze-count")
    if not isinstance(entries, list) or len(entries) != 38:
        raise AuthorityError("freeze-count")
    if any(not isinstance(entry, dict) or not isinstance(entry.get("path"), str) for entry in entries):
        raise AuthorityError("freeze-path-type")
    paths: list[str] = [entry["path"] for entry in entries]
    if len(paths) != 38 or paths != sorted(paths) or len(set(paths)) != 38:
        raise AuthorityError("freeze-path-order")

    tuples: list[tuple[str, int, str]] = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict) or set(entry) != {"path", "size", "sha256"}:
            raise AuthorityError(f"artifact-shape:{index}")
        rel = entry["path"]
        if PurePosixPath(rel).name in EXCLUDED_BASENAMES or rel.startswith("reviews/"):
            raise AuthorityError(f"authority-file-listed:{rel}")
        size = entry["size"]
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise AuthorityError(f"invalid-size:{rel}")
        expected_sha = validate_hex(entry["sha256"], f"artifact:{rel}")
        payload = safe_relative(root, rel).read_bytes()
        if len(payload) != size or not hmac.compare_digest(digest(payload), expected_sha):
            raise AuthorityError(f"artifact-mismatch:{rel}")
        tuples.append((rel, size, expected_sha))

    payload_digest = digest(json.dumps(tuples, separators=(",", ":"), ensure_ascii=True).encode())
    if not hmac.compare_digest(
        payload_digest,
        validate_hex(freeze.get("self_excluding_digest_sha256"), "payload-digest"),
    ):
        raise AuthorityError("payload-digest")
    if freeze_ref.get("artifact_count") != 38 or freeze_ref.get("payload_digest_sha256") != payload_digest:
        raise AuthorityError("authority-freeze-metadata")

    reviews = authority.get("reviews")
    if not isinstance(reviews, list) or len(reviews) != 2:
        raise AuthorityError("review-count")
    if {item.get("role") for item in reviews if isinstance(item, dict)} != {"parent", "adversarial"}:
        raise AuthorityError("review-roles")
    required_strings = [freeze_sha, repair_sha, contract_sha, payload_digest, "entries=38"]
    for review in reviews:
        if not isinstance(review, dict):
            raise AuthorityError("review-shape")
        review_path = safe_relative(root, review.get("path"))
        review_bytes = verify_file(review_path, review.get("sha256"), f"review:{review.get('role')}")
        if not review_bytes.startswith(b"GO\n"):
            raise AuthorityError(f"review-verdict:{review.get('role')}")
        text = review_bytes.decode("utf-8")
        if any(value not in text for value in required_strings):
            raise AuthorityError(f"review-binding:{review.get('role')}")

    return len(entries), len(reviews)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--authority", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        entries, reviews = verify(args.authority)
    except (AuthorityError, OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        print(f"PHASE7B_AUTHORITY_INVALID reason={exc}", file=sys.stderr)
        return 1
    print(f"PHASE7B_AUTHORITY_OK entries={entries} reviews={reviews}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
