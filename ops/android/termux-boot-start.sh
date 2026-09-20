#!/data/data/com.termux/files/usr/bin/bash
# termux-boot-start.sh — Termux:Boot entry point.
#
# Wave 1 contract:
#   * Dispatch the phone control-plane sshd loop INDEPENDENTLY before
#     any app dispatch. A missing or broken $ROOT/current must NOT block
#     the control-plane dispatch: the control plane is the recovery
#     surface for an unhealthy app tree.
#   * After control-plane dispatch is attempted, dispatch the existing
#     termux-main-start.sh path (retained app dispatch semantics).
#   * Idempotent enough that a single boot may safely run this script
#     more than once during a recovery cycle; each dispatch logs a clear
#     tag.
set -uo pipefail
umask 077

export HOME=/data/data/com.termux/files/home
export PREFIX=/data/data/com.termux/files/usr
export PATH="$HOME/bin:$PREFIX/bin:/system/bin"

ROOT="$HOME/brewing-central"
LOG="$ROOT/logs/termux-boot-dispatch.log"
mkdir -p "$ROOT/logs"
exec >>"$LOG" 2>&1

printf '%s dispatch=start\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"

# ---- control-plane dispatch (independent of $ROOT/current) -------------
# Deployed control plane lives at $ROOT/control/phone-sshd-loop.sh. If it
# is missing, we log and continue — the app dispatch below is best-effort
# recovery, not a prerequisite for the control plane.
CONTROL_LOOP="$ROOT/control/phone-sshd-loop.sh"
if [[ -x "$CONTROL_LOOP" ]]; then
  for attempt in $(seq 1 12); do
    output=$(
      /system/bin/am startservice --user 0 \
        -n com.termux/com.termux.app.RunCommandService \
        -a com.termux.RUN_COMMAND \
        --es com.termux.RUN_COMMAND_PATH "$CONTROL_LOOP" \
        --es com.termux.RUN_COMMAND_WORKDIR "$HOME" \
        --ez com.termux.RUN_COMMAND_BACKGROUND 'true' \
        --es com.termux.RUN_COMMAND_LABEL 'Brewing Central control plane' \
        2>&1
    )
    rc=$?
    printf '%s\n' "$output"
    if [[ $rc -eq 0 && "$output" != *"Error:"* && "$output" != *"Exception"* ]]; then
      printf '%s dispatch=control-accepted attempt=%s\n' \
        "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$attempt"
      break
    fi
    printf '%s dispatch=control-retry attempt=%s rc=%s\n' \
      "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$attempt" "$rc"
    sleep 5
  done
else
  printf '%s dispatch=control-missing path=%s\n' \
    "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$CONTROL_LOOP"
fi

# ---- app dispatch (existing semantics, retained) -----------------------
for attempt in $(seq 1 60); do
  output=$(
    /system/bin/am startservice --user 0 \
      -n com.termux/com.termux.app.RunCommandService \
      -a com.termux.RUN_COMMAND \
      --es com.termux.RUN_COMMAND_PATH "$ROOT/current/ops/android/termux-main-start.sh" \
      --es com.termux.RUN_COMMAND_WORKDIR "$ROOT/current" \
      --ez com.termux.RUN_COMMAND_BACKGROUND 'true' \
      --es com.termux.RUN_COMMAND_LABEL 'Brewing Central boot recovery' \
      2>&1
  )
  rc=$?
  printf '%s\n' "$output"
  if [[ $rc -eq 0 && "$output" != *"Error:"* && "$output" != *"Exception"* ]]; then
    printf '%s dispatch=accepted attempt=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$attempt"
    exit 0
  fi
  printf '%s dispatch=retry attempt=%s rc=%s\n' \
    "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$attempt" "$rc"
  sleep 10
done

printf '%s dispatch=failed attempts=60\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" >&2
exit 1
