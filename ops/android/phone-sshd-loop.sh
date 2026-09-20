#!/data/data/com.termux/files/usr/bin/bash
# phone-sshd-loop.sh — Termux openssh control-plane supervisor.
#
# Contract (Wave 1 phone control plane):
#   * Stable control path outside the active app tree (deployed at install time;
#     never symlinked through an app release to keep supervisor alive across app
#     upgrades).
#   * Single-instance via flock on a stable lock file in $ROOT/control/sshd.
#   * Bounded Tailnet wait: poll the configured bind IP up to a fixed
#     bound before announcing readiness. Never busy-loops indefinitely.
#   * Runs `sshd -t -f <config>` BEFORE launch; refuses to launch on
#     non-zero exit so a bad config can never expose a half-started listener.
#   * Command-bound existing-daemon detection: any prior sshd pid we touch
#     must match by /proc/<pid>/cmdline prefix so a recycled PID never gets
#     signalled by mistake.
#   * Idempotent: re-running while a healthy daemon is up exits 0 without
#     starting a second sshd.
#   * Exactly one sshd process under our PID file at any time.
set -uo pipefail
umask 077

export HOME="${HOME:-/data/data/com.termux/files/home}"
export PREFIX="${PREFIX:-/data/data/com.termux/files/usr}"
export PATH="$HOME/bin:$PREFIX/bin:/system/bin"

# Stable control path outside current release.
ROOT="${BREWING_CENTRAL_ROOT:-$HOME/brewing-central}"
CONTROL_SSHD_DIR="$ROOT/control/sshd"
RUN_DIR="$ROOT/run"
LOG_DIR="$ROOT/logs"
PID_FILE="$RUN_DIR/phone-sshd-loop.pid"
START_FILE="$RUN_DIR/phone-sshd-loop.start"
LOCK_FILE="$CONTROL_SSHD_DIR/phone-sshd-loop.lock"
LOG_FILE="$LOG_DIR/phone-sshd-loop.log"

CONFIG_FILE="${PHONE_SSHD_CONFIG:-$CONTROL_SSHD_DIR/sshd_config}"
EFFECTIVE_CONFIG="$CONTROL_SSHD_DIR/sshd_config.effective"
HOST_KEY="${PHONE_SSHD_HOST_KEY:-$CONTROL_SSHD_DIR/host_ed25519_key}"
SSHD_BIN="${PHONE_SSHD_BIN:-$PREFIX/bin/sshd}"
BIND_IP="${PHONE_SSHD_BIND_IP:-auto}"
SSHD_PORT="${PHONE_SSHD_PORT:-8022}"
PYTHON_BIN="${PHONE_SSHD_PYTHON:-$PREFIX/bin/python3}"
TAILNET_PROBE_HOST="${PHONE_SSHD_TAILNET_PROBE_HOST:-auto}"

mkdir -p "$CONTROL_SSHD_DIR" "$RUN_DIR" "$LOG_DIR"
: >>"$LOG_FILE"
exec >>"$LOG_FILE" 2>&1

log() {
  printf '%s sshd-loop=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"
}

log "start config=$CONFIG_FILE bind=$BIND_IP port=$SSHD_PORT"

# ---- single-instance lock -----------------------------------------------
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  log "already-running lock=$LOCK_FILE"
  exit 0
fi

# ---- config validation before any launch -------------------------------
if [[ ! -f "$CONFIG_FILE" ]]; then
  log "config-missing path=$CONFIG_FILE"
  exit 1
fi
if [[ ! -x "$SSHD_BIN" ]]; then
  log "sshd-missing bin=$SSHD_BIN"
  exit 1
fi
if [[ ! -x "$PYTHON_BIN" ]]; then
  log "python-missing bin=$PYTHON_BIN"
  exit 1
fi
# Tailnet resolution must happen before the effective config is rendered so
# the bind address is validated before sshd -t or launch.
TAILNET_WAIT_BOUND="${PHONE_SSHD_TAILNET_WAIT_MAX:-12}"
if ! [[ "$TAILNET_WAIT_BOUND" =~ ^(0|[1-9][0-9]?)$ ]] \
    || (( TAILNET_WAIT_BOUND > 60 )); then
  log "invalid-tailnet-wait-bound value=$TAILNET_WAIT_BOUND"
  exit 1
fi
RESOLVED_BIND_IP=""
resolve_tailnet_ipv4() {
  local probe_host="$TAILNET_PROBE_HOST"
  if [[ -z "$probe_host" || "$probe_host" == "auto" ]]; then
    probe_host="$(printf '%s.%s.%s.%s' 100 64 0 1)"
  fi
  "$PYTHON_BIN" - "$probe_host" <<'PY'
import ipaddress
import socket
import sys

try:
    target = ipaddress.ip_address(sys.argv[1])
    if target.version != 4:
        raise ValueError("probe host must be IPv4")
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.settimeout(2)
        probe.connect((str(target), 9))
        candidate = ipaddress.ip_address(probe.getsockname()[0])
    packed = candidate.packed
    if packed[0] == 100 and 64 <= packed[1] <= 127:
        print(candidate)
except (OSError, ValueError, IndexError):
    pass
PY
}
waited=0
while :; do
  if [[ "$BIND_IP" == "auto" || -z "$BIND_IP" ]]; then
    # Android denies netlink interface enumeration to ordinary app UIDs. Use
    # a UDP route probe and accept only the returned RFC-6598 source address.
    RESOLVED_BIND_IP="$(resolve_tailnet_ipv4 2>/dev/null || true)"
  elif [[ "$BIND_IP" =~ ^([0-9]{1,3})\.([0-9]{1,3})\.([0-9]{1,3})\.([0-9]{1,3})$ ]]; then
    RESOLVED_BIND_IP="$BIND_IP"
  fi
  if [[ -n "$RESOLVED_BIND_IP" ]]; then
    break
  fi
  if (( waited >= TAILNET_WAIT_BOUND )); then
    break
  fi
  sleep 1
  waited=$((waited + 1))
done
if [[ -z "$RESOLVED_BIND_IP" ]]; then
  log "tailnet-wait-exceeded bound=$TAILNET_WAIT_BOUND mode=${BIND_IP:-auto}"
  exit 1
fi
BIND_IP="$RESOLVED_BIND_IP"
tailnet_ipv4_valid() {
  local value="$1" a b c d octet
  [[ "$value" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]] || return 1
  IFS=. read -r a b c d <<<"$value"
  for octet in "$a" "$b" "$c" "$d"; do
    [[ "$octet" =~ ^(0|[1-9][0-9]{0,2})$ ]] || return 1
    (( 10#$octet <= 255 )) || return 1
  done
  (( 10#$a == 100 && 10#$b >= 64 && 10#$b <= 127 ))
}
if ! tailnet_ipv4_valid "$BIND_IP"; then
  log "invalid-tailnet-bind bind=$BIND_IP"
  exit 1
fi
log "tailnet-ready waited=$waited bind=$BIND_IP"

if ! [[ "$SSHD_PORT" =~ ^[1-9][0-9]{0,4}$ ]] || (( SSHD_PORT > 65535 )); then
  log "invalid-port port=$SSHD_PORT"
  exit 1
fi
listener_ready() {
  "$PYTHON_BIN" - "$BIND_IP" "$SSHD_PORT" <<'PY'
import ipaddress
import socket
import sys

try:
    address = ipaddress.ip_address(sys.argv[1])
    port = int(sys.argv[2])
    if address.version != 4 or not (address.packed[0] == 100 and 64 <= address.packed[1] <= 127):
        raise ValueError("non-tailnet bind")
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(2)
        raise SystemExit(probe.connect_ex((str(address), port)) != 0)
except (OSError, ValueError, IndexError):
    raise SystemExit(1)
PY
}

render_effective_config() {
  local temp="${EFFECTIVE_CONFIG}.tmp.$$"
  rm -f "$temp"
  if ! awk '
    /^[[:space:]]*#/ { print; next }
    {
      directive = $1
      sub(/=.*/, "", directive)
      directive = tolower(directive)
      if (directive == "include" || directive == "listenaddress" || directive == "port") {
        next
      }
      print
    }
  ' "$CONFIG_FILE" >"$temp"; then
    rm -f "$temp"
    return 1
  fi
  if ! printf '\nListenAddress %s\nPort %s\n' "$BIND_IP" "$SSHD_PORT" >>"$temp"; then
    rm -f "$temp"
    return 1
  fi
  if ! chmod 0600 "$temp" || ! mv -f "$temp" "$EFFECTIVE_CONFIG"; then
    rm -f "$temp"
    return 1
  fi
}

if ! render_effective_config; then
  log "effective-config-render-failed source=$CONFIG_FILE"
  exit 1
fi
log "effective-config-ready path=$EFFECTIVE_CONFIG"

if ! "$SSHD_BIN" -t -f "$EFFECTIVE_CONFIG" 2>>"$LOG_FILE"; then
  log "config-test-failed config=$EFFECTIVE_CONFIG bind=$BIND_IP"
  exit 1
fi
log "config-test-ok"

sshd_cmdline_matches() {
  local pid="$1"
  local -a argv=()
  [[ "$pid" =~ ^[0-9]+$ ]] || return 1
  [[ -r "/proc/$pid/cmdline" ]] || return 1
  mapfile -d '' -t argv <"/proc/$pid/cmdline" 2>/dev/null || true
  [[ "${argv[0]:-}" == "$SSHD_BIN" \
    && "${argv[1]:-}" == "-D" \
    && "${argv[2]:-}" == "-f" \
    && "${argv[3]:-}" == "$EFFECTIVE_CONFIG" ]]
}

process_start_time() {
  local pid="$1"
  awk '{print $22}' "/proc/$pid/stat" 2>/dev/null
}

process_identity_matches() {
  local pid="$1" start_time="$2"
  [[ -n "$start_time" ]] || return 1
  [[ "$(process_start_time "$pid")" == "$start_time" ]] \
    && sshd_cmdline_matches "$pid"
}

# ---- idempotent existing-daemon detection ------------------------------
# Walk every candidate pidfile and verify cmdline before trusting it.
# Exclude our own PID (this loop script) and require the cmdline to match
# the exact sshd binary, so a recycled PID or a foreign process can never
# be confused with our sshd.
existing_pid=""
existing_pid_file=""
existing_start_file=""
existing_start_time=""
for candidate in "$PID_FILE" "$RUN_DIR"/phone-sshd.*.pid; do
  [[ -f "$candidate" ]] || continue
  pid="$(tr -d '[:space:]' <"$candidate" 2>/dev/null || true)"
  [[ "$pid" =~ ^[0-9]+$ ]] || { rm -f "$candidate"; continue; }
  [[ "$pid" == "$$" ]] && { rm -f "$candidate"; continue; }
  candidate_start_file="${candidate%.pid}.start"
  candidate_start_time="$(tr -d '[:space:]' <"$candidate_start_file" 2>/dev/null || true)"
  if kill -0 "$pid" 2>/dev/null \
      && process_identity_matches "$pid" "$candidate_start_time"; then
    existing_pid="$pid"
    existing_pid_file="$candidate"
    existing_start_file="$candidate_start_file"
    existing_start_time="$candidate_start_time"
    break
  fi
  rm -f "$candidate" "$candidate_start_file"
done

if [[ -n "$existing_pid" ]]; then
  if listener_ready; then
    log "already-active pid=$existing_pid"
    exit 0
  fi
  log "stale-daemon-no-listener pid=$existing_pid"
  if process_identity_matches "$existing_pid" "$existing_start_time"; then
    kill -TERM "$existing_pid" 2>/dev/null || true
    for _ in $(seq 1 10); do
      process_identity_matches "$existing_pid" "$existing_start_time" || break
      sleep 1
    done
    if process_identity_matches "$existing_pid" "$existing_start_time"; then
      kill -KILL "$existing_pid" 2>/dev/null || true
    fi
  fi
  rm -f "$existing_pid_file" "$existing_start_file"
elif listener_ready; then
  # A listener without a matching, start-pinned supervisor PID is ambiguous;
  # never launch a second daemon into an endpoint we cannot own.
  log "listener-unowned bind=$BIND_IP port=$SSHD_PORT"
  exit 1
fi
rm -f "$START_FILE"

# ---- launch exactly one sshd -------------------------------------------
LOG_PREFIX="$LOG_DIR/phone-sshd-loop.launch"
launch_pid=""
launch_start_time=""
cleanup_started_sshd() {
  local rc
  if (( $# > 0 )); then
    rc="$1"
  else
    rc="$?"
  fi
  # launch_pid is a direct child of this shell; Linux will not reuse that PID
  # until this parent reaps the child, so signals remain child-bound.
  if [[ "${launch_pid:-}" =~ ^[0-9]+$ ]] \
      && kill -0 "$launch_pid" 2>/dev/null; then
    kill -TERM "$launch_pid" 2>/dev/null || true
    for _ in $(seq 1 10); do
      kill -0 "$launch_pid" 2>/dev/null || break
      sleep 1
    done
    if kill -0 "$launch_pid" 2>/dev/null; then
      kill -KILL "$launch_pid" 2>/dev/null || true
    fi
  fi
  if [[ "${launch_pid:-}" =~ ^[0-9]+$ ]]; then
    wait "$launch_pid" 2>/dev/null || true
  fi
  rm -f "$PID_FILE"
  rm -f "$START_FILE"
  if [[ "${launch_pid:-}" =~ ^[0-9]+$ ]]; then
    rm -f "$RUN_DIR/phone-sshd.$launch_pid.pid"
    rm -f "$RUN_DIR/phone-sshd.$launch_pid.start"
  fi
  log "stopped launch_pid=${launch_pid:-none} rc=$rc"
  return "$rc"
}

trap cleanup_started_sshd EXIT
trap 'exit 0' TERM INT
while :; do
  launch_pid=""
  launch_start_time=""
  "$SSHD_BIN" -D -f "$EFFECTIVE_CONFIG" \
                -E "$LOG_PREFIX.log" 9>&- >>"$LOG_PREFIX.stdout.log" 2>&1 & launch_pid=$!
  launch_start_time="$(process_start_time "$launch_pid")"
  if [[ -z "$launch_start_time" ]]; then
    log "sshd-start-time-missing pid=$launch_pid"
    exit 1
  fi
  printf '%s\n' "$launch_pid" >"$PID_FILE"
  printf '%s\n' "$launch_start_time" >"$START_FILE"
  log "spawned launch_pid=$launch_pid"

  # Wait briefly for the listener to come up; bounded so we never hang.
  # Readiness uses a same-UID TCP connect because Android app UIDs cannot read
  # /proc/net/tcp or use netlink.
  up=0
  for _ in $(seq 1 20); do
    if listener_ready; then
      up=1
      break
    fi
    sleep 1
  done
  if (( up != 1 )); then
    log "listener-not-ready bind=$BIND_IP port=$SSHD_PORT"
    exit 1
  fi

  # The foreground -D child is the exact sshd process owned by this supervisor.
  if ! kill -0 "$launch_pid" 2>/dev/null \
      || ! sshd_cmdline_matches "$launch_pid"; then
    log "sshd-pid-not-found bind=$BIND_IP port=$SSHD_PORT"
    exit 1
  fi
  DISCOVERED_SSHD_PID="$launch_pid"
  printf '%s\n' "$DISCOVERED_SSHD_PID" >"$RUN_DIR/phone-sshd.$DISCOVERED_SSHD_PID.pid"
  printf '%s\n' "$launch_start_time" >"$RUN_DIR/phone-sshd.$DISCOVERED_SSHD_PID.start"
  log "ready pid=$launch_pid sshd_pid=${DISCOVERED_SSHD_PID:-unknown} port=$SSHD_PORT bind=$BIND_IP"

  # Keep the lock and PID state tied to the actual sshd child. A child exit
  # releases both before a bounded restart delay.
  child_rc=0
  wait "$launch_pid" || child_rc=$?
  log "sshd-exited pid=$launch_pid rc=$child_rc restart_delay=5"
  cleanup_started_sshd "$child_rc"
  launch_pid=""
  launch_start_time=""
  sleep 5 9>&-
done
