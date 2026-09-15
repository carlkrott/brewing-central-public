#!/usr/bin/env bash
set -euo pipefail

ROOT="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd -P)"
export ISPINDEL_EVIDENCE_DIR="${ISPINDEL_EVIDENCE_DIR:-$(mktemp -d)}"
export ISPINDEL_SECRETS_DIR="${ISPINDEL_SECRETS_DIR:-$(mktemp -d)}"
export ISPINDEL_GID="${ISPINDEL_GID:-10001}"
cleanup() {
  if [[ "$ISPINDEL_EVIDENCE_DIR" == /tmp/* ]]; then rm -rf "$ISPINDEL_EVIDENCE_DIR"; fi
  if [[ "$ISPINDEL_SECRETS_DIR" == /tmp/* ]]; then rm -rf "$ISPINDEL_SECRETS_DIR"; fi
}
trap cleanup EXIT

touch "$ISPINDEL_SECRETS_DIR/ingest-tokens.json"
compose_json="$(cd "$ROOT" && docker compose config --format json)"
python3 -c 'import json,sys
cfg=json.loads(sys.stdin.read()); svc=cfg["services"]["ispindel-dashboard"]
ports=svc.get("ports",[])
assert len(ports)==1, ports
p=ports[0]
assert p["host_ip"]=="127.0.0.1" and int(p["published"])==18098 and int(p["target"])==8098, p
print("NETWORK_BOUNDARY_OK fastapi=127.0.0.1:18098")' <<<"$compose_json"
