#!/data/data/com.termux/files/usr/bin/bash
# phone-health-loop.sh — single scheduler owner of phone health-evidence.
#
# Contract (W6 phone-health evidence slice):
#   - Sole scheduler is Termux:Boot -> termux-main-start.sh -> this loop.
#   - No Termux systemd unit, no cron daemon, no termux-job-scheduler.
#   - Bounded positive interval (default 300s, configurable by
#     PHONE_EVIDENCE_INTERVAL_SECONDS, upper bound 3600s).
#   - Single-instance via flock; command-bound PID file so stop can match
#     exactly this loop and clean its PID.
#   - Clean exit on TERM / INT.
#   - Sources $ROOT/config/phone.env with export semantics before
#     invoking the producer so BATTERY_EVIDENCE_PATH, HEARTBEAT_EVIDENCE_PATH,
#     ZEROCLAW_URL, and dashboard settings reach the producer even though
#     Termux:Boot does not pre-populate them. Secrets from phone.env are
#     never echoed into argv, stdout, or log lines.
#   - Writes go to the existing scripts/ops_common.py atomic writer; never
#     exposes tokens or request bodies.
set -uo pipefail
umask 077

export HOME=${HOME:-/data/data/com.termux/files/home}
export PREFIX=${PREFIX:-/data/data/com.termux/files/usr}
export PATH="$HOME/bin:$PREFIX/bin:/system/bin"

ROOT="${BREWING_CENTRAL_ROOT:-$HOME/brewing-central}"
RUN_DIR="$ROOT/run"
LOG_DIR="$ROOT/logs"
PID_FILE="$RUN_DIR/phone-health-loop.pid"
LOCK_FILE="$RUN_DIR/phone-health-loop.lock"
LOG_FILE="$LOG_DIR/phone-health-loop.log"
EVIDENCE_DIR="$ROOT/data"

PRODUCER_REL="ops/android/write-phone-evidence.py"
PRODUCER="$ROOT/current/${PRODUCER_REL}"
PYTHON_BIN="${PHONE_EVIDENCE_PYTHON:-$ROOT/venv/bin/python}"

# Source the deployed phone.env with export semantics BEFORE the producer is
# invoked. The real Termux:Boot environment does not pre-populate
# BATTERY_EVIDENCE_PATH, HEARTBEAT_EVIDENCE_PATH, ZEROCLAW_URL, or dashboard
# settings; sourcing the deployed config makes those variables available to
# the producer without leaking secrets into argv/log lines. We validate that
# the file exists and is mode 0600 before sourcing and refuse to echo any
# of its values.
PHONE_ENV="${PHONE_ENV_FILE:-$ROOT/config/phone.env}"

# ---- phone.env source ----------------------------------------------------
# Source once before interval calculation so the configured interval is used
# for the loop's initial scheduling. A missing file remains a fail-closed
# skip condition handled in the loop below; source errors are remembered and
# explicitly skip the producer for that iteration.
PHONE_ENV_SOURCE_FAILED=0
if [[ -f "$PHONE_ENV" ]]; then
  # shellcheck disable=SC1090
  set -a
  if ! . "$PHONE_ENV" >/dev/null 2>&1; then
    PHONE_ENV_SOURCE_FAILED=1
  fi
  set +a
else
  PHONE_ENV_SOURCE_FAILED=1
fi

# ---- interval bound ------------------------------------------------------
DEFAULT_INTERVAL=300
UPPER_BOUND_INTERVAL=3600
LOWER_BOUND_INTERVAL=1
if (( PHONE_ENV_SOURCE_FAILED != 0 )); then
  RAW_INTERVAL="$DEFAULT_INTERVAL"
else
  RAW_INTERVAL="${PHONE_EVIDENCE_INTERVAL_SECONDS:-$DEFAULT_INTERVAL}"
fi
if ! [[ "$RAW_INTERVAL" =~ ^[0-9]+$ ]] || (( RAW_INTERVAL < LOWER_BOUND_INTERVAL )); then
  printf '%s interval=invalid raw=%s default=%s\n' \
    "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$RAW_INTERVAL" "$DEFAULT_INTERVAL" >&2
  INTERVAL=$DEFAULT_INTERVAL
elif (( RAW_INTERVAL > UPPER_BOUND_INTERVAL )); then
  printf '%s interval=capped raw=%s cap=%s\n' \
    "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$RAW_INTERVAL" "$UPPER_BOUND_INTERVAL" >&2
  INTERVAL=$UPPER_BOUND_INTERVAL
else
  INTERVAL=$RAW_INTERVAL
fi

mkdir -p "$RUN_DIR" "$LOG_DIR" "$EVIDENCE_DIR"
: >>"$LOG_FILE"
exec >>"$LOG_FILE" 2>&1

printf '%s loop=start interval=%s producer=%s\n' \
  "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$INTERVAL" "$PRODUCER"

# ---- single-instance flock ----------------------------------------------
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  printf '%s loop=already-running lock=%s\n' \
    "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$LOCK_FILE" >&2
  exit 0
fi

# ---- command-bound PID --------------------------------------------------
# The cmdline starts with the absolute path of this script, so the stop
# script can command-match exactly this process tree.
printf '%s\n' "$$" >"$PID_FILE"
trap 'rm -f "$PID_FILE"; printf "%s loop=stopped\n" "$(date -u +%Y-%m-%dT%H:%M:%SZ)"' EXIT
trap 'exit 0' TERM INT

# ---- main loop -----------------------------------------------------------
while true; do
  if [[ ! -x "$PRODUCER" ]]; then
    printf '%s loop=producer-missing producer=%s\n' \
      "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$PRODUCER" >&2
    sleep "$INTERVAL" 9>&-
    continue
  fi
  if [[ ! -x "$PYTHON_BIN" ]]; then
    PYTHON_BIN="$(command -v python3 || command -v python)"
  fi
  if [[ -z "${PYTHON_BIN:-}" ]]; then
    printf '%s loop=python-missing\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" >&2
    sleep "$INTERVAL" 9>&-
    continue
  fi
  # Source the deployed phone.env every iteration: PHONE_BIND_IP and other
  # Tailnet-derived values may rotate across reboots; re-sourcing keeps the
  # producer's view fresh without leaking secrets (we never echo any value).
  # Fail-closed: if phone.env is missing or fails to source, keep the loop
  # alive but skip the run rather than invoking the producer with partial or
  # stale variables.
  if [[ ! -f "$PHONE_ENV" ]]; then
    printf '%s loop=phone-env-missing path=%s\n' \
      "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$PHONE_ENV" >&2
    sleep "$INTERVAL" 9>&-
    continue
  fi
  # shellcheck disable=SC1090
  set -a
  if ! . "$PHONE_ENV" >/dev/null 2>&1; then
    set +a
    printf '%s loop=phone-env-source-failed path=%s\n' \
      "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$PHONE_ENV" >&2
    sleep "$INTERVAL" 9>&-
    continue
  fi
  set +a
  "$PYTHON_BIN" "$PRODUCER" 9>&- || true
  # Child processes must not inherit the singleton lock. Otherwise an
  # interrupted loop leaves its sleep process holding FD 9 until the full
  # interval expires, and an immediate restart exits as already-running.
  sleep "$INTERVAL" 9>&- &
  wait $!
done
