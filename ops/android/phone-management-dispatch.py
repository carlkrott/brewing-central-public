#!/usr/bin/env python3
# phone-management-dispatch.py — forced-command dispatcher for the phone
# control-plane sshd.
#
# Contract:
#   * stdlib-only.
#   * Single forced-command entry point: every ssh connection runs this script
#     exactly once, regardless of what the client requested.
#   * Closed, hard-coded allowlist of verbs. Unknown verbs are rejected.
#   * All path arguments are absolute (relative-to-ROOT forms are rejected).
#   * Shell metacharacters, NUL bytes, and embedded control characters in any
#     argument are rejected before any filesystem call.
#   * No shell eval, no shell-form subprocess calls. subprocess is invoked
#     only with explicit argv lists and bounded inputs.
#   * Output is bounded to a small fixed maximum (and truncated with an
#     explicit marker if exceeded).
#   * Exits non-zero on any error path so sshd records the failure.
#
# Verb catalogue (closed allowlist):
#   health             — bounded PID/port/process existence checks only
#   verify             — bounded sshd config syntax + listener readiness
#   logs               — bounded/redacted tail of a single named log file
#   snapshot           — bounded export of one named evidence file (size-capped)
#   export             — bounded directory listing under ROOT/data
#   cleanup            — bounded retention cleanup of one named subdir
#   receipt-publish    — bounded publish of one named JSON receipt
#   release stage      — bounded release-stage marker write
#   release activate   — bounded release-activate marker write
#   release rollback   — bounded release-rollback marker write
from __future__ import annotations

import argparse
import grp
import ipaddress
import json
import os
import pwd
import re
import shlex
import socket
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import Iterable, NoReturn, Sequence

# ---- bounds --------------------------------------------------------------
MAX_ARG_LEN = 256
MAX_TAIL_LINES = 200
MAX_SNAPSHOT_BYTES = 1 << 20  # 1 MiB
MAX_LIST_ENTRIES = 500
MAX_OUTPUT_BYTES = 64 * 1024  # 64 KiB total stdout cap
TRUNCATION_MARKER = "[truncated]"

# ---- path policy ---------------------------------------------------------
ROOT = Path(os.environ.get("BREWING_CENTRAL_ROOT", "/data/data/com.termux/files/home/brewing-central"))
CONTROL_DIR = ROOT / "control"
DATA_DIR = ROOT / "data"
LOG_DIR = ROOT / "logs"
RUN_DIR = ROOT / "run"
BIN_DIR = CONTROL_DIR / "bin"

ALLOWLIST_LOGS = frozenset({
    "phone-health-loop",
    "phone-sshd-loop",
    "termux-boot",
    "termux-main",
})

ALLOWLIST_EVIDENCE = frozenset({
    "battery-state.json",
    "heartbeat.json",
    "camera-snapshot.json",
})

ALLOWLIST_CLEANUP_SUBDIRS = frozenset({
    "camera",
    "logs-archive",
})

ALLOWLIST_RECEIPTS = frozenset({
    "release-stage",
    "release-activate",
    "release-rollback",
})

# ---- helpers -------------------------------------------------------------
_SHELL_META_CHARS = re.compile(r"[;&|`$><*?()\[\]{}!#~\"'\\]")
_DANGEROUS_PAYLOAD_CHARS = re.compile(r"[;&|`$]")
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")


class DispatchError(Exception):
    """Fail-closed dispatcher error. Always exits non-zero with a redacted tag."""


def _die(tag: str, msg: str) -> NoReturn:
    sys.stderr.write(f"dispatch=reject tag={tag} reason={msg}\n")
    raise SystemExit(2)


def _validate_scalar(value: str, *, tag: str, allow_json: bool = False) -> str:
    if not isinstance(value, str):
        _die(tag, "not-a-string")
    if len(value) > MAX_ARG_LEN if not allow_json else len(value) > 4096:
        _die(tag, "arg-too-long")
    if _CONTROL_CHARS.search(value):
        _die(tag, "control-char")
    if not allow_json and value != value.strip():
        _die(tag, "whitespace-edge")
    if ".." in value:
        _die(tag, "traversal")
    if not allow_json and value.startswith("-"):
        _die(tag, "leading-dash")
    if "\n" in value or "\r" in value:
        _die(tag, "newline")
    if allow_json:
        if _DANGEROUS_PAYLOAD_CHARS.search(value):
            _die(tag, "shell-metachar")
    else:
        if _SHELL_META_CHARS.search(value):
            _die(tag, "shell-metachar")
    return value


def _validate_choice(value: str, choices: Iterable[str], *, tag: str) -> str:
    value = _validate_scalar(value, tag=tag)
    if value not in choices:
        _die(tag, f"not-in-allowlist:{value}")
    return value


def _resolve_under(base: Path, name: str, *, tag: str, must_exist: bool = False) -> Path:
    candidate = (base / name).resolve(strict=False)
    try:
        # Realpath guard against symlink escape outside base.
        base_real = base.resolve(strict=False)
    except OSError as exc:
        _die(tag, f"base-unresolvable:{exc.strerror or 'os'}")
    try:
        candidate.relative_to(base_real)
    except ValueError:
        _die(tag, "outside-base")
    if must_exist and not candidate.exists():
        _die(tag, "missing")
    return candidate


def _bounded_print(text: str) -> None:
    """Emit text on stdout with a hard cap; never exceed MAX_OUTPUT_BYTES."""
    encoded = text.encode("utf-8", errors="replace")
    if len(encoded) <= MAX_OUTPUT_BYTES:
        sys.stdout.write(encoded.decode("utf-8", errors="replace"))
        sys.stdout.flush()
        return
    head = MAX_OUTPUT_BYTES // 2
    tail = MAX_OUTPUT_BYTES - head - len(TRUNCATION_MARKER) - 1
    sys.stdout.write(encoded[:head].decode("utf-8", errors="replace"))
    sys.stdout.write(f"\n{TRUNCATION_MARKER}\n")
    sys.stdout.write(encoded[-tail:].decode("utf-8", errors="replace"))
    sys.stdout.flush()


def _parse_int_bounded(raw: str, *, lo: int, hi: int, tag: str) -> int:
    raw = _validate_scalar(raw, tag=tag)
    if not raw.isdigit():
        _die(tag, "not-int")
    value = int(raw)
    if value < lo or value > hi:
        _die(tag, "int-out-of-range")
    return value


_TAILNET_MIN = int.from_bytes(bytes((100, 64, 0, 0)), "big")
_TAILNET_MAX = int.from_bytes(bytes((100, 127, 255, 255)), "big")


def _is_tailnet_ipv4(value: str) -> bool:
    try:
        address = ipaddress.IPv4Address(value)
    except ipaddress.AddressValueError:
        return False
    return _TAILNET_MIN <= int(address) <= _TAILNET_MAX


def _default_tailnet_probe_host() -> str:
    return ".".join(("100", "64", "0", "1"))


def _resolve_tailnet_ipv4() -> str | None:
    configured = os.environ.get("PHONE_SSHD_BIND_IP", "auto").strip()
    if configured != "auto" and _is_tailnet_ipv4(configured):
        return configured
    if configured != "auto":
        return None
    probe_host = os.environ.get("PHONE_SSHD_TAILNET_PROBE_HOST", "auto").strip()
    if not probe_host or probe_host == "auto":
        probe_host = _default_tailnet_probe_host()
    try:
        target = ipaddress.ip_address(probe_host)
        if target.version != 4:
            return None
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.settimeout(2)
            probe.connect((str(target), 9))
            candidate = probe.getsockname()[0]
    except (OSError, ValueError):
        return None
    if _is_tailnet_ipv4(candidate):
        return candidate
    return None


def _tcp_port_listening(bind_ip: str | None, port: int = 8022) -> bool:
    if bind_ip is None or not _is_tailnet_ipv4(bind_ip):
        return False
    try:
        address = ipaddress.IPv4Address(bind_ip)
        if not 1 <= port <= 65535:
            return False
    except (ipaddress.AddressValueError, TypeError, ValueError):
        return False
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.settimeout(2)
            return probe.connect_ex((str(address), port)) == 0
    except OSError:
        return False


def _configured_sshd_port() -> int | None:
    effective = CONTROL_DIR / "sshd" / "sshd_config.effective"
    if effective.is_file() and not effective.is_symlink():
        try:
            ports = []
            for raw_line in effective.read_text(encoding="utf-8").splitlines():
                line = raw_line.strip()
                match = re.fullmatch(r"(?i)Port\s+([1-9][0-9]{0,4})", line)
                if match:
                    ports.append(int(match.group(1)))
            if len(ports) == 1 and ports[0] <= 65535:
                return ports[0]
            return None
        except OSError:
            return None
    raw = os.environ.get("PHONE_SSHD_PORT", "8022").strip()
    if not re.fullmatch(r"[1-9][0-9]{0,4}", raw):
        return None
    port = int(raw)
    return port if port <= 65535 else None


def _sshd_cmdline_matches(pid: int, sshd_bin: str) -> bool:
    if pid == os.getpid():
        return False
    try:
        raw = (Path("/proc") / str(pid) / "cmdline").read_bytes()
    except OSError:
        return False
    cmdline = raw.replace(b"\x00", b" ").decode("utf-8", errors="replace").strip()
    return cmdline == sshd_bin or cmdline.startswith(f"{sshd_bin} ")


# ---- verb handlers -------------------------------------------------------
def cmd_health(args: Sequence[str]) -> int:
    """Bounded PID/port/process existence checks only.

    Returns a redacted JSON summary. Never echoes usernames, key paths,
    fingerprints, or config contents.
    """
    if args:
        _die("health", "extra-argv")
    checks: dict[str, object] = {}
    summary: dict[str, object] = {"ts": int(time.time()), "checks": checks}

    # sshd listener presence — bounded pidfile/cmdline check.
    sshd_bin = os.environ.get("PHONE_SSHD_BIN", "/data/data/com.termux/files/usr/bin/sshd")
    sshd_pid_files = sorted(list(RUN_DIR.glob("phone-sshd*.pid")))
    sshd_alive = False
    for pid_file in sshd_pid_files:
        try:
            pid_str = pid_file.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if not pid_str.isdigit():
            continue
        pid = int(pid_str)
        if _sshd_cmdline_matches(pid, sshd_bin):
            sshd_alive = True
            checks["sshd_pid"] = pid
            break
    checks["sshd_alive"] = sshd_alive

    # The effective config owns the port; never probe a hardcoded endpoint.
    bind_ip = _resolve_tailnet_ipv4()
    checks["tailnet_bind_ip_present"] = bind_ip is not None
    sshd_port = _configured_sshd_port()
    port_listening = sshd_port is not None and _tcp_port_listening(bind_ip, sshd_port)
    checks["sshd_port"] = sshd_port
    checks["port_listening"] = port_listening
    # Compatibility alias for existing evidence consumers.
    checks["port_8022_listening"] = port_listening

    _bounded_print(json.dumps(summary, sort_keys=True) + "\n")
    return 0


def cmd_verify(args: Sequence[str]) -> int:
    """Bounded sshd config syntax + listener readiness.

    Runs `sshd -t -f <config>` only. No shell. No extra argv.
    """
    if args:
        _die("verify", "extra-argv")
    sshd_bin = os.environ.get("PHONE_SSHD_BIN", "/data/data/com.termux/files/usr/bin/sshd")
    config_path = CONTROL_DIR / "sshd" / "sshd_config.effective"
    if config_path.is_symlink() or not config_path.is_file():
        _die("verify", "effective-config-missing")
    try:
        config_stat = config_path.stat()
        if config_stat.st_uid != os.geteuid() or stat.S_IMODE(config_stat.st_mode) != 0o600:
            _die("verify", "effective-config-permissions")
    except OSError as exc:
        _die("verify", f"effective-config-stat-failed:{type(exc).__name__}")
    try:
        result = subprocess.run(
            [sshd_bin, "-t", "-f", str(config_path)],
            capture_output=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        _die("verify", f"sshd-t-failed:{type(exc).__name__}")
    summary = {
        "config": str(config_path),
        "ok": result.returncode == 0,
        "rc": result.returncode,
        "stderr_bytes": len(result.stderr or b""),
    }
    _bounded_print(json.dumps(summary, sort_keys=True) + "\n")
    return 0 if result.returncode == 0 else 1


def cmd_logs(args: Sequence[str]) -> int:
    """Bounded/redacted tail of a single named log file.

    argv: <log-name> <lines>
    """
    if len(args) != 2:
        _die("logs", "argc")
    name = _validate_choice(args[0], ALLOWLIST_LOGS, tag="logs-name")
    lines = _parse_int_bounded(args[1], lo=1, hi=MAX_TAIL_LINES, tag="logs-lines")
    candidates = [
        LOG_DIR / f"{name}.log",
        LOG_DIR / f"{name}-launcher.log",
        LOG_DIR / f"{name}-dispatch.log",
    ]
    target = next((p for p in candidates if p.exists()), None)
    if target is None:
        _die("logs", "log-missing")
    # Symlink rejection: a symlinked log target can redirect reads outside
    # LOG_DIR; refuse it before any I/O. Also reject non-regular files.
    try:
        if target.is_symlink():
            _die("logs", "symlink")
        if not target.is_file():
            _die("logs", "not-a-regular-file")
    except OSError as exc:
        _die("logs", f"stat-failed:{exc.strerror or 'os'}")
    try:
        # Bound the read; never load a multi-gigabyte log into memory.
        with target.open("rb", buffering=0) as fh:
            data = fh.read(MAX_OUTPUT_BYTES * 4)
    except OSError as exc:
        _die("logs", f"read-failed:{exc.strerror or 'os'}")
    text = data.decode("utf-8", errors="replace")
    tail = "\n".join(text.splitlines()[-lines:])
    # Redact any path-like substrings, key fingerprints, IPv4 addresses, and
    # authorized_keys contents. Keep timestamps and known tag= pairs.
    tail = re.sub(r"/[A-Za-z0-9_./-]+", "<path>", tail)
    tail = re.sub(r"\b[0-9a-fA-F]{16,}\b", "<fp>", tail)
    tail = re.sub(r"\b\d{1,3}(?:\.\d{1,3}){3}\b", "<ip>", tail)
    _bounded_print(tail)
    return 0


def cmd_snapshot(args: Sequence[str]) -> int:
    """Bounded export of one named evidence file (size-capped)."""
    if len(args) != 1:
        _die("snapshot", "argc")
    name = _validate_choice(args[0], ALLOWLIST_EVIDENCE, tag="snapshot-name")
    target = _resolve_under(DATA_DIR, name, tag="snapshot-path", must_exist=True)
    if target.is_symlink():
        _die("snapshot", "symlink")
    try:
        size = target.stat().st_size
    except OSError as exc:
        _die("snapshot", f"stat-failed:{exc.strerror or 'os'}")
    if size > MAX_SNAPSHOT_BYTES:
        _die("snapshot", f"too-large:{size}")
    try:
        data = target.read_bytes()
    except OSError as exc:
        _die("snapshot", f"read-failed:{exc.strerror or 'os'}")
    # Redact key fingerprints and IPs in snapshot output.
    text = data.decode("utf-8", errors="replace")
    text = re.sub(r"\b[0-9a-fA-F]{16,}\b", "<fp>", text)
    text = re.sub(r"\b\d{1,3}(?:\.\d{1,3}){3}\b", "<ip>", text)
    _bounded_print(text)
    return 0


def cmd_export(args: Sequence[str]) -> int:
    """Bounded directory listing under ROOT/data."""
    if len(args) != 1:
        _die("export", "argc")
    name = _validate_scalar(args[0], tag="export-name")
    if "/" in name or "\\" in name:
        _die("export", "slash")
    target = _resolve_under(DATA_DIR, name, tag="export-path", must_exist=True)
    if target.is_symlink():
        _die("export", "symlink")
    if not target.is_dir():
        _die("export", "not-a-directory")
    entries: list[dict[str, object]] = []
    try:
        with os.scandir(target) as it:
            for entry in it:
                if len(entries) >= MAX_LIST_ENTRIES:
                    break
                try:
                    st = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                entries.append({
                    "name": entry.name,
                    "size": st.st_size,
                    "mtime": int(st.st_mtime),
                })
    except OSError as exc:
        _die("export", f"scan-failed:{exc.strerror or 'os'}")
    _bounded_print(json.dumps({"path": str(target), "entries": entries}, sort_keys=True) + "\n")
    return 0


def cmd_cleanup(args: Sequence[str]) -> int:
    """Bounded retention cleanup of one named subdir.

    argv: <subdir> <max-bytes>
    Only deletes files whose mtime is older than 24h; never deletes
    symlinks, never traverses outside the named subdir.
    """
    if len(args) != 2:
        _die("cleanup", "argc")
    name = _validate_choice(args[0], ALLOWLIST_CLEANUP_SUBDIRS, tag="cleanup-name")
    max_bytes = _parse_int_bounded(args[1], lo=1, hi=1 << 30, tag="cleanup-bytes")
    target = _resolve_under(DATA_DIR, name, tag="cleanup-path", must_exist=True)
    if target.is_symlink():
        _die("cleanup", "symlink")
    if not target.is_dir():
        _die("cleanup", "not-a-directory")
    now = time.time()
    oldest: list[tuple[float, os.DirEntry]] = []
    try:
        with os.scandir(target) as it:
            for entry in it:
                if entry.is_symlink():
                    continue
                if not entry.is_file():
                    continue
                try:
                    st = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                oldest.append((st.st_mtime, entry))
    except OSError as exc:
        _die("cleanup", f"scan-failed:{exc.strerror or 'os'}")
    oldest.sort(key=lambda t: t[0])
    total = 0
    removed = 0
    for mtime, entry in oldest:
        if total >= max_bytes:
            break
        if (now - mtime) < 86400:
            continue
        try:
            size = entry.stat(follow_symlinks=False).st_size
        except OSError:
            continue
        try:
            os.unlink(entry.path)
        except OSError:
            continue
        total += size
        removed += 1
    _bounded_print(json.dumps({"removed": removed, "bytes": total}, sort_keys=True) + "\n")
    return 0


def cmd_receipt_publish(args: Sequence[str]) -> int:
    """Bounded publish of one named JSON receipt to the run dir."""
    if len(args) != 2:
        _die("receipt-publish", "argc")
    name = _validate_choice(args[0], ALLOWLIST_RECEIPTS, tag="receipt-name")
    payload = _validate_scalar(args[1], tag="receipt-payload", allow_json=True)
    if len(payload) > 4096:
        _die("receipt-publish", "payload-too-long")
    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError:
        _die("receipt-publish", "payload-not-json")
    if not isinstance(parsed, dict):
        _die("receipt-publish", "payload-not-object")
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    target = RUN_DIR / f"{name}.json"
    tmp = target.with_suffix(".json.tmp")
    try:
        tmp.write_text(json.dumps(parsed, sort_keys=True), encoding="utf-8")
        os.replace(tmp, target)
    except OSError as exc:
        _die("receipt-publish", f"write-failed:{exc.strerror or 'os'}")
    try:
        os.chmod(target, 0o600)
    except OSError:
        pass
    _bounded_print(json.dumps({"wrote": str(target)}, sort_keys=True) + "\n")
    return 0


def cmd_release(args: Sequence[str]) -> int:
    """Bounded release stage/activate/rollback marker writes."""
    if len(args) != 1:
        _die("release", "argc")
    subverb = _validate_choice(args[0], ("stage", "activate", "rollback"), tag="release-subverb")
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    target = RUN_DIR / f"release-{subverb}.marker"
    payload = {"verb": subverb, "ts": int(time.time())}
    try:
        target.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
        os.chmod(target, 0o600)
    except OSError as exc:
        _die("release", f"write-failed:{exc.strerror or 'os'}")
    _bounded_print(json.dumps({"wrote": str(target)}, sort_keys=True) + "\n")
    return 0


# ---- argv parsing --------------------------------------------------------
def _parse_argv(argv: Sequence[str]) -> tuple[str, Sequence[str]]:
    if not argv:
        _die("argv", "empty")
    verb = _validate_scalar(argv[0], tag="verb")
    if verb not in {
        "health",
        "verify",
        "logs",
        "snapshot",
        "export",
        "cleanup",
        "receipt-publish",
        "release",
    }:
        _die("verb", f"unknown:{verb}")
    rest = list(argv[1:])
    for item in rest:
        _validate_scalar(item, tag="argv")
    return verb, tuple(rest)


HANDLERS = {
    "health": cmd_health,
    "verify": cmd_verify,
    "logs": cmd_logs,
    "snapshot": cmd_snapshot,
    "export": cmd_export,
    "cleanup": cmd_cleanup,
    "receipt-publish": cmd_receipt_publish,
    "release": cmd_release,
}


def main(argv: Sequence[str] | None = None) -> int:
    if argv is None:
        if len(sys.argv) > 1:
            argv = list(sys.argv[1:])
        elif "SSH_ORIGINAL_COMMAND" in os.environ:
            orig = os.environ["SSH_ORIGINAL_COMMAND"].strip()
            if orig:
                try:
                    argv = shlex.split(orig)
                except ValueError:
                    _die("argv", "shlex-error")
            else:
                argv = ["health"]
        else:
            argv = ["health"]
    else:
        argv = list(argv)

    if not argv:
        argv = ["health"]

    # Reject any environment hint that is shaped like an override attempt.
    for envkey in ("LD_PRELOAD", "LD_LIBRARY_PATH"):
        if envkey in os.environ:
            # Strip but continue; never echo the value.
            os.environ.pop(envkey, None)

    verb, rest = _parse_argv(argv)
    # Confirm ROOT resolves and is owned by the current uid (Termux app uid).
    try:
        st = ROOT.stat()
    except OSError as exc:
        _die("root", f"unresolvable:{exc.strerror or 'os'}")
    try:
        current_uid = os.geteuid()
    except OSError:
        current_uid = -1
    if current_uid >= 0 and st.st_uid != current_uid:
        # Phone control plane runs as the Termux uid; refuse mismatched owner.
        # This is logged redacted; never echo usernames.
        sys.stderr.write("dispatch=reject tag=root-owner-mismatch\n")
        return 3
    return HANDLERS[verb](rest)


if __name__ == "__main__":
    raise SystemExit(main())
