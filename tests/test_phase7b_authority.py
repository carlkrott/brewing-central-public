"""Closed tests for the Phase 7B repaired-authority verifier."""
from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "verify_phase7b_authority", ROOT / "scripts" / "verify-phase7b-authority.py"
)
assert SPEC and SPEC.loader
VERIFIER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(VERIFIER)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_json(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, indent=2) + "\n")


def _fixture(tmp_path: Path) -> Path:
    evidence = tmp_path / "evidence"
    payload = evidence / "payload"
    reviews = evidence / "reviews"
    payload.mkdir(parents=True)
    reviews.mkdir()
    contract = tmp_path / "contract.md"
    contract.write_text("immutable contract\n")

    entries = []
    for index in range(38):
        path = payload / f"artifact-{index:02}.txt"
        path.write_text(f"payload {index}\n")
        entries.append(
            {
                "path": path.relative_to(evidence).as_posix(),
                "size": path.stat().st_size,
                "sha256": _sha(path),
            }
        )
    tuples = [(item["path"], item["size"], item["sha256"]) for item in entries]
    payload_digest = hashlib.sha256(
        json.dumps(tuples, separators=(",", ":"), ensure_ascii=True).encode()
    ).hexdigest()
    freeze = evidence / "PHASE7B-FREEZE-RECONSTRUCTED.json"
    _write_json(
        freeze,
        {
            "schema_version": "phase06a2-r4c-phase7b-reconstructed-freeze-v1",
            "self_excluding": True,
            "artifact_count": 38,
            "self_excluding_digest_sha256": payload_digest,
            "artifacts": entries,
        },
    )
    repair = evidence / "PHASE7B-REPAIR-RECEIPT.json"
    _write_json(repair, {"repair": "fixture"})
    bindings = "\n".join(
        [_sha(freeze), _sha(repair), _sha(contract), payload_digest, "entries=38"]
    )
    for name in ("reconstructed-parent.txt", "reconstructed-adversarial.txt"):
        (reviews / name).write_text(f"GO\n{bindings}\n")
    authority = evidence / "PHASE7B-REPAIR-AUTHORITY.json"
    _write_json(
        authority,
        {
            "schema_version": "phase7b-repair-authority-v1",
            "receipt_kind": "phase7b_repair_authority",
            "verdict": "GO",
            "evidence_root": str(evidence),
            "contract": {"path": str(contract), "sha256": _sha(contract)},
            "repair_receipt": {"path": repair.name, "sha256": _sha(repair)},
            "reconstructed_freeze": {
                "path": freeze.name,
                "sha256": _sha(freeze),
                "artifact_count": 38,
                "payload_digest_sha256": payload_digest,
            },
            "reviews": [
                {
                    "role": "parent",
                    "path": "reviews/reconstructed-parent.txt",
                    "sha256": _sha(reviews / "reconstructed-parent.txt"),
                },
                {
                    "role": "adversarial",
                    "path": "reviews/reconstructed-adversarial.txt",
                    "sha256": _sha(reviews / "reconstructed-adversarial.txt"),
                },
            ],
        },
    )
    return authority


def test_phase7b_authority_accepts_exact_closed_fixture(tmp_path: Path):
    assert VERIFIER.verify(_fixture(tmp_path)) == (38, 2)


def test_phase7b_authority_rejects_payload_drift(tmp_path: Path):
    authority = _fixture(tmp_path)
    (authority.parent / "payload" / "artifact-00.txt").write_text("changed\n")
    try:
        VERIFIER.verify(authority)
    except VERIFIER.AuthorityError as exc:
        assert "artifact-mismatch" in str(exc)
    else:
        raise AssertionError("payload drift was accepted")


def test_phase7b_authority_rejects_non_byte_zero_review(tmp_path: Path):
    authority = _fixture(tmp_path)
    body = json.loads(authority.read_text())
    review = authority.parent / body["reviews"][0]["path"]
    review.write_text("# Review\nGO\n")
    body["reviews"][0]["sha256"] = _sha(review)
    _write_json(authority, body)
    try:
        VERIFIER.verify(authority)
    except VERIFIER.AuthorityError as exc:
        assert "review-verdict" in str(exc)
    else:
        raise AssertionError("non-byte-zero review was accepted")
