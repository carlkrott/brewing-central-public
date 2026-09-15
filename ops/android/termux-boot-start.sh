#!/data/data/com.termux/files/usr/bin/bash
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
