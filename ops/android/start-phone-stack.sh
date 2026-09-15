#!/data/data/com.termux/files/usr/bin/bash
set -euo pipefail
umask 077

export HOME=/data/data/com.termux/files/home
export PREFIX=/data/data/com.termux/files/usr
export PATH="$HOME/bin:$PREFIX/bin:/system/bin"

ROOT="$HOME/brewing-central"
ENV_FILE="$ROOT/config/phone.env"
if [[ ! -r "$ENV_FILE" ]]; then
  printf 'missing environment file: %s\n' "$ENV_FILE" >&2
  exit 2
fi
# shellcheck disable=SC1090
set -a
. "$ENV_FILE"
set +a

: "${PHONE_BIND_IP:?PHONE_BIND_IP is required}"
: "${TAILNET_FQDN:?TAILNET_FQDN is required}"
: "${SQLITE_PATH:?SQLITE_PATH is required}"
: "${BREW_SQLITE_PATH:?BREW_SQLITE_PATH is required}"
: "${ZEROCLAW_URL:?ZEROCLAW_URL is required}"
: "${ZEROCLAW_TOKEN_FILE:?ZEROCLAW_TOKEN_FILE is required}"
: "${ASSISTANT_STRUCTURED_MODEL_URL:?ASSISTANT_STRUCTURED_MODEL_URL is required}"

mkdir -p "$ROOT/data" "$ROOT/logs" "$ROOT/run"

exec 9>"$ROOT/run/start-phone-stack.lock"
if ! flock -w 90 9; then
  printf 'start_lock=timeout\n' >&2
  exit 1
fi

if command -v termux-wake-lock >/dev/null 2>&1; then
  termux-wake-lock || true
fi

is_running() {
  local pid_file=$1
  local expected=$2
  [[ -s "$pid_file" ]] || return 1
  local pid
  pid=$(<"$pid_file")
  [[ "$pid" =~ ^[0-9]+$ ]] || return 1
  kill -0 "$pid" 2>/dev/null || return 1
  [[ -r "/proc/$pid/cmdline" ]] || return 1
  local command_line
  command_line=$(tr '\0' ' ' <"/proc/$pid/cmdline")
  [[ "$command_line" == "$expected "* ]]
}

if ! is_running "$ROOT/run/zeroclaw.pid" "$HOME/bin/zeroclaw gateway"; then
  nohup "$HOME/bin/zeroclaw" gateway 9>&- >"$ROOT/logs/zeroclaw.log" 2>&1 </dev/null &
  printf '%s\n' "$!" >"$ROOT/run/zeroclaw.pid"
fi

if ! is_running "$ROOT/run/dashboard.pid" "$ROOT/venv/bin/python -m uvicorn app.main:app"; then
  cd "$ROOT/current"
  nohup "$ROOT/venv/bin/python" -m uvicorn app.main:app \
    --host "$PHONE_BIND_IP" \
    --port 8098 \
    --no-proxy-headers \
    --no-access-log \
    9>&- \
    >"$ROOT/logs/dashboard.log" 2>&1 </dev/null &
  printf '%s\n' "$!" >"$ROOT/run/dashboard.pid"
fi

"$ROOT/venv/bin/python" - <<'PY'
import os,time
from urllib.request import Request,urlopen
checks = [
    ("zeroclaw", "http://127.0.0.1:3100/health", None),
    ("dashboard", f"http://{os.environ['PHONE_BIND_IP']}:8098/health", os.environ["TAILNET_FQDN"]),
]
for name,url,host in checks:
    error = None
    for _ in range(30):
        try:
            headers = {"Host": host} if host else {}
            with urlopen(Request(url, headers=headers), timeout=2) as response:
                if response.status == 200:
                    print(f"{name}=ready")
                    break
        except Exception as exc:
            error = exc
            time.sleep(1)
    else:
        raise SystemExit(f"{name}=not-ready: {type(error).__name__}")
PY

if [[ "${CAMERA_OBSERVER_ENABLED:-false}" == "true" ]] && ! is_running "$ROOT/run/camera-observer.pid" "$ROOT/venv/bin/python -m app.camera_observer"; then
  cd "$ROOT/current"
  nohup "$ROOT/venv/bin/python" -m app.camera_observer \
    9>&- \
    >>"$ROOT/logs/camera-observer.log" 2>&1 </dev/null &
  printf '%s\n' "$!" >"$ROOT/run/camera-observer.pid"
fi

if [[ "${CAMERA_OBSERVER_ENABLED:-false}" == "true" ]]; then
  sleep 1
  if ! is_running "$ROOT/run/camera-observer.pid" "$ROOT/venv/bin/python -m app.camera_observer"; then
    printf 'camera_observer=not-ready\n' >&2
    exit 1
  fi
  printf 'camera_observer=ready\n'
fi
