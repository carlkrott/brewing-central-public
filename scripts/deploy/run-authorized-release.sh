#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR=$(CDPATH='' cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)

usage() {
  printf '%s\n' "usage: run-authorized-release.sh --confirm-release-id RELEASE_ID --execute"
}

release_id=
execute=0
while (($#)); do
  case "$1" in
    --confirm-release-id) release_id=${2-}; shift 2 ;;
    --execute) execute=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) printf 'unknown argument: %s\n' "$1" >&2; usage >&2; exit 2 ;;
  esac
done

[[ $execute -eq 1 ]] || { printf '%s\n' 'authorized runner refused: missing --execute' >&2; exit 2; }
[[ $release_id =~ ^[0-9]{8}T[0-9]{6}Z-[0-9a-f]{12}$ ]] || { printf '%s\n' 'invalid release ID' >&2; exit 2; }
: "${ISPINDEL_DEPLOY_REMOTE:?required}"
: "${ISPINDEL_REMOTE_RELEASE_ROOT:?required}"
: "${ISPINDEL_OFFHOST:?required}"
: "${ISPINDEL_DEPLOY_EVIDENCE_DIR:?required}"
: "${ISPINDEL_BROWSER_COMMAND:?required}"
: "${ISPINDEL_SECURITY_COMMAND:?required}"
: "${ISPINDEL_ALERT_COMMAND:?required}"
: "${ISPINDEL_LAN_URL:?required}"
: "${ISPINDEL_TAILNET_URL:?required}"
: "${ISPINDEL_TEST_INGEST_COMMAND:?required}"
: "${ISPINDEL_TEST_INGEST_POLICY:?required}"
: "${ISPINDEL_EXPECTED_MISSING_CONTRACT_VERSION:?required}"

common=(--remote "$ISPINDEL_DEPLOY_REMOTE" --remote-root "${ISPINDEL_REMOTE_ROOT:-/opt/ispindel-dashboard}" --evidence-dir "$ISPINDEL_DEPLOY_EVIDENCE_DIR" --confirm-release-id "$release_id" --execute)
"$SCRIPT_DIR/01-backup-predecessor.sh" "${common[@]}" --offhost "$ISPINDEL_OFFHOST" \
  --allow-expected-missing \
  --expected-missing-contract-version "$ISPINDEL_EXPECTED_MISSING_CONTRACT_VERSION"
stage01="$ISPINDEL_DEPLOY_EVIDENCE_DIR/$release_id/01-backup-predecessor.json"
stage01_sha256=$(sha256sum "$stage01" | cut -d' ' -f1)
backup_manifest=$(python3 -c 'import json,pathlib,sys; p=json.load(open(sys.argv[1],encoding="utf-8"))["backup_manifest"]; assert isinstance(p,str) and pathlib.PurePosixPath(p).is_absolute(); print(p)' "$stage01")
"$SCRIPT_DIR/02-rehearse-release.sh" "${common[@]}" --remote-release-root "$ISPINDEL_REMOTE_RELEASE_ROOT" --backup-manifest "$backup_manifest"
stage02="$ISPINDEL_DEPLOY_EVIDENCE_DIR/$release_id/02-rehearse-release.json"
stage02_sha256=$(sha256sum "$stage02" | cut -d' ' -f1)
remote_root=${ISPINDEL_REMOTE_ROOT:-/opt/ispindel-dashboard}
stage03_backup_root=${ISPINDEL_STAGE03_BACKUP_ROOT:-$remote_root/evidence/$release_id/backup}
[[ "$stage03_backup_root" = /* && "$stage03_backup_root" != *$'\n'* && "$stage03_backup_root" != *$'\r'* && "$stage03_backup_root" != *..* ]]

run_with_rollback() {
  local stage=$1
  shift
  local rc
  set +e
  "$stage" "$@"
  rc=$?
  set -e
  if [[ $rc -ne 0 ]]; then
    local rollback_rc
    set +e
    "$SCRIPT_DIR/05-rollback-application.sh" "${common[@]}" --stage01-receipt "$stage01" --stage01-receipt-sha256 "$stage01_sha256"
    rollback_rc=$?
    set -e
    if [[ $rollback_rc -ne 0 ]]; then
      printf '%s\n' "authorized release rollback failed after stage rc=$rc rollback rc=$rollback_rc" >&2
      exit 20
    fi
    printf '%s\n' "authorized release rolled back after stage failure rc=$rc" >&2
    exit "$rc"
  fi
}

run_with_rollback "$SCRIPT_DIR/03-promote-release.sh" "${common[@]}" --stage01-receipt "$stage01" --stage01-receipt-sha256 "$stage01_sha256" --stage02-receipt "$stage02" --stage02-receipt-sha256 "$stage02_sha256" --remote-release-root "$ISPINDEL_REMOTE_RELEASE_ROOT" --backup-root "$stage03_backup_root" --lan-url "$ISPINDEL_LAN_URL" --tailnet-url "$ISPINDEL_TAILNET_URL"

stage04_args=("${common[@]}" --browser-command "$ISPINDEL_BROWSER_COMMAND" --security-command "$ISPINDEL_SECURITY_COMMAND" --alert-command "$ISPINDEL_ALERT_COMMAND" --test-ingest-command "$ISPINDEL_TEST_INGEST_COMMAND" --test-ingest-policy "$ISPINDEL_TEST_INGEST_POLICY" --lan-url "$ISPINDEL_LAN_URL" --tailnet-url "$ISPINDEL_TAILNET_URL")
set +e
"$SCRIPT_DIR/04-validate-live.sh" "${stage04_args[@]}"
stage04_rc=$?
set -e
if [[ $stage04_rc -ne 0 ]]; then
  retry_delay=${ISPINDEL_DEPLOY_RETRY_DELAY_SECONDS:-5}
  [[ $retry_delay =~ ^[0-9]+$ ]] || { printf '%s\n' 'invalid ISPINDEL_DEPLOY_RETRY_DELAY_SECONDS' >&2; exit 2; }
  sleep "$retry_delay"
  set +e
  "$SCRIPT_DIR/04-validate-live.sh" "${stage04_args[@]}"
  stage04_rc=$?
  set -e
fi

if [[ $stage04_rc -ne 0 ]]; then
  remote_root=${ISPINDEL_REMOTE_ROOT:-/opt/ispindel-dashboard}
  direct_probe="import urllib.request; r=urllib.request.urlopen('http://127.0.0.1:8098/health/ready',timeout=5); raise SystemExit(0 if r.status==200 else 1)"
  printf -v remote_command 'cd %q && docker exec ispindel-dashboard python3 -c %q' "$remote_root" "$direct_probe"
  set +e
  ssh -o BatchMode=yes -- "$ISPINDEL_DEPLOY_REMOTE" -- "$remote_command"
  direct_rc=$?
  set -e
  if [[ $direct_rc -ne 0 && $direct_rc -ne 255 ]]; then
    "$SCRIPT_DIR/05-rollback-application.sh" "${common[@]}" --stage01-receipt "$stage01" --stage01-receipt-sha256 "$stage01_sha256"
    printf '%s\n' 'authorized release rolled back after repeated validation failure and failed direct container readiness' >&2
    exit 20
  fi
  if [[ $direct_rc -eq 255 ]]; then
    printf '%s\n' 'direct container readiness was inconclusive because SSH transport failed; automatic rollback refused' >&2
  else
    printf '%s\n' 'validation failed repeatedly but direct container readiness passed; automatic application rollback refused' >&2
  fi
  exit "$stage04_rc"
fi
printf 'AUTHORIZED_RELEASE_OK release_id=%s\n' "$release_id"
