"""Disposable Docker runtime proof for Phase 5C hardening."""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path


PRODUCTION_CONTAINER = "ispindel-dashboard"
PRODUCTION_VOLUME = "ispindel-dashboard_ispindel-data"
SUCCESS_BODY = {"status": "ok", "service": "ispindel-dashboard"}


def _docker(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(
        ["docker", *args], text=True, capture_output=True, check=False
    )
    if check and proc.returncode:
        raise AssertionError(
            f"docker {' '.join(args)} failed ({proc.returncode})\n"
            f"stdout={proc.stdout}\nstderr={proc.stderr}"
        )
    return proc


def _json_exec(container: str, script: str, *, user: str | None = None) -> dict:
    args = ["exec"]
    if user is not None:
        args.extend(["--user", user])
    args.extend([container, "python", "-c", script])
    return json.loads(_docker(*args).stdout)


def _get_json(url: str) -> tuple[int, dict]:
    with urllib.request.urlopen(url, timeout=3) as response:
        return response.status, json.load(response)


def test_disposable_image_runtime_contract(request):
    image = os.environ.get("PHASE05C_RUNTIME_IMAGE", "")
    source_env = os.environ.get("PHASE05C_RUNTIME_SOURCE_ROOT", "")
    assert image.startswith("sha256:") and len(image) == 71
    assert all(ch in "0123456789abcdef" for ch in image[7:])
    source = Path(source_env).resolve(strict=True)
    assert source == Path(__file__).resolve().parents[1]
    assert (source / "Dockerfile").is_file()
    assert (source / "docker-compose.yml").is_file()
    assert _docker("image", "inspect", image, check=False).returncode == 0

    nonce = f"{os.getpid()}-{secrets.token_hex(5)}"
    container = f"ispindel-phase05c-test-container-{nonce}"
    network = f"ispindel-phase05c-test-network-{nonce}"
    volume = f"ispindel-phase05c-test-volume-{nonce}"
    assert container != PRODUCTION_CONTAINER and volume != PRODUCTION_VOLUME
    created = {"container": False, "network": False, "volume": False}

    try:
        _docker("network", "create", network)
        created["network"] = True
        _docker("volume", "create", volume)
        created["volume"] = True
        _docker(
            "run", "--rm", "--network", "none", "--user", "0:0",
            "--entrypoint", "python", "-v", f"{volume}:/data", image,
            "-c", "import os; os.chown('/data',10001,10001)",
        )

        with tempfile.TemporaryDirectory(prefix="ispindel-phase05c-runtime-") as td:
            evidence = Path(td)
            battery = evidence / "battery.json"
            heartbeat = evidence / "heartbeat.json"
            battery.write_text(
                '{"schema_version":"health-evidence-v1","kind":"battery",'
                '"observed_at":"2026-07-29T00:00:00Z","state":"ok",'
                '"detail":"runtime fixture","percent":90}'
            )
            heartbeat.write_text(
                '{"schema_version":"health-evidence-v1","kind":"heartbeat",'
                '"observed_at":"2026-07-29T00:00:00Z","state":"ok",'
                '"detail":"runtime fixture","poll_failed":false,'
                '"alert_attempted":false,"alert_delivered":false}'
            )
            _docker(
                "run", "-d", "--name", container, "--network", network,
                "--user", "10001:10001", "--read-only",
                "--tmpfs", "/tmp:size=64m,mode=1777", "--cap-drop", "ALL",
                "--security-opt", "no-new-privileges:true",
                "-e", "ISPINDEL_MODE=test",
                "-e", "SQLITE_PATH=/data/ispindel.db",
                "-e", "BREW_SQLITE_PATH=/data/brew.db",
                "-e", "BATTERY_EVIDENCE_PATH=/health-evidence/battery-state.json",
                "-e", "HEARTBEAT_EVIDENCE_PATH=/health-evidence/heartbeat.json",
                "-v", f"{volume}:/data",
                "-v", f"{battery}:/health-evidence/battery-state.json:ro",
                "-v", f"{heartbeat}:/health-evidence/heartbeat.json:ro",
                "-p", "127.0.0.1::8098", image,
            )
            created["container"] = True

            deadline = time.monotonic() + 75
            health = None
            while time.monotonic() < deadline:
                state = json.loads(_docker("inspect", container).stdout)[0]
                health = state.get("State", {}).get("Health", {}).get("Status")
                if health == "healthy":
                    break
                if state.get("State", {}).get("Status") == "exited":
                    logs = _docker("logs", container, check=False)
                    raise AssertionError(logs.stdout + logs.stderr)
                time.sleep(0.25)
            assert health == "healthy"

            host_port = _docker("port", container, "8098/tcp").stdout.strip()
            assert host_port.startswith("127.0.0.1:")
            base = f"http://{host_port}"
            for path in ("/health", "/health/live", "/health/ready"):
                assert _get_json(base + path) == (200, SUCCESS_BODY)
            assert _get_json(base + "/api/devices")[0] == 200
            with urllib.request.urlopen(base + "/static/chart.umd.js", timeout=3) as response:
                assert response.status == 200 and len(response.read()) > 1000

            inspect = json.loads(_docker("inspect", container).stdout)[0]
            host = inspect["HostConfig"]
            assert inspect["Config"]["User"] == "10001:10001"
            assert host["ReadonlyRootfs"] is True
            assert host["CapDrop"] == ["ALL"]
            assert host["SecurityOpt"] == ["no-new-privileges:true"]
            assert host["Tmpfs"] == {"/tmp": "size=64m,mode=1777"}
            mount_targets = {item["Destination"]: item for item in inspect["Mounts"]}
            assert mount_targets["/data"]["Type"] == "volume"
            assert mount_targets["/data"]["RW"] is True
            assert mount_targets["/health-evidence/battery-state.json"]["RW"] is False
            assert mount_targets["/health-evidence/heartbeat.json"]["RW"] is False

            process = _json_exec(
                container,
                "import json,os,pathlib; "
                "s=dict(line.split(':',1) for line in pathlib.Path('/proc/1/status').read_text().splitlines() if ':' in line); "
                "print(json.dumps({'uid':os.getuid(),'gid':os.getgid(),'cap':s['CapEff'].strip(),'nnp':s['NoNewPrivs'].strip()}))",
            )
            assert process == {
                "uid": 10001, "gid": 10001,
                "cap": "0000000000000000", "nnp": "1",
            }

            filesystem = _json_exec(
                container,
                "import errno,json,pathlib; r={}; "
                "exec(\"try:\\n pathlib.Path('/app/phase05c-write-probe').write_text('x')\\n r['rootfs']=False\\nexcept OSError as e:\\n r['rootfs']=(e.errno==errno.EROFS)\"); "
                "p=pathlib.Path('/tmp/phase05c-probe'); p.write_text('tmp'); r['tmp']=p.read_text(); p.unlink(); "
                "p=pathlib.Path('/data/phase05c-probe'); p.write_text('data'); r['data']=p.read_text(); p.unlink(); "
                "print(json.dumps(r))",
            )
            assert filesystem == {"rootfs": True, "tmp": "tmp", "data": "data"}

            wal = _json_exec(
                container,
                "import json,os,sqlite3; c=sqlite3.connect('/data/ispindel.db'); "
                "c.execute('BEGIN IMMEDIATE'); "
                "c.execute(\"INSERT INTO devices(device_id,device_name,expected_interval_sec,created_at,last_seen,config_json) VALUES('phase05c-probe','probe',900,'2026-07-29T00:00:00+00:00','2026-07-29T00:00:00+00:00','{}')\"); "
                "paths=['/data/ispindel.db','/data/ispindel.db-wal','/data/ispindel.db-shm']; "
                "r={'journal':c.execute('PRAGMA journal_mode').fetchone()[0], 'owners':{p:[os.stat(p).st_uid,os.stat(p).st_gid] for p in paths}}; "
                "c.rollback(); c.close(); print(json.dumps(r))",
            )
            assert wal["journal"] == "wal"
            assert wal["owners"] == {
                "/data/ispindel.db": [10001, 10001],
                "/data/ispindel.db-wal": [10001, 10001],
                "/data/ispindel.db-shm": [10001, 10001],
            }

            image_config = json.loads(_docker("image", "inspect", image).stdout)[0]["Config"]
            assert image_config["User"] == "10001:10001"
            hc = image_config["Healthcheck"]
            assert hc["Interval"] == 30_000_000_000
            assert hc["Timeout"] == 3_000_000_000
            assert hc["StartPeriod"] == 10_000_000_000
            assert hc["Retries"] == 3
            assert "http://127.0.0.1:8098/health/live" in " ".join(hc["Test"])

            image_app = _docker(
                "run", "--rm", "--network", "none", "--user", "0:0",
                "--entrypoint", "python", image, "-c",
                "import hashlib,json,os,pathlib; p=pathlib.Path('/app/app/main.py'); "
                "print(json.dumps({'uid':os.stat(p).st_uid,'gid':os.stat(p).st_gid,'sha256':hashlib.sha256(p.read_bytes()).hexdigest()}))",
            )
            app_meta = json.loads(image_app.stdout)
            assert app_meta["uid"] == 0 and app_meta["gid"] == 0
            assert app_meta["sha256"] == hashlib.sha256(
                (source / "app/main.py").read_bytes()
            ).hexdigest()
    finally:
        if created["container"]:
            _docker("rm", "-f", container, check=False)
        if created["network"]:
            _docker("network", "rm", network, check=False)
        if created["volume"]:
            _docker("volume", "rm", "-f", volume, check=False)
        assert _docker("container", "inspect", container, check=False).returncode != 0
        assert _docker("network", "inspect", network, check=False).returncode != 0
        assert _docker("volume", "inspect", volume, check=False).returncode != 0

    reporter = request.config.pluginmanager.get_plugin("terminalreporter")
    reporter.write_line("PHASE05C_RUNTIME_OK")