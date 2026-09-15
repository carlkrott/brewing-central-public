#!/usr/bin/env python3
"""Poll effective device freshness and deliver deduplicated state transitions."""
from __future__ import annotations

import argparse
import json
import subprocess
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ops_common import atomic_write_json, run_id, terminal_event, utc_now


def _utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp is naive")
    return parsed.astimezone(timezone.utc)


def classify_device(device: dict[str, Any], *, now: datetime) -> tuple[str, float, float]:
    interval = float(device["effective_interval_sec"])
    if interval <= 0:
        raise ValueError("effective device interval must be positive")
    last_seen = device.get("last_seen")
    if not last_seen:
        return "critical", float("inf"), interval
    age = max(0.0, (now - _utc(str(last_seen))).total_seconds())
    if age > interval * 6:
        state = "critical"
    elif age > interval * 3:
        state = "warning"
    elif age <= interval * 2:
        state = "healthy"
    else:
        state = "hold"
    return state, age, interval


def _fetch(url: str, timeout: float) -> dict[str, Any]:
    request = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        value = json.load(response)
    if not isinstance(value, dict):
        raise ValueError("endpoint response must be an object")
    return value


def _read_state(path: Path, now: datetime) -> dict[str, Any]:
    if not path.exists():
        return {"schema_version": "alert-state-v1", "started_at": now.isoformat(), "devices": {}}
    value = json.loads(path.read_text())
    if not isinstance(value, dict) or value.get("schema_version") != "alert-state-v1":
        raise ValueError("alert state schema is invalid")
    if not isinstance(value.get("devices"), dict) or _utc(str(value.get("started_at"))) is None:
        raise ValueError("alert state fields are invalid")
    return value


def _message(device_id: str, target: str, age: float, interval: float) -> str:
    if target == "healthy":
        return f"iSpindel {device_id} telemetry recovered"
    age_text = "never" if age == float("inf") else f"{round(age)}s"
    return f"iSpindel {device_id} telemetry {target}: age={age_text} effective_interval={round(interval)}s"


def apply_transitions(
    devices: list[dict[str, Any]],
    state: dict[str, Any],
    *,
    now: datetime,
    startup_grace: int,
    sender: Any,
    persist: Any = lambda: None,
) -> tuple[list[dict[str, str]], bool]:
    transitions: list[dict[str, str]] = []
    delivery_failed = False
    in_grace = (now - _utc(state["started_at"])).total_seconds() < startup_grace
    records: dict[str, Any] = state["devices"]
    for device in devices:
        device_id = str(device["device_id"])
        operating_intent = device.get("operating_intent")
        mode = device.get("operating_mode")
        if mode is None and isinstance(operating_intent, dict):
            mode = operating_intent.get("mode")
        if mode == "stored":
            transitions.append({"device_id": device_id, "state": "stored", "outcome": "suppressed_intent"})
            continue
        observed, age, interval = classify_device(device, now=now)
        previous = records.get(device_id, {"state": "healthy", "delivery": "sent"})
        target = previous["state"] if observed == "hold" else observed
        if in_grace and target in {"warning", "critical"}:
            transitions.append({"device_id": device_id, "state": target, "outcome": "startup_grace"})
            continue
        if target == previous.get("state") and previous.get("delivery") == "sent":
            transitions.append({"device_id": device_id, "state": target, "outcome": "suppressed"})
            continue
        record = {"state": target, "delivery": "pending", "updated_at": now.isoformat()}
        records[device_id] = record
        persist()
        message = _message(device_id, target, age, interval)
        delivered = sender(message)
        if delivered:
            record["delivery"] = "sent"
            persist()
            transitions.append({"device_id": device_id, "state": target, "outcome": "sent"})
        else:
            delivery_failed = True
            transitions.append({"device_id": device_id, "state": target, "outcome": "failed"})
    return transitions, delivery_failed


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:18098")
    parser.add_argument("--state", type=Path, default=Path("/var/lib/ispindel/evidence/alert-state.json"))
    parser.add_argument("--poll-state", type=Path, default=Path("/var/lib/ispindel/evidence/poll-state.json"))
    parser.add_argument("--send-alert", type=Path, default=Path("/usr/local/lib/ispindel/send-alert.sh"))
    parser.add_argument("--startup-grace", type=int, default=900)
    parser.add_argument("--timeout", type=float, default=10.0)
    args = parser.parse_args(argv)
    if args.timeout <= 0 or args.startup_grace < 0 or not args.url.startswith(("http://", "https://")):
        parser.error("timeout/url/startup-grace are invalid")
    return args


def main(argv: list[str] | None = None) -> int:
    started, run = time.monotonic(), run_id()
    args: argparse.Namespace | None = None
    try:
        args = parse_args(argv)
        now = datetime.now(timezone.utc)
        ready = _fetch(args.url.rstrip("/") + "/health/ready", args.timeout)
        if ready.get("status") != "ok":
            raise RuntimeError("application readiness is not ok")
        devices_value = _fetch(args.url.rstrip("/") + "/api/devices", args.timeout)
        devices = devices_value.get("devices")
        if not isinstance(devices, list):
            raise ValueError("devices response is invalid")
        state = _read_state(args.state, now)

        def sender(message: str) -> bool:
            result = subprocess.run([str(args.send_alert), message], check=False, timeout=args.timeout)
            return result.returncode == 0

        transitions, delivery_failed = apply_transitions(
            devices, state, now=now, startup_grace=args.startup_grace, sender=sender,
            persist=lambda: atomic_write_json(args.state, state),
        )
        atomic_write_json(args.state, state)
        attempted = any(item["outcome"] in {"sent", "failed"} for item in transitions)
        delivered = attempted and not delivery_failed
        atomic_write_json(args.poll_state, {
            "schema_version": "poll-state-v1",
            "observed_at": utc_now(),
            "poll_failed": False,
            "alert_attempted": attempted,
            "alert_delivered": delivered,
            "transitions": transitions,
        })
        for item in transitions:
            transition_event = {
                "sent": "alert_sent", "failed": "alert_failed",
                "suppressed": "alert_suppressed", "startup_grace": "alert_suppressed",
                "suppressed_intent": "alert_suppressed",
            }[item["outcome"]]
            terminal_event(
                transition_event, run, started,
                "failed" if item["outcome"] == "failed" else "ok",
                error_class="AlertDeliveryError" if item["outcome"] == "failed" else None,
                device_id=item["device_id"], state=item["state"],
            )
        terminal_event("poll_completed", run, started, "failed" if delivery_failed else "ok",
                       error_class="AlertDeliveryError" if delivery_failed else None,
                       device_count=len(devices), transition_count=len(transitions))
        return 1 if delivery_failed else 0
    except SystemExit as exc:
        terminal_event("poll_completed", run, started, "invalid", error_class="ConfigurationError")
        return int(exc.code or 2)
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError, urllib.error.URLError, subprocess.TimeoutExpired) as exc:
        try:
            if args is not None:
                atomic_write_json(args.poll_state, {
                    "schema_version": "poll-state-v1", "observed_at": utc_now(),
                    "poll_failed": True, "alert_attempted": False,
                    "alert_delivered": False, "error_class": type(exc).__name__,
                })
        except OSError:
            pass
        terminal_event("poll_completed", run, started, "failed", error_class=type(exc).__name__)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
