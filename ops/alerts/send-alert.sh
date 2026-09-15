#!/usr/bin/env bash
set -euo pipefail

started_ms="$(( $(date +%s%N) / 1000000 ))"
run_id="$(date -u +%Y%m%dT%H%M%SZ)-$(python3 -c 'import secrets; print(secrets.token_hex(4))')"
message="${1:-}"
event() {
  local event_name="$1" outcome="$2" error_class="${3:-}"
  local now_ms duration
  now_ms="$(( $(date +%s%N) / 1000000 ))"
  duration="$((now_ms - started_ms))"
  if [[ -n "$error_class" ]]; then
    printf '{"duration_ms":%s,"error_class":"%s","event":"%s","outcome":"%s","run_id":"%s"}\n' "$duration" "$error_class" "$event_name" "$outcome" "$run_id"
  else
    printf '{"duration_ms":%s,"event":"%s","outcome":"%s","run_id":"%s"}\n' "$duration" "$event_name" "$outcome" "$run_id"
  fi
}

if [[ -z "$message" ]]; then
  event alert_failed invalid ConfigurationError
  exit 2
fi
if [[ "${ISPINDEL_DRY_RUN:-0}" == "1" ]]; then
  event alert_sent ok
  exit 0
fi
if [[ -z "${ISPINDEL_ALERT_URL:-}" ]]; then
  event alert_failed invalid ConfigurationError
  exit 2
fi

tmp="$(mktemp)"
chmod 600 "$tmp"
cleanup() { rm -f "$tmp"; }
trap cleanup EXIT
error_class="TransportError"
for attempt in 1 2; do
  : > "$tmp"
  if status="$(curl --silent --show-error --output "$tmp" --write-out '%{http_code}' \
      --max-time "${ISPINDEL_ALERT_TIMEOUT:-10}" --request POST \
      --header 'Content-Type: application/json' \
      --data "$(python3 -c 'import json,sys; print(json.dumps({"message":sys.argv[1]}))' "$message")" \
      "$ISPINDEL_ALERT_URL")"; then
    error_class="AcknowledgementError"
    if [[ "$status" =~ ^2[0-9][0-9]$ ]] && python3 -c 'import json,sys; v=json.load(open(sys.argv[1])); raise SystemExit(0 if isinstance(v,dict) and v.get("ok") is True else 1)' "$tmp"; then
      event alert_sent ok
      exit 0
    fi
  fi
  [[ "$attempt" == "2" ]] || sleep 1
done
event alert_failed failed "$error_class"
exit 1
