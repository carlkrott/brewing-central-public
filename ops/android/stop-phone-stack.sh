#!/data/data/com.termux/files/usr/bin/bash
set -euo pipefail

export HOME=/data/data/com.termux/files/home
export PREFIX=/data/data/com.termux/files/usr
export PATH="$PREFIX/bin:/system/bin"
ROOT="$HOME/brewing-central"

"$PREFIX/bin/python" - <<'PY'
import os
import pathlib
import signal
import time

root = pathlib.Path("/data/data/com.termux/files/home/brewing-central")
services = (
    ("camera-observer", "/data/data/com.termux/files/home/brewing-central/venv/bin/python -m app.camera_observer"),
    ("dashboard", "/data/data/com.termux/files/home/brewing-central/venv/bin/python -m uvicorn app.main:app"),
    ("zeroclaw", "/data/data/com.termux/files/home/bin/zeroclaw gateway"),
)
uid = str(os.geteuid())
matches: dict[str, list[int]] = {name: [] for name, _ in services}
phone_health_loop: list[int] = []
for entry in pathlib.Path("/proc").iterdir():
    if not entry.name.isdigit():
        continue
    try:
        argv = [
            raw.decode(errors="replace")
            for raw in (entry / "cmdline").read_bytes().split(b"\0")
            if raw
        ]
        command = " ".join(argv)
        process_uid = (entry / "status").read_text().split("Uid:", 1)[1].split()[0]
    except (FileNotFoundError, PermissionError, IndexError):
        continue
    if process_uid != uid:
        continue
    matched = False
    for name, prefix in services:
        if command.startswith(prefix):
            matches[name].append(int(entry.name))
            matched = True
    if not matched:
        for argument in argv:
            path = pathlib.Path(argument)
            if path.name != "phone-health-loop.sh":
                continue
            try:
                path.resolve().relative_to(root)
            except (OSError, ValueError):
                continue
            phone_health_loop.append(int(entry.name))
            break

for pids in matches.values():
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass

deadline = time.monotonic() + 5
while time.monotonic() < deadline:
    if not any(pathlib.Path(f"/proc/{pid}").exists() for pids in matches.values() for pid in pids):
        break
    time.sleep(0.1)

for pids in matches.values():
    for pid in pids:
        if pathlib.Path(f"/proc/{pid}").exists():
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
time.sleep(0.1)

for pid in phone_health_loop:
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
deadline = time.monotonic() + 3
while time.monotonic() < deadline:
    if not any(pathlib.Path(f"/proc/{pid}").exists() for pid in phone_health_loop):
        break
    time.sleep(0.1)
for pid in phone_health_loop:
    if pathlib.Path(f"/proc/{pid}").exists():
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

remaining = [pid for pids in matches.values() for pid in pids if pathlib.Path(f"/proc/{pid}").exists()]
if remaining:
    raise SystemExit(f"service processes did not stop: {remaining}")
for name, _ in services:
    (root / "run" / f"{name}.pid").unlink(missing_ok=True)
    print(f"{name}=stopped count={len(matches[name])}")
phone_health_pid = root / "run" / "phone-health-loop.pid"
if phone_health_pid.exists():
    phone_health_pid.unlink()
print(f"phone-health-loop=stopped count={len(phone_health_loop)}")
PY

if command -v termux-wake-unlock >/dev/null 2>&1; then
  termux-wake-unlock || true
fi
