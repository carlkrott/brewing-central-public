#!/usr/bin/env bash
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)
VENV=${ISPINDEL_TEST_VENV:-"$ROOT/.venv"}
PYTHON="$VENV/bin/python"
MODE=${1:-all}

contract() {
  python3 - "$ROOT" <<'PY'
import json, sys
print(json.dumps({
    "source_root": sys.argv[1],
    "runtime_image_env": "PHASE05C_RUNTIME_IMAGE",
    "runtime_source_env": "PHASE05C_RUNTIME_SOURCE_ROOT",
    "manual_export_required": False,
}, sort_keys=True))
PY
}

bootstrap() {
  if [[ ! -x "$PYTHON" ]]; then
    python3 -m venv --system-site-packages "$VENV"
  fi
  if compgen -G "$ROOT/wheelhouse/*.whl" >/dev/null; then
    "$PYTHON" -m pip install --disable-pip-version-check --no-index \
      --find-links "$ROOT/wheelhouse" -r "$ROOT/requirements-test.lock" >/dev/null
  else
    "$PYTHON" - <<'PY'
import fastapi, httpx, playwright, pytest  # noqa: F401
print("LOCK_INSTALL_DEFERRED: frozen wheelhouse is not present; using compatible host packages", flush=True)
PY
  fi
}

unit() {
  bootstrap
  cd "$ROOT"
  "$PYTHON" -m pytest -q --ignore=tests/test_docker_runtime.py
}

browser() {
  bootstrap
  cd "$ROOT"
  "$PYTHON" -m pytest -q tests/browser
}

resolve_runtime_image() {
  if [[ -n "${PHASE05C_RUNTIME_IMAGE:-}" ]]; then
    docker image inspect "$PHASE05C_RUNTIME_IMAGE" --format '{{.Id}}'
    return
  fi
  local td image ref source_hash image_hash lock_hash sums_hash
  td=$(mktemp -d)
  trap 'rm -rf "$td"' RETURN
  mkdir -p "$td/evidence" "$td/secrets"
  lock_hash=$(sha256sum "$ROOT/requirements.lock" | cut -d' ' -f1)
  sums_hash=$(sha256sum "$ROOT/wheels/SHA256SUMS" | cut -d' ' -f1)
  [[ "$lock_hash" == "f4c988304571562aba1c3df7560d9ad8777e5ea86543fe97ebe7955bddd2b834" ]]
  [[ "$sums_hash" == "e0edca24acf443627f01f245acdfe108c401113443aaf0dfeb21951ed1bda981" ]]
  (cd "$ROOT/wheels" && sha256sum -c SHA256SUMS >/dev/null)
  ref=$(cd "$ROOT" && ISPINDEL_EVIDENCE_DIR="$td/evidence" ISPINDEL_SECRETS_DIR="$td/secrets" ISPINDEL_GID=10001 docker compose config --images | head -n1)
  image=$(docker image inspect "$ref" --format '{{.Id}}' 2>/dev/null || true)
  source_hash=$(sha256sum "$ROOT/app/main.py" | cut -d' ' -f1)
  if [[ "$image" =~ ^sha256:[0-9a-f]{64}$ ]]; then
    image_hash=$(docker run --rm --network none --entrypoint python "$image" -c "import hashlib,pathlib; print(hashlib.sha256(pathlib.Path('/app/app/main.py').read_bytes()).hexdigest())")
  else
    image_hash=""
  fi
  if [[ "$source_hash" != "$image_hash" ]]; then
    printf 'Building source-identical candidate from the frozen wheelhouse (network=none).\n' >&2
    (cd "$ROOT" && \
      ISPINDEL_EVIDENCE_DIR="$td/evidence" \
      ISPINDEL_SECRETS_DIR="$td/secrets" \
      ISPINDEL_GID=10001 \
      docker compose build --pull=false >&2)
    image=$(docker image inspect "$ref" --format '{{.Id}}')
    image_hash=$(docker run --rm --network none --entrypoint python "$image" -c "import hashlib,pathlib; print(hashlib.sha256(pathlib.Path('/app/app/main.py').read_bytes()).hexdigest())")
  fi
  [[ "$image" =~ ^sha256:[0-9a-f]{64}$ ]] || {
    printf 'Offline build did not produce an immutable image ID.\n' >&2
    return 2
  }
  [[ "$source_hash" == "$image_hash" ]] || {
    printf 'Built image app hash does not match source.\n' >&2
    return 2
  }
  printf '%s\n' "$image"
}

runtime() {
  bootstrap
  local image
  image=$(resolve_runtime_image)
  cd "$ROOT"
  PHASE05C_RUNTIME_IMAGE="$image" \
  PHASE05C_RUNTIME_SOURCE_ROOT="$ROOT" \
    "$PYTHON" -m pytest -q tests/test_docker_runtime.py
}

verify_offline() {
  [[ -f "$ROOT/descriptors/RELEASE.json" ]] || {
    printf 'Verified release descriptor is not present.\n' >&2
    return 2
  }
  "$ROOT/scripts/verify-release.py" --manifest "$ROOT/descriptors/RELEASE.json"
}

case "$MODE" in
  contract) contract ;;
  unit) unit ;;
  browser) browser ;;
  runtime) runtime ;;
  verify-offline) verify_offline ;;
  all) unit; runtime ;;
  *) printf 'usage: %s {contract|unit|browser|runtime|verify-offline|all}\n' "$0" >&2; exit 2 ;;
esac
