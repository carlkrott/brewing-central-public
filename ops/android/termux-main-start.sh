#!/data/data/com.termux/files/usr/bin/bash
set -uo pipefail
umask 077

export HOME=/data/data/com.termux/files/home
export PREFIX=/data/data/com.termux/files/usr
export PATH="$HOME/bin:$PREFIX/bin:/system/bin"

ROOT="$HOME/brewing-central"
LOG="$ROOT/logs/termux-boot.log"
mkdir -p "$ROOT/logs" "$ROOT/run"
exec >>"$LOG" 2>&1

printf '%s boot=start context=termux-main\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"

# PIDs cannot survive an Android reboot and may be reused by unrelated processes.
rm -f "$ROOT/run/zeroclaw.pid" "$ROOT/run/dashboard.pid" "$ROOT/run/camera-observer.pid" "$ROOT/run/phone-health-loop.pid"

if command -v termux-wake-lock >/dev/null 2>&1; then
  termux-wake-lock || true
fi

for attempt in $(seq 1 60); do
  if [[ -x "$ROOT/current/ops/android/start-phone-stack.sh" ]] && \
     "$ROOT/current/ops/android/start-phone-stack.sh"; then
    printf '%s boot=ready attempt=%s context=termux-main\n' \
      "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$attempt"
    # Start the phone-health evidence loop exactly once after stack readiness.
    # Sole scheduler owner: Termux:Boot -> termux-main-start.sh -> phone-health-loop.sh.
    if [[ -x "$ROOT/current/ops/android/phone-health-loop.sh" ]]; then
      nohup "$ROOT/current/ops/android/phone-health-loop.sh" 9>&- \
        >>"$ROOT/logs/phone-health-loop-launcher.log" 2>&1 </dev/null &
      printf '%s boot=evidence-loop-launched pid=%s\n' \
        "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$!"
    else
      printf '%s boot=evidence-loop-missing\n' \
        "$(date -u +%Y-%m-%dT%H:%M:%SZ)" >&2
    fi
    exit 0
  fi
  printf '%s boot=retry attempt=%s context=termux-main\n' \
    "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$attempt"
  sleep 10
done

printf '%s boot=failed attempts=60 context=termux-main\n' \
  "$(date -u +%Y-%m-%dT%H:%M:%SZ)" >&2
exit 1
