#!/usr/bin/env python3
"""Write canonical heartbeat evidence from the last poll outcome."""
from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timezone
from pathlib import Path

from ops_common import SCHEMA_VERSION, atomic_write_json, run_id, terminal_event, utc_now


def _timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("timestamp missing")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp is naive")
    return parsed.astimezone(timezone.utc)


def build_heartbeat(poll: dict[str, object], *, now: datetime, poll_ttl: int) -> dict[str, object]:
    if poll.get("schema_version") != "poll-state-v1":
        raise ValueError("poll state schema is invalid")
    observed = _timestamp(poll.get("observed_at"))
    stale = (now - observed).total_seconds() > poll_ttl
    poll_failed = bool(poll.get("poll_failed")) or stale
    attempted = bool(poll.get("alert_attempted"))
    delivered = bool(poll.get("alert_delivered"))
    state = "critical" if poll_failed or (attempted and not delivered) else "ok"
    detail = "poll state is stale" if stale else ("poll failed" if poll_failed else "poll completed")
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "heartbeat",
        "observed_at": now.isoformat().replace("+00:00", "Z"),
        "state": state,
        "detail": detail,
        "poll_failed": poll_failed,
        "alert_attempted": attempted,
        "alert_delivered": delivered,
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--poll-state", type=Path, default=Path("/var/lib/ispindel/evidence/poll-state.json"))
    parser.add_argument("--output", type=Path, default=Path("/var/lib/ispindel/evidence/heartbeat.json"))
    parser.add_argument("--poll-ttl", type=int, default=600)
    args = parser.parse_args(argv)
    if args.poll_ttl <= 0:
        parser.error("--poll-ttl must be positive")
    return args


def main(argv: list[str] | None = None) -> int:
    started, run = time.monotonic(), run_id()
    try:
        args = parse_args(argv)
        poll = json.loads(args.poll_state.read_text())
        if not isinstance(poll, dict):
            raise ValueError("poll state must be an object")
        value = build_heartbeat(poll, now=datetime.now(timezone.utc), poll_ttl=args.poll_ttl)
        atomic_write_json(args.output, value)
        terminal_event("heartbeat_written", run, started, "ok", state=value["state"])
        return 0
    except SystemExit as exc:
        terminal_event("heartbeat_written", run, started, "invalid", error_class="ConfigurationError")
        return int(exc.code or 2)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        terminal_event("heartbeat_written", run, started, "failed", error_class=type(exc).__name__)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
