#!/usr/bin/env python3
"""Write phone battery + heartbeat health-evidence-v1 JSON for the dashboard.

Stdlib-only so it runs unmodified inside Termux (no third-party deps).

Behaviour:

* Battery percent is read from ``termux-battery-status`` when that command is
  available; otherwise from the first ``/sys/class/power_supply/BAT*`` entry.
* If neither source is available, the producer writes an explicit
  ``source=unavailable`` marker with a non-green ``percent`` that the existing
  parser in ``app/main.py`` rejects as ``parse_error``. The producer still
  writes heartbeat evidence and exits non-zero.
* Heartbeat probes loopback ZeroClaw and the local dashboard; ``poll_failed``
  is set when either probe fails, and ``state`` becomes ``critical``. A failed
  probe must not prevent evidence files from being written.
* All subprocess and HTTP timeouts are bounded; secrets, headers, and request
  bodies are never echoed.
"""
from __future__ import annotations

import json
import os
import secrets
import shlex
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Import the project's evidence primitives. The script lives at
# ``ops/android/write-phone-evidence.py`` and ``ops_common`` lives at
# ``scripts/ops_common.py``; both are two directories under the repo root.
_HERE = Path(__file__).resolve().parent
_SCRIPTS_DIR = _HERE.parent.parent / "scripts"
if _SCRIPTS_DIR.is_dir():
    sys.path.insert(0, str(_SCRIPTS_DIR))

try:
    from ops_common import SCHEMA_VERSION, atomic_write_json, run_id, terminal_event, utc_now
except ModuleNotFoundError:
    # The Android release intentionally ships only app/, ops/android/, and the
    # requirements files. Keep the stdlib-only producer self-contained there
    # while retaining the shared helper when run from a full source checkout.
    SCHEMA_VERSION = "health-evidence-v1"

    def utc_now() -> str:
        return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    def run_id() -> str:
        return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + secrets.token_hex(4)

    def atomic_write_json(path: Path, value: dict[str, Any], *, mode: int = 0o640) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.parent / f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp"
        payload = (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
        try:
            with os.fdopen(descriptor, "wb", closefd=True) as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, mode)
            os.replace(temporary, path)
            directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise

    def terminal_event(
        event: str,
        run: str,
        started: float,
        outcome: str,
        *,
        error_class: str | None = None,
        **fields: Any,
    ) -> None:
        record: dict[str, Any] = {
            "event": event,
            "run_id": run,
            "duration_ms": max(0, round((time.monotonic() - started) * 1000)),
            "outcome": outcome,
        }
        if error_class:
            record["error_class"] = error_class
        record.update(fields)
        print(json.dumps(record, sort_keys=True, separators=(",", ":")), flush=True)


DEFAULT_BATTERY_COMMAND = "termux-battery-status"
BATTERY_COMMAND_TIMEOUT_SECONDS = 3
HTTP_TIMEOUT_SECONDS = 2
UPPER_BOUND_INTERVAL_SECONDS = 3600
DEFAULT_INTERVAL_SECONDS = 300
# Default phone-side dashboard port; mirrors start-phone-stack.sh uvicorn.
DEFAULT_DASHBOARD_PORT = 8098
DEFAULT_DASHBOARD_SCHEME = "http"


# --- helpers --------------------------------------------------------------


def _bounded_subprocess_json(argv: list[str], *, timeout: float) -> dict[str, Any] | None:
    """Run argv with a hard timeout and parse stdout as JSON; return None on failure."""
    try:
        completed = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0 or not completed.stdout:
        return None
    try:
        parsed = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _read_sysfs_battery(root: Path) -> tuple[int | None, str | None]:
    """Return (percent, status) from the first sysfs battery directory."""
    if not root.is_dir():
        return None, None
    entries = sorted(root.glob("BAT*"))
    for entry in entries:
        capacity_path = entry / "capacity"
        status_path = entry / "status"
        if not capacity_path.is_file() or not status_path.is_file():
            continue
        try:
            percent = int(capacity_path.read_text().strip())
            status = status_path.read_text().strip().lower()
        except (OSError, ValueError):
            continue
        if 0 <= percent <= 100 and status:
            return percent, status
    return None, None


def _charging(status: str | None) -> bool | str:
    """Map raw battery status text to the schema's boolean-or-string union."""
    if not status:
        return False
    normalized = status.lower()
    if normalized in {"charging", "full"}:
        return True
    if normalized in {"discharging", "notcharging", "unknown"}:
        return False
    return status


def _battery_state(percent: int) -> tuple[str, str]:
    if percent <= 10:
        return "critical", "battery level is critical"
    if percent <= 20:
        return "warning", "battery level is low"
    return "ok", "battery evidence is current"


def _battery_from_command(command_string: str) -> dict[str, Any] | None:
    if not command_string:
        return None
    try:
        argv = shlex.split(command_string)
    except ValueError:
        return None
    if not argv:
        return None
    payload = _bounded_subprocess_json(
        argv,
        timeout=BATTERY_COMMAND_TIMEOUT_SECONDS,
    )
    if not payload:
        return None
    raw_percent: Any = payload.get("percentage")
    try:
        percent: int = int(raw_percent)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if not 0 <= percent <= 100:
        return None
    return {
        "percent": percent,
        "status": str(payload.get("status") or ""),
        "source": "termux-battery-status",
    }


def _battery_from_sysfs(sysfs_root: str | None) -> dict[str, Any] | None:
    """Try the project-provided sysfs root first, then the live Android path."""
    candidates: list[Path] = []
    if sysfs_root:
        candidates.append(Path(sysfs_root))
    candidates.append(Path("/sys/class/power_supply"))
    for candidate in candidates:
        percent, status = _read_sysfs_battery(candidate)
        if percent is not None:
            return {
                "percent": percent,
                "status": status or "",
                "source": "sysfs" if candidate == Path(sysfs_root or "/sys/class/power_supply") else "sysfs",
            }
    return None


# --- HTTP probe -----------------------------------------------------------


def _probe(url: str, host: str | None) -> bool:
    """Return True only if the loopback HTTP probe returns 2xx."""
    if not url:
        return False
    try:
        request = urllib.request.Request(url, method="GET")
        if host:
            request.add_header("Host", host)
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
            return 200 <= response.status < 300
    except (OSError, urllib.error.URLError, urllib.error.HTTPError, ValueError):
        return False


def _resolve_dashboard_url(env: dict[str, str]) -> str:
    """Return the explicit DASHBOARD_URL or derive it from PHONE_BIND_IP.

    Real-Termux:Boot environments do not pre-populate DASHBOARD_URL, so
    the producer derives the dashboard probe URL from PHONE_BIND_IP and a
    bounded PHONE_DASHBOARD_PORT (default 8098, matching uvicorn in
    start-phone-stack.sh). Explicit DASHBOARD_URL still wins.
    """
    explicit = (env.get("DASHBOARD_URL") or "").strip()
    if explicit:
        return explicit
    bind_ip = (env.get("PHONE_BIND_IP") or "").strip()
    if not bind_ip:
        return ""
    port_raw = (env.get("PHONE_DASHBOARD_PORT") or "").strip()
    if port_raw.isdigit() and 1 <= int(port_raw) <= 65535:
        port = int(port_raw)
    else:
        port = DEFAULT_DASHBOARD_PORT
    scheme = (env.get("PHONE_DASHBOARD_SCHEME") or DEFAULT_DASHBOARD_SCHEME).strip()
    return f"{scheme}://{bind_ip}:{port}/health"


def _resolve_dashboard_host_header(env: dict[str, str]) -> str | None:
    """Return the explicit DASHBOARD_HOST_HEADER or derive it from TAILNET_FQDN."""
    explicit = (env.get("DASHBOARD_HOST_HEADER") or "").strip()
    if explicit:
        return explicit
    fqdn = (env.get("TAILNET_FQDN") or "").strip()
    return fqdn or None


def _resolve_zeroclaw_health_url(env: dict[str, str]) -> str:
    """Return an explicit health URL or derive ``/health`` from ZEROCLAW_URL."""
    explicit = (env.get("ZEROCLAW_HEALTH_URL") or "").strip()
    if explicit:
        return explicit
    base = (env.get("ZEROCLAW_URL") or "").strip()
    if not base:
        return ""
    parsed = urllib.parse.urlsplit(base)
    if parsed.path not in {"", "/"}:
        return base
    return urllib.parse.urlunsplit(
        (parsed.scheme, parsed.netloc, "/health", parsed.query, parsed.fragment)
    )


# --- evidence writers -----------------------------------------------------


def build_battery_evidence(env: dict[str, str]) -> dict[str, Any]:
    """Compose the battery evidence record. Never invents a usable percent."""
    battery = _battery_from_command(env.get("PHONE_BATTERY_COMMAND", DEFAULT_BATTERY_COMMAND))
    if battery is None:
        battery = _battery_from_sysfs(env.get("PHONE_BATTERY_SYSFS_ROOT"))
    observed = utc_now()
    if battery is None:
        # Explicit non-green marker: percent outside 0..100 forces the existing
        # parser in app/main.py to return parse_error.
        return {
            "schema_version": SCHEMA_VERSION,
            "kind": "battery",
            "observed_at": observed,
            "state": "critical",
            "detail": "battery source is unavailable",
            "percent": -1,
            "charging": False,
            "source": "unavailable",
        }
    percent = battery["percent"]
    state, detail = _battery_state(percent)
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "battery",
        "observed_at": observed,
        "state": state,
        "detail": detail,
        "percent": percent,
        "charging": _charging(battery.get("status")),
        "source": battery["source"],
    }


def build_heartbeat_evidence(env: dict[str, str]) -> dict[str, Any]:
    """Compose the heartbeat evidence record from a bounded phone-stack probe."""
    zeroclaw_ok = _probe(_resolve_zeroclaw_health_url(env), None)
    dashboard_url = _resolve_dashboard_url(env)
    dashboard_host = _resolve_dashboard_host_header(env)
    dashboard_ok = _probe(dashboard_url, dashboard_host)
    poll_failed = not (zeroclaw_ok and dashboard_ok)
    state = "critical" if poll_failed else "ok"
    if not zeroclaw_ok and not dashboard_ok:
        detail = "phone-stack probe failed: zeroclaw and dashboard unreachable"
    elif not zeroclaw_ok:
        detail = "phone-stack probe failed: zeroclaw unreachable"
    elif not dashboard_ok:
        detail = "phone-stack probe failed: dashboard unreachable"
    else:
        detail = "phone-stack probe ok"
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "heartbeat",
        "observed_at": utc_now(),
        "state": state,
        "detail": detail,
        "poll_failed": poll_failed,
        # Phone-side heartbeat has no alert transport; both are always false.
        "alert_attempted": False,
        "alert_delivered": False,
    }


# --- paths + main ---------------------------------------------------------


def _output_paths(env: dict[str, str]) -> tuple[Path, Path]:
    battery = Path(env.get("BATTERY_EVIDENCE_PATH", "/var/lib/ispindel/evidence/battery-state.json"))
    heartbeat = Path(env.get("HEARTBEAT_EVIDENCE_PATH", "/var/lib/ispindel/evidence/heartbeat.json"))
    return battery, heartbeat


def write_evidence(env: dict[str, str] | None = None) -> int:
    env = env or dict(os.environ)
    started, run = time.monotonic(), run_id()
    battery_path, heartbeat_path = _output_paths(env)
    battery_failed = False
    try:
        battery_value = build_battery_evidence(env)
        if battery_value["source"] == "unavailable":
            battery_failed = True
        atomic_write_json(battery_path, battery_value)
        terminal_event(
            "phone_battery_written",
            run,
            started,
            "failed" if battery_failed else "ok",
            path=str(battery_path),
            source=battery_value["source"],
        )
    except (OSError, ValueError) as exc:
        battery_failed = True
        terminal_event(
            "phone_battery_written",
            run,
            started,
            "failed",
            error_class=type(exc).__name__,
        )

    try:
        heartbeat_value = build_heartbeat_evidence(env)
        atomic_write_json(heartbeat_path, heartbeat_value)
        terminal_event(
            "phone_heartbeat_written",
            run,
            started,
            "ok",
            path=str(heartbeat_path),
            state=heartbeat_value["state"],
        )
    except (OSError, ValueError) as exc:
        terminal_event(
            "phone_heartbeat_written",
            run,
            started,
            "failed",
            error_class=type(exc).__name__,
        )
        return 1

    return 1 if battery_failed else 0


def main(argv: list[str] | None = None) -> int:
    return write_evidence()


if __name__ == "__main__":
    raise SystemExit(main())
