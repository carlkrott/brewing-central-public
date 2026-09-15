#!/usr/bin/env python3
"""Independently verify one exact iSpindel backup manifest and generation."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

from backup_common import BackupError, validate_generation


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description="Verify one exact manifest path; no latest/glob selection.")
    value.add_argument("--manifest", required=True, type=Path)
    value.add_argument("--json", action="store_true")
    return value


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        manifest = validate_generation(args.manifest)
    except BackupError as exc:
        print(json.dumps({"event": "backup_verification_failed", "error": str(exc)[:1000]}), file=sys.stderr)
        return 1
    file_block = manifest.get("file")
    if not isinstance(file_block, dict):
        print(json.dumps({"event": "backup_verification_failed", "error": "manifest file block invalid"}), file=sys.stderr)
        return 1
    result = {
        "event": "backup_verified",
        "result": "VERIFIED",
        "run_id": manifest["run_id"],
        "basename": manifest["basename"],
        "sha256": file_block["sha256"],
    }
    print(json.dumps(manifest if args.json else result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
