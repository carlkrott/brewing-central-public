#!/usr/bin/env python3
"""Check dashboard/device health and optionally deliver one Telegram alert."""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

DEFAULT_URL = "http://127.0.0.1:8098"
DEFAULT_CONFIG = Path.home() / ".zeroclaw" / "config.toml"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", help="Dashboard base URL; overrides ISPINDEL_URL")
    parser.add_argument("--json", action="store_true", help="Print JSON and do not notify")
    parser.add_argument("--timeout", type=float, default=float(os.getenv("ISPIND_TIMEOUT", "10")))
    parser.add_argument("--low-battery", type=float, default=float(os.getenv("ISPIND_LOW_BATTERY", "3.4")))
    parser.add_argument("--stale-minutes", type=int, default=int(os.getenv("ISPIND_STALE_MINUTES", "45")))
    parser.add_argument("--config", type=Path, default=Path(os.getenv("ZEROCLAW_CONFIG", DEFAULT_CONFIG)))
    args = parser.parse_args(argv)
    args.url = (args.url or os.getenv("ISPINDEL_URL") or DEFAULT_URL).rstrip("/")
    if args.timeout <= 0 or args.stale_minutes <= 0:
        parser.error("--timeout and --stale-minutes must be positive")
    return args


def fetch(base_url: str, path: str, timeout: float) -> Any:
    request = urllib.request.Request(f"{base_url}{path}", method="GET")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)


def load_telegram(config: Path) -> tuple[str | None, str | None]:
    text = config.read_text() if config.exists() else ""
    token_match = re.search(r'bot_token\s*=\s*"([^"]+)"', text)
    user_match = re.search(r'allowed_users\s*=\s*\[\s*"([0-9]+)"', text)
    token = token_match.group(1) if token_match else os.getenv("TELEGRAM_BOT_TOKEN")
    user = user_match.group(1) if user_match else os.getenv("TELEGRAM_CHAT_ID")
    return token, user


def send_telegram(text: str, config: Path, timeout: float) -> bool:
    token, chat = load_telegram(config)
    if not token or not chat:
        print(text)
        return True
    data = json.dumps({"chat_id": chat, "text": text}).encode()
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        response.read()
    return True


def evaluate(args: argparse.Namespace) -> dict[str, Any]:
    status = fetch(args.url, "/api/status", args.timeout)
    devices = fetch(args.url, "/api/devices", args.timeout).get("devices", [])
    notes: list[str] = []
    suppressed_devices: list[str] = []
    now = datetime.now(timezone.utc)
    stale_cutoff = timedelta(minutes=args.stale_minutes)

    for device in devices:
        device_id = str(device.get("device_id", "unknown"))
        operating_intent = device.get("operating_intent")
        mode = device.get("operating_mode")
        if mode is None and isinstance(operating_intent, dict):
            mode = operating_intent.get("mode")
        if mode == "stored":
            suppressed_devices.append(device_id)
            continue
        last_seen = device.get("last_seen")
        if not last_seen:
            notes.append(f"{device_id}: no telemetry yet")
            continue
        observed = datetime.fromisoformat(str(last_seen).replace("Z", "+00:00"))
        if observed.tzinfo is None:
            observed = observed.replace(tzinfo=timezone.utc)
        age = now - observed.astimezone(timezone.utc)
        if age > stale_cutoff:
            notes.append(f"{device_id}: stale telemetry ({int(age.total_seconds() / 60)} min)")

        samples = fetch(
            args.url,
            f"/api/device/{urllib.parse.quote(device_id, safe='')}/samples?hours=1",
            args.timeout,
        ).get("samples", [])
        if not samples:
            continue
        latest = samples[-1]
        battery = latest.get("battery")
        if battery is not None and float(battery) < args.low_battery:
            notes.append(f"{device_id}: low battery {battery}")
        rssi = latest.get("rssi")
        if latest.get("ssid") and rssi is not None and int(rssi) < -85:
            notes.append(f"{device_id}: weak RSSI {rssi} on {latest['ssid']}")

    state = "unhealthy" if notes else "healthy"
    message = (
        "iSpindel checks:\n" + "\n".join(notes)
        if notes
        else f"iSpindel checks: all green (devices={status.get('devices', 0)}, stale={status.get('stale_devices', 0)}, stored={len(suppressed_devices)})"
    )
    return {
        "state": state,
        "url": args.url,
        "notes": notes,
        "suppressed_devices": suppressed_devices,
        "message": message,
    }


def main(argv: list[str] | None = None) -> int:
    try:
        args = parse_args(argv)
    except SystemExit as exc:
        return int(exc.code or 2)
    try:
        result = evaluate(args)
    except (OSError, ValueError, KeyError, json.JSONDecodeError, urllib.error.URLError) as exc:
        result = {"state": "transport_error", "url": args.url, "error": type(exc).__name__}
        print(json.dumps(result, sort_keys=True))
        return 2

    if args.json:
        print(json.dumps(result, sort_keys=True))
    else:
        try:
            send_telegram(result["message"], args.config, args.timeout)
        except (OSError, urllib.error.URLError):
            return 2
    return 1 if result["state"] == "unhealthy" else 0


if __name__ == "__main__":
    raise SystemExit(main())
