#!/usr/bin/env bash
set -euo pipefail
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)
[[ "$PWD" == "$ROOT" ]] || {
  printf 'RELEASE_BUILD_FAILED reason=run from repository root: %s\n' "$ROOT" >&2
  exit 2
}
exec python3 "$ROOT/scripts/build_release.py" "$@"
